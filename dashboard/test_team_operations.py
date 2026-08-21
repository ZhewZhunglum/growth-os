from __future__ import annotations

from datetime import timedelta

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from releasegate.models import ChannelAccount


class TeamOperationsTests(TestCase):
    def setUp(self):
        self.owner = self._user("owner-team", Principal.Role.OWNER)
        self.admin = self._user("admin-team", Principal.Role.OPERATIONS_ADMIN)
        self.operator = self._user("operator-team", Principal.Role.OPERATOR)
        self.other_operator = self._user("operator-other", Principal.Role.OPERATOR)
        self.service = Principal.objects.create_user(
            username="service-team",
            password="NotUsed123!",
            role=Principal.Role.OPERATOR,
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
        )
        self.owner_manage = self._grant(
            self.owner,
            PermissionGrant.Action.MANAGE_ACCOUNT,
            actor=self.owner,
        )
        self.admin_manage = self._grant(
            self.admin,
            PermissionGrant.Action.MANAGE_ACCOUNT,
            actor=self.owner,
        )

    def _user(self, username, role):
        return Principal.objects.create_user(
            username=username,
            display_name=username.replace("-", " ").title(),
            password="StartPassword123!",
            role=role,
            principal_type=Principal.PrincipalType.HUMAN_USER,
        )

    def _grant(self, principal, action, *, actor, **kwargs):
        defaults = {
            "scope_kind": PermissionGrant.ScopeKind.GLOBAL,
            "effect": PermissionGrant.Effect.ALLOW,
            "risk_level": PermissionGrant.RiskLevel.LOW,
            "valid_from": timezone.now() - timedelta(hours=1),
            "valid_until": timezone.now() + timedelta(days=30),
            "grant_status": PermissionGrant.GrantStatus.ACTIVE,
            "granted_by_principal": actor,
        }
        defaults.update(kwargs)
        return PermissionGrant.objects.create(principal=principal, action=action, **defaults)

    @staticmethod
    def _datetime_input(value):
        return timezone.localtime(value).strftime("%Y-%m-%dT%H:%M")

    def test_owner_sees_all_human_staff_but_not_service_identity(self):
        self.client.force_login(self.owner)
        response = self.client.get(reverse("dashboard:team-members"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.owner.username)
        self.assertContains(response, self.admin.username)
        self.assertContains(response, self.operator.username)
        self.assertNotContains(response, self.service.username)

    def test_admin_sees_only_operators_and_cannot_probe_owner(self):
        self.client.force_login(self.admin)
        response = self.client.get(reverse("dashboard:team-members"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.operator.username)
        self.assertNotContains(response, self.owner.username)
        self.assertNotContains(response, self.admin.username)
        hidden = self.client.get(reverse("dashboard:team-member-detail", args=[self.owner.pk]))
        self.assertEqual(hidden.status_code, 404)

    def test_team_endpoint_requires_realtime_manage_grant_not_role_alone(self):
        owner_without_grant = self._user("owner-no-grant", Principal.Role.OWNER)
        self.client.force_login(owner_without_grant)
        self.assertEqual(self.client.get(reverse("dashboard:team-members")).status_code, 403)

    def test_operator_cannot_manage_even_with_forged_url(self):
        self.client.force_login(self.operator)
        response = self.client.get(reverse("dashboard:team-members"))
        self.assertEqual(response.status_code, 403)

    def test_admin_can_issue_low_risk_edit_to_operator(self):
        self.client.force_login(self.admin)
        now = timezone.now()
        response = self.client.post(
            reverse("dashboard:team-grant-issue", args=[self.operator.pk]),
            {
                "scope_kind": PermissionGrant.ScopeKind.GLOBAL,
                "product": "",
                "platform_code": "",
                "channel_account": "",
                "surface_ref": "",
                "action": PermissionGrant.Action.EDIT,
                "effect": PermissionGrant.Effect.ALLOW,
                "risk_level": PermissionGrant.RiskLevel.LOW,
                "valid_from": self._datetime_input(now),
                "valid_until": self._datetime_input(now + timedelta(days=10)),
            },
        )
        self.assertEqual(response.status_code, 302)
        grant = PermissionGrant.objects.get(principal=self.operator, action=PermissionGrant.Action.EDIT)
        self.assertEqual(grant.granted_by_principal, self.admin)

    def test_admin_cannot_issue_publish_even_with_malicious_post(self):
        account = ChannelAccount.objects.create(
            platform_code="pinterest",
            account_code="pinterest-us",
            external_account_ref="external-1",
            display_name="Pinterest US",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.client.force_login(self.admin)
        now = timezone.now()
        response = self.client.post(
            reverse("dashboard:team-grant-issue", args=[self.operator.pk]),
            {
                "scope_kind": PermissionGrant.ScopeKind.ACCOUNT,
                "channel_account": str(account.pk),
                "action": PermissionGrant.Action.PUBLISH,
                "effect": PermissionGrant.Effect.ALLOW,
                "risk_level": PermissionGrant.RiskLevel.HIGH,
                "valid_from": self._datetime_input(now),
                "valid_until": self._datetime_input(now + timedelta(days=10)),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(
            PermissionGrant.objects.filter(principal=self.operator, action=PermissionGrant.Action.PUBLISH).exists()
        )

    def test_owner_publish_is_exact_account_high_risk_and_expires(self):
        account = ChannelAccount.objects.create(
            platform_code="quora",
            account_code="quora-us",
            external_account_ref="external-2",
            display_name="Quora US",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.client.force_login(self.owner)
        now = timezone.now()
        response = self.client.post(
            reverse("dashboard:team-grant-issue", args=[self.operator.pk]),
            {
                "scope_kind": PermissionGrant.ScopeKind.ACCOUNT,
                "product": "",
                "platform_code": "",
                "channel_account": str(account.pk),
                "surface_ref": "",
                "action": PermissionGrant.Action.PUBLISH,
                "effect": PermissionGrant.Effect.ALLOW,
                "risk_level": PermissionGrant.RiskLevel.HIGH,
                "valid_from": self._datetime_input(now),
                "valid_until": self._datetime_input(now + timedelta(days=10)),
            },
        )
        self.assertEqual(response.status_code, 302)
        grant = PermissionGrant.objects.get(principal=self.operator, action=PermissionGrant.Action.PUBLISH)
        self.assertEqual(grant.scope_kind, PermissionGrant.ScopeKind.ACCOUNT)
        self.assertEqual(grant.account_ref, account.account_code)
        self.assertEqual(grant.risk_level, PermissionGrant.RiskLevel.HIGH)
        self.assertIsNotNone(grant.valid_until)

    def test_renew_creates_new_record_and_revoke_preserves_authority(self):
        original = self._grant(
            self.operator,
            PermissionGrant.Action.EDIT,
            actor=self.owner,
            valid_from=timezone.now() - timedelta(days=1),
            valid_until=timezone.now() + timedelta(days=1),
        )
        self.client.force_login(self.owner)
        new_start = timezone.now()
        response = self.client.post(
            reverse("dashboard:team-grant-renew", args=[original.pk]),
            {
                "valid_from": self._datetime_input(new_start),
                "valid_until": self._datetime_input(new_start + timedelta(days=30)),
            },
        )
        self.assertEqual(response.status_code, 302)
        renewed = PermissionGrant.objects.get(supersedes_grant=original)
        self.assertNotEqual(renewed.pk, original.pk)
        original.refresh_from_db()
        self.assertEqual(original.grant_status, PermissionGrant.GrantStatus.ACTIVE)

        response = self.client.post(
            reverse("dashboard:team-grant-revoke", args=[renewed.pk]),
            {"reason": "岗位职责已调整"},
        )
        self.assertEqual(response.status_code, 302)
        renewed.refresh_from_db()
        self.assertEqual(renewed.grant_status, PermissionGrant.GrantStatus.REVOKED)
        self.assertEqual(renewed.revoked_by_principal, self.owner)
        self.assertEqual(renewed.revocation_reason, "岗位职责已调整")
        self.assertEqual(renewed.action, PermissionGrant.Action.EDIT)

    def test_password_change_requires_current_password_and_invalidates_other_session(self):
        first = Client()
        other = Client()
        first.force_login(self.operator)
        other.force_login(self.operator)
        url = reverse("dashboard:change-my-password")

        wrong = first.post(
            url,
            {
                "current_password": "wrong-password",
                "new_password1": "NewStrongPassword456!",
                "new_password2": "NewStrongPassword456!",
            },
        )
        self.assertEqual(wrong.status_code, 200)
        self.operator.refresh_from_db()
        self.assertTrue(self.operator.check_password("StartPassword123!"))

        changed = first.post(
            url,
            {
                "current_password": "StartPassword123!",
                "new_password1": "NewStrongPassword456!",
                "new_password2": "NewStrongPassword456!",
            },
        )
        self.assertRedirects(changed, url)
        self.assertEqual(first.get(url).status_code, 200)
        self.assertRedirects(other.get(url), f"/accounts/login/?next={url}")
        self.operator.refresh_from_db()
        self.assertTrue(self.operator.check_password("NewStrongPassword456!"))
