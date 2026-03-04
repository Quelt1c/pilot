"""
monobank_service.py — Клієнт Monobank Acquiring API

Документація: https://api.monobank.ua/docs/acquiring.html

Потрібен токен продавця:
  - Реєструєтесь на https://web.monobank.ua → Бізнес → Еквайринг
  - Отримуєте X-Token для API
  - Встановлюєте в .env: MONOBANK_TOKEN=...

Статуси рахунку:
  created    → рахунок створено, ще не відкривали
  processing → юзер відкрив сторінку оплати
  hold       → кошти заблоковані (для 2-step)
  success    → оплата пройшла ✅
  failure    → оплата відхилена ❌
  reversed   → повернення
  expired    → час вийшов
"""

import os
import requests
import logging

logger = logging.getLogger(__name__)

MONOBANK_API = 'https://api.monobank.ua'


class MonobankService:
    def __init__(self):
        self.token = os.getenv('MONOBANK_TOKEN', '')
        if not self.token:
            logger.warning('MONOBANK_TOKEN не встановлено в .env')

    def _headers(self) -> dict:
        return {'X-Token': self.token, 'Content-Type': 'application/json'}

    # ─────────────────────────────────────
    # СТВОРЕННЯ РАХУНКУ
    # ─────────────────────────────────────

    def create_invoice(self, amount: int, order_id: str,
                       description: str, webhook_url: str) -> dict:
        """
        Створює рахунок на оплату.

        Параметри:
          amount      — сума в копійках (100 грн = 10000)
          order_id    — унікальний ID замовлення (ваш внутрішній)
          description — призначення платежу (видно юзеру)
          webhook_url — повний URL для сповіщень (з BASE_URL)

        Повертає:
          { 'invoice_id': 'p2mFnhf...', 'page_url': 'https://pay.monobank.ua/...' }
        """
        base_url = os.getenv('BASE_URL', 'https://yourdomain.com')
        full_webhook = f"{base_url.rstrip('/')}{webhook_url}"

        payload = {
            'amount': amount,
            'ccy': 980,         # UAH
            'merchantPaymInfo': {
                'reference': order_id,
                'destination': description,
            },
            'redirectUrl': os.getenv('PAYMENT_REDIRECT_URL', base_url),
            'webHookUrl': full_webhook,
            'validity': 3600,   # рахунок дійсний 1 годину
        }

        try:
            resp = requests.post(
                f'{MONOBANK_API}/api/merchant/invoice/create',
                json=payload,
                headers=self._headers(),
                timeout=15,
            )
            data = resp.json()

            if resp.status_code == 200 and 'invoiceId' in data:
                return {
                    'invoice_id': data['invoiceId'],
                    'page_url':   data['pageUrl'],
                }
            else:
                logger.error('Monobank create_invoice error: %s', data)
                return {}

        except requests.RequestException as e:
            logger.error('Monobank request failed: %s', e)
            return {}

    # ─────────────────────────────────────
    # ПЕРЕВІРКА СТАТУСУ
    # ─────────────────────────────────────

    def check_status(self, invoice_id: str) -> str:
        """
        Перевіряє поточний статус рахунку.

        Повертає: 'success' | 'failure' | 'processing' | 'created' | 'expired' | 'error'
        """
        try:
            resp = requests.get(
                f'{MONOBANK_API}/api/merchant/invoice/status',
                params={'invoiceId': invoice_id},
                headers=self._headers(),
                timeout=10,
            )
            data = resp.json()
            status = data.get('status', 'error')
            logger.info('Monobank status [%s]: %s', invoice_id, status)
            return status

        except requests.RequestException as e:
            logger.error('Monobank status check failed: %s', e)
            return 'error'

    # ─────────────────────────────────────
    # СКАСУВАННЯ / ПОВЕРНЕННЯ
    # ─────────────────────────────────────

    def cancel_invoice(self, invoice_id: str) -> bool:
        """Скасовує рахунок (якщо ще не оплачений)."""
        try:
            resp = requests.post(
                f'{MONOBANK_API}/api/merchant/invoice/cancel',
                json={'invoiceId': invoice_id},
                headers=self._headers(),
                timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error('Monobank cancel failed: %s', e)
            return False
