"""
bot_engine.py — Ядро BotFlow: інтерпретатор схеми

Режими сесії:
  'bot'     → бот відповідає автоматично по схемі (default)
  'pending' → чекаємо оператора, бот тримає юзера
  'human'   → оператор відповідає вручну, бот мовчить

Переходи:
  bot → (Handoff вузол) → pending → (оператор /take_N) → human
  human → (оператор /close_N) → bot
"""

import os
import json
import logging
from datetime import datetime

from extensions import db
from models import FlowNode, FlowEdge, DeployedBot, ConversationSession, PaymentTransaction
from telegram_service import TelegramService

logger = logging.getLogger(__name__)

AUTO_ADVANCE_TYPES = {'start', 'product', 'send_link'}


class BotEngine:
    def __init__(self, bot: DeployedBot):
        self.bot = bot
        self.tg  = TelegramService(bot.access_token)

    # ─────────────────────────────────────
    # ГОЛОВНА ТОЧКА ВХОДУ
    # ─────────────────────────────────────

    def process(self, update: dict):
        chat_id = update.get('chat_id')
        if not chat_id:
            return

        if update.get('callback_query_id'):
            self.tg.answer_callback(update['callback_query_id'])

        session = self._get_or_create_session(chat_id)
        text    = (update.get('text') or '').strip()

        # Режим HUMAN: бот мовчить, пересилаємо оператору
        if session.mode == 'human':
            self._forward_to_operator(session, update)
            return

        # Режим PENDING: чекаємо оператора
        if session.mode == 'pending':
            self.tg.send_text(chat_id, 'Оператор скоро підключиться. Зачекайте, будь ласка.')
            return

        # Режим BOT: звичайна логіка
        if text.lower() in ('/start', '/restart') or session.current_node_id is None:
            self._restart_session(session, update)
            return

        self._advance(session, update)

    # ─────────────────────────────────────
    # ПЕРЕСИЛАННЯ ДО ОПЕРАТОРА
    # ─────────────────────────────────────

    def _forward_to_operator(self, session: ConversationSession, update: dict):
        admin_chat_id = os.getenv('ADMIN_CHAT_ID')
        if not admin_chat_id:
            return
        text = update.get('text', '')
        self.tg.send_text(
            admin_chat_id,
            f"Покупець {session.customer_id}:\n{text}\n\n"
            f"Відповісти: /reply_{session.id} текст\n"
            f"Закрити: /close_{session.id}"
        )

    # ─────────────────────────────────────
    # КОМАНДИ ОПЕРАТОРА З АДМІН-ЧАТУ
    # ─────────────────────────────────────

    def process_operator_command(self, text: str, operator_chat_id: str):
        admin_tg = TelegramService(self.bot.access_token)

        # /sessions — список активних розмов
        if text.strip() == '/sessions':
            sessions = ConversationSession.query.filter(
                ConversationSession.deployed_bot_id == self.bot.id,
                ConversationSession.mode.in_(['pending', 'human'])
            ).all()
            if not sessions:
                admin_tg.send_text(operator_chat_id, 'Активних розмов немає.')
                return
            lines = ['Активні розмови:\n']
            for s in sessions:
                icon = 'Очікує' if s.mode == 'pending' else 'Активна'
                lines.append(
                    f"{icon} | Сесія #{s.id} | Покупець: {s.customer_id}\n"
                    f"  /take_{s.id} — прийняти   /close_{s.id} — закрити"
                )
            admin_tg.send_text(operator_chat_id, '\n'.join(lines))
            return

        # /take_<id> — прийняти розмову
        if text.startswith('/take_'):
            session_id = self._extract_id(text, '/take_')
            session    = ConversationSession.query.get(session_id)
            if not session:
                admin_tg.send_text(operator_chat_id, 'Сесію не знайдено.')
                return
            session.mode        = 'human'
            session.operator_id = operator_chat_id
            db.session.commit()
            self.tg.send_text(session.customer_id, 'Оператор підключився! Ви можете писати.')
            admin_tg.send_text(
                operator_chat_id,
                f"Ви прийняли розмову з покупцем {session.customer_id}.\n"
                f"Відповісти: /reply_{session.id} текст\n"
                f"Закрити: /close_{session.id}"
            )
            return

        # /reply_<id> текст — відповісти покупцю
        if text.startswith('/reply_'):
            parts      = text.split(' ', 1)
            session_id = self._extract_id(parts[0], '/reply_')
            reply_text = parts[1] if len(parts) > 1 else ''
            session    = ConversationSession.query.get(session_id)
            if not session or not reply_text:
                admin_tg.send_text(operator_chat_id, 'Помилка. Формат: /reply_<id> текст')
                return
            self.tg.send_text(session.customer_id, f"Оператор:\n{reply_text}")
            admin_tg.send_text(operator_chat_id, 'Надіслано.')
            return

        # /close_<id> — закрити розмову, передати боту
        if text.startswith('/close_'):
            session_id = self._extract_id(text, '/close_')
            session    = ConversationSession.query.get(session_id)
            if not session:
                admin_tg.send_text(operator_chat_id, 'Сесію не знайдено.')
                return
            session.mode        = 'bot'
            session.operator_id = None
            db.session.commit()
            self.tg.send_text(
                session.customer_id,
                'Розмову з оператором завершено. Якщо потрібна допомога — напишіть /start.'
            )
            admin_tg.send_text(operator_chat_id, f'Розмову #{session_id} закрито.')
            return

    def _extract_id(self, text: str, prefix: str) -> int:
        try:
            return int(text.replace(prefix, '').strip().split()[0])
        except (ValueError, IndexError):
            return 0

    # ─────────────────────────────────────
    # СЕСІЯ
    # ─────────────────────────────────────

    def _get_or_create_session(self, chat_id: str) -> ConversationSession:
        session = ConversationSession.query.filter_by(
            deployed_bot_id=self.bot.id,
            customer_id=chat_id
        ).first()
        if not session:
            session = ConversationSession(
                deployed_bot_id=self.bot.id,
                customer_id=chat_id,
                current_node_id=None,
                mode='bot',
                state_json='{}'
            )
            db.session.add(session)
            db.session.commit()
        return session

    def _restart_session(self, session: ConversationSession, update: dict):
        start_node = FlowNode.query.filter_by(
            template_id=self.bot.template_id,
            node_type='start'
        ).first()
        if not start_node:
            self.tg.send_text(session.customer_id, 'Схему ще не налаштовано.')
            return
        session.current_node_id = start_node.id
        session.mode            = 'bot'
        session.operator_id     = None
        session.set_state({})
        db.session.commit()
        self._execute_node(start_node, session, update)

    # ─────────────────────────────────────
    # ВИКОНАННЯ ВУЗЛА
    # ─────────────────────────────────────

    def _advance(self, session: ConversationSession, update: dict):
        node = FlowNode.query.get(session.current_node_id)
        if not node:
            self._restart_session(session, update)
            return
        self._execute_node(node, session, update)

    def _execute_node(self, node: FlowNode, session: ConversationSession, update: dict):
        config  = json.loads(node.config_json or '{}')
        logger.info('Execute [%s] %s -> chat=%s', node.node_type, node.id, session.customer_id)

        handlers = {
            'start':         self._handle_start,
            'message':       self._handle_message,
            'condition':     self._handle_condition,
            'product':       self._handle_product,
            'payment':       self._handle_payment,
            'check_payment': self._handle_check_payment,
            'send_link':     self._handle_send_link,
            'handoff':       self._handle_handoff,
        }
        handler = handlers.get(node.node_type)
        if not handler:
            return

        next_port = handler(node, config, session, update)

        if next_port is not None:
            next_node = self._follow_edge(node, next_port)
            if next_node:
                session.current_node_id = next_node.id
                session.updated_at      = datetime.utcnow()
                db.session.commit()
                if next_node.node_type in AUTO_ADVANCE_TYPES:
                    self._execute_node(next_node, session, update)
        else:
            db.session.commit()

    # ─────────────────────────────────────
    # HANDLERS
    # ─────────────────────────────────────

    def _handle_start(self, node, config, session, update):
        self.tg.send_text(session.customer_id, config.get('welcomeText', 'Привіт!'))
        return 'out-0'

    def _handle_message(self, node, config, session, update):
        text    = config.get('text', '')
        buttons = config.get('buttons', [])
        if buttons:
            self.tg.send_buttons(session.customer_id, text, buttons)
            state = session.get_state()
            state['waiting_node']    = node.id
            state['waiting_buttons'] = buttons
            session.set_state(state)
            session.current_node_id = node.id
            return None
        else:
            self.tg.send_text(session.customer_id, text)
            return 'out-0'

    def _handle_condition(self, node, config, session, update):
        conditions = config.get('conditions', [])
        user_text  = (update.get('text') or '').strip()
        if not user_text:
            return None
        for idx, cond in enumerate(conditions):
            if user_text.lower() == cond.lower():
                return f'out-{idx}'
        for idx, cond in enumerate(conditions):
            if cond.lower() in user_text.lower():
                return f'out-{idx}'
        return 'out-0'

    def _handle_product(self, node, config, session, update):
        name, price = config.get('name', 'Товар'), config.get('price', '0')
        desc, img   = config.get('description', ''), config.get('imageUrl', '')
        self.tg.send_product_card(session.customer_id, name, desc, price, img)
        state = session.get_state()
        state['product'] = {'name': name, 'price': price, 'description': desc}
        session.set_state(state)
        return 'out-0'

    def _handle_payment(self, node, config, session, update):
        from monobank_service import MonobankService
        state = session.get_state()
        if state.get('invoice_id'):
            tx = PaymentTransaction.query.filter_by(invoice_id=state['invoice_id']).first()
            if tx and tx.status == 'pending':
                self.tg.send_text(session.customer_id, 'Ваш рахунок ще активний.')
                return None

        amount_uah  = config.get('amount') or state.get('product', {}).get('price', '0')
        description = config.get('description', 'Оплата замовлення')
        try:
            amount_kopecks = int(float(amount_uah) * 100)
        except (ValueError, TypeError):
            self.tg.send_text(session.customer_id, 'Помилка: невірна сума.')
            return None

        invoice = MonobankService().create_invoice(
            amount=amount_kopecks,
            order_id=f'session_{session.id}_{node.id}',
            description=description,
            webhook_url=f'/webhook/monobank/{session.id}',
        )
        if not invoice.get('invoice_id'):
            self.tg.send_text(session.customer_id, 'Не вдалося створити рахунок.')
            return None

        tx = PaymentTransaction(
            session_id=session.id, invoice_id=invoice['invoice_id'],
            amount=amount_kopecks, status='pending',
            product_json=json.dumps(state.get('product', {}), ensure_ascii=False)
        )
        db.session.add(tx)
        state['invoice_id']  = invoice['invoice_id']
        state['invoice_url'] = invoice['page_url']
        session.set_state(state)
        db.session.commit()
        self.tg.send_payment_link(session.customer_id, invoice['page_url'], amount_uah, description)
        return 'out-0'

    def _handle_check_payment(self, node, config, session, update):
        state      = session.get_state()
        invoice_id = state.get('invoice_id')
        if not invoice_id:
            return 'unpaid'
        tx = PaymentTransaction.query.filter_by(invoice_id=invoice_id).first()
        if tx and tx.status == 'paid':
            self.tg.send_text(session.customer_id, 'Оплату підтверджено! Дякуємо!')
            return 'paid'
        user_text = (update.get('text') or '')
        if user_text == 'check_payment' or update.get('type') == 'callback':
            from monobank_service import MonobankService
            status = MonobankService().check_status(invoice_id)
            if status == 'success':
                if tx:
                    tx.status  = 'paid'
                    tx.paid_at = datetime.utcnow()
                    db.session.commit()
                self.tg.send_text(session.customer_id, 'Оплату підтверджено! Дякуємо!')
                return 'paid'
            self.tg.send_text(session.customer_id, 'Оплата ще не надійшла. Спробуйте ще раз.')
            return 'unpaid'
        self.tg.send_text(session.customer_id, 'Очікуємо оплату. Натисніть "Я вже оплатив".')
        return None

    def _handle_send_link(self, node, config, session, update):
        url     = config.get('url', '')
        message = config.get('message', 'Ось ваш доступ:')
        if not url:
            self.tg.send_text(session.customer_id, 'Посилання ще не налаштовано.')
            return None
        state      = session.get_state()
        invoice_id = state.get('invoice_id')
        if invoice_id:
            tx = PaymentTransaction.query.filter_by(invoice_id=invoice_id).first()
            if tx:
                tx.access_link = url
                db.session.commit()
        self.tg.send_link(session.customer_id, message, url)
        return None

    def _handle_handoff(self, node, config, session, update):
        message = config.get('message', 'Передаю вас оператору підтримки...')
        self.tg.send_text(
            session.customer_id,
            f"{message}\n\nОчікуйте — оператор підключиться найближчим часом."
        )
        session.mode = 'pending'
        db.session.commit()

        admin_chat_id = os.getenv('ADMIN_CHAT_ID')
        if admin_chat_id:
            state       = session.get_state()
            product     = state.get('product', {})
            product_str = f"\nТовар: {product['name']} — {product['price']} грн" if product else ''
            self.tg.send_buttons(
                admin_chat_id,
                f"Новий запит на підтримку!\n\n"
                f"Покупець: {session.customer_id}\n"
                f"Сесія: #{session.id}"
                f"{product_str}\n\n"
                f"Прийміть розмову:",
                [f'/take_{session.id}']
            )
        else:
            logger.warning('ADMIN_CHAT_ID не встановлено в .env')
        return None

    def _follow_edge(self, node: FlowNode, port_id: str):
        edge = FlowEdge.query.filter_by(source_node_id=node.id, port_id=port_id).first()
        if not edge:
            return None
        return FlowNode.query.get(edge.target_node_id)
