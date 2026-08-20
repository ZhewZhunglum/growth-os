from types import SimpleNamespace

from django import forms
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import AnonymousUser
from django.template.loader import render_to_string
from django.test import SimpleTestCase

from dashboard.templatetags.dashboard_display import (
    check_result_zh,
    render_delivery_check,
    review_decision_zh,
    task_state_zh,
)


class DeliveryChoiceForm(forms.Form):
    criterion__primary_deliverable = forms.ChoiceField(
        choices=(("", "请选择结果"), ("PASS", "通过（PASS）"), ("BLOCKED", "阻塞（BLOCKED）"))
    )


class DashboardTemplateUXTests(SimpleTestCase):
    def test_login_uses_password_manager_metadata_and_stores_username_only(self):
        html = render_to_string(
            "registration/login.html",
            {"form": AuthenticationForm(), "next": ""},
        )

        self.assertIn('autocomplete="username"', html)
        self.assertIn('autocomplete="current-password"', html)
        self.assertIn("在这台电脑上记住用户名", html)
        self.assertIn('const usernameKey = "growth_os.remembered_username.v1"', html)
        self.assertNotIn("remembered_password", html)
        self.assertNotIn("localStorage.setItem(password", html)

    def test_delivery_check_keeps_machine_values_but_explains_them_plainly(self):
        form = DeliveryChoiceForm()
        html = str(render_delivery_check(form["criterion__primary_deliverable"]))

        self.assertIn('value="PASS"', html)
        self.assertIn(">确认完整交付并送审</option>", html)
        self.assertIn('value="BLOCKED"', html)
        self.assertIn(">尚未准备好，本次不送审</option>", html)

    def test_chinese_display_helpers_do_not_change_stored_enum_values(self):
        self.assertEqual(task_state_zh("UNDER_REVIEW"), "审核中")
        self.assertEqual(check_result_zh("PASS"), "已通过")
        self.assertEqual(review_decision_zh("CHANGES_REQUESTED"), "需要修改")
        self.assertEqual(task_state_zh("FUTURE_STATE"), "FUTURE_STATE")

    def test_today_queue_regions_render_safe_zero_state_without_backend_context(self):
        html = render_to_string(
            "dashboard/home.html",
            {
                "user": AnonymousUser(),
                "tasks": [],
                "task_count": 0,
                "active_task_count": 0,
                "blocked_task_count": 0,
                "can_create_task": False,
            },
        )

        self.assertIn("待我审核", html)
        self.assertIn("待我发布", html)
        self.assertIn("待我确认完成", html)
        self.assertIn("这些不是我的执行任务", html)
        self.assertIn("今天没有分配给你的任务", html)

    def test_review_queue_has_safe_empty_read_only_history_region(self):
        html = render_to_string(
            "dashboard/review_queue.html",
            {"user": AnonymousUser(), "tasks": []},
        )

        self.assertIn("我已完成的审核", html)
        self.assertIn("还没有已完成的审核记录", html)

    def test_review_history_detail_is_read_only_and_names_exact_asset_version(self):
        principal = SimpleNamespace(display_name="审核人员", username="reviewer")
        product = SimpleNamespace(name="PUKO")
        task = SimpleNamespace(title="审核历史测试", description="只读说明", product=product)
        submission = SimpleNamespace(submission_number=2, sealed_at="2026-08-20 10:00")
        review = SimpleNamespace(
            decision="APPROVED",
            rationale="内容与规则一致。",
            decided_at="2026-08-20 11:00",
            reviewer_principal=principal,
        )
        asset_version = SimpleNamespace(
            version_number=3,
            mime_type="text/plain",
            content_sha256="a" * 64,
            object_key="controlled/task/asset-v3.txt",
            metadata={"original_filename": "asset-v3.txt"},
        )

        html = render_to_string(
            "dashboard/review_history_detail.html",
            {
                "user": AnonymousUser(),
                "task": task,
                "submission": submission,
                "review": review,
                "asset_version": asset_version,
            },
        )

        self.assertIn("只读记录", html)
        self.assertIn("asset-v3.txt", html)
        self.assertIn("a" * 64, html)
        self.assertNotIn("<form", html)
