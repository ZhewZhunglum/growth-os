from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant


class PresentationLanguageTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="language-owner",
            password="LocalPass123!",
            display_name="Language Owner",
            principal_type="HUMAN_USER",
            role="OWNER",
        )
        self.team_management_grant = PermissionGrant.objects.create(
            principal=self.user,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1),
            granted_by_principal=self.user,
        )

    def test_chinese_is_the_default_and_switching_to_english_is_session_only(self):
        login_url = reverse("login")
        response = self.client.get(login_url)
        self.assertContains(response, "登录后继续")
        self.assertContains(response, '<html lang="zh-CN">')

        response = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": login_url},
        )
        self.assertRedirects(response, login_url, fetch_redirect_response=False)
        response = self.client.get(login_url)
        self.assertContains(response, "Sign in to continue")
        self.assertContains(response, '<html lang="en">')

        self.user.refresh_from_db()
        self.assertEqual(self.user.role, "OWNER")
        self.assertEqual(self.user.display_name, "Language Owner")

    def test_authenticated_navigation_uses_selected_session_language(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("set_language"),
            {"language": "en", "next": reverse("dashboard:home")},
        )
        response = self.client.get(reverse("dashboard:home"))
        self.assertContains(response, "Daily Operations")
        self.assertContains(response, "Team & access")
        self.assertContains(response, "Product & runtime setup")
        self.assertContains(response, "Sign out")

    def test_daily_team_and_configuration_main_labels_switch_to_english(self):
        self.client.force_login(self.user)
        self.client.post(
            reverse("set_language"),
            {"language": "en", "next": reverse("dailyops:home")},
        )

        response = self.client.get(reverse("dailyops:home"))
        self.assertContains(response, "Start with real external signals")
        self.assertContains(response, "Create seven-platform checklist")

        response = self.client.get(reverse("dashboard:team-members"))
        self.assertContains(response, "Staff accounts and live permissions")
        self.assertContains(response, "View and manage access")

        response = self.client.get(reverse("dashboard:configuration-home"))
        self.assertContains(response, "Prepare real products, rules, and accounts")
        self.assertContains(response, "Products I can configure")

    def test_language_switch_requires_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        response = csrf_client.post(
            reverse("set_language"),
            {"language": "en", "next": reverse("login")},
        )
        self.assertEqual(response.status_code, 403)

        login_response = csrf_client.get(reverse("login"))
        token = login_response.cookies["csrftoken"].value
        response = csrf_client.post(
            reverse("set_language"),
            {
                "language": "en",
                "next": reverse("login"),
                "csrfmiddlewaretoken": token,
            },
        )
        self.assertEqual(response.status_code, 302)

    def test_external_next_url_is_not_used(self):
        response = self.client.post(
            reverse("set_language"),
            {"language": "en", "next": "https://evil.example/phish"},
        )
        self.assertRedirects(response, "/", fetch_redirect_response=False)
