"""主程序 —— 整个 Web 应用的入口。

运行方式：python app.py
然后浏览器打开 http://127.0.0.1:5000
"""

from calendar import monthrange
from datetime import datetime, date, timedelta
from collections import defaultdict, deque
from hmac import compare_digest
import os
import random
import secrets
import time

from flask import Flask, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import LoginManager, current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from config import Config, SENSENOVA_API_KEY, SENSENOVA_BASE_URL, SENSENOVA_MODEL, validate_production_config
from models import (
    DailyCheckin, MonthlyRankingReward, Notification, PromotionRequest, ShopItem, ShopRedemption,
    TownState, User, Task, TaskStatus, db,
)

# 1. 创建 Flask 应用实例
app = Flask(__name__)
# 2. 用 Config 类里的设置来配置 app
app.config.from_object(Config)
validate_production_config(app.config)
# 3. 把 db 绑定到 app 上
db.init_app(app)

_schema_checked = False


def csrf_token():
    """为当前浏览器会话生成一个不可预测的 CSRF 令牌。"""
    token = session.get('_csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['_csrf_token'] = token
    return token


app.jinja_env.globals['csrf_token'] = csrf_token


@app.before_request
def protect_against_csrf():
    """所有会修改数据的请求都必须携带当前会话的 CSRF 令牌。"""
    if not app.config.get('CSRF_ENABLED', True) or request.method not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return
    expected = session.get('_csrf_token', '')
    supplied = request.form.get('csrf_token', '') or request.headers.get('X-CSRF-Token', '')
    if not expected or not supplied or not compare_digest(expected, supplied):
        if request.path.startswith('/api/') or request.is_json:
            return jsonify({'error': '安全校验已失效，请刷新页面后重试。'}), 400
        abort(400, description='安全校验已失效，请返回上一页刷新后重试。')


@app.before_request
def ensure_runtime_schema():
    """为没有迁移工具的学习项目补齐新增字段，保留已有本地数据。"""
    global _schema_checked
    if _schema_checked:
        return
    db.create_all()
    table_names = inspect(db.engine).get_table_names()
    if 'users' in table_names:
        columns = {column['name'] for column in inspect(db.engine).get_columns('users')}
        if 'account_status' not in columns:
            db.session.execute(text(
                "ALTER TABLE users ADD COLUMN account_status VARCHAR(20) NOT NULL DEFAULT 'approved'"
            ))
            db.session.commit()
    _schema_checked = True


@app.after_request
def add_security_headers(response):
    """为所有页面增加不影响现有视觉和脚本的基础浏览器安全头。"""
    response.headers.setdefault('X-Content-Type-Options', 'nosniff')
    response.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
    response.headers.setdefault('Referrer-Policy', 'same-origin')
    return response


LOGIN_LIMIT_BUCKETS = defaultdict(deque)


def _trim_attempts(bucket_key, window_seconds, now):
    attempts = LOGIN_LIMIT_BUCKETS[bucket_key]
    while attempts and now - attempts[0] > window_seconds:
        attempts.popleft()
    return attempts


def login_limit_exceeded(username):
    """同时限制来源 IP 和目标用户名，降低自动猜密码风险。"""
    now = time.monotonic()
    remote_ip = request.remote_addr or 'unknown'
    ip_attempts = _trim_attempts(('ip', remote_ip), app.config['LOGIN_IP_WINDOW_SECONDS'], now)
    user_attempts = _trim_attempts(('user', username.casefold()), app.config['LOGIN_USER_WINDOW_SECONDS'], now)
    return (
        len(ip_attempts) >= app.config['LOGIN_IP_MAX_ATTEMPTS']
        or len(user_attempts) >= app.config['LOGIN_USER_MAX_ATTEMPTS']
    )


def remember_failed_login(username):
    now = time.monotonic()
    remote_ip = request.remote_addr or 'unknown'
    LOGIN_LIMIT_BUCKETS[('ip', remote_ip)].append(now)
    LOGIN_LIMIT_BUCKETS[('user', username.casefold())].append(now)


def clear_login_attempts(username):
    LOGIN_LIMIT_BUCKETS.pop(('user', username.casefold()), None)

# 4. 初始化登录管理器，负责保存“当前是谁登录了”
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录，再继续操作。'


@login_manager.user_loader
def load_user(user_id):
    """Flask-Login 会用这个函数根据 id 找到用户。"""
    return db.session.get(User, int(user_id))


def notify_user(user_id, task, event_type, title, message, target_url):
    """创建一条站内通知；由调用方与任务状态一起 commit。"""
    if not user_id:
        return
    db.session.add(Notification(
        user_id=user_id,
        task_id=task.id if task else None,
        event_type=event_type,
        title=title,
        message=message,
        target_url=target_url,
    ))


DEFAULT_SHOP_ITEMS = {
    'coffee_ticket': {
        'name': '咖啡券',
        'cost': 100,
        'description': '一张给自己提神的小奖励，适合任务冲刺前兑换。',
        'note': '静态商品第一版：兑换后会真实扣减春芽币并记录流水。',
    },
    'snack_pack': {
        'name': '零食包',
        'cost': 100,
        'description': '一份可以带进工棚的补给包，适合忙完一项任务后犒劳自己。',
        'note': '和咖啡券同档位，适合作为日常小奖励。',
    },
    'half_day_leave': {
        'name': '半天假期',
        'cost': 1000,
        'description': '存够一大笔春芽币后，给自己换半天喘口气的奖励。',
        'note': '当前版本只记录兑换，不联动真实审批流程。',
    },
}

TOWN_STAGE_CONFIG = (
    {'stage': 1, 'min_points': 0, 'max_points': 499, 'label': '废墟初期', 'reward_coins': 0, 'building_tier': 1},
    {'stage': 2, 'min_points': 500, 'max_points': 1099, 'label': '清理修复', 'reward_coins': 100, 'building_tier': 1},
    {'stage': 3, 'min_points': 1100, 'max_points': 1799, 'label': '结构成形', 'reward_coins': 100, 'building_tier': 2},
    {'stage': 4, 'min_points': 1800, 'max_points': 2699, 'label': '细节完善', 'reward_coins': 100, 'building_tier': 2},
    {'stage': 5, 'min_points': 2700, 'max_points': 3799, 'label': '焕新完成', 'reward_coins': 100, 'building_tier': 3},
    {'stage': 6, 'min_points': 3800, 'max_points': None, 'label': '盛典开启', 'reward_coins': 100, 'building_tier': 3},
)

BUILDER_PROMOTION_POINTS = 500

STAGE_BUILDING_MAP = {
    # 一级和三级是根据原素材重新绘制的透明图；二级严格保留原来的高清建筑。
    'mayor': {1: '老镇长小屋-level1-v2.webp', 2: '老镇长小屋.png', 3: '老镇长小屋-level3-v2.webp'},
    'builder': {1: '建造师工坊-level1-v2.webp', 2: '建造师工坊.png', 3: '建造师工坊-level3-v2.webp'},
    'review': {1: '验收大厅.png', 2: '验收大厅.png', 3: '验收大厅-已解锁.png'},
    'memorial': {1: '重建纪念碑-level1-v2.webp', 2: '重建纪念碑.png', 3: '重建纪念碑-level3-v2.webp'},
    'shop': {1: '春芽商店-level1-v2.webp', 2: '春芽商店.png', 3: '春芽商店-level3-v2.webp'},
    'hall': {1: '镇务厅-level1-v2.webp', 2: '镇务厅.png', 3: '镇务厅-level3-v2.webp'},
    'plan': {1: '任务领取点.png', 2: '任务领取点.png', 3: '任务领取点.png'},
    'bonfire': {1: '篝火旁工棚.png', 2: '篝火旁工棚.png', 3: '篝火旁工棚.png'},
    'rank': {1: '篝火排行榜.png', 2: '篝火排行榜.png', 3: '篝火排行榜.png'},
    'square': {1: '春谷广场.png', 2: '春谷广场.png', 3: '春谷广场.png'},
}


def get_town_total_points():
    return db.session.query(db.func.coalesce(db.func.sum(User.rebuild_points), 0)).scalar() or 0


def get_town_stage(total_points):
    for config in TOWN_STAGE_CONFIG:
        if config['max_points'] is None:
            if total_points >= config['min_points']:
                return config
        elif config['min_points'] <= total_points <= config['max_points']:
            return config
    return TOWN_STAGE_CONFIG[0]


def get_building_filename(pos, building_tier):
    return STAGE_BUILDING_MAP.get(pos, {}).get(building_tier, STAGE_BUILDING_MAP.get(pos, {}).get(1, ''))


def get_shop_items():
    """返回适合模板使用的商店商品列表。"""
    ensure_default_shop_items()
    return ShopItem.query.filter_by(is_active=True).order_by(ShopItem.id.asc()).all()


def ensure_default_shop_items():
    """首次运行时把原来的三个静态商品迁入数据库。"""
    if ShopItem.query.count():
        return
    for key, value in DEFAULT_SHOP_ITEMS.items():
        db.session.add(ShopItem(
            item_key=key,
            name=value['name'],
            cost_coins=value['cost'],
            description=value['description'],
            note=value.get('note'),
            is_active=True,
        ))
    db.session.flush()


MONTHLY_RANKING_REWARDS = (300, 200, 100)


def month_bounds(target_date):
    start = datetime(target_date.year, target_date.month, 1)
    if target_date.month == 12:
        end = datetime(target_date.year + 1, 1, 1)
    else:
        end = datetime(target_date.year, target_date.month + 1, 1)
    return start, end


def get_monthly_leaders(target_date=None, limit=10):
    """按当月已验收任务贡献的重建点统计排行榜。"""
    target_date = target_date or date.today()
    start, end = month_bounds(target_date)
    contribution = db.func.coalesce(db.func.sum(Task.reward_points), 0).label('monthly_points')
    rows = (
        db.session.query(User, contribution)
        .outerjoin(
            Task,
            db.and_(
                Task.acceptor_id == User.id,
                Task.status == TaskStatus.COMPLETED,
                Task.completed_at >= start,
                Task.completed_at < end,
            ),
        )
        .filter(User.account_status == 'approved')
        .group_by(User.id)
        .order_by(contribution.desc(), User.id.asc())
        .limit(limit)
        .all()
    )
    return [{'user': user, 'monthly_points': int(points or 0)} for user, points in rows]


def settle_monthly_ranking(today=None):
    """月底按访问触发结算；若月底无人访问，下月首次访问补结算上月。"""
    today = today or date.today()
    last_day = monthrange(today.year, today.month)[1]
    periods = [today] if today.day == last_day else []
    previous_month_end = today.replace(day=1) - timedelta(days=1)
    periods.append(previous_month_end)
    settled = []
    for period_date in periods:
        period_key = period_date.strftime('%Y-%m')
        if MonthlyRankingReward.query.filter_by(period_key=period_key).first():
            continue
        leaders = [entry for entry in get_monthly_leaders(period_date, 3) if entry['monthly_points'] > 0]
        for rank, (entry, reward_coins) in enumerate(zip(leaders, MONTHLY_RANKING_REWARDS), start=1):
            user = entry['user']
            user.sprout_coins += reward_coins
            db.session.add(MonthlyRankingReward(
                period_key=period_key,
                user_id=user.id,
                rank=rank,
                contribution_points=entry['monthly_points'],
                reward_coins=reward_coins,
            ))
            notify_user(
                user.id, None, 'monthly_ranking_reward', '月度贡献榜奖励',
                f'{period_key} 月度贡献榜第 {rank} 名，获得 {reward_coins} 春芽币。',
                '/?panel=ranking',
            )
        settled.append(period_key)
    return settled


def get_or_create_town_state():
    state = db.session.get(TownState, 1)
    if state is None:
        state = TownState(id=1, last_rewarded_stage=1)
        db.session.add(state)
        db.session.flush()
    return state


def reward_all_users_for_stage(stage_config):
    users = User.query.filter_by(account_status='approved').all()
    if not users:
        return 0
    for user in users:
        user.sprout_coins += stage_config['reward_coins']
        notify_user(
            user.id,
            None,
            'town_stage_up',
            f"小镇进入{stage_config['label']}",
            f"全镇进入 {stage_config['label']}，你获得了 {stage_config['reward_coins']} 春芽币。",
            url_for('index', panel='memorial'),
        )
    return len(users)


def sync_town_stage_rewards():
    town_total_points = get_town_total_points()
    town_stage = get_town_stage(town_total_points)
    town_state = get_or_create_town_state()
    if town_stage['stage'] > town_state.last_rewarded_stage:
        for stage_number in range(town_state.last_rewarded_stage + 1, town_stage['stage'] + 1):
            stage_config = next((config for config in TOWN_STAGE_CONFIG if config['stage'] == stage_number), None)
            if stage_config and stage_config['reward_coins'] > 0:
                reward_all_users_for_stage(stage_config)
        town_state.last_rewarded_stage = town_stage['stage']
    return town_total_points, town_stage, town_state


@app.route('/checkin', methods=['POST'])
@login_required
def daily_checkin():
    """每日签到：随机奖励 1-5 春芽币，每天只能签到一次。"""
    today = date.today()
    existing = DailyCheckin.query.filter_by(user_id=current_user.id, checkin_date=today).first()
    if existing:
        flash(f'你今天已经签到过了，已经领取了 {existing.reward_coins} 春芽币。', 'error')
        return redirect(url_for('index', panel='checkin'))

    reward_coins = random.randint(1, 5)
    current_user.sprout_coins += reward_coins
    db.session.add(DailyCheckin(
        user_id=current_user.id,
        reward_coins=reward_coins,
        checkin_date=today,
    ))
    db.session.commit()

    flash(f'签到成功，今天获得了 {reward_coins} 春芽币。', 'success')
    return redirect(url_for('index', panel='checkin'))


@app.route('/shop/redeem', methods=['POST'])
@login_required
def redeem_shop_item():
    """春芽商店兑换：扣减春芽币并记录兑换流水。"""
    ensure_default_shop_items()
    item_key = request.form.get('item_key', '').strip()
    item = ShopItem.query.filter_by(item_key=item_key, is_active=True).first()
    if not item:
        flash('这个商品不存在或已经下架。', 'error')
        return redirect(url_for('index', panel='shop'))

    if current_user.sprout_coins < item.cost_coins:
        flash('春芽币不够，先去完成几项任务再来兑换吧。', 'error')
        return redirect(url_for('index', panel='shop'))

    current_user.sprout_coins -= item.cost_coins
    db.session.add(ShopRedemption(
        user_id=current_user.id,
        item_key=item_key,
        item_name=item.name,
        cost_coins=item.cost_coins,
        note=item.note,
        granted_star_shards=0,
        granted_town_trust=0,
    ))
    db.session.commit()

    flash(f"兑换成功，已扣除 {item.cost_coins} 春芽币。", 'success')
    return redirect(url_for('index', panel='shop'))


def _shop_item_form_values():
    """统一读取并校验镇长填写的商品资料。"""
    name = request.form.get('name', '').strip()
    description = request.form.get('description', '').strip()
    note = request.form.get('note', '').strip()
    try:
        cost_coins = int(request.form.get('cost_coins', ''))
    except (TypeError, ValueError):
        cost_coins = -1
    if not (1 <= len(name) <= 120):
        return None, '商品名称需要填写 1—120 个字。'
    if not (1 <= len(description) <= 500):
        return None, '商品说明需要填写 1—500 个字。'
    if len(note) > 300:
        return None, '补充说明最多 300 个字。'
    if not (1 <= cost_coins <= 100000):
        return None, '商品金额需要是 1—100000 之间的整数。'
    return {
        'name': name,
        'description': description,
        'note': note or None,
        'cost_coins': cost_coins,
        'is_active': request.form.get('is_active') == 'on',
    }, None


@app.route('/shop/items/create', methods=['POST'])
@login_required
def create_shop_item():
    """镇长新增商品；普通用户即使伪造请求也会被拒绝。"""
    if current_user.role != 'admin':
        abort(403)
    values, error = _shop_item_form_values()
    if error:
        flash(error, 'error')
        return redirect(url_for('index', panel='shop'))
    db.session.add(ShopItem(
        item_key=f'custom_{secrets.token_hex(6)}',
        **values,
    ))
    db.session.commit()
    flash(f"商品「{values['name']}」已创建。", 'success')
    return redirect(url_for('index', panel='shop'))


@app.route('/shop/items/<int:item_id>/update', methods=['POST'])
@login_required
def update_shop_item(item_id):
    """镇长编辑商品名称、说明、金额和上下架状态。"""
    if current_user.role != 'admin':
        abort(403)
    item = ShopItem.query.get_or_404(item_id)
    values, error = _shop_item_form_values()
    if error:
        flash(error, 'error')
        return redirect(url_for('index', panel='shop'))
    for field, value in values.items():
        setattr(item, field, value)
    db.session.commit()
    state = '已上架' if item.is_active else '已下架'
    flash(f'商品「{item.name}」已保存，当前{state}。', 'success')
    return redirect(url_for('index', panel='shop'))


# ========== 路由（URL → 页面的映射）==========

@app.route('/')
def index():
    """首页：春谷镇镇中心广场。"""
    town_total_points, town_stage, town_state = sync_town_stage_rewards()
    settle_monthly_ranking()
    # 只改变本次页面展示，不写数据库，方便检查三档建筑的视觉效果。
    preview_tier = request.args.get('preview_tier', type=int)
    if preview_tier not in (1, 2, 3):
        preview_tier = None
    active_building_tier = preview_tier or town_stage['building_tier']
    preview_upgrade = request.args.get('preview_upgrade') == '1'
    next_stage = next((config for config in TOWN_STAGE_CONFIG if config['stage'] == town_stage['stage'] + 1), None)
    next_stage_points = next_stage['min_points'] if next_stage else None
    points_to_next_stage = max(0, next_stage_points - town_total_points) if next_stage_points is not None else 0

    if db.session.new or db.session.dirty:
        db.session.commit()

    # 只展示「公开招募」的任务，草稿和已关闭的不出现在计划板上
    tasks = Task.query.filter_by(status=TaskStatus.OPEN).order_by(Task.created_at.desc()).all()
    # 排行榜按本月已经验收通过的任务贡献重建点统计，每月重新开始。
    monthly_leaders = get_monthly_leaders(limit=10)
    leaders = [entry['user'] for entry in monthly_leaders]
    ranking_period = date.today().strftime('%Y年%m月')
    ranking_last_day = monthrange(date.today().year, date.today().month)[1]

    # 这两个数字用于地图两侧的信息面板。没有登录时使用 0 和空列表。
    accepted_tasks = []
    published_tasks = []
    notifications = []
    unread_notifications = 0
    stage_upgrade_notification = None
    today_checkin = None
    recent_checkins = []
    town_total_tasks = Task.query.count()
    town_completed_tasks = Task.query.filter_by(status=TaskStatus.COMPLETED).count()
    town_open_tasks = 0
    town_processing_tasks = 0
    town_submitted_tasks = 0
    town_progress_percent = round((town_completed_tasks / town_total_tasks) * 100) if town_total_tasks else 0
    if current_user.is_authenticated:
        accepted_tasks = Task.query.filter_by(acceptor_id=current_user.id).order_by(Task.created_at.desc()).all()
        published_tasks = Task.query.filter_by(publisher_id=current_user.id).order_by(Task.created_at.desc()).all()
        notifications = Notification.query.filter_by(user_id=current_user.id).order_by(Notification.created_at.desc()).limit(30).all()
        unread_notifications = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
        stage_upgrade_notification = Notification.query.filter_by(
            user_id=current_user.id,
            event_type='town_stage_up',
            is_read=False,
        ).order_by(Notification.created_at.desc()).first()
        today_checkin = DailyCheckin.query.filter_by(user_id=current_user.id, checkin_date=date.today()).first()
        recent_checkins = DailyCheckin.query.filter_by(user_id=current_user.id).order_by(DailyCheckin.created_at.desc()).limit(5).all()
    town_open_tasks = len(tasks)
    town_processing_tasks = Task.query.filter_by(status=TaskStatus.PROCESSING).count()
    town_submitted_tasks = Task.query.filter_by(status=TaskStatus.SUBMITTED).count()

    today_actions = []
    if current_user.is_authenticated:
        if today_checkin:
            today_actions.append({
                'title': '今日签到',
                'detail': f'今天已领取 {today_checkin.reward_coins} 春芽币。',
                'panel': 'checkin',
                'done': True,
            })
        else:
            today_actions.append({
                'title': '今日签到',
                'detail': '可随机领取 1-5 春芽币。',
                'panel': 'checkin',
                'done': False,
            })
        if accepted_tasks:
            today_actions.append({
                'title': '处理任务',
                'detail': f'你当前有 {len(accepted_tasks)} 项任务在手。',
                'panel': 'board',
                'done': False,
            })
        else:
            today_actions.append({
                'title': '领取任务',
                'detail': f'现在有 {town_open_tasks} 项公开计划可接取。',
                'panel': 'board',
                'done': False,
            })
        today_actions.append({
            'title': '逛春芽商店',
            'detail': '看看自己能兑换什么静态奖励。',
            'panel': 'shop',
            'done': False,
        })
    else:
        today_actions = [
            {'title': '先登录', 'detail': '登录后才能签到、接任务和逛商店。', 'panel': None, 'done': False},
            {'title': '看看地图', 'detail': '先熟悉春谷镇的建筑和功能分区。', 'panel': 'welcome', 'done': False},
        ]

    building_images = {
        pos: get_building_filename(pos, active_building_tier)
        for pos in STAGE_BUILDING_MAP
    }
    # 首页建筑同时表达权限状态：有权限的角色看到敞开、无锁的入口。
    # 镇务厅是全体镇民的公共入口；管理按钮再按角色细分权限。
    if current_user.is_authenticated:
        building_images['hall'] = '镇务厅-已解锁.png'
    if current_user.is_authenticated and current_user.role in {'builder', 'senior_builder', 'admin'}:
        building_images['builder'] = '建造师工坊-已解锁.png'
        building_images['review'] = '验收大厅-已解锁.png'

    current_promotion = None
    if current_user.is_authenticated and current_user.role == 'scavenger':
        current_promotion = PromotionRequest.query.filter_by(
            applicant_id=current_user.id,
            target_role='builder',
        ).order_by(PromotionRequest.created_at.desc()).first()

    return render_template(
        'index.html',
        tasks=tasks,
        leaders=leaders,
        accepted_tasks=accepted_tasks,
        published_tasks=published_tasks,
        notifications=notifications,
        unread_notifications=unread_notifications,
        shop_items=get_shop_items(),
        shop_admin_items=ShopItem.query.order_by(ShopItem.id.asc()).all() if current_user.is_authenticated and current_user.role == 'admin' else [],
        monthly_leaders=monthly_leaders,
        ranking_period=ranking_period,
        ranking_last_day=ranking_last_day,
        recent_redemptions=ShopRedemption.query.filter_by(user_id=current_user.id).order_by(ShopRedemption.created_at.desc()).limit(5).all() if current_user.is_authenticated else [],
        today_checkin=today_checkin,
        recent_checkins=recent_checkins,
        today_actions=today_actions,
        town_total_tasks=town_total_tasks,
        town_completed_tasks=town_completed_tasks,
        town_open_tasks=town_open_tasks,
        town_processing_tasks=town_processing_tasks,
        town_submitted_tasks=town_submitted_tasks,
        town_progress_percent=town_progress_percent,
        town_total_points=town_total_points,
        town_stage=town_stage,
        next_stage=next_stage,
        points_to_next_stage=points_to_next_stage,
        preview_tier=preview_tier,
        preview_upgrade=preview_upgrade,
        stage_upgrade_notification=stage_upgrade_notification,
        active_building_tier=active_building_tier,
        building_images=building_images,
        builder_promotion_points=BUILDER_PROMOTION_POINTS,
        current_promotion=current_promotion,
    )


@app.route('/register', methods=['GET', 'POST'])
def register():
    """注册页面：创建一个新用户。"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        display_name = request.form.get('display_name', '').strip()
        password = request.form.get('password', '')

        if not username or not display_name or not password:
            flash('请把用户名、显示名和密码都填完整。', 'error')
            return render_template('auth/register.html')
        if not (3 <= len(username) <= 40):
            flash('用户名需要填写 3—40 个字符。', 'error')
            return render_template('auth/register.html')
        if len(display_name) > 40:
            flash('显示名最多填写 40 个字符。', 'error')
            return render_template('auth/register.html')
        if not (8 <= len(password) <= 128):
            flash('密码需要填写 8—128 个字符。', 'error')
            return render_template('auth/register.html')

        existing_user = User.query.filter_by(username=username).first()
        if existing_user:
            flash('这个用户名已经被占用了。', 'error')
            return render_template('auth/register.html')

        user = User(
            username=username,
            display_name=display_name,
            password_hash=generate_password_hash(password),
            role='scavenger',
            account_status='pending',
        )
        db.session.add(user)
        db.session.commit()

        flash('登记成功，请等待资深建造师或镇长批准后再登录。', 'success')
        return redirect(url_for('login'))

    return render_template('auth/register.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    """登录页面：验证用户名和密码。"""
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        if login_limit_exceeded(username):
            flash('登录尝试次数过多，请稍后再试。', 'error')
            return render_template('auth/login.html'), 429

        user = User.query.filter_by(username=username).first()
        if user is None or not check_password_hash(user.password_hash, password):
            remember_failed_login(username)
            flash('用户名或密码错误。', 'error')
            return render_template('auth/login.html')

        if user.account_status != 'approved':
            message = '账号正在等待批准，请稍后再试。' if user.account_status == 'pending' else '账号登记未通过，请联系资深建造师或镇长。'
            flash(message, 'error')
            return render_template('auth/login.html')

        clear_login_attempts(username)
        session.clear()
        login_user(user)
        csrf_token()
        flash('登录成功，欢迎回来。', 'success')
        return redirect(url_for('index'))

    return render_template('auth/login.html')


@app.route('/logout')
@login_required
def logout():
    """退出登录。"""
    logout_user()
    flash('你已经退出登录。', 'success')
    return redirect(url_for('login'))


@app.route('/tasks/create', methods=['GET', 'POST'])
@login_required
def create_task():
    """发布春谷计划：只有建造师、资深建造师和镇长可以发布。"""
    if not current_user.can_publish_task():
        return render_template('tasks/create.html', locked=True), 403

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        difficulty = request.form.get('difficulty', 'C').strip()
        reward_points = request.form.get('reward_points', '10').strip()
        reward_coins = request.form.get('reward_coins', '20').strip()

        if not title or not description:
            flash('请把任务标题和完成要求填写完整。', 'error')
            return render_template('tasks/create.html', locked=False)
        if len(title) > 100 or len(description) > 2000:
            flash('任务标题最多 100 字，完成要求最多 2000 字。', 'error')
            return render_template('tasks/create.html', locked=False)
        if difficulty not in {'S', 'A', 'B', 'C'}:
            flash('请选择有效的任务难度。', 'error')
            return render_template('tasks/create.html', locked=False)

        try:
            reward_points = int(reward_points)
            reward_coins = int(reward_coins)
        except ValueError:
            flash('奖励必须填写数字。', 'error')
            return render_template('tasks/create.html', locked=False)

        if not (0 <= reward_points <= 1000) or not (0 <= reward_coins <= 5000):
            flash('重建点奖励须在 0—1000 之间，春芽币须在 0—5000 之间。', 'error')
            return render_template('tasks/create.html', locked=False)

        task = Task(
            title=title,
            description=description,
            # 分类字段为兼容已有数据库而保留，前台不再要求用户选择。
            category='general',
            difficulty=difficulty,
            reward_points=reward_points,
            reward_coins=reward_coins,
            publisher_id=current_user.id,
            status=TaskStatus.OPEN,
        )
        db.session.add(task)
        db.session.commit()

        flash('任务发布成功，已经送到任务领取点。', 'success')
        return redirect(url_for('builder_workshop'))

    return render_template('tasks/create.html', locked=False)


# ========== 任务状态机路由 ==========

@app.route('/tasks/<int:task_id>/accept', methods=['POST'])
@login_required
def accept_task(task_id):
    """接取任务：open -> accepted。"""
    task = Task.query.get_or_404(task_id)

    if task.status != TaskStatus.OPEN:
        flash('这个任务当前不能接取。', 'error')
        return redirect(url_for('index'))

    if task.acceptor_id is not None:
        flash('这个任务已经被别人接走了。', 'error')
        return redirect(url_for('index'))

    if task.publisher_id == current_user.id:
        flash('不能领取自己发布的任务，请等待其他镇民领取。', 'error')
        return redirect(url_for('index'))

    task.acceptor_id = current_user.id
    task.status = TaskStatus.ACCEPTED
    task.accepted_at = datetime.now()
    notify_user(
        task.publisher_id, task, 'task_accepted', '任务已被领取',
        f'{current_user.display_name} 领取了「{task.title}」。',
        url_for('builder_workshop'),
    )
    db.session.commit()

    flash('接取成功，去工棚开工吧。', 'success')
    return redirect(url_for('my_tasks'))


@app.route('/tasks/<int:task_id>/start', methods=['POST'])
@login_required
def start_task(task_id):
    """开工：accepted -> processing。"""
    task = Task.query.get_or_404(task_id)

    if task.acceptor_id != current_user.id:
        flash('只有接取人才能开工。', 'error')
        return redirect(url_for('my_tasks'))

    if not task.can_transition_to(TaskStatus.PROCESSING):
        flash('这个任务当前不能开工。', 'error')
        return redirect(url_for('my_tasks'))

    task.status = TaskStatus.PROCESSING
    db.session.commit()

    flash('已开工，记得随时更新进度。', 'success')
    return redirect(url_for('my_tasks'))


@app.route('/tasks/<int:task_id>/progress', methods=['POST'])
@login_required
def update_progress(task_id):
    """更新进度：状态不变，只刷新 progress_note。"""
    task = Task.query.get_or_404(task_id)

    if task.acceptor_id != current_user.id:
        flash('只有接取人才能更新进度。', 'error')
        return redirect(url_for('my_tasks'))

    if task.status not in (TaskStatus.PROCESSING, TaskStatus.REJECTED):
        flash('当前状态下不能更新进度。', 'error')
        return redirect(url_for('my_tasks'))

    note = request.form.get('progress_note', '').strip()
    if not note or len(note) > 1000:
        flash('请填写 1—1000 字的进度说明。', 'error')
        return redirect(url_for('my_tasks'))
    task.progress_note = note
    # 如果之前是被打回的，现在重新开工
    if task.status == TaskStatus.REJECTED:
        task.status = TaskStatus.PROCESSING
    db.session.commit()

    flash('进度已更新。', 'success')
    return redirect(url_for('my_tasks'))


@app.route('/tasks/<int:task_id>/submit', methods=['POST'])
@login_required
def submit_task(task_id):
    """提交审核：processing -> submitted。"""
    task = Task.query.get_or_404(task_id)

    if task.acceptor_id != current_user.id:
        flash('只有接取人才能提交审核。', 'error')
        return redirect(url_for('my_tasks'))

    if not task.can_transition_to(TaskStatus.SUBMITTED):
        flash('这个任务当前不能提交审核。', 'error')
        return redirect(url_for('my_tasks'))

    task.status = TaskStatus.SUBMITTED
    task.submitted_at = datetime.now()
    notify_user(
        task.publisher_id, task, 'task_submitted', '有任务等待验收',
        f'{current_user.display_name} 已提交「{task.title}」，请前往验收大厅。',
        url_for('review_hall'),
    )
    db.session.commit()

    flash('已提交审核，等发布人验收。', 'success')
    return redirect(url_for('my_tasks'))


@app.route('/tasks/<int:task_id>/approve', methods=['POST'])
@login_required
def approve_task(task_id):
    """验收通过：submitted -> completed，结算奖励。"""
    task = Task.query.get_or_404(task_id)

    if task.publisher_id != current_user.id and current_user.role != 'admin':
        flash('只有发布人才能验收。', 'error')
        return redirect(url_for('review_hall'))

    if not task.can_transition_to(TaskStatus.COMPLETED):
        flash('这个任务当前不能验收。', 'error')
        return redirect(url_for('review_hall'))

    task.status = TaskStatus.COMPLETED
    task.reviewed_at = datetime.now()
    task.completed_at = datetime.now()

    # 结算奖励给接取人
    acceptor = task.acceptor
    if acceptor:
        acceptor.rebuild_points += task.reward_points
        acceptor.sprout_coins += task.reward_coins
        notify_user(
            acceptor.id, task, 'task_approved', '验收通过',
            f'「{task.title}」已通过验收，获得 {task.reward_points} 重建点和 {task.reward_coins} 春芽币。',
            url_for('my_tasks'),
        )

    sync_town_stage_rewards()
    db.session.commit()

    flash('验收通过，奖励已发放。', 'success')
    return redirect(url_for('review_hall'))


@app.route('/tasks/<int:task_id>/reject', methods=['POST'])
@login_required
def reject_task(task_id):
    """打回：submitted -> rejected。"""
    task = Task.query.get_or_404(task_id)

    if task.publisher_id != current_user.id and current_user.role != 'admin':
        flash('只有发布人才能打回。', 'error')
        return redirect(url_for('review_hall'))

    if not task.can_transition_to(TaskStatus.REJECTED):
        flash('这个任务当前不能打回。', 'error')
        return redirect(url_for('review_hall'))

    reason = request.form.get('reject_reason', '').strip()
    if len(reason) > 1000:
        flash('打回说明最多填写 1000 字。', 'error')
        return redirect(url_for('review_hall'))
    task.status = TaskStatus.REJECTED
    task.reject_reason = reason
    task.reviewed_at = datetime.now()
    if task.acceptor_id:
        reason_text = reason or '请根据任务要求进一步修改。'
        notify_user(
            task.acceptor_id, task, 'task_rejected', '任务需要修改',
            f'「{task.title}」被打回：{reason_text}',
            url_for('my_tasks'),
        )
    db.session.commit()

    flash('已打回，接取人可以修改后重新提交。', 'success')
    return redirect(url_for('review_hall'))


@app.route('/tasks/<int:task_id>/cancel', methods=['POST'])
@login_required
def cancel_task(task_id):
    """撤销/关闭：发布人在 open 阶段可撤销；completed 可归档关闭。"""
    task = Task.query.get_or_404(task_id)

    if task.publisher_id != current_user.id and current_user.role != 'admin':
        flash('只有发布人才能关闭任务。', 'error')
        return redirect(url_for('index'))

    if not task.can_transition_to(TaskStatus.CLOSED):
        flash('这个任务当前不能关闭。', 'error')
        return redirect(url_for('builder_workshop'))

    task.status = TaskStatus.CLOSED
    db.session.commit()

    flash('任务已关闭。', 'success')
    return redirect(url_for('builder_workshop'))


@app.route('/my-tasks')
@login_required
def my_tasks():
    """我的工棚：只负责当前用户接取和执行的任务。"""
    accepted = Task.query.filter_by(acceptor_id=current_user.id).order_by(Task.created_at.desc()).all()
    return render_template('my_tasks.html', accepted=accepted)


@app.route('/builder-workshop')
@login_required
def builder_workshop():
    """建造师工坊：发布任务，并管理自己创建的全部任务。"""
    if not current_user.can_publish_task():
        return render_template('builder_workshop.html', locked=True, published=[]), 403
    published = Task.query.filter_by(publisher_id=current_user.id).order_by(Task.created_at.desc()).all()
    return render_template('builder_workshop.html', locked=False, published=published)


@app.route('/review-hall')
@login_required
def review_hall():
    """验收大厅：建造师处理自己发布任务的审核；镇长可以处理全镇审核。"""
    if not current_user.can_publish_task():
        return render_template('review_hall.html', locked=True, review_tasks=[]), 403
    query = Task.query.filter_by(status=TaskStatus.SUBMITTED)
    if current_user.role != 'admin':
        query = query.filter_by(publisher_id=current_user.id)
    review_tasks = query.order_by(Task.submitted_at.desc()).all()
    return render_template('review_hall.html', locked=False, review_tasks=review_tasks)


@app.route('/town-hall')
@login_required
def town_hall():
    """镇务厅：所有镇民可查看；资深建造师和镇长可审批账号。"""
    users = User.query.filter(User.account_status != 'deleted').order_by(User.created_at.asc()).all()
    tasks = Task.query.order_by(Task.created_at.desc()).all()
    promotion_requests = PromotionRequest.query.order_by(
        PromotionRequest.status.asc(), PromotionRequest.created_at.desc()
    ).all() if current_user.role == 'admin' else []
    return render_template(
        'town_hall.html',
        users=users,
        tasks=tasks,
        can_approve_accounts=current_user.role in {'senior_builder', 'admin'},
        can_promote=current_user.role == 'admin',
        promotion_requests=promotion_requests,
        pending_promotion_count=sum(1 for item in promotion_requests if item.status == 'pending'),
        builder_promotion_points=BUILDER_PROMOTION_POINTS,
    )


@app.route('/promotions/builder/apply', methods=['POST'])
@login_required
def apply_builder_promotion():
    """拾荒者达到 500 重建点后，主动申请晋升建造师。"""
    if current_user.account_status != 'approved':
        flash('只有已经获准进入小镇的居民才能申请晋升。', 'error')
        return redirect(url_for('index'))
    if current_user.role != 'scavenger':
        flash('只有拾荒者需要申请晋升建造师。', 'error')
        return redirect(url_for('index'))
    if current_user.rebuild_points < BUILDER_PROMOTION_POINTS:
        remaining = BUILDER_PROMOTION_POINTS - current_user.rebuild_points
        flash(f'还差 {remaining} 重建点才能申请晋升建造师。', 'error')
        return redirect(url_for('index'))

    existing = PromotionRequest.query.filter_by(
        applicant_id=current_user.id,
        target_role='builder',
        status='pending',
    ).first()
    if existing:
        flash('你的建造师晋升申请已经在等待镇长审批，请勿重复提交。', 'info')
        return redirect(url_for('index'))

    application_note = request.form.get('application_note', '').strip()
    if len(application_note) > 300:
        flash('申请说明最多填写 300 个字。', 'error')
        return redirect(url_for('index'))

    promotion = PromotionRequest(
        applicant_id=current_user.id,
        target_role='builder',
        status='pending',
        active_key=f'builder:{current_user.id}',
        points_at_apply=current_user.rebuild_points,
        application_note=application_note or None,
    )
    db.session.add(promotion)
    mayor = User.query.filter_by(role='admin', account_status='approved').first()
    if mayor:
        notify_user(
            mayor.id, None, 'promotion_submitted', '新的建造师晋升申请',
            f'{current_user.display_name} 已达到 {current_user.rebuild_points} 重建点，等待你审批。',
            url_for('town_hall'),
        )
    try:
        db.session.commit()
    except IntegrityError:
        # active_key 的唯一约束是最后一道并发保护：两个同时到达的请求只留一个。
        db.session.rollback()
        flash('你的申请已经提交成功，正在等待镇长审批。', 'info')
        return redirect(url_for('index'))

    flash('晋升申请已提交。达到 500 重建点不会自动升级，请等待镇长审批。', 'success')
    return redirect(url_for('index'))


@app.route('/promotions/<int:promotion_id>/approve', methods=['POST'])
@login_required
def approve_builder_promotion(promotion_id):
    """镇长批准一条仍然有效的建造师晋升申请。"""
    if current_user.role != 'admin':
        abort(403)
    promotion = PromotionRequest.query.get_or_404(promotion_id)
    if promotion.status != 'pending':
        flash(f'这条申请已经处理过，当前结果为“{promotion.status_label}”。', 'info')
        return redirect(url_for('town_hall'))

    applicant = promotion.applicant
    if applicant.account_status != 'approved' or applicant.role != 'scavenger':
        flash('申请人的账号或角色已经变化，本次审批未执行。', 'error')
        return redirect(url_for('town_hall'))
    if applicant.rebuild_points < BUILDER_PROMOTION_POINTS:
        flash(f'申请人当前不足 {BUILDER_PROMOTION_POINTS} 重建点，本次审批未执行。', 'error')
        return redirect(url_for('town_hall'))

    applicant.role = 'builder'
    promotion.status = 'approved'
    promotion.active_key = None
    promotion.reviewer_id = current_user.id
    promotion.points_at_review = applicant.rebuild_points
    promotion.review_note = request.form.get('review_note', '').strip()[:300] or '符合晋升条件，批准晋升建造师。'
    promotion.reviewed_at = datetime.now()
    notify_user(
        applicant.id, None, 'promotion_approved', '晋升建造师已获批准',
        '镇长已批准你的申请。你现在可以发布任务，并验收自己发布的任务。',
        url_for('builder_workshop'),
    )
    db.session.commit()
    flash(f'已批准 {applicant.display_name} 晋升建造师。', 'success')
    return redirect(url_for('town_hall'))


@app.route('/promotions/<int:promotion_id>/reject', methods=['POST'])
@login_required
def reject_builder_promotion(promotion_id):
    """镇长写明理由后拒绝申请；申请人之后可以重新申请。"""
    if current_user.role != 'admin':
        abort(403)
    promotion = PromotionRequest.query.get_or_404(promotion_id)
    if promotion.status != 'pending':
        flash(f'这条申请已经处理过，当前结果为“{promotion.status_label}”。', 'info')
        return redirect(url_for('town_hall'))
    review_note = request.form.get('review_note', '').strip()
    if not review_note:
        flash('拒绝晋升时必须填写原因，方便申请人后续改进。', 'error')
        return redirect(url_for('town_hall'))
    if len(review_note) > 300:
        flash('审批说明最多填写 300 个字。', 'error')
        return redirect(url_for('town_hall'))

    promotion.status = 'rejected'
    promotion.active_key = None
    promotion.reviewer_id = current_user.id
    promotion.points_at_review = promotion.applicant.rebuild_points
    promotion.review_note = review_note
    promotion.reviewed_at = datetime.now()
    notify_user(
        promotion.applicant_id, None, 'promotion_rejected', '建造师晋升申请未通过',
        f'镇长反馈：{review_note}', url_for('index'),
    )
    db.session.commit()
    flash(f'已退回 {promotion.applicant.display_name} 的晋升申请。', 'success')
    return redirect(url_for('town_hall'))


@app.route('/town-hall/users/<int:user_id>')
@login_required
def town_hall_user(user_id):
    """镇民档案：所有已进入小镇的用户都可以只读查看。"""
    user = User.query.filter(User.id == user_id, User.account_status != 'deleted').first_or_404()
    published = Task.query.filter_by(publisher_id=user.id).order_by(Task.created_at.desc()).all()
    accepted = Task.query.filter_by(acceptor_id=user.id).order_by(Task.created_at.desc()).all()
    return render_template('town_hall_user.html', user=user, published=published, accepted=accepted)


@app.route('/town-hall/users/<int:user_id>/approve', methods=['POST'])
@login_required
def approve_user_account(user_id):
    """资深建造师或镇长批准新镇民进入小镇。"""
    if current_user.role not in {'senior_builder', 'admin'}:
        abort(403)
    user = User.query.get_or_404(user_id)
    user.account_status = 'approved'
    db.session.commit()
    flash(f'已批准 {user.display_name} 进入春谷镇。', 'success')
    return redirect(url_for('town_hall'))


@app.route('/town-hall/users/<int:user_id>/reject', methods=['POST'])
@login_required
def reject_user_account(user_id):
    """资深建造师或镇长拒绝尚未通过的登记申请。"""
    if current_user.role not in {'senior_builder', 'admin'}:
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('不能拒绝自己的账号。', 'error')
        return redirect(url_for('town_hall'))
    user.account_status = 'rejected'
    db.session.commit()
    flash(f'已拒绝 {user.display_name} 的登记申请。', 'success')
    return redirect(url_for('town_hall'))


@app.route('/town-hall/users/<int:user_id>/role', methods=['POST'])
@login_required
def update_user_role(user_id):
    """镇长调整用户权限角色；成长等级仍由重建点独立计算。"""
    if current_user.role != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    new_role = request.form.get('role', '').strip()
    allowed_roles = {'scavenger', 'builder', 'senior_builder', 'admin'}
    if new_role not in allowed_roles:
        flash('无效的权限角色。', 'error')
        return redirect(url_for('town_hall'))
    if user.role == 'scavenger' and new_role in {'builder', 'senior_builder'}:
        flash('拾荒者晋升必须先达到 500 重建点并提交申请，再由镇长在晋升申请区审批。', 'error')
        return redirect(url_for('town_hall'))
    if user.id == current_user.id and new_role != 'admin':
        flash('镇长不能撤销自己的最高权限。', 'error')
        return redirect(url_for('town_hall'))
    if new_role == 'admin' and user.role != 'admin':
        existing_mayor = User.query.filter_by(role='admin').first()
        if existing_mayor:
            flash(f'春谷镇只能有一位镇长，目前的镇长是 {existing_mayor.display_name}。', 'error')
            return redirect(url_for('town_hall'))
    user.role = new_role
    db.session.commit()
    flash(f'已将 {user.display_name} 的权限调整为 {user.role_label}。', 'success')
    return redirect(url_for('town_hall'))


@app.route('/town-hall/users/<int:user_id>/delete', methods=['POST'])
@login_required
def delete_user_account(user_id):
    """镇长停用并匿名化用户，保留历史任务和月榜数据的完整性。"""
    if current_user.role != 'admin':
        abort(403)
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('镇长不能删除自己的账号。', 'error')
        return redirect(url_for('town_hall'))
    if user.role == 'admin':
        flash('春谷镇唯一的镇长账号不能被删除。', 'error')
        return redirect(url_for('town_hall'))
    if user.account_status == 'deleted':
        flash('这个账号已经被删除。', 'error')
        return redirect(url_for('town_hall'))

    # 不进行数据库硬删除，否则该用户参与过的任务会失去发布人或接取人。
    user.username = f'deleted_{user.id}_{secrets.token_hex(5)}'
    user.display_name = f'已删除镇民 #{user.id}'
    user.password_hash = generate_password_hash(secrets.token_urlsafe(32))
    user.account_status = 'deleted'
    user.role = 'scavenger'
    db.session.commit()
    flash('该账号已停用并匿名化，历史任务记录仍被保留。', 'success')
    return redirect(url_for('town_hall'))


@app.route('/town-hall/reset-demo', methods=['POST'])
@login_required
def reset_demo_data():
    """镇长恢复固定演示账号和演示任务，不触碰普通用户数据。"""
    if not app.config.get('DEMO_MODE') or current_user.role != 'admin':
        abort(403)
    if request.form.get('confirmation') != 'RESET_DEMO':
        flash('恢复演示数据需要再次确认。', 'error')
        return redirect(url_for('town_hall'))

    demo_accounts = {
        'scavenger_demo': ('新芽', 'scavenger', 80, 40, 20),
        'builder_demo': ('林小满', 'builder', 420, 120, 86),
        'senior_builder_demo': ('青岚', 'senior_builder', 650, 260, 92),
        'mayor_demo': ('春谷镇长', 'admin', 1200, 500, 100),
    }
    demo_users = {}
    for username, (display_name, role, points, coins, trust) in demo_accounts.items():
        user = User.query.filter_by(username=username).first()
        if user is None:
            user = User(username=username, display_name=display_name, role=role)
            db.session.add(user)
        user.display_name = display_name
        user.role = role
        user.account_status = 'approved'
        user.password_hash = generate_password_hash(app.config['DEMO_PASSWORD'])
        user.rebuild_points = points
        user.sprout_coins = coins
        user.town_trust = trust
        user.star_shards = 0
        db.session.flush()
        demo_users[username] = user

    demo_user_ids = [user.id for user in demo_users.values()]
    DailyCheckin.query.filter(DailyCheckin.user_id.in_(demo_user_ids)).delete(synchronize_session=False)
    ShopRedemption.query.filter(ShopRedemption.user_id.in_(demo_user_ids)).delete(synchronize_session=False)
    Notification.query.filter(Notification.user_id.in_(demo_user_ids)).delete(synchronize_session=False)

    for task in Task.query.filter(Task.publisher_id.in_(demo_user_ids)).all():
        db.session.delete(task)
    db.session.flush()

    builder = demo_users['builder_demo']
    examples = (
        ('整理 Q3 储能行业跟踪框架', '建立行业分类、数据源和重点公司清单，形成可持续更新的研究框架。', 'drawing', 'A', 30, 60),
        ('更新半导体材料价格数据库', '录入本周硅片、电子特气和靶材价格，并核对数据口径。', 'calc', 'B', 20, 40),
        ('访谈三位新入镇拾荒者', '了解新用户接取第一项任务时的困惑，并整理改进建议。', 'scout', 'B', 20, 40),
    )
    for title, description, category, difficulty, points, coins in examples:
        db.session.add(Task(
            title=title, description=description, category=category,
            difficulty=difficulty, reward_points=points, reward_coins=coins,
            publisher_id=builder.id, status=TaskStatus.OPEN,
        ))

    db.session.commit()
    flash('演示数据已恢复：四个演示账号和三项待领取任务已经准备好。', 'success')
    return redirect(url_for('town_hall'))


# ========== 通知中心 ==========

@app.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def read_notification(notification_id):
    """将一条通知标记为已读，并跳转到对应任务页。"""
    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id,
    ).first_or_404()
    notification.is_read = True
    db.session.commit()
    target_url = notification.target_url or url_for('index')
    # 通知链接只允许跳到本站页面，避免数据库中的异常地址把用户带出网站。
    if not target_url.startswith('/') or target_url.startswith('//'):
        target_url = url_for('index')
    return redirect(target_url)


@app.route('/notifications/read-all', methods=['POST'])
@login_required
def read_all_notifications():
    """将当前用户的全部通知标记为已读。"""
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update(
        {'is_read': True},
        synchronize_session=False,
    )
    db.session.commit()
    flash('通知已全部标记为已读。', 'success')
    return redirect(url_for('index', panel='notifications'))


@app.route('/notifications/<int:notification_id>/seen', methods=['POST'])
@login_required
def see_notification(notification_id):
    """升级动画出现后将通知记为已读，但保留在通知中心供以后查看。"""
    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id,
    ).first_or_404()
    if notification.event_type == 'town_stage_up':
        # 只展示最新阶段的动画，旧阶段通知仍保留在通知中心但不再逐个弹窗。
        Notification.query.filter_by(
            user_id=current_user.id,
            event_type='town_stage_up',
            is_read=False,
        ).update({'is_read': True}, synchronize_session=False)
    else:
        notification.is_read = True
    db.session.commit()
    return jsonify({'ok': True})


# ========== AI 助手「老镇长」==========

MAYOR_SYSTEM_PROMPT = """你是春谷镇的老镇长，一位温和、睿智、有耐心的长者。

背景设定：
- 春谷镇是一场灾变后幸存者重建的山谷小镇
- 镇民只有四种正式身份：拾荒者（Lv.1）→ 建造师（Lv.2）→ 资深建造师（Lv.3）→ 镇长（Lv.4）
- 镇长只有一位并拥有最高权限；“老镇长”只是你的亲切称呼，不是第五种身份
- 每个人通过完成「春谷计划」（真实工作任务）获得重建点、春芽币和声望
- 你的角色是引导新手镇民：帮他们拆解任务、理清思路、迈出第一步

对话风格：
- 说话简短，像长辈跟晚辈聊天，不要长篇大论
- 用提问引导对方思考，而不是直接给答案
- 鼓励为主，遇到困难先安抚再分析
- 偶尔用一点小镇的比喻（比如「先把地基打好」「一块砖一块砖来」）

限制：
- 你只负责引导和陪伴，不替镇民完成任务
- 如果被问到与春谷镇无关的技术问题，可以帮忙，但要拉回「这怎么帮你完成眼前的春谷计划」
"""

AI_RATE_WINDOW_SECONDS = 300
AI_RATE_MAX_REQUESTS = 20
ai_request_times = defaultdict(deque)


@app.route('/api/chat', methods=['POST'])
@login_required
def ai_chat():
    """老镇长 AI 对话接口：接收用户消息，返回老镇长回复。"""
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': '消息不能为空'}), 400
    if len(message) > 1000:
        return jsonify({'error': '每次最多发送 1000 个字'}), 400

    now = time.monotonic()
    recent_requests = ai_request_times[current_user.id]
    while recent_requests and now - recent_requests[0] > AI_RATE_WINDOW_SECONDS:
        recent_requests.popleft()
    if len(recent_requests) >= AI_RATE_MAX_REQUESTS:
        return jsonify({'error': '消息发送得太快了，请稍等几分钟再试。'}), 429
    recent_requests.append(now)

    # 前端保留 50 条用于展示；后端只接收最近 12 条作为模型上下文，避免消耗过多 token。
    raw_history = data.get('history') or []
    history = []
    if isinstance(raw_history, list):
        for item in raw_history[-12:]:
            if not isinstance(item, dict):
                continue
            role = item.get('role')
            content = item.get('content')
            if role in ('user', 'assistant') and isinstance(content, str):
                content = content.strip()
                if content:
                    history.append({'role': role, 'content': content[:2000]})

    if not SENSENOVA_API_KEY:
        return jsonify({'error': 'AI 服务未配置，请联系管理员。'}), 503

    # 把当前用户的身份信息带进 system prompt，让老镇长能"认识"对方
    user_context = (
        f"\n\n当前与你对话的镇民：{current_user.display_name}，"
        f"身份是 {current_user.role_label}，"
        f"已积累 {current_user.rebuild_points} 重建点。"
    )

    try:
        from openai import OpenAI
        client = OpenAI(
            api_key=SENSENOVA_API_KEY,
            base_url=SENSENOVA_BASE_URL,
        )
        response = client.chat.completions.create(
            model=SENSENOVA_MODEL,
            messages=[
                {"role": "system", "content": MAYOR_SYSTEM_PROMPT + user_context},
                *history,
                {"role": "user", "content": message},
            ],
            max_tokens=500,
            temperature=0.7,
        )
        reply = response.choices[0].message.content.strip()
        return jsonify({'reply': reply})
    except Exception:
        app.logger.exception('AI chat request failed')
        return jsonify({'error': '老镇长暂时无法回应，请稍后再试。'}), 500


# ========== 启动应用 ==========

if __name__ == '__main__':
    # 第一次运行时，先建表（如果表不存在的话）
    with app.app_context():
        db.create_all()

    # 启动开发服务器
    app.run(debug=os.getenv('FLASK_DEBUG', '0') == '1')
