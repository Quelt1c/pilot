"""
webhook_handlers.py — Прийом webhook подій

  POST /webhook/telegram/<bot_id>          ← Telegram (покупці і оператор)
  POST /webhook/monobank/<session_id>      ← Monobank статус оплати
  GET  /webhook/telegram/<bot_id>/info     ← debug стан webhook
  POST /webhook/telegram/<bot_id>/register ← реєстрація webhook
  POST /webhook/telegram/<bot_id>/test     ← симуляція (тільки dev)
"""

import os
import json
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, abort
from extensions import db
from models import DeployedBot, ConversationSession, PaymentTransaction
from telegram_service import TelegramService
from bot_engine import BotEngine

logger   = logging.getLogger(__name__)
webhooks = Blueprint('webhooks', __name__, url_prefix='/webhook')


# ── TELEGRAM ──────────────────────────────────────────────────────────────────

@webhooks.route('/telegram/<int:bot_id>', methods=['POST'])
def telegram_webhook(bot_id):
    bot = DeployedBot.query.get(bot_id)
    if not bot or not bot.is_active:
        return jsonify({'ok': True}), 200

    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({'ok': True}), 200

    if not data:
        return jsonify({'ok': True}), 200

    update = TelegramService.parse_update(data)
    if not update or not update.get('chat_id'):
        return jsonify({'ok': True}), 200

    admin_chat_id = os.getenv('ADMIN_CHAT_ID', '')
    text          = (update.get('text') or '').strip()

    # ── Повідомлення від ОПЕРАТОРА з адмін-чату ──────────────────────────────
    # Якщо пише людина з ADMIN_CHAT_ID і починає з / — це команда оператора
    if admin_chat_id and update['chat_id'] == admin_chat_id and text.startswith('/'):
        try:
            engine = BotEngine(bot)
            engine.process_operator_command(text, admin_chat_id)
        except Exception as e:
            logger.exception('Operator command error: %s', e)
        return jsonify({'ok': True}), 200

    # ── Повідомлення від ПОКУПЦЯ ──────────────────────────────────────────────
    try:
        engine = BotEngine(bot)
        engine.process(update)
    except Exception as e:
        logger.exception('BotEngine error bot=%s: %s', bot_id, e)
        try:
            TelegramService(bot.access_token).send_text(
                update['chat_id'],
                'Сталася помилка. Напишіть /start щоб почати знову.'
            )
        except Exception:
            pass

    return jsonify({'ok': True}), 200


@webhooks.route('/telegram/<int:bot_id>/info', methods=['GET'])
def telegram_webhook_info(bot_id):
    bot  = DeployedBot.query.get_or_404(bot_id)
    info = TelegramService(bot.access_token).get_webhook_info()
    return jsonify(info)


@webhooks.route('/telegram/<int:bot_id>/register', methods=['POST'])
def register_webhook(bot_id):
    bot      = DeployedBot.query.get_or_404(bot_id)
    body     = request.get_json(silent=True) or {}
    base_url = body.get('base_url') or os.getenv('BASE_URL', '')
    if not base_url:
        return jsonify({'ok': False, 'error': 'base_url не вказано і BASE_URL не в .env'}), 400

    webhook_url = f'{base_url.rstrip("/")}/webhook/telegram/{bot_id}'
    ok          = TelegramService(bot.access_token).register_webhook(webhook_url)
    if ok:
        return jsonify({'ok': True, 'webhook_url': webhook_url})
    return jsonify({'ok': False, 'error': 'Не вдалося зареєструвати webhook'}), 500


@webhooks.route('/telegram/<int:bot_id>/unregister', methods=['POST'])
def unregister_webhook(bot_id):
    bot = DeployedBot.query.get_or_404(bot_id)
    ok  = TelegramService(bot.access_token).delete_webhook()
    return jsonify({'ok': ok})


# ── MONOBANK ──────────────────────────────────────────────────────────────────

@webhooks.route('/monobank/<int:session_id>', methods=['POST'])
def monobank_webhook(session_id):
    session = ConversationSession.query.get(session_id)
    if not session:
        return jsonify({'ok': True}), 200

    try:
        data = request.get_json(force=True) or {}
    except Exception:
        return jsonify({'ok': True}), 200

    invoice_id = data.get('invoiceId')
    status     = data.get('status')
    if not invoice_id or not status:
        return jsonify({'ok': True}), 200

    tx = PaymentTransaction.query.filter_by(invoice_id=invoice_id).first()
    if tx:
        if status == 'success':
            tx.status  = 'paid'
            tx.paid_at = datetime.utcnow()
        elif status == 'failure':
            tx.status = 'failure'
        db.session.commit()

    if status == 'success':
        bot = DeployedBot.query.get(session.deployed_bot_id)
        if bot and bot.is_active:
            try:
                engine = BotEngine(bot)
                engine.process({
                    'type': 'callback', 'chat_id': session.customer_id,
                    'text': 'check_payment', 'callback_query_id': None,
                })
            except Exception as e:
                logger.exception('Post-payment error: %s', e)

    return jsonify({'ok': True}), 200


# ── TEST (тільки dev) ─────────────────────────────────────────────────────────

@webhooks.route('/telegram/<int:bot_id>/test', methods=['POST'])
def test_message(bot_id):
    if os.getenv('FLASK_ENV') != 'development' and os.getenv('ALLOW_TEST_WEBHOOK') != 'true':
        abort(403)

    bot  = DeployedBot.query.get_or_404(bot_id)
    body = request.get_json(silent=True) or {}

    update = {
        'type': 'message',
        'chat_id': str(body.get('chat_id', 'test_user')),
        'text':    body.get('text', '/start'),
        'message_id': 1,
        'callback_query_id': None,
    }
    try:
        BotEngine(bot).process(update)
        return jsonify({'ok': True, 'message': f'Оброблено: {update["text"]}'})
    except Exception as e:
        logger.exception('Test webhook error: %s', e)
        return jsonify({'ok': False, 'error': str(e)}), 500
