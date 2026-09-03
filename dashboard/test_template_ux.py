from pathlib import Path
from types import SimpleNamespace

from django import forms
from django.conf import settings
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
    def test_major_authenticated_pages_have_explicit_logical_parent_links(self):
        cases = (
            (
                "dashboard/feature_center.html",
                {"feature_center": SimpleNamespace(groups=())},
                'href="/"',
                "返回首页",
            ),
            ("dashboard/review_queue.html", {"tasks": [], "completed_reviews": []}, 'href="/"', "返回首页"),
            ("dashboard/release_queue.html", {"tasks": []}, 'href="/"', "返回首页"),
            ("dashboard/configuration_home.html", {"products": []}, 'href="/"', "返回首页"),
            (
                "dashboard/runtime_configuration.html",
                {
                    "execution_platforms": [],
                    "analytical_platforms": [],
                    "environment_summaries": [],
                    "ready_execution_count": 0,
                    "execution_count": 0,
                },
                'href="/configuration/"',
                "返回设置",
            ),
            (
                "dashboard/runtime_configuration_advanced.html",
                {
                    "forms": SimpleNamespace(),
                    "accounts": [],
                    "environments": [],
                    "bindings": [],
                    "capabilities": [],
                },
                'href="/configuration/runtime/"',
                "返回渠道状态",
            ),
            ("dashboard/team_members.html", {"staff_rows": []}, 'href="/features/"', "返回功能中心"),
            ("dashboard/guide.html", {}, 'href="/features/"', "返回功能中心"),
            ("dashboard/change_password.html", {}, 'href="/features/"', "返回功能中心"),
            (
                "dailyops/home.html",
                {
                    "configured_source_count": 0,
                    "expected_source_count": 0,
                    "data_recommendations": [],
                    "recent_batches": [],
                },
                'href="/features/"',
                "返回功能中心",
            ),
            (
                "feedbackui/home.html",
                {"can_record_geo": False, "can_configure_geo": False},
                'href="/features/"',
                "返回功能中心",
            ),
            (
                "governanceui/home.html",
                {"policy_definitions": [], "issues": [], "meetings": [], "proposals": []},
                'href="/features/"',
                "返回功能中心",
            ),
        )

        for template_name, context, expected_href, expected_label in cases:
            with self.subTest(template=template_name):
                html = render_to_string(
                    template_name,
                    {"user": AnonymousUser(), **context},
                )
                self.assertIn(f'<a class="back-link" {expected_href}', html)
                self.assertIn(expected_label, html)
                self.assertNotIn("history.back", html)

    def test_global_clickable_cursor_rules_keep_disabled_controls_distinct(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn("a[href],button:not(:disabled),summary,select:not(:disabled)", css)
        self.assertIn('input[type="checkbox"]:not(:disabled)', css)
        self.assertIn('[role="button"]:not([aria-disabled="true"]),label[for]{cursor:pointer}', css)
        self.assertIn('[aria-disabled="true"],.is-disabled{cursor:not-allowed}', css)

    def test_mobile_navigation_keeps_primary_links_available(self):
        css = (Path(settings.BASE_DIR) / "static" / "css" / "app.css").read_text(encoding="utf-8")

        self.assertIn(".sidebar nav{display:flex", css)
        self.assertIn("overflow-x:auto", css)
        self.assertIn(".sidebar nav .nav-item.is-active{order:-1}", css)
        self.assertIn(".connection-account-row p,.connection-empty p", css)
        self.assertIn(".connection-status,.connection-account-state", css)

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

        self.assertIn("今天的待办已经清空", html)
        self.assertIn("现在没有执行任务", html)
        self.assertNotIn("先弄清楚为什么做", html)
        self.assertNotIn("开工检查（DoR）", html)
        self.assertNotIn("交付检查（DoD）", html)

    def test_review_queue_has_safe_empty_read_only_history_region(self):
        html = render_to_string(
            "dashboard/review_queue.html",
            {"user": AnonymousUser(), "tasks": []},
        )

        self.assertIn("我已完成的审核", html)
        self.assertIn("还没有已完成的审核记录", html)

    def test_review_history_detail_is_read_only_and_hides_link_without_permission(self):
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
            mime_type="text/uri-list",
            content_sha256="a" * 64,
            object_key="https://docs.example.com/private/asset-v3",
            metadata={"source": "external-url"},
        )

        html = render_to_string(
            "dashboard/review_history_detail.html",
            {
                "user": AnonymousUser(),
                "task": task,
                "submission": submission,
                "review": review,
                "asset_version": asset_version,
                "is_link_delivery": True,
                "can_view_asset": False,
            },
        )

        self.assertIn("只读记录", html)
        self.assertNotIn(asset_version.object_key, html)
        self.assertIn("a" * 64, html)
        self.assertIn("当前审核权限已失效", html)
        # The shared header contains only the locale switch.  The immutable
        # review content below the header must expose no editing form.
        review_content = html.split("</header>", 1)[1]
        self.assertNotIn("<form", review_content)
