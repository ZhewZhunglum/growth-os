from datetime import timedelta

from django.contrib.auth import authenticate
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from accounts.authorization import require_authorization, resolve_authorization
from products.models import Product


class PrincipalTests(TestCase):
    def test_decommissioning_disables_login(self):
        principal = Principal.objects.create_user(username="operator", password="not-a-real-secret")
        principal.principal_status = Principal.PrincipalStatus.DECOMMISSIONED
        principal.save()
        self.assertFalse(principal.is_active)
        self.assertIsNotNone(principal.decommissioned_at)

    def test_only_active_status_can_authenticate(self):
        active = Principal.objects.create_user(username="active", password="valid-test-password")
        self.assertIsNotNone(authenticate(username=active.username, password="valid-test-password"))

        for status in (
            Principal.PrincipalStatus.SUSPENDED,
            Principal.PrincipalStatus.LOCKED,
            Principal.PrincipalStatus.DECOMMISSIONED,
        ):
            username = status.lower()
            principal = Principal.objects.create_user(
                username=username,
                password="valid-test-password",
                principal_status=status,
            )
            principal.refresh_from_db()
            self.assertFalse(principal.is_active)
            self.assertIsNone(authenticate(username=username, password="valid-test-password"))

    def test_non_human_principal_cannot_use_interactive_password_login(self):
        for principal_type in (
            Principal.PrincipalType.SERVICE_ACCOUNT,
            Principal.PrincipalType.API_CLIENT,
            Principal.PrincipalType.SYSTEM,
        ):
            with self.subTest(principal_type=principal_type):
                username = f"non-human-{principal_type.lower()}"
                principal = Principal.objects.create_user(
                    username=username,
                    password="valid-test-password",
                    principal_type=principal_type,
                )
                self.assertTrue(principal.is_active)
                self.assertTrue(principal.has_usable_password())
                self.assertIsNone(
                    authenticate(username=username, password="valid-test-password")
                )

    def test_status_only_update_also_disables_login_flag(self):
        principal = Principal.objects.create_user(username="locked-later", password="valid-test-password")
        principal.principal_status = Principal.PrincipalStatus.LOCKED
        principal.save(update_fields=["principal_status"])
        principal.refresh_from_db()
        self.assertFalse(principal.is_active)


class PermissionGrantTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="owner", password="not-a-real-secret", role=Principal.Role.OWNER,
        )
        self.operator = Principal.objects.create_user(
            username="operator", password="not-a-real-secret", role=Principal.Role.OPERATOR,
        )
        self.product = Product.objects.create(
            product_code="PUKO_MAG", name="PUKO Magnesium", market_code="US", language_code="en",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )

    def test_explicit_product_scope_is_current(self):
        grant = PermissionGrant.objects.create(
            principal=self.operator, scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=self.product,
            action=PermissionGrant.Action.EDIT, risk_level=PermissionGrant.RiskLevel.MEDIUM,
            valid_from=timezone.now() - timedelta(minutes=1), valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        self.assertTrue(grant.is_current)

    def test_invalid_implicit_scope_is_rejected_before_database_write(self):
        with self.assertRaises(ValidationError):
            PermissionGrant.objects.create(
                principal=self.operator, scope_kind=PermissionGrant.ScopeKind.GLOBAL, product=self.product,
                action=PermissionGrant.Action.EDIT, valid_from=timezone.now(), granted_by_principal=self.owner,
            )

    def _grant(self, *, scope_kind, action=PermissionGrant.Action.PUBLISH, effect=PermissionGrant.Effect.ALLOW, **scope):
        return PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=scope_kind,
            action=action,
            effect=effect,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
            **scope,
        )

    def _revoke(self, grant):
        grant.grant_status = PermissionGrant.GrantStatus.REVOKED
        grant.revoked_at = timezone.now()
        grant.revoked_by_principal = self.owner
        grant.revocation_reason = "Authorization test moved to the next exact scope."
        grant.save(
            update_fields=[
                "grant_status",
                "revoked_at",
                "revoked_by_principal",
                "revocation_reason",
                "updated_at",
            ]
        )

    def test_account_and_surface_scopes_are_exact(self):
        account_grant = self._grant(
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="tiktok:account:puko-us",
        )
        surface_grant = self._grant(
            scope_kind=PermissionGrant.ScopeKind.SURFACE,
            action=PermissionGrant.Action.EDIT,
            surface_ref="tiktok:surface:profile",
        )

        account = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="tiktok:account:puko-us",
        )
        wrong_account = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="tiktok:account:other",
        )
        surface = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.SURFACE,
            surface_ref="tiktok:surface:profile",
        )

        self.assertTrue(account.allowed)
        self.assertEqual(account.grant, account_grant)
        self.assertFalse(wrong_account.allowed)
        self.assertEqual(wrong_account.reason, "NO_ALLOW_GRANT")
        self.assertTrue(surface.allowed)
        self.assertEqual(surface.grant, surface_grant)

    def test_any_applicable_deny_wins_over_allow(self):
        self._grant(scope_kind=PermissionGrant.ScopeKind.GLOBAL)
        deny = self._grant(
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="tiktok:account:puko-us",
            effect=PermissionGrant.Effect.DENY,
        )

        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="tiktok:account:puko-us",
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "DENY_GRANT")
        self.assertEqual(decision.denied_by, deny)

    def test_account_context_applies_matching_product_and_platform_denies(self):
        account_allow = self._grant(
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us",
        )
        product_deny = self._grant(
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            effect=PermissionGrant.Effect.DENY,
        )
        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            product=self.product,
            platform_code="TIKTOK",
            account_ref="puko-us",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denied_by, product_deny)

        self._revoke(product_deny)
        platform_deny = self._grant(
            scope_kind=PermissionGrant.ScopeKind.PLATFORM,
            platform_code="TIKTOK",
            effect=PermissionGrant.Effect.DENY,
        )
        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            product=self.product,
            platform_code="TIKTOK",
            account_ref="puko-us",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denied_by, platform_deny)

        self._revoke(platform_deny)
        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            product=self.product,
            platform_code="TIKTOK",
            account_ref="puko-us",
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.grant, account_allow)

    def test_allow_uses_most_specific_matching_scope(self):
        self._grant(scope_kind=PermissionGrant.ScopeKind.GLOBAL)
        self._grant(scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=self.product)
        self._grant(scope_kind=PermissionGrant.ScopeKind.PLATFORM, platform_code="TIKTOK")
        account_allow = self._grant(
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us",
        )
        surface_allow = self._grant(
            scope_kind=PermissionGrant.ScopeKind.SURFACE,
            surface_ref="creator-center",
        )

        account = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            product=self.product,
            platform_code="TIKTOK",
            account_ref="puko-us",
        )
        surface = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.SURFACE,
            product=self.product,
            platform_code="TIKTOK",
            account_ref="puko-us",
            surface_ref="creator-center",
        )

        self.assertEqual(account.grant, account_allow)
        self.assertEqual(surface.grant, surface_allow)

    def test_surface_context_applies_all_matching_ancestor_denies(self):
        self._grant(
            scope_kind=PermissionGrant.ScopeKind.SURFACE,
            action=PermissionGrant.Action.EDIT,
            surface_ref="creator-center",
        )
        account_deny = self._grant(
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            action=PermissionGrant.Action.EDIT,
            account_ref="puko-us",
            effect=PermissionGrant.Effect.DENY,
        )

        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.SURFACE,
            product=self.product,
            platform_code="TIKTOK",
            account_ref="puko-us",
            surface_ref="creator-center",
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.denied_by, account_deny)

    def test_legacy_single_scope_calls_remain_supported(self):
        product_allow = self._grant(
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            action=PermissionGrant.Action.EDIT,
            product=self.product,
        )
        platform_allow = self._grant(
            scope_kind=PermissionGrant.ScopeKind.PLATFORM,
            action=PermissionGrant.Action.REVIEW,
            platform_code="TIKTOK",
        )
        account_allow = self._grant(
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us",
        )

        product = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        platform = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PLATFORM,
            platform_code="TIKTOK",
        )
        account = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us",
        )

        self.assertEqual(product.grant, product_allow)
        self.assertEqual(platform.grant, platform_allow)
        self.assertEqual(account.grant, account_allow)

    def test_claimed_acting_role_is_checked_against_persisted_role(self):
        self._grant(scope_kind=PermissionGrant.ScopeKind.GLOBAL)

        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OWNER,
            action=PermissionGrant.Action.PUBLISH,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ACTING_ROLE_MISMATCH")
        with self.assertRaises(PermissionDenied):
            require_authorization(
                principal=self.operator,
                acting_role=Principal.Role.OWNER,
                action=PermissionGrant.Action.PUBLISH,
            )

    def test_inactive_principal_is_denied_even_with_allow(self):
        self._grant(scope_kind=PermissionGrant.ScopeKind.GLOBAL)
        self.operator.principal_status = Principal.PrincipalStatus.SUSPENDED
        self.operator.save()

        decision = resolve_authorization(
            principal=self.operator,
            acting_role=Principal.Role.OPERATOR,
            action=PermissionGrant.Action.PUBLISH,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "INACTIVE_PRINCIPAL")
