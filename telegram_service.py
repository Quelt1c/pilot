"""
telegram_service.py — Обгортка над Telegram Bot API

Відповідає за:
  - Надсилання повідомлень, кнопок, фото
  - Реєстрацію/видалення webhook
  - Парсинг вхідного update (повідомлення / callback)
"""

import requests
import logging

logger = logging.getLogger(__name__)

TELEGRAM_API = 'https://api.telegram.org/bot{token}/{method}'


class TelegramService:
    def __init__(self, token: str):
        self.token = token

    def _url(self, method: str) -> str:
        return TELEGRAM_API.format(token=self.token, method=method)

    def _post(self, method: str, data: dict) -> dict:
        try:
            resp = requests.post(self._url(method), json=data, timeout=10)
            result = resp.json()
            if not result.get('ok'):
                logger.warning('Telegram API error [%s]: %s', method, result.get('description'))
            return result
        except requests.RequestException as e:
            logger.error('Telegram request failed [%s]: %s', method, e)
            return {'ok': False, 'description': str(e)}

    # ─────────────────────────────────────
    # WEBHOOK
    # ─────────────────────────────────────

    def register_webhook(self, webhook_url: str) -> bool:
        """
        Реєструє webhook у Telegram.
        webhook_url — публічний HTTPS URL, напр:
            https://yourdomain.com/webhook/telegram/42
        """
        result = self._post('setWebhook', {
            'url': webhook_url,
            'allowed_updates': ['message', 'callback_query'],
            'drop_pending_updates': True,
        })
        ok = result.get('ok', False)
        logger.info('setWebhook %s → %s', webhook_url, 'OK' if ok else 'FAIL')
        return ok

    def delete_webhook(self) -> bool:
        result = self._post('deleteWebhook', {'drop_pending_updates': True})
        return result.get('ok', False)

    def get_webhook_info(self) -> dict:
        try:
            resp = requests.get(self._url('getWebhookInfo'), timeout=10)
            return resp.json()
        except Exception:
            return {}

    # ─────────────────────────────────────
    # НАДСИЛАННЯ ПОВІДОМЛЕНЬ
    # ─────────────────────────────────────

    def send_text(self, chat_id: str, text: str) -> dict:
        """Простий текст."""
        return self._post('sendMessage', {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
        })

    def send_buttons(self, chat_id: str, text: str, buttons: list[str]) -> dict:
        """
        Текст + inline-кнопки.
        buttons = ['Купити', 'Підтримка', ...]
        Кожна кнопка — окремий рядок (1 колонка).
        """
        keyboard = [[{'text': btn, 'callback_data': btn}] for btn in buttons]
        return self._post('sendMessage', {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'reply_markup': {'inline_keyboard': keyboard},
        })

    def send_product_card(self, chat_id: str, name: str, description: str,
                          price: str, image_url: str = None) -> dict:
        """
        Картка товару — фото + підпис з ціною і кнопкою купити.
        Якщо image_url відсутній — надсилає текстом.
        """
        caption = (
            f"<b>{name}</b>\n\n"
            f"{description}\n\n"
            f"💰 <b>Ціна: {price} грн</b>"
        )
        keyboard = {'inline_keyboard': [[
            {'text': '🛒 Купити', 'callback_data': f'buy:{name}'}
        ]]}

        if image_url:
            return self._post('sendPhoto', {
                'chat_id': chat_id,
                'photo': image_url,
                'caption': caption,
                'parse_mode': 'HTML',
                'reply_markup': keyboard,
            })
        else:
            return self._post('sendMessage', {
                'chat_id': chat_id,
                'text': caption,
                'parse_mode': 'HTML',
                'reply_markup': keyboard,
            })

    def send_payment_link(self, chat_id: str, invoice_url: str,
                          amount: str, description: str) -> dict:
        """Надсилає посилання на оплату Monobank."""
        text = (
            f"💳 <b>Оплата замовлення</b>\n\n"
            f"{description}\n"
            f"Сума: <b>{amount} грн</b>\n\n"
            f"Натисніть кнопку нижче для оплати:"
        )
        keyboard = {'inline_keyboard': [[
            {'text': '💳 Оплатити', 'url': invoice_url}
        ], [
            {'text': '✅ Я вже оплатив', 'callback_data': 'check_payment'}
        ]]}
        return self._post('sendMessage', {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'reply_markup': keyboard,
        })

    def send_link(self, chat_id: str, message: str, url: str) -> dict:
        """Надсилає посилання (Google Drive тощо) після оплати."""
        text = f"{message}\n\n🔗 <a href='{url}'>Відкрити посилання</a>"
        return self._post('sendMessage', {
            'chat_id': chat_id,
            'text': text,
            'parse_mode': 'HTML',
            'disable_web_page_preview': False,
        })

    def send_handoff(self, chat_id: str, message: str) -> dict:
        """Повідомлення про передачу оператору."""
        return self._post('sendMessage', {
            'chat_id': chat_id,
            'text': f"👤 {message}",
            'parse_mode': 'HTML',
        })

    def answer_callback(self, callback_query_id: str, text: str = '') -> dict:
        """Підтверджує callback_query (прибирає годинник у кнопки)."""
        return self._post('answerCallbackQuery', {
            'callback_query_id': callback_query_id,
            'text': text,
        })

    # ─────────────────────────────────────
    # ПАРСИНГ ВХІДНОГО UPDATE
    # ─────────────────────────────────────

    @staticmethod
    def parse_update(data: dict) -> dict:
        """
        Нормалізує Telegram Update у простий словник:
        {
            'type':       'message' | 'callback',
            'chat_id':    '123456789',
            'text':       'текст від юзера або callback_data',
            'message_id': 42,
            'callback_query_id': '...'  (тільки для callback)
        }
        """
        if 'message' in data:
            msg = data['message']
            return {
                'type':       'message',
                'chat_id':    str(msg['chat']['id']),
                'text':       msg.get('text', ''),
                'message_id': msg.get('message_id'),
                'callback_query_id': None,
            }

        if 'callback_query' in data:
            cb = data['callback_query']
            return {
                'type':       'callback',
                'chat_id':    str(cb['message']['chat']['id']),
                'text':       cb.get('data', ''),
                'message_id': cb['message'].get('message_id'),
                'callback_query_id': cb['id'],
            }

        return {}
