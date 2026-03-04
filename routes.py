import json
import logging
from datetime import datetime
from flask import Blueprint, request, jsonify
from extensions import db
from models import (User, FlowTemplate, FlowNode, FlowEdge,
                    DeployedBot, ConversationSession, PaymentTransaction)

logger = logging.getLogger(__name__)
api = Blueprint('api', __name__, url_prefix='/api')


def get_or_create_default_user():
    user = User.query.first()
    if not user:
        user = User(user_id='admin_1', email='admin@botflow.app', status='ACTIVE')
        db.session.add(user)
        db.session.commit()
    return user

def ok(data, status=200):
    return jsonify({'ok': True, 'data': data}), status

def err(message, status=400):
    return jsonify({'ok': False, 'error': message}), status


# ── TEMPLATES ────────────────────────────────────────────────────────────────

@api.route('/templates', methods=['GET'])
def list_templates():
    user = get_or_create_default_user()
    templates = FlowTemplate.query.filter_by(owner_id=user.id).order_by(FlowTemplate.updated_at.desc()).all()
    return ok([t.to_dict() for t in templates])

@api.route('/templates', methods=['POST'])
def create_template():
    user = get_or_create_default_user()
    body = request.get_json(silent=True) or {}
    template = FlowTemplate(name=body.get('name', 'Новий шаблон'),
                            description=body.get('description', ''), owner_id=user.id)
    db.session.add(template)
    db.session.commit()
    return ok(template.to_dict(), status=201)

@api.route('/templates/<int:template_id>', methods=['GET'])
def get_template(template_id):
    template = FlowTemplate.query.get_or_404(template_id)
    return ok(template.to_dict(include_flow=True))

@api.route('/templates/<int:template_id>', methods=['PUT'])
def save_template(template_id):
    template = FlowTemplate.query.get_or_404(template_id)
    body = request.get_json(silent=True)
    if not body:
        return err('Порожнє тіло запиту')
    if 'name' in body:
        template.name = body['name']
    if 'description' in body:
        template.description = body['description']
    template.updated_at = datetime.utcnow()

    FlowEdge.query.filter_by(template_id=template_id).delete()
    FlowNode.query.filter_by(template_id=template_id).delete()
    db.session.flush()

    client_to_server = {}
    for node_data in body.get('nodes', []):
        node = FlowNode(
            template_id=template_id,
            node_type=node_data.get('node_type', 'message'),
            pos_x=float(node_data.get('x', 100)),
            pos_y=float(node_data.get('y', 100)),
            config_json=json.dumps(node_data.get('config', {}), ensure_ascii=False)
        )
        db.session.add(node)
        db.session.flush()
        client_to_server[str(node_data.get('id'))] = node.id

    for edge_data in body.get('edges', []):
        from_id = client_to_server.get(str(edge_data.get('from')))
        to_id   = client_to_server.get(str(edge_data.get('to')))
        if not from_id or not to_id:
            continue
        edge = FlowEdge(template_id=template_id, source_node_id=from_id,
                        target_node_id=to_id, port_id=edge_data.get('portId', 'out-0'))
        db.session.add(edge)

    db.session.commit()
    return ok(template.to_dict(include_flow=True))

@api.route('/templates/<int:template_id>', methods=['DELETE'])
def delete_template(template_id):
    template = FlowTemplate.query.get_or_404(template_id)
    db.session.delete(template)
    db.session.commit()
    return ok({'deleted_id': template_id})


# ── DEPLOY ────────────────────────────────────────────────────────────────────

@api.route('/deploy', methods=['GET'])
def list_deployments():
    user = get_or_create_default_user()
    template_ids = [t.id for t in user.templates]
    bots = DeployedBot.query.filter(DeployedBot.template_id.in_(template_ids)).all()
    return ok([b.to_dict() for b in bots])

@api.route('/deploy', methods=['POST'])
def deploy_template():
    body = request.get_json(silent=True)
    if not body:
        return err('Порожнє тіло запиту')
    template_id  = body.get('template_id')
    platform     = body.get('platform')
    access_token = body.get('access_token', '').strip()
    if not template_id or not platform or not access_token:
        return err('Обовязкові поля: template_id, platform, access_token')
    if platform not in ('telegram', 'facebook', 'messenger', 'instagram'):
        return err('Непідтримувана платформа')
    template = FlowTemplate.query.get(template_id)
    if not template:
        return err('Шаблон не знайдено', 404)

    existing = DeployedBot.query.filter_by(template_id=template_id, platform=platform, is_active=True).first()
    if existing:
        existing.access_token  = access_token
        existing.page_id       = body.get('page_id')
        existing.webhook_secret= body.get('webhook_secret')
        db.session.commit()
        bot = existing
    else:
        bot = DeployedBot(template_id=template_id, platform=platform, access_token=access_token,
                          page_id=body.get('page_id'), webhook_secret=body.get('webhook_secret'), is_active=True)
        db.session.add(bot)
        db.session.commit()

    if platform == 'telegram':
        try:
            from main import auto_register_webhook
            auto_register_webhook(bot.id)
        except Exception as e:
            logger.warning('Webhook auto-register failed: %s', e)

    result = bot.to_dict()
    result['webhook_url'] = f'/webhook/telegram/{bot.id}'
    return ok(result, status=201)

@api.route('/deploy/<int:bot_id>', methods=['DELETE'])
def undeploy(bot_id):
    bot = DeployedBot.query.get_or_404(bot_id)
    bot.is_active = False
    db.session.commit()
    return ok({'deactivated_id': bot_id})


# ── CONVERSATIONS ─────────────────────────────────────────────────────────────

@api.route('/conversations', methods=['GET'])
def list_conversations():
    bot_id = request.args.get('bot_id', type=int)
    mode   = request.args.get('mode')
    limit  = request.args.get('limit', 50, type=int)
    query  = ConversationSession.query
    if bot_id:
        query = query.filter_by(deployed_bot_id=bot_id)
    if mode:
        query = query.filter_by(mode=mode)
    sessions = query.order_by(ConversationSession.updated_at.desc()).limit(limit).all()
    return ok([s.to_dict() for s in sessions])

@api.route('/conversations/<int:session_id>', methods=['GET'])
def get_conversation(session_id):
    session = ConversationSession.query.get_or_404(session_id)
    return ok(session.to_dict())

@api.route('/conversations/<int:session_id>/takeover', methods=['POST'])
def takeover(session_id):
    """Оператор приймає розмову через API (альтернатива до /take_ команди в Telegram)."""
    session = ConversationSession.query.get_or_404(session_id)
    body    = request.get_json(silent=True) or {}
    operator_id = body.get('operator_id', 'api_operator')

    bot = DeployedBot.query.get(session.deployed_bot_id)
    if bot:
        from telegram_service import TelegramService
        TelegramService(bot.access_token).send_text(
            session.customer_id, 'Оператор підключився! Ви можете писати.')

    session.mode        = 'human'
    session.operator_id = operator_id
    db.session.commit()
    return ok(session.to_dict())

@api.route('/conversations/<int:session_id>/reply', methods=['POST'])
def reply_to_customer(session_id):
    """Оператор відповідає покупцю через API."""
    session = ConversationSession.query.get_or_404(session_id)
    body    = request.get_json(silent=True) or {}
    text    = body.get('text', '').strip()
    if not text:
        return err('Текст не може бути порожнім')
    bot = DeployedBot.query.get(session.deployed_bot_id)
    if bot:
        from telegram_service import TelegramService
        TelegramService(bot.access_token).send_text(session.customer_id, f"Оператор:\n{text}")
    return ok({'sent': True})

@api.route('/conversations/<int:session_id>/close', methods=['POST'])
def close_conversation(session_id):
    """Оператор закриває розмову, бот продовжує."""
    session = ConversationSession.query.get_or_404(session_id)
    bot     = DeployedBot.query.get(session.deployed_bot_id)
    if bot:
        from telegram_service import TelegramService
        TelegramService(bot.access_token).send_text(
            session.customer_id,
            'Розмову з оператором завершено. Якщо потрібна допомога — напишіть /start.')
    session.mode        = 'bot'
    session.operator_id = None
    db.session.commit()
    return ok(session.to_dict())


# ── PAYMENTS ──────────────────────────────────────────────────────────────────

@api.route('/payments', methods=['GET'])
def list_payments():
    status = request.args.get('status')
    limit  = request.args.get('limit', 50, type=int)
    query  = PaymentTransaction.query
    if status:
        query = query.filter_by(status=status)
    transactions = query.order_by(PaymentTransaction.created_at.desc()).limit(limit).all()
    return ok([t.to_dict() for t in transactions])

@api.route('/payments/<int:payment_id>', methods=['GET'])
def get_payment(payment_id):
    tx = PaymentTransaction.query.get_or_404(payment_id)
    return ok(tx.to_dict())


# ── HEALTH ────────────────────────────────────────────────────────────────────

@api.route('/health', methods=['GET'])
def health():
    return ok({'status': 'ok', 'timestamp': datetime.utcnow().isoformat()})
