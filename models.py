"""数据模型层 —— 定义「用户」「任务」这些表长什么样。

每个 class 就是一张数据库表，class 里的变量就是表的列。
"""

from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

# 创建一个全局的 db 对象，整个项目共用这一个数据库连接
db = SQLAlchemy()


# ========== 任务状态常量 ==========
# 把所有合法状态写成一个集合，代码里引用常量而不是裸字符串，
# 这样既不会拼错，也方便以后统一改名。
class TaskStatus:
    DRAFT = 'draft'              # 草稿：发布人暂存，不出现在公开计划板
    OPEN = 'open'                # 公开招募：出现在计划板上，等人接取
    ACCEPTED = 'accepted'        # 已领取：有人接了，但还没正式开工
    PROCESSING = 'processing'    # 执行中：接取人开工，开始记进度
    SUBMITTED = 'submitted'      # 提交审核：接取人说做完了，等验收
    REJECTED = 'rejected'        # 打回：验收不通过，退回重做
    COMPLETED = 'completed'      # 完成：验收通过，已结算奖励
    CLOSED = 'closed'            # 关闭：归档；或发布人在 open 阶段撤销


# 合法的状态流转映射：key 是当前状态，value 是这个状态可以转去哪些状态。
# 状态机校验时就查这张表，不在表里的流转一律拒绝。
VALID_TRANSITIONS = {
    TaskStatus.DRAFT:      {TaskStatus.OPEN, TaskStatus.CLOSED},
    TaskStatus.OPEN:       {TaskStatus.ACCEPTED, TaskStatus.CLOSED},
    TaskStatus.ACCEPTED:   {TaskStatus.PROCESSING, TaskStatus.OPEN},
    TaskStatus.PROCESSING: {TaskStatus.SUBMITTED, TaskStatus.ACCEPTED},
    TaskStatus.SUBMITTED:  {TaskStatus.COMPLETED, TaskStatus.REJECTED},
    TaskStatus.REJECTED:   {TaskStatus.PROCESSING},   # 打回后重新开工
    TaskStatus.COMPLETED:  {TaskStatus.CLOSED},
    TaskStatus.CLOSED:     set(),                      # 终态
}


class User(UserMixin, db.Model):
    """用户表 —— 对应世界观里的「拾荒者 / 建造师 / 镇长」。"""

    __tablename__ = 'users'

    # id 是主键，每新增一个用户自动 +1，永远不会重复
    id = db.Column(db.Integer, primary_key=True)

    # 用户名：登录用，唯一（不能两个人同名）
    username = db.Column(db.String(80), unique=True, nullable=False)

    # 密码哈希：存的是加密后的密码，不存明文
    password_hash = db.Column(db.String(255), nullable=False)

    # 显示名：在页面上展示的名字（可以中文）
    display_name = db.Column(db.String(80), nullable=False)

    # 角色：admin（镇长）/ senior_builder（资深建造师）/ builder（建造师）/ scavenger（拾荒者）
    role = db.Column(db.String(20), default='scavenger', nullable=False)

    # 新镇民必须经过资深建造师或镇长批准，才能登录进入小镇。
    account_status = db.Column(db.String(20), default='approved', nullable=False)

    # 游戏化数据
    rebuild_points = db.Column(db.Integer, default=0)        # 经验值
    sprout_coins = db.Column(db.Integer, default=0)          # 金币
    town_trust = db.Column(db.Integer, default=0)            # 声望
    star_shards = db.Column(db.Integer, default=0)           # 稀有材料

    # 注册时间
    created_at = db.Column(db.DateTime, default=datetime.now)

    @property
    def role_label(self):
        """系统只保留四种正式身份，角色决定权限。"""
        labels = {
            'scavenger': '拾荒者',
            'builder': '建造师',
            'senior_builder': '资深建造师',
            'admin': '镇长',
        }
        return labels.get(self.role, self.role)

    @property
    def growth_level(self):
        """身份、等级和头像统一由正式角色决定，避免积分导致身份错位。"""
        levels = {
            'scavenger': {'number': 1, 'title': '拾荒者', 'avatar': 'scavenger.jpg'},
            'builder': {'number': 2, 'title': '建造师', 'avatar': 'builder.jpg'},
            'senior_builder': {'number': 3, 'title': '资深建造师', 'avatar': 'senior-builder.jpg'},
            'admin': {'number': 4, 'title': '镇长', 'avatar': 'mayor.jpg'},
        }
        return levels.get(self.role, levels['scavenger'])

    @property
    def level_label(self):
        level = self.growth_level
        return f"Lv.{level['number']} {level['title']}"

    @property
    def avatar_filename(self):
        return f"images/avatars/{self.growth_level['avatar']}"

    @property
    def next_level_progress(self):
        """身份阶梯展示；角色提拔由镇长决定，不再由积分自动改名。"""
        return self.growth_level['number'] * 25

    def can_publish_task(self):
        """判断当前用户是否有发布春谷计划的权限。"""
        return self.role in {'admin', 'senior_builder', 'builder'}

    @property
    def account_status_label(self):
        return {
            'pending': '等待批准',
            'approved': '已批准',
            'rejected': '未通过',
            'deleted': '已删除',
        }.get(self.account_status, self.account_status)


class PromotionRequest(db.Model):
    """身份晋升申请 —— 完整保留申请与审批记录。

    active_key 只在待审批时有值，并受唯一约束保护。这样即使用户连续点击
    两次“申请”，数据库也只允许存在一条正在处理的建造师申请；审批结束后
    active_key 清空，未来如果申请曾被拒绝，用户仍可重新提交一条新记录。
    """

    __tablename__ = 'promotion_requests'

    id = db.Column(db.Integer, primary_key=True)
    applicant_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True, index=True)
    target_role = db.Column(db.String(20), nullable=False, default='builder')
    status = db.Column(db.String(20), nullable=False, default='pending', index=True)
    active_key = db.Column(db.String(80), unique=True, nullable=True)
    points_at_apply = db.Column(db.Integer, nullable=False)
    points_at_review = db.Column(db.Integer, nullable=True)
    application_note = db.Column(db.String(300), nullable=True)
    review_note = db.Column(db.String(300), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    reviewed_at = db.Column(db.DateTime, nullable=True)

    applicant = db.relationship(
        'User', foreign_keys=[applicant_id],
        backref=db.backref('promotion_requests', lazy='dynamic', cascade='all, delete-orphan'),
    )
    reviewer = db.relationship('User', foreign_keys=[reviewer_id])

    @property
    def status_label(self):
        return {
            'pending': '等待镇长审批',
            'approved': '已批准',
            'rejected': '未通过',
        }.get(self.status, self.status)


class Task(db.Model):
    """任务表 —— 对应世界观里计划板上的一项项春谷计划。"""

    CATEGORY_LABELS = {
        'general': '协作任务',
        'drawing': '图纸测绘',
        'calc': '物资测算',
        'build': '结构搭建',
        'scout': '远行勘探',
    }

    STATUS_LABELS = {
        TaskStatus.DRAFT:      '草稿',
        TaskStatus.OPEN:       '待接取',
        TaskStatus.ACCEPTED:   '已领取',
        TaskStatus.PROCESSING: '执行中',
        TaskStatus.SUBMITTED:  '待审核',
        TaskStatus.REJECTED:   '已打回',
        TaskStatus.COMPLETED:  '已完成',
        TaskStatus.CLOSED:     '已关闭',
    }

    __tablename__ = 'tasks'

    id = db.Column(db.Integer, primary_key=True)

    # 任务标题，比如「修补北区屋顶」
    title = db.Column(db.String(200), nullable=False)

    # 任务详细描述
    description = db.Column(db.Text, nullable=False)

    # 任务分类：drawing（图纸测绘）/ calc（物资测算）/ build（结构搭建）/ scout（远行勘探）
    category = db.Column(db.String(20), nullable=False)

    # 难度等级：S / A / B / C
    difficulty = db.Column(db.String(5), default='C', nullable=False)

    # 奖励
    reward_points = db.Column(db.Integer, default=10)   # 完成后给的重建点
    reward_coins = db.Column(db.Integer, default=20)   # 完成后给的春芽币

    # 发布人（外键 → users.id）
    publisher_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)

    # 接取人（可空，表示还没人接）
    acceptor_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    # 任务状态：见 TaskStatus
    status = db.Column(db.String(20), default=TaskStatus.OPEN, nullable=False)

    # 进度记录：接取人每次更新进度时写的说明，最新一条覆盖旧的
    progress_note = db.Column(db.Text, nullable=True)

    # 打回理由：验收不通过时填写
    reject_reason = db.Column(db.Text, nullable=True)

    # 时间戳
    created_at = db.Column(db.DateTime, default=datetime.now)
    accepted_at = db.Column(db.DateTime, nullable=True)
    submitted_at = db.Column(db.DateTime, nullable=True)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    completed_at = db.Column(db.DateTime, nullable=True)

    # 关系映射：方便从 task 直接拿到 publisher / acceptor 对象
    publisher = db.relationship('User', foreign_keys=[publisher_id], backref='published_tasks')
    acceptor = db.relationship('User', foreign_keys=[acceptor_id], backref='accepted_tasks')

    @property
    def category_label(self):
        return self.CATEGORY_LABELS.get(self.category, self.category)

    @property
    def status_label(self):
        return self.STATUS_LABELS.get(self.status, self.status)

    def can_transition_to(self, new_status):
        """检查当前状态是否允许流转到 new_status。"""
        return new_status in VALID_TRANSITIONS.get(self.status, set())


class DailyCheckin(db.Model):
    """签到记录表 —— 记录镇民每天领取了多少春芽币。"""

    __tablename__ = 'daily_checkins'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    reward_coins = db.Column(db.Integer, nullable=False)
    checkin_date = db.Column(db.Date, nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    user = db.relationship('User', backref=db.backref('daily_checkins', lazy='dynamic', cascade='all, delete-orphan'))


class ShopRedemption(db.Model):
    """兑换记录表 —— 记录镇民在春芽商店里兑换过什么。"""

    __tablename__ = 'shop_redemptions'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    item_key = db.Column(db.String(40), nullable=False)
    item_name = db.Column(db.String(120), nullable=False)
    cost_coins = db.Column(db.Integer, nullable=False)
    note = db.Column(db.String(300), nullable=True)
    granted_star_shards = db.Column(db.Integer, nullable=False, default=0)
    granted_town_trust = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    user = db.relationship('User', backref=db.backref('shop_redemptions', lazy='dynamic', cascade='all, delete-orphan'))


class ShopItem(db.Model):
    """春芽商店商品表 —— 镇长可编辑内容、金额并控制上下架。"""

    __tablename__ = 'shop_items'

    id = db.Column(db.Integer, primary_key=True)
    item_key = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    cost_coins = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(500), nullable=False)
    note = db.Column(db.String(300), nullable=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)

    @property
    def key(self):
        return self.item_key

    @property
    def cost(self):
        return self.cost_coins


class MonthlyRankingReward(db.Model):
    """月度排行结算记录 —— 防止前三名奖励重复发放。"""

    __tablename__ = 'monthly_ranking_rewards'
    __table_args__ = (
        db.UniqueConstraint('period_key', 'user_id', name='uq_monthly_reward_period_user'),
    )

    id = db.Column(db.Integer, primary_key=True)
    period_key = db.Column(db.String(7), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    rank = db.Column(db.Integer, nullable=False)
    contribution_points = db.Column(db.Integer, nullable=False)
    reward_coins = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    user = db.relationship('User', backref=db.backref('monthly_ranking_rewards', lazy='dynamic'))


class TownState(db.Model):
    """全镇阶段状态 —— 记录阶段奖励已经发放到哪一档。"""

    __tablename__ = 'town_state'

    id = db.Column(db.Integer, primary_key=True, default=1)
    last_rewarded_stage = db.Column(db.Integer, nullable=False, default=1)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.now, onupdate=datetime.now)


class Notification(db.Model):
    """通知表 —— 记录任务领取、提交和审核结果。"""

    __tablename__ = 'notifications'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False, index=True)
    task_id = db.Column(db.Integer, db.ForeignKey('tasks.id'), nullable=True, index=True)
    event_type = db.Column(db.String(30), nullable=False)
    title = db.Column(db.String(120), nullable=False)
    message = db.Column(db.String(500), nullable=False)
    target_url = db.Column(db.String(300), nullable=False, default='/')
    is_read = db.Column(db.Boolean, nullable=False, default=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.now)

    user = db.relationship('User', backref=db.backref('notifications', lazy='dynamic', cascade='all, delete-orphan'))
    task = db.relationship('Task', backref=db.backref('notifications', lazy='dynamic', cascade='all, delete-orphan'))

    @property
    def icon(self):
        return {
            'task_accepted': '🤝',
            'task_submitted': '📜',
            'task_approved': '⭐',
            'task_rejected': '🔧',
            'shop_redeemed': '🛍️',
            'town_stage_up': '🏛️',
            'monthly_ranking_reward': '🏆',
            'promotion_submitted': '📨',
            'promotion_approved': '🛠️',
            'promotion_rejected': '📋',
        }.get(self.event_type, '🔔')
