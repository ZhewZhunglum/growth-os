from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal, SecretReference
from accounts.services import GrantScope, issue_permission_grant, renew_permission_grant, revoke_permission_grant
from releasegate.models import ChannelAccount


class PermissionGrantLifecycleTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="grant-owner",
            password="local-test-password",
            role=Principal.Role.OWNER,
        )
        self.admin = Principal.objects.create_user(
            username="grant-admin",
            password="local-test-password",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.operator = Principal.objects.create_user(
            username="grant-operator",
            password="local-test-password",
            role=Principal.Role.OPERATOR,
        )
        self.now = timezone.now()
        self.owner_manage = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.CRITICAL,
            valid_from=self.now - timedelta(minutes=1),
            valid_until=self.now + timedelta(days=30),
            granted_by_principal=self.owner,
        )
        self.admin_manage = PermissionGrant.objects.create(
            principal=self.admin,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=self.now - timedelta(minutes=1),
            valid_until=self.now + timedelta(days=30),
            granted_by_principal=self.owner,
        )

    def test_owner_can_issue_exact_account_publish_grant(self):
        ChannelAccount.objects.create(
            platform_code="pinterest",
            account_code="channel-account-1",
            external_account_ref="pinterest-test-account",
            display_name="Pinterest test account",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        grant = issue_permission_grant(
            actor=self.owner,
            principal=self.operator,
            scope=GrantScope(
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                account_ref="channel-account-1",
            ),
            action=PermissionGrant.Action.PUBLISH,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=self.now,
            valid_until=self.now + timedelta(hours=8),
        )

        self.assertEqual(grant.account_ref, "channel-account-1")
        self.assertEqual(grant.granted_by_principal, self.owner)

    def test_publish_grant_rejects_unknown_or_inactive_account_reference(self):
        account = ChannelAccount.objects.create(
            platform_code="pinterest",
            account_code="retired-account",
            external_account_ref="retired-pinterest-account",
            display_name="Retired account",
            status=ChannelAccount.Status.RETIRED,
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        for account_ref in ("made-up-account", account.account_code):
            with self.subTest(account_ref=account_ref), self.assertRaises(ValidationError):
                issue_permission_grant(
                    actor=self.owner,
                    principal=self.operator,
                    scope=GrantScope(
                        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                        account_ref=account_ref,
                    ),
                    action=PermissionGrant.Action.PUBLISH,
                    effect=PermissionGrant.Effect.ALLOW,
                    risk_level=PermissionGrant.RiskLevel.HIGH,
                    valid_from=self.now,
                    valid_until=self.now + timedelta(hours=8),
                )

    def test_admin_cannot_issue_publish_or_manage_account(self):
        for action in (PermissionGrant.Action.PUBLISH, PermissionGrant.Action.MANAGE_ACCOUNT):
            with self.subTest(action=action), self.assertRaises(PermissionDenied):
                issue_permission_grant(
                    actor=self.admin,
                    principal=self.operator,
                    scope=GrantScope(scope_kind=PermissionGrant.ScopeKind.GLOBAL),
                    action=action,
                    effect=PermissionGrant.Effect.ALLOW,
                    risk_level=PermissionGrant.RiskLevel.HIGH,
                    valid_from=self.now,
                    valid_until=self.now + timedelta(hours=1),
                )

    def test_admin_cannot_renew_or_revoke_an_existing_publish_grant(self):
        account = ChannelAccount.objects.create(
            platform_code="pinterest",
            account_code="admin-boundary-account",
            external_account_ref="pinterest-admin-boundary",
            display_name="Admin boundary account",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        grant = issue_permission_grant(
            actor=self.owner,
            principal=self.admin,
            scope=GrantScope(
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                account_ref=account.account_code,
            ),
            action=PermissionGrant.Action.PUBLISH,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=self.now,
            valid_until=self.now + timedelta(hours=8),
        )

        with self.assertRaises(PermissionDenied):
            renew_permission_grant(
                actor=self.admin,
                grant_id=grant.pk,
                valid_from=self.now + timedelta(hours=8),
                valid_until=self.now + timedelta(hours=16),
            )
        with self.assertRaises(PermissionDenied):
            revoke_permission_grant(
                actor=self.admin,
                grant_id=grant.pk,
                reason="Admin must not control PUBLISH grants.",
            )

        grant.refresh_from_db()
        self.assertEqual(grant.grant_status, PermissionGrant.GrantStatus.ACTIVE)
        self.assertFalse(PermissionGrant.objects.filter(supersedes_grant=grant).exists())

    def test_renewal_creates_a_new_exact_grant(self):
        original = issue_permission_grant(
            actor=self.owner,
            principal=self.operator,
            scope=GrantScope(scope_kind=PermissionGrant.ScopeKind.PLATFORM, platform_code="pinterest"),
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.LOW,
            valid_from=self.now,
            valid_until=self.now + timedelta(days=1),
        )

        renewed = renew_permission_grant(
            actor=self.owner,
            grant_id=original.pk,
            valid_from=self.now + timedelta(days=1),
            valid_until=self.now + timedelta(days=31),
        )

        self.assertNotEqual(renewed.pk, original.pk)
        self.assertEqual(renewed.supersedes_grant, original)
        self.assertEqual(renewed.action, original.action)
        original.refresh_from_db()
        self.assertEqual(original.grant_status, PermissionGrant.GrantStatus.ACTIVE)

    def test_revocation_is_one_way_and_retains_authority(self):
        grant = issue_permission_grant(
            actor=self.owner,
            principal=self.operator,
            scope=GrantScope(scope_kind=PermissionGrant.ScopeKind.PLATFORM, platform_code="quora"),
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.LOW,
            valid_from=self.now,
            valid_until=self.now + timedelta(days=1),
        )

        revoked = revoke_permission_grant(
            actor=self.owner,
            grant_id=grant.pk,
            reason="Access no longer required.",
        )
        self.assertEqual(revoked.grant_status, PermissionGrant.GrantStatus.REVOKED)
        self.assertEqual(revoked.revoked_by_principal, self.owner)
        self.assertTrue(revoked.revoked_at)

        with self.assertRaises(ValidationError):
            revoke_permission_grant(
                actor=self.owner,
                grant_id=grant.pk,
                reason="Duplicate revocation.",
            )

    def test_authority_fields_cannot_be_edited(self):
        grant = issue_permission_grant(
            actor=self.owner,
            principal=self.operator,
            scope=GrantScope(scope_kind=PermissionGrant.ScopeKind.PLATFORM, platform_code="ga4"),
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.LOW,
            valid_from=self.now,
            valid_until=self.now + timedelta(days=1),
        )
        grant.platform_code = "gsc"
        with self.assertRaises(ValidationError):
            grant.save(update_fields=["platform_code", "updated_at"])


class SecretReferenceTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="secret-owner",
            password="local-test-password",
            role=Principal.Role.OWNER,
        )

    def test_reference_stores_metadata_only(self):
        reference = SecretReference(
            secret_key="DEEPSEEK_API_KEY",
            provider_code="deepseek",
            backend=SecretReference.Backend.FILE_MOUNT,
            reference_name="DEEPSEEK_API_KEY",
            environment_scope=SecretReference.EnvironmentScope.STAGING,
            purpose="Daily Operations AI provider",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        reference.full_clean()

    def test_file_mount_reference_rejects_file_suffix(self):
        reference = SecretReference(
            secret_key="DEEPSEEK_API_KEY",
            provider_code="deepseek",
            backend=SecretReference.Backend.FILE_MOUNT,
            reference_name="DEEPSEEK_API_KEY_FILE",
            environment_scope=SecretReference.EnvironmentScope.STAGING,
            purpose="Invalid double-suffix reference",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        with self.assertRaises(ValidationError):
            reference.full_clean()

    def test_obvious_secret_material_is_rejected(self):
        reference = SecretReference(
            secret_key="BAD",
            provider_code="deepseek",
            backend=SecretReference.Backend.FILE_MOUNT,
            reference_name="sk-not-a-reference",
            environment_scope=SecretReference.EnvironmentScope.LOCAL,
            purpose="Invalid test",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        with self.assertRaises(ValidationError):
            reference.full_clean()
