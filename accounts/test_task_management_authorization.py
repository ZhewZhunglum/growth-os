from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from products.models import Product


TASK_MANAGEMENT_ACTIONS = (
    PermissionGrant.Action.CREATE_TASK,
    PermissionGrant.Action.ASSIGN_TASK,
    PermissionGrant.Action.CANCEL_TASK,
    PermissionGrant.Action.COMPLETE_TASK,
)


class TaskManagementAuthorizationTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="task-permission-owner",
            password="not-a-real-secret",
            role=Principal.Role.OWNER,
        )
        self.product = self._product("TASK-A")
        self.other_product = self._product("TASK-B")

    def _product(self, code: str) -> Product:
        return Product.objects.create(
            product_code=code,
            name=code,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )

    def _grant(
        self,
        *,
        action: str,
        scope_kind: str = PermissionGrant.ScopeKind.PRODUCT,
        product: Product | None = None,
        effect: str = PermissionGrant.Effect.ALLOW,
        valid_from=None,
        valid_until=None,
    ) -> PermissionGrant:
        return PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=scope_kind,
            product=(product or self.product) if scope_kind == PermissionGrant.ScopeKind.PRODUCT else None,
            action=action,
            effect=effect,
            risk_level=PermissionGrant.RiskLevel.MEDIUM,
            valid_from=valid_from or timezone.now() - timedelta(minutes=1),
            valid_until=valid_until or timezone.now() + timedelta(days=1),
            granted_by_principal=self.owner,
        )

    def _decision(self, action: str, *, product=None, at=None, acting_role=None):
        return resolve_authorization(
            principal=self.owner,
            acting_role=acting_role or Principal.Role.OWNER,
            action=action,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product or self.product,
            at=at,
        )

    def test_role_never_authorizes_task_management_without_explicit_grants(self):
        for action in TASK_MANAGEMENT_ACTIONS:
            with self.subTest(action=action):
                decision = self._decision(action)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "NO_ALLOW_GRANT")

    def test_each_task_management_action_resolves_from_its_own_grant(self):
        for action in TASK_MANAGEMENT_ACTIONS:
            grant = self._grant(action=action)
            with self.subTest(action=action):
                decision = self._decision(action)
                self.assertTrue(decision.allowed)
                self.assertEqual(decision.grant, grant)

    def test_grant_is_product_scoped(self):
        self._grant(action=PermissionGrant.Action.CREATE_TASK)

        allowed = self._decision(PermissionGrant.Action.CREATE_TASK)
        outside_scope = self._decision(
            PermissionGrant.Action.CREATE_TASK,
            product=self.other_product,
        )

        self.assertTrue(allowed.allowed)
        self.assertFalse(outside_scope.allowed)
        self.assertEqual(outside_scope.reason, "NO_ALLOW_GRANT")

    def test_applicable_deny_wins_over_allow(self):
        self._grant(action=PermissionGrant.Action.ASSIGN_TASK)
        deny = self._grant(
            action=PermissionGrant.Action.ASSIGN_TASK,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            effect=PermissionGrant.Effect.DENY,
        )

        decision = self._decision(PermissionGrant.Action.ASSIGN_TASK)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "DENY_GRANT")
        self.assertEqual(decision.denied_by, deny)

    def test_expired_and_future_grants_do_not_authorize(self):
        instant = timezone.now()
        self._grant(
            action=PermissionGrant.Action.CANCEL_TASK,
            valid_from=instant - timedelta(hours=2),
            valid_until=instant - timedelta(hours=1),
        )
        self._grant(
            action=PermissionGrant.Action.CANCEL_TASK,
            valid_from=instant + timedelta(hours=1),
            valid_until=instant + timedelta(hours=2),
        )

        decision = self._decision(PermissionGrant.Action.CANCEL_TASK, at=instant)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "NO_ALLOW_GRANT")

    def test_claimed_acting_role_must_equal_persisted_principal_role(self):
        self._grant(action=PermissionGrant.Action.COMPLETE_TASK)

        decision = self._decision(
            PermissionGrant.Action.COMPLETE_TASK,
            acting_role=Principal.Role.OPERATIONS_ADMIN,
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "ACTING_ROLE_MISMATCH")
