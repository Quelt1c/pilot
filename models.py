from datetime import datetime
from extensions import db


class User(db.Model):
    __tablename__ = 'user'
    id         = db.Column(db.Integer, primary_key=True)
    user_id    = db.Column(db.String(50), unique=True, nullable=False)
    email      = db.Column(db.String(120), unique=True, nullable=True)
    status     = db.Column(db.String(20), default='ACTIVE')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    templates  = db.relationship('FlowTemplate', backref='owner', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {'id': self.id, 'user_id': self.user_id, 'email': self.email,
                'status': self.status, 'created_at': self.created_at.isoformat()}


class FlowTemplate(db.Model):
    __tablename__ = 'flow_template'
    id          = db.Column(db.Integer, primary_key=True)
    name        = db.Column(db.String(120), nullable=False, default='Новий шаблон')
    description = db.Column(db.Text, nullable=True)
    owner_id    = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    nodes       = db.relationship('FlowNode', backref='template', lazy=True, cascade='all, delete-orphan')
    edges       = db.relationship('FlowEdge', backref='template', lazy=True, cascade='all, delete-orphan')
    deployments = db.relationship('DeployedBot', backref='template', lazy=True, cascade='all, delete-orphan')

    def to_dict(self, include_flow=False):
        data = {'id': self.id, 'name': self.name, 'description': self.description,
                'owner_id': self.owner_id, 'created_at': self.created_at.isoformat(),
                'updated_at': self.updated_at.isoformat()}
        if include_flow:
            data['nodes'] = [n.to_dict() for n in self.nodes]
            data['edges'] = [e.to_dict() for e in self.edges]
        return data


class FlowNode(db.Model):
    __tablename__ = 'flow_node'
    id          = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey('flow_template.id'), nullable=False)
    node_type   = db.Column(db.String(30), nullable=False)
    pos_x       = db.Column(db.Float, default=100)
    pos_y       = db.Column(db.Float, default=100)
    config_json = db.Column(db.Text, default='{}')

    def to_dict(self):
        import json
        return {'id': self.id, 'template_id': self.template_id, 'node_type': self.node_type,
                'x': self.pos_x, 'y': self.pos_y, 'config': json.loads(self.config_json or '{}')}


class FlowEdge(db.Model):
    __tablename__ = 'flow_edge'
    id             = db.Column(db.Integer, primary_key=True)
    template_id    = db.Column(db.Integer, db.ForeignKey('flow_template.id'), nullable=False)
    source_node_id = db.Column(db.Integer, db.ForeignKey('flow_node.id'), nullable=False)
    target_node_id = db.Column(db.Integer, db.ForeignKey('flow_node.id'), nullable=False)
    port_id        = db.Column(db.String(20), default='out-0')

    def to_dict(self):
        return {'id': self.id, 'from': self.source_node_id,
                'to': self.target_node_id, 'portId': self.port_id}


class DeployedBot(db.Model):
    __tablename__ = 'deployed_bot'
    id             = db.Column(db.Integer, primary_key=True)
    template_id    = db.Column(db.Integer, db.ForeignKey('flow_template.id'), nullable=False)
    platform       = db.Column(db.String(20), nullable=False)
    access_token   = db.Column(db.String(512), nullable=False)
    page_id        = db.Column(db.String(100), nullable=True)
    webhook_secret = db.Column(db.String(100), nullable=True)
    is_active      = db.Column(db.Boolean, default=True)
    deployed_at    = db.Column(db.DateTime, default=datetime.utcnow)
    sessions       = db.relationship('ConversationSession', backref='bot', lazy=True, cascade='all, delete-orphan')

    def to_dict(self):
        return {'id': self.id, 'template_id': self.template_id, 'platform': self.platform,
                'is_active': self.is_active, 'deployed_at': self.deployed_at.isoformat()}


class ConversationSession(db.Model):
    __tablename__ = 'conversation_session'
    id              = db.Column(db.Integer, primary_key=True)
    deployed_bot_id = db.Column(db.Integer, db.ForeignKey('deployed_bot.id'), nullable=False)
    customer_id     = db.Column(db.String(100), nullable=False)
    current_node_id = db.Column(db.Integer, db.ForeignKey('flow_node.id'), nullable=True)

    # Режим розмови:
    #   'bot'     → бот відповідає автоматично по схемі
    #   'pending' → чекаємо оператора, бот тримає юзера
    #   'human'   → оператор відповідає вручну, бот мовчить
    mode = db.Column(db.String(20), default='bot')

    # Telegram chat_id оператора що прийняв розмову
    operator_id = db.Column(db.String(100), nullable=True)

    state_json  = db.Column(db.Text, default='{}')
    created_at  = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at  = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    transactions = db.relationship('PaymentTransaction', backref='session', lazy=True)

    def get_state(self):
        import json
        return json.loads(self.state_json or '{}')

    def set_state(self, data):
        import json
        self.state_json = json.dumps(data, ensure_ascii=False)

    def to_dict(self):
        return {'id': self.id, 'customer_id': self.customer_id,
                'current_node_id': self.current_node_id, 'mode': self.mode,
                'operator_id': self.operator_id, 'state': self.get_state(),
                'updated_at': self.updated_at.isoformat()}


class PaymentTransaction(db.Model):
    __tablename__ = 'payment_transaction'
    id           = db.Column(db.Integer, primary_key=True)
    session_id   = db.Column(db.Integer, db.ForeignKey('conversation_session.id'), nullable=False)
    invoice_id   = db.Column(db.String(100), unique=True, nullable=False)
    amount       = db.Column(db.Integer, nullable=False)
    status       = db.Column(db.String(20), default='pending')
    product_json = db.Column(db.Text, default='{}')
    access_link  = db.Column(db.Text, nullable=True)
    created_at   = db.Column(db.DateTime, default=datetime.utcnow)
    paid_at      = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        import json
        return {'id': self.id, 'invoice_id': self.invoice_id, 'amount': self.amount,
                'status': self.status, 'product': json.loads(self.product_json or '{}'),
                'access_link': self.access_link, 'created_at': self.created_at.isoformat(),
                'paid_at': self.paid_at.isoformat() if self.paid_at else None}
