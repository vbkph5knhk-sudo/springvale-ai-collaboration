"""创建本地学习用的演示账号和任务。

运行：python seed_demo.py

这个脚本可以重复运行：如果账号或任务已经存在，就不会重复创建。
注意：这里的密码只是本地演示密码，正式部署前必须删除或更换。
"""

from werkzeug.security import generate_password_hash

from app import app
from models import Task, TaskStatus, User, db

if not app.config.get('DEMO_MODE'):
    raise SystemExit('DEMO_MODE 未开启：生产环境禁止创建或重置演示账号。')


def get_or_create_user(username, display_name, role):
    user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(
            username=username,
            display_name=display_name,
            role=role,
            account_status='approved',
            password_hash=generate_password_hash(app.config['DEMO_PASSWORD']),
            rebuild_points=420 if role == 'builder' else 80,
            sprout_coins=120 if role == 'builder' else 40,
            town_trust=86 if role == 'builder' else 20,
        )
        db.session.add(user)
        db.session.flush()
    else:
        # 部署平台修改 DEMO_PASSWORD 后，让已有演示账号与登录页提示保持一致。
        # 只更新固定演示账号，不影响普通注册用户。
        user.password_hash = generate_password_hash(app.config['DEMO_PASSWORD'])
        user.account_status = 'approved'
    return user


with app.app_context():
    db.create_all()

    builder = get_or_create_user('builder_demo', '林小满', 'builder')
    get_or_create_user('scavenger_demo', '新芽', 'scavenger')
    senior_builder = get_or_create_user('senior_builder_demo', '青岚', 'senior_builder')
    mayor = get_or_create_user('mayor_demo', '春谷镇长', 'admin')
    senior_builder.rebuild_points = max(senior_builder.rebuild_points or 0, 650)
    senior_builder.sprout_coins = max(senior_builder.sprout_coins or 0, 260)
    senior_builder.town_trust = max(senior_builder.town_trust or 0, 92)
    mayor.rebuild_points = max(mayor.rebuild_points or 0, 1200)
    mayor.sprout_coins = max(mayor.sprout_coins or 0, 500)
    mayor.town_trust = max(mayor.town_trust or 0, 100)

    demo_tasks = [
        ('整理 Q3 储能行业跟踪框架', '建立行业分类、数据源和重点公司清单，形成可持续更新的研究框架。', 'drawing', 'A', 30, 60),
        ('更新半导体材料价格数据库', '录入本周硅片、电子特气和靶材价格，并核对数据口径。', 'calc', 'B', 20, 40),
        ('访谈三位新入镇拾荒者', '了解新用户接取第一项任务时的困惑，并整理改进建议。', 'scout', 'B', 20, 40),
    ]

    for title, description, category, difficulty, points, coins in demo_tasks:
        if Task.query.filter_by(title=title).first() is None:
            db.session.add(Task(
                title=title,
                description=description,
                category=category,
                difficulty=difficulty,
                reward_points=points,
                reward_coins=coins,
                publisher_id=builder.id,
                status=TaskStatus.OPEN,
            ))

    db.session.commit()
    print('演示数据已准备完成。')
    print(f"建造师：builder_demo / {app.config['DEMO_PASSWORD']}")
    print(f"资深建造师：senior_builder_demo / {app.config['DEMO_PASSWORD']}")
    print(f"拾荒者：scavenger_demo / {app.config['DEMO_PASSWORD']}")
    print(f"镇长：mayor_demo / {app.config['DEMO_PASSWORD']}")
