"""
BotFlow — Головний файл додатку

Запуск:
    pip install flask flask-sqlalchemy flask-cors requests python-dotenv
    python main.py

Змінні середовища (.env):
    DATABASE_URL        — рядок підключення (default: SQLite)
    SECRET_KEY          — секретний ключ Flask
    PORT                — порт (default: 5000)
    BASE_URL            — публічний URL сервера (для webhook реєстрації)
                          напр: https://yourdomain.com або ngrok URL під час розробки
    MONOBANK_TOKEN      — токен Monobank Acquiring API
    PAYMENT_REDIRECT_URL — куди редіректити після оплати
    FLASK_ENV           — development | production
    ALLOW_TEST_WEBHOOK  — true (дозволяє /webhook/telegram/<id>/test)
    LOG_LEVEL           — DEBUG | INFO | WARNING (default: INFO)
"""

import os
import logging
from flask import Flask, send_from_directory, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# ─────────────────────────────────────
# ЛОГУВАННЯ
# ─────────────────────────────────────
log_level = getattr(logging, os.getenv('LOG_LEVEL', 'INFO').upper(), logging.INFO)
logging.basicConfig(
    level=log_level,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


from extensions import db
from routes import api
from webhook_handlers import webhooks


def create_app():
    app = Flask(__name__, static_folder='static')

    # ── Конфігурація ──────────────────────────────────────────
    app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'dev-secret-change-in-prod')
    app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///botflow.db')
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}

    # ── Розширення ────────────────────────────────────────────
    db.init_app(app)
    CORS(app, resources={r'/api/*': {'origins': '*'}})

    # ── Blueprints ────────────────────────────────────────────
    app.register_blueprint(api)
    app.register_blueprint(webhooks)

    # ── Статичні файли ────────────────────────────────────────
    @app.route('/')
    def index():
        return send_from_directory('.', 'flow_builder.html')

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({'ok': False, 'error': 'Not found'}), 404

    @app.errorhandler(500)
    def server_error(e):
        logger.exception('Internal error: %s', e)
        return jsonify({'ok': False, 'error': 'Internal server error'}), 500

    # ── Ініціалізація БД ──────────────────────────────────────
    with app.app_context():
        import models  # noqa: F401
        db.create_all()
        logger.info('✅ База даних готова')
        _print_routes(app)

    return app


def auto_register_webhook(bot_id: int):
    """Реєструє Telegram webhook одразу після деплою."""
    base_url = os.getenv('BASE_URL', '').strip()
    if not base_url:
        logger.warning(
            'BASE_URL не встановлено — зробіть вручну: '
            'POST /webhook/telegram/%s/register', bot_id
        )
        return False

    from models import DeployedBot
    from telegram_service import TelegramService

    bot = DeployedBot.query.get(bot_id)
    if not bot or bot.platform != 'telegram':
        return False

    webhook_url = f'{base_url.rstrip("/")}/webhook/telegram/{bot_id}'
    tg = TelegramService(bot.access_token)
    ok = tg.register_webhook(webhook_url)
    logger.info('%s Webhook: %s', '✅' if ok else '❌', webhook_url)
    return ok


def _print_routes(app):
    logger.info('📡 Маршрути:')
    rules = sorted(app.url_map.iter_rules(), key=lambda r: r.rule)
    for rule in rules:
        if any(rule.rule.startswith(p) for p in ('/api', '/webhook', '/')):
            methods = ', '.join(m for m in sorted(rule.methods) if m not in ('HEAD', 'OPTIONS'))
            logger.info('  %-30s %s', methods, rule.rule)


if __name__ == '__main__':
    app = create_app()
    port = int(os.getenv('PORT', 5000))
    logger.info('🚀 http://localhost:%s', port)
    app.run(debug=os.getenv('FLASK_ENV') == 'development', host='0.0.0.0', port=port)
