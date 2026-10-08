"""春谷镇核心业务闭环回归测试。

运行：venv/bin/python -m unittest tests.test_workflow -v
测试只使用临时数据库，不会触碰项目里的 springvale.db。
"""

import os
import shutil
import tempfile
import unittest
from datetime import date, datetime
from unittest.mock import patch
from urllib.parse import quote

_TEMP_DATA_DIR = tempfile.mkdtemp(prefix="springvale-workflow-")
os.environ["SPRINGVALE_DATA_DIR"] = _TEMP_DATA_DIR
os.environ.pop("DATABASE_URL", None)

from werkzeug.security import generate_password_hash

from app import LOGIN_LIMIT_BUCKETS, app, db, settle_monthly_ranking
from config import validate_production_config
from models import (
    DailyCheckin, MonthlyRankingReward, Notification, PromotionRequest, ShopItem, ShopRedemption,
    Task, TaskStatus, TownState, User,
)


class WorkflowTestCase(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.session.remove()
            db.engine.dispose()
        shutil.rmtree(_TEMP_DATA_DIR, ignore_errors=True)

    def setUp(self):
        app.config.update(
            TESTING=True,
            CSRF_ENABLED=False,
            DEMO_MODE=True,
            LOGIN_IP_MAX_ATTEMPTS=10,
            LOGIN_USER_MAX_ATTEMPTS=5,
        )
        LOGIN_LIMIT_BUCKETS.clear()
        with app.app_context():
            db.drop_all()
            db.create_all()
            self.user_ids = {}
            for username, role in (
                ("scavenger", "scavenger"),
                ("builder", "builder"),
                ("senior_builder", "senior_builder"),
                ("other_builder", "builder"),
                ("mayor", "admin"),
            ):
                user = User(
                    username=username,
                    display_name=username,
                    password_hash=generate_password_hash("test-password"),
                    role=role,
                    rebuild_points=0,
                    sprout_coins=0,
                )
                db.session.add(user)
                db.session.flush()
                self.user_ids[username] = user.id
            db.session.add(TownState(id=1, last_rewarded_stage=1))
            db.session.commit()
        self.client = app.test_client()

    def login_as(self, username):
        with self.client.session_transaction() as session:
            session.clear()
            session["_user_id"] = str(self.user_ids[username])
            session["_fresh"] = True

    def create_task(self, *, points=120, coins=35, title="闭环测试任务"):
        self.login_as("builder")
        response = self.client.post(
            "/tasks/create",
            data={
                "title": title,
                "description": "验证任务从发布到验收的完整流程。",
                "difficulty": "B",
                "reward_points": str(points),
                "reward_coins": str(coins),
            },
        )
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            return Task.query.filter_by(title=title).one().id

    def test_complete_workflow_permissions_and_no_double_reward(self):
        task_id = self.create_task()

        # 发布人不能领取自己的任务。
        self.client.post(f"/tasks/{task_id}/accept")
        with app.app_context():
            self.assertEqual(db.session.get(Task, task_id).status, TaskStatus.OPEN)

        # 拾荒者领取；无关用户不能替他开工。
        self.login_as("scavenger")
        self.client.post(f"/tasks/{task_id}/accept")
        self.login_as("other_builder")
        self.client.post(f"/tasks/{task_id}/start")
        with app.app_context():
            self.assertEqual(db.session.get(Task, task_id).status, TaskStatus.ACCEPTED)

        # 接取人开工、记录进度并提交。
        self.login_as("scavenger")
        self.client.post(f"/tasks/{task_id}/start")
        self.client.post(f"/tasks/{task_id}/progress", data={"progress_note": "完成 80%"})
        self.client.post(f"/tasks/{task_id}/submit")
        with app.app_context():
            task = db.session.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.SUBMITTED)
            self.assertEqual(task.progress_note, "完成 80%")

        # 无关建造师不能审核，发布人可以审核。
        self.login_as("other_builder")
        self.client.post(f"/tasks/{task_id}/approve")
        with app.app_context():
            self.assertEqual(db.session.get(Task, task_id).status, TaskStatus.SUBMITTED)

        self.login_as("builder")
        self.client.post(f"/tasks/{task_id}/approve")
        self.client.post(f"/tasks/{task_id}/approve")
        with app.app_context():
            acceptor = db.session.get(User, self.user_ids["scavenger"])
            self.assertEqual((acceptor.rebuild_points, acceptor.sprout_coins), (120, 35))
            self.assertEqual(
                Notification.query.filter_by(
                    user_id=acceptor.id,
                    event_type="task_approved",
                ).count(),
                1,
            )

    def test_reject_then_resubmit(self):
        task_id = self.create_task(title="打回测试任务")
        self.login_as("scavenger")
        self.client.post(f"/tasks/{task_id}/accept")
        self.client.post(f"/tasks/{task_id}/start")
        self.client.post(f"/tasks/{task_id}/submit")

        self.login_as("builder")
        self.client.post(f"/tasks/{task_id}/reject", data={"reject_reason": "请补充截图"})
        with app.app_context():
            self.assertEqual(db.session.get(Task, task_id).status, TaskStatus.REJECTED)

        self.login_as("scavenger")
        self.client.post(f"/tasks/{task_id}/progress", data={"progress_note": "已补充截图"})
        self.client.post(f"/tasks/{task_id}/submit")
        with app.app_context():
            task = db.session.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.SUBMITTED)
            self.assertEqual(task.progress_note, "已补充截图")

    def test_role_pages_and_notification_ownership(self):
        self.login_as("scavenger")
        self.assertEqual(self.client.get("/tasks/create").status_code, 403)
        self.assertEqual(self.client.get("/builder-workshop").status_code, 403)
        self.assertEqual(self.client.get("/review-hall").status_code, 403)
        self.assertEqual(self.client.get("/town-hall").status_code, 200)
        self.assertEqual(self.client.get(f"/town-hall/users/{self.user_ids['builder']}").status_code, 200)

        with app.app_context():
            notice = Notification(
                user_id=self.user_ids["other_builder"],
                event_type="task_accepted",
                title="私有通知",
                message="这条通知属于别人。",
                target_url="/",
            )
            db.session.add(notice)
            db.session.commit()
            notice_id = notice.id
        self.assertEqual(self.client.post(f"/notifications/{notice_id}/read").status_code, 404)

        self.login_as("mayor")
        self.assertEqual(self.client.get("/town-hall").status_code, 200)

    def test_home_building_lock_images_follow_role_permissions(self):
        locked_builder = quote("建造师工坊-level1-v2.webp").encode()
        unlocked_builder = quote("建造师工坊-已解锁.png").encode()
        unlocked_review = quote("验收大厅-已解锁.png").encode()
        unlocked_hall = quote("镇务厅-已解锁.png").encode()

        self.login_as("scavenger")
        scavenger_home = self.client.get("/").data
        self.assertIn(locked_builder, scavenger_home)
        self.assertNotIn(unlocked_builder, scavenger_home)
        self.assertIn(unlocked_hall, scavenger_home)

        for username in ("builder", "senior_builder"):
            self.login_as(username)
            home = self.client.get("/").data
            self.assertIn(unlocked_builder, home)
            self.assertIn(unlocked_review, home)
            self.assertIn(unlocked_hall, home)

        self.login_as("mayor")
        mayor_home = self.client.get("/").data
        self.assertIn(unlocked_builder, mayor_home)
        self.assertIn(unlocked_review, mayor_home)
        self.assertIn(unlocked_hall, mayor_home)

    def test_registration_requires_approval_and_role_permissions_are_separated(self):
        response = self.client.post("/register", data={
            "username": "new_citizen",
            "display_name": "新镇民",
            "password": "safe-password",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            newcomer = User.query.filter_by(username="new_citizen").one()
            newcomer_id = newcomer.id
            self.user_ids["new_citizen"] = newcomer_id
            self.assertEqual(newcomer.account_status, "pending")

        blocked_login = self.client.post("/login", data={
            "username": "new_citizen",
            "password": "safe-password",
        })
        self.assertIn("等待批准".encode(), blocked_login.data)

        self.login_as("builder")
        self.assertEqual(self.client.get("/town-hall").status_code, 200)
        self.assertEqual(self.client.post(f"/town-hall/users/{newcomer_id}/approve").status_code, 403)
        self.assertEqual(
            self.client.post(f"/town-hall/users/{self.user_ids['scavenger']}/role", data={"role": "builder"}).status_code,
            403,
        )

        self.login_as("senior_builder")
        self.assertEqual(self.client.post(f"/town-hall/users/{newcomer_id}/approve").status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(User, newcomer_id).account_status, "approved")
        self.assertEqual(
            self.client.post(f"/town-hall/users/{newcomer_id}/role", data={"role": "builder"}).status_code,
            403,
        )

        self.client.post("/logout")
        approved_login = self.client.post("/login", data={
            "username": "new_citizen",
            "password": "safe-password",
        })
        self.assertEqual(approved_login.status_code, 302)

        # 镇长也不能从旧的角色下拉框绕过“500 点 + 本人申请”规则。
        self.login_as("mayor")
        self.client.post(f"/town-hall/users/{newcomer_id}/role", data={"role": "builder"})
        with app.app_context():
            self.assertEqual(db.session.get(User, newcomer_id).role, "scavenger")

        with app.app_context():
            db.session.get(User, newcomer_id).rebuild_points = 500
            db.session.commit()
        self.login_as("new_citizen")
        self.client.post("/promotions/builder/apply")
        with app.app_context():
            promotion_id = PromotionRequest.query.filter_by(applicant_id=newcomer_id).one().id
        self.login_as("mayor")
        self.client.post(f"/promotions/{promotion_id}/approve")
        with app.app_context():
            self.assertEqual(db.session.get(User, newcomer_id).role, "builder")

        # 镇长只有一位，不能再把其他用户提升为第二位镇长。
        self.client.post(f"/town-hall/users/{newcomer_id}/role", data={"role": "admin"})
        with app.app_context():
            self.assertEqual(db.session.get(User, newcomer_id).role, "builder")
            self.assertEqual(User.query.filter_by(role="admin").count(), 1)

    def test_builder_promotion_requires_500_points_and_blocks_duplicate_request(self):
        self.login_as("scavenger")

        # 499 点时后端拒绝，不能只靠页面隐藏按钮。
        with app.app_context():
            db.session.get(User, self.user_ids["scavenger"]).rebuild_points = 499
            db.session.commit()
        response = self.client.post("/promotions/builder/apply", follow_redirects=True)
        self.assertIn("还差 1 重建点".encode(), response.data)
        with app.app_context():
            self.assertEqual(PromotionRequest.query.count(), 0)

        # 达到 500 点后创建一条申请，但角色不会自动改变。
        with app.app_context():
            db.session.get(User, self.user_ids["scavenger"]).rebuild_points = 500
            db.session.commit()
        self.client.post("/promotions/builder/apply", data={"application_note": "希望发布协作任务"})
        self.client.post("/promotions/builder/apply", data={"application_note": "重复点击"})
        with app.app_context():
            applicant = db.session.get(User, self.user_ids["scavenger"])
            promotion = PromotionRequest.query.one()
            self.assertEqual(applicant.role, "scavenger")
            self.assertEqual(promotion.status, "pending")
            self.assertEqual(promotion.points_at_apply, 500)
            self.assertEqual(promotion.application_note, "希望发布协作任务")
            self.assertEqual(
                Notification.query.filter_by(
                    user_id=self.user_ids["mayor"], event_type="promotion_submitted"
                ).count(),
                1,
            )

    def test_mayor_approves_promotion_once_and_audit_record_is_complete(self):
        with app.app_context():
            db.session.get(User, self.user_ids["scavenger"]).rebuild_points = 520
            db.session.commit()
        self.login_as("scavenger")
        self.client.post("/promotions/builder/apply")
        with app.app_context():
            promotion_id = PromotionRequest.query.one().id

        # 建造师无审批权，镇长才可以批准。
        self.login_as("builder")
        self.assertEqual(self.client.post(f"/promotions/{promotion_id}/approve").status_code, 403)
        self.login_as("mayor")
        self.client.post(f"/promotions/{promotion_id}/approve")
        self.client.post(f"/promotions/{promotion_id}/approve")

        with app.app_context():
            applicant = db.session.get(User, self.user_ids["scavenger"])
            promotion = db.session.get(PromotionRequest, promotion_id)
            self.assertEqual(applicant.role, "builder")
            self.assertEqual(promotion.status, "approved")
            self.assertEqual(promotion.reviewer_id, self.user_ids["mayor"])
            self.assertEqual(promotion.points_at_review, 520)
            self.assertIsNotNone(promotion.reviewed_at)
            self.assertIsNone(promotion.active_key)
            self.assertEqual(
                Notification.query.filter_by(
                    user_id=applicant.id, event_type="promotion_approved"
                ).count(),
                1,
            )

    def test_rejected_promotion_keeps_role_and_allows_a_new_application(self):
        with app.app_context():
            db.session.get(User, self.user_ids["scavenger"]).rebuild_points = 500
            db.session.commit()
        self.login_as("scavenger")
        self.client.post("/promotions/builder/apply")
        with app.app_context():
            first_id = PromotionRequest.query.one().id

        self.login_as("mayor")
        # 空理由不能拒绝。
        self.client.post(f"/promotions/{first_id}/reject", data={"review_note": ""})
        with app.app_context():
            self.assertEqual(db.session.get(PromotionRequest, first_id).status, "pending")
        self.client.post(f"/promotions/{first_id}/reject", data={"review_note": "请先补充一次协作说明"})

        self.login_as("scavenger")
        self.client.post("/promotions/builder/apply", data={"application_note": "已补充说明"})
        with app.app_context():
            applicant = db.session.get(User, self.user_ids["scavenger"])
            requests = PromotionRequest.query.order_by(PromotionRequest.id).all()
            self.assertEqual(applicant.role, "scavenger")
            self.assertEqual([item.status for item in requests], ["rejected", "pending"])
            self.assertEqual(requests[0].review_note, "请先补充一次协作说明")
            self.assertEqual(
                Notification.query.filter_by(
                    user_id=applicant.id, event_type="promotion_rejected"
                ).count(),
                1,
            )

    def test_four_roles_keep_matching_names_and_avatars_regardless_of_points(self):
        expected = {
            "scavenger": ("Lv.1 拾荒者", "scavenger.jpg"),
            "builder": ("Lv.2 建造师", "builder.jpg"),
            "senior_builder": ("Lv.3 资深建造师", "senior-builder.jpg"),
            "mayor": ("Lv.4 镇长", "mayor.jpg"),
        }
        with app.app_context():
            for username, (label, avatar) in expected.items():
                user = db.session.get(User, self.user_ids[username])
                user.rebuild_points = 9999
                self.assertEqual(user.level_label, label)
                self.assertTrue(user.avatar_filename.endswith(avatar))

    def test_demo_reset_is_admin_only_idempotent_and_preserves_regular_data(self):
        regular_task_id = self.create_task(title="普通用户必须保留的任务")

        self.login_as("scavenger")
        self.assertEqual(
            self.client.post("/town-hall/reset-demo", data={"confirmation": "RESET_DEMO"}).status_code,
            403,
        )

        self.login_as("mayor")
        self.client.post("/town-hall/reset-demo", data={"confirmation": "RESET_DEMO"})
        self.client.post("/town-hall/reset-demo", data={"confirmation": "RESET_DEMO"})

        with app.app_context():
            self.assertIsNotNone(db.session.get(Task, regular_task_id))
            demo_users = User.query.filter(User.username.in_((
                "scavenger_demo", "builder_demo", "senior_builder_demo", "mayor_demo"
            ))).all()
            self.assertEqual(len(demo_users), 4)
            demo_ids = [user.id for user in demo_users]
            self.assertEqual(Task.query.filter(Task.publisher_id.in_(demo_ids)).count(), 3)

    def test_town_stage_reward_is_only_paid_once(self):
        task_id = self.create_task(points=500, coins=0, title="阶段升级任务")
        self.login_as("scavenger")
        self.client.post(f"/tasks/{task_id}/accept")
        self.client.post(f"/tasks/{task_id}/start")
        self.client.post(f"/tasks/{task_id}/submit")
        self.login_as("builder")
        self.client.post(f"/tasks/{task_id}/approve")
        self.client.get("/")
        self.client.get("/")

        with app.app_context():
            users = User.query.order_by(User.id).all()
            # 全镇四人各获得一次阶段奖励；接取人的任务奖励金币为 0。
            self.assertEqual([user.sprout_coins for user in users], [100, 100, 100, 100, 100])
            self.assertEqual(
                Notification.query.filter_by(event_type="town_stage_up").count(),
                5,
            )
            self.assertEqual(db.session.get(TownState, 1).last_rewarded_stage, 2)

    def test_checkin_and_shop_boundaries(self):
        self.login_as("scavenger")
        with patch("app.random.randint", return_value=3):
            self.client.post("/checkin")
            self.client.post("/checkin")
        with app.app_context():
            user = db.session.get(User, self.user_ids["scavenger"])
            self.assertEqual(user.sprout_coins, 3)
            self.assertEqual(DailyCheckin.query.filter_by(user_id=user.id).count(), 1)

        # 余额不足时不创建兑换记录。
        self.client.post("/shop/redeem", data={"item_key": "coffee_ticket"})
        with app.app_context():
            self.assertEqual(ShopRedemption.query.count(), 0)

        # 余额足够时只扣除商品价格并创建一条流水。
        with app.app_context():
            user = db.session.get(User, self.user_ids["scavenger"])
            user.sprout_coins = 120
            db.session.commit()
        self.client.post("/shop/redeem", data={"item_key": "coffee_ticket"})
        with app.app_context():
            user = db.session.get(User, self.user_ids["scavenger"])
            self.assertEqual(user.sprout_coins, 20)
            self.assertEqual(ShopRedemption.query.filter_by(user_id=user.id).count(), 1)

    def test_mayor_manages_shop_and_regular_user_cannot(self):
        item_data = {
            "name": "春谷纪念徽章",
            "cost_coins": "88",
            "description": "用于测试镇长新增商品。",
            "note": "测试商品",
            "is_active": "on",
        }
        self.login_as("scavenger")
        self.assertEqual(self.client.post("/shop/items/create", data=item_data).status_code, 403)

        self.login_as("mayor")
        self.assertEqual(self.client.post("/shop/items/create", data=item_data).status_code, 302)
        with app.app_context():
            item = ShopItem.query.filter_by(name="春谷纪念徽章").one()
            item_id = item.id
            item_key = item.item_key
            self.assertTrue(item.is_active)

        update_data = {
            "name": "春谷纪念徽章（调整）",
            "cost_coins": "66",
            "description": "镇长已经调整内容和金额。",
            "note": "暂时下架",
        }
        self.client.post(f"/shop/items/{item_id}/update", data=update_data)
        with app.app_context():
            item = db.session.get(ShopItem, item_id)
            self.assertEqual(item.cost_coins, 66)
            self.assertFalse(item.is_active)

        self.login_as("scavenger")
        with app.app_context():
            user = db.session.get(User, self.user_ids["scavenger"])
            user.sprout_coins = 1000
            db.session.commit()
        self.client.post("/shop/redeem", data={"item_key": item_key})
        with app.app_context():
            self.assertEqual(ShopRedemption.query.filter_by(item_key=item_key).count(), 0)
            self.assertEqual(db.session.get(User, self.user_ids["scavenger"]).sprout_coins, 1000)

    def test_mayor_soft_deletes_user_and_cannot_delete_self(self):
        target_id = self.user_ids["scavenger"]
        self.login_as("builder")
        self.assertEqual(self.client.post(f"/town-hall/users/{target_id}/delete").status_code, 403)

        self.login_as("mayor")
        self.assertEqual(self.client.post(f"/town-hall/users/{target_id}/delete").status_code, 302)
        self.client.post(f"/town-hall/users/{self.user_ids['mayor']}/delete")
        with app.app_context():
            target = db.session.get(User, target_id)
            mayor = db.session.get(User, self.user_ids["mayor"])
            self.assertEqual(target.account_status, "deleted")
            self.assertTrue(target.username.startswith("deleted_"))
            self.assertEqual(target.role, "scavenger")
            self.assertEqual(mayor.account_status, "approved")
            self.assertEqual(mayor.role, "admin")

        self.client.post("/logout")
        response = self.client.post("/login", data={
            "username": "scavenger",
            "password": "test-password",
        })
        self.assertNotEqual(response.status_code, 302)

    def test_monthly_ranking_uses_completed_contribution_and_pays_once(self):
        with app.app_context():
            mayor_id = self.user_ids["mayor"]
            contributions = (
                ("scavenger", 90),
                ("builder", 150),
                ("senior_builder", 120),
                ("other_builder", 30),
            )
            for username, points in contributions:
                db.session.add(Task(
                    title=f"{username} 七月贡献",
                    description="月榜结算测试",
                    category="general",
                    difficulty="B",
                    reward_points=points,
                    reward_coins=0,
                    publisher_id=mayor_id,
                    acceptor_id=self.user_ids[username],
                    status=TaskStatus.COMPLETED,
                    completed_at=datetime(2026, 7, 20, 12, 0),
                ))
            db.session.commit()

            settle_monthly_ranking(date(2026, 7, 31))
            db.session.commit()
            settle_monthly_ranking(date(2026, 7, 31))
            db.session.commit()

            rewards = MonthlyRankingReward.query.filter_by(period_key="2026-07").order_by(
                MonthlyRankingReward.rank
            ).all()
            self.assertEqual(
                [(reward.user_id, reward.reward_coins) for reward in rewards],
                [
                    (self.user_ids["builder"], 300),
                    (self.user_ids["senior_builder"], 200),
                    (self.user_ids["scavenger"], 100),
                ],
            )
            self.assertEqual(len(rewards), 3)
            self.assertEqual(db.session.get(User, self.user_ids["builder"]).sprout_coins, 300)
            self.assertEqual(db.session.get(User, self.user_ids["senior_builder"]).sprout_coins, 200)
            self.assertEqual(db.session.get(User, self.user_ids["scavenger"]).sprout_coins, 100)

    def test_server_rejects_invalid_registration_task_and_empty_progress(self):
        self.client.post("/register", data={
            "username": "x", "display_name": "测试", "password": "123",
        })
        with app.app_context():
            self.assertIsNone(User.query.filter_by(username="x").first())

        self.login_as("builder")
        self.client.post("/tasks/create", data={
            "title": "超额奖励任务", "description": "不应创建",
            "difficulty": "Z", "reward_points": "999999", "reward_coins": "999999",
        })
        with app.app_context():
            self.assertIsNone(Task.query.filter_by(title="超额奖励任务").first())

        task_id = self.create_task(title="空进度保护任务")
        self.login_as("scavenger")
        self.client.post(f"/tasks/{task_id}/accept")
        self.client.post(f"/tasks/{task_id}/start")
        self.client.post(f"/tasks/{task_id}/progress", data={"progress_note": "   "})
        with app.app_context():
            task = db.session.get(Task, task_id)
            self.assertEqual(task.status, TaskStatus.PROCESSING)
            self.assertIsNone(task.progress_note)

    def test_ai_failure_does_not_expose_internal_error(self):
        self.login_as("scavenger")
        with patch("app.SENSENOVA_API_KEY", "fake-key"), patch(
            "openai.OpenAI", side_effect=RuntimeError("private-provider-detail")
        ):
            response = self.client.post("/api/chat", json={"message": "帮我拆任务"})
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("private-provider-detail", response.get_data(as_text=True))
        self.assertEqual(response.get_json()["error"], "老镇长暂时无法回应，请稍后再试。")

    def test_ai_rate_limit_and_security_headers(self):
        self.login_as("scavenger")
        with patch("app.AI_RATE_MAX_REQUESTS", 0):
            response = self.client.post("/api/chat", json={"message": "连续请求测试"})
        self.assertEqual(response.status_code, 429)

        page = self.client.get("/")
        self.assertEqual(page.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(page.headers["X-Frame-Options"], "SAMEORIGIN")
        self.assertEqual(page.headers["Referrer-Policy"], "same-origin")

    def test_csrf_rejects_missing_token_and_accepts_valid_token(self):
        app.config["CSRF_ENABLED"] = True
        self.client.get("/login")
        response = self.client.post("/login", data={
            "username": "scavenger",
            "password": "test-password",
        })
        self.assertEqual(response.status_code, 400)

        with self.client.session_transaction() as session:
            token = session["_csrf_token"]
        response = self.client.post("/login", data={
            "username": "scavenger",
            "password": "test-password",
            "csrf_token": token,
        })
        self.assertEqual(response.status_code, 302)

    def test_login_rate_limit_blocks_repeated_password_guessing(self):
        app.config.update(LOGIN_USER_MAX_ATTEMPTS=2, LOGIN_IP_MAX_ATTEMPTS=20)
        payload = {"username": "scavenger", "password": "wrong-password"}
        self.assertEqual(self.client.post("/login", data=payload).status_code, 200)
        self.assertEqual(self.client.post("/login", data=payload).status_code, 200)
        limited = self.client.post("/login", data=payload)
        self.assertEqual(limited.status_code, 429)
        self.assertIn("登录尝试次数过多", limited.get_data(as_text=True))

    def test_production_config_rejects_weak_secret_and_demo_mode(self):
        with self.assertRaises(RuntimeError):
            validate_production_config({
                "APP_ENV": "production",
                "SECRET_KEY": "dev-secret-key-change-in-production",
                "DEMO_MODE": False,
            })
        with self.assertRaises(RuntimeError):
            validate_production_config({
                "APP_ENV": "production",
                "SECRET_KEY": "x" * 40,
                "DEMO_MODE": True,
            })
        validate_production_config({
            "APP_ENV": "production",
            "SECRET_KEY": "x" * 40,
            "DEMO_MODE": False,
        })

    def test_demo_reset_is_blocked_when_demo_mode_is_off(self):
        self.login_as("mayor")
        app.config["DEMO_MODE"] = False
        response = self.client.post(
            "/town-hall/reset-demo",
            data={"confirmation": "RESET_DEMO"},
        )
        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
