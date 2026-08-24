import os
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from accounts.models import PermissionGrant, Principal
from products.models import Product, ProductProfileVersion
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
    PolicyVersion,
    RuntimeEnvironment,
)
from workflow.models import Task, TaskContractPolicyLink, TaskContractVersion


PASSWORD_ENV_NAMES = {
    "BOOTSTRAP_OWNER_PASSWORD",
    "BOOTSTRAP_ADMIN_PASSWORD",
    "BOOTSTRAP_OPERATOR_PASSWORD",
    "BOOTSTRAP_REVIEWER_PASSWORD",
    "BOOTSTRAP_PUBLISHER_PASSWORD",
}


class BootstrapDogfoodTests(TestCase):
    def setUp(self):
        self.stdout = StringIO()

    def _owner(self):
        return Principal.objects.create_superuser(
            username="owner",
            password="existing-owner-test-password",
            role=Principal.Role.OWNER,
        )

    def _clear_password_env(self):
        clean = dict(os.environ)
        for name in PASSWORD_ENV_NAMES:
            clean.pop(name, None)
        return clean

    def test_requires_environment_password_when_owner_does_not_exist(self):
        with patch.dict(os.environ, self._clear_password_env(), clear=True):
            with self.assertRaisesMessage(CommandError, "BOOTSTRAP_OWNER_PASSWORD"):
                call_command("bootstrap_dogfood", stdout=self.stdout)
        self.assertFalse(Principal.objects.exists())
        self.assertFalse(Product.objects.exists())

    def test_new_owner_password_comes_from_environment(self):
        environment = self._clear_password_env()
        environment["BOOTSTRAP_OWNER_PASSWORD"] = "owner-env-only-test-password"
        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", stdout=self.stdout)
        owner = Principal.objects.get(username="owner")
        self.assertTrue(owner.check_password("owner-env-only-test-password"))
        self.assertEqual(owner.role, Principal.Role.OWNER)

    def test_reused_internal_human_without_usable_password_is_rejected(self):
        owner = Principal(
            username="owner",
            role=Principal.Role.OWNER,
            principal_type=Principal.PrincipalType.HUMAN_USER,
            is_staff=True,
            is_superuser=True,
        )
        owner.set_unusable_password()
        owner.save()

        with patch.dict(os.environ, self._clear_password_env(), clear=True):
            with self.assertRaisesMessage(CommandError, "has no usable password"):
                call_command("bootstrap_dogfood", stdout=self.stdout)

        self.assertFalse(Product.objects.exists())

    def test_local_six_character_password_is_accepted(self):
        environment = self._clear_password_env()
        environment["BOOTSTRAP_OWNER_PASSWORD"] = "Q7!zP2"

        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", stdout=self.stdout)

        self.assertTrue(Principal.objects.get(username="owner").check_password("Q7!zP2"))

    @override_settings(IS_LOCAL=False)
    def test_bootstrap_is_disabled_outside_local_without_writes(self):
        environment = self._clear_password_env()
        environment["BOOTSTRAP_OWNER_PASSWORD"] = "owner-env-only-test-password"

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesMessage(CommandError, "local-only fixture command"):
                call_command("bootstrap_dogfood", stdout=self.stdout)

        self.assertFalse(Principal.objects.exists())
        self.assertFalse(Product.objects.exists())

    def test_password_shorter_than_local_minimum_is_rejected_without_writes(self):
        environment = self._clear_password_env()
        environment["BOOTSTRAP_OWNER_PASSWORD"] = "Q7!zP"

        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesMessage(
                CommandError,
                "BOOTSTRAP_OWNER_PASSWORD does not satisfy the configured password policy",
            ):
                call_command("bootstrap_dogfood", stdout=self.stdout)

        self.assertFalse(Principal.objects.exists())
        self.assertFalse(Product.objects.exists())

    def test_default_builds_complete_base_context_idempotently_without_staff_or_task(self):
        owner = self._owner()

        call_command("bootstrap_dogfood", stdout=self.stdout)
        first_counts = {
            "principals": Principal.objects.count(),
            "products": Product.objects.count(),
            "profiles": ProductProfileVersion.objects.count(),
            "policies": PolicyDefinition.objects.count(),
            "policy_versions": PolicyVersion.objects.count(),
            "contracts": TaskContractVersion.objects.count(),
            "policy_links": TaskContractPolicyLink.objects.count(),
            "grants": PermissionGrant.objects.count(),
            "accounts": ChannelAccount.objects.count(),
            "environments": RuntimeEnvironment.objects.count(),
            "bindings": AccountEnvironmentBinding.objects.count(),
            "capabilities": CapabilityState.objects.count(),
        }
        call_command("bootstrap_dogfood", stdout=self.stdout)

        self.assertEqual(first_counts, {
            "principals": 1,
            "products": 1,
            "profiles": 1,
            "policies": 1,
            "policy_versions": 1,
            "contracts": 1,
            "policy_links": 1,
            "grants": 19,
            "accounts": 1,
            "environments": 1,
            "bindings": 1,
            "capabilities": 1,
        })
        second_counts = {
            "principals": Principal.objects.count(),
            "products": Product.objects.count(),
            "profiles": ProductProfileVersion.objects.count(),
            "policies": PolicyDefinition.objects.count(),
            "policy_versions": PolicyVersion.objects.count(),
            "contracts": TaskContractVersion.objects.count(),
            "policy_links": TaskContractPolicyLink.objects.count(),
            "grants": PermissionGrant.objects.count(),
            "accounts": ChannelAccount.objects.count(),
            "environments": RuntimeEnvironment.objects.count(),
            "bindings": AccountEnvironmentBinding.objects.count(),
            "capabilities": CapabilityState.objects.count(),
        }
        self.assertEqual(first_counts, second_counts)
        product = Product.objects.get(product_code="PUKO")
        self.assertTrue(product.current_profile_version.is_sealed)
        self.assertTrue(owner.groups.filter(name="Owner").exists())
        self.assertTrue(Group.objects.filter(name="Operations Admin").exists())
        self.assertSetEqual(
            set(
                PermissionGrant.objects.filter(
                    principal=owner,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                ).values_list("action", flat=True)
            ),
            {
                PermissionGrant.Action.VIEW,
                PermissionGrant.Action.EDIT,
                PermissionGrant.Action.COLLECT_READ_ONLY,
                PermissionGrant.Action.CREATE_TASK,
                PermissionGrant.Action.ASSIGN_TASK,
                PermissionGrant.Action.CANCEL_TASK,
                PermissionGrant.Action.COMPLETE_TASK,
                PermissionGrant.Action.REVIEW,
            },
        )
        self.assertEqual(CapabilityState.objects.get().state, CapabilityState.State.OPEN)
        self.assertEqual(Task.objects.count(), 0)
        self.assertFalse(Principal.objects.filter(username__in=["admin", "operator", "reviewer", "publisher"]).exists())
        self.assertIn("No Task was created", self.stdout.getvalue())

    def test_replay_preserves_existing_current_sealed_profile_version(self):
        owner = self._owner()
        call_command("bootstrap_dogfood", stdout=self.stdout)
        product = Product.objects.get(product_code="PUKO")
        profile_v2 = ProductProfileVersion.objects.create(
            product=product,
            version_number=2,
            market_code="US",
            language_code="en",
            audience={"business_mode": "B2C", "revision": 2},
            core_value_proposition="A deliberately newer sealed profile.",
            brand_voice={"tone": ["clear", "measured"]},
            product_facts={"pilot": "PUKO", "revision": 2},
            prohibited_expressions=["cure"],
            objective_profile_key="VISIBILITY_AND_SEO_PROOF",
            created_by_principal=owner,
        )
        profile_v2.seal(owner)
        product.current_profile_version = profile_v2
        product.updated_by_principal = owner
        product.full_clean()
        product.save(update_fields=["current_profile_version", "updated_by_principal", "updated_at"])

        self.stdout = StringIO()
        call_command("bootstrap_dogfood", stdout=self.stdout)

        product.refresh_from_db()
        self.assertEqual(product.current_profile_version_id, profile_v2.id)
        self.assertTrue(product.current_profile_version.is_sealed)
        self.assertIn("Preserved existing current Product profile v2 (sealed)", self.stdout.getvalue())
        self.assertIn("current profile v2", self.stdout.getvalue())

    def test_full_demo_refuses_missing_human_passwords_without_partial_writes(self):
        self._owner()
        with patch.dict(os.environ, self._clear_password_env(), clear=True):
            with self.assertRaisesMessage(CommandError, "BOOTSTRAP_ADMIN_PASSWORD"):
                call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)
        self.assertEqual(Principal.objects.count(), 1)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(Principal.objects.filter(username="rule-evaluator").exists())

    def test_full_demo_creates_scoped_identities_and_grants_then_replays(self):
        owner = self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_ADMIN_PASSWORD": "admin-env-only-test-password",
            "BOOTSTRAP_OPERATOR_PASSWORD": "operator-env-only-test-password",
        })
        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)

        operator = Principal.objects.get(username="operator")
        admin = Principal.objects.get(username="admin")
        evaluator = Principal.objects.get(username="rule-evaluator")
        product = Product.objects.get(product_code="PUKO")

        self.assertTrue(operator.check_password("operator-env-only-test-password"))
        self.assertTrue(admin.check_password("admin-env-only-test-password"))
        self.assertFalse(evaluator.has_usable_password())
        self.assertEqual(admin.role, Principal.Role.OPERATIONS_ADMIN)
        self.assertEqual(evaluator.principal_type, Principal.PrincipalType.SERVICE_ACCOUNT)
        self.assertEqual(Principal.objects.count(), 4)
        self.assertEqual(PermissionGrant.objects.count(), 54)
        self.assertFalse(Principal.objects.filter(username__in=["reviewer", "publisher"]).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=operator, action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=admin, action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=owner, action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        for principal in (owner, admin, operator):
            grant = PermissionGrant.objects.get(
                principal=principal,
                action=PermissionGrant.Action.PUBLISH,
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                account_ref="puko-us",
                product__isnull=True,
            )
            self.assertEqual(grant.risk_level, PermissionGrant.RiskLevel.HIGH)
            self.assertEqual(grant.granted_by_principal, owner)
            self.assertIsNotNone(grant.valid_until)
        self.assertSetEqual(
            set(
                PermissionGrant.objects.filter(
                    principal=admin,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                ).values_list("action", flat=True)
            ),
            {
                PermissionGrant.Action.VIEW,
                PermissionGrant.Action.EDIT,
                PermissionGrant.Action.COLLECT_READ_ONLY,
                PermissionGrant.Action.CREATE_TASK,
                PermissionGrant.Action.ASSIGN_TASK,
                PermissionGrant.Action.CANCEL_TASK,
                PermissionGrant.Action.COMPLETE_TASK,
                PermissionGrant.Action.REVIEW,
            },
        )
        self.assertSetEqual(
            set(
                PermissionGrant.objects.filter(
                    principal=operator,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=product,
                ).values_list("action", flat=True)
            ),
            {
                PermissionGrant.Action.VIEW,
                PermissionGrant.Action.EDIT,
                PermissionGrant.Action.COLLECT_READ_ONLY,
            },
        )
        self.assertTrue(PermissionGrant.objects.filter(
            principal=evaluator, action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertEqual(Task.objects.count(), 0)

    def test_full_demo_reuses_existing_humans_without_password_environment(self):
        self._owner()
        Principal.objects.create_user(username="operator", password="existing-operator", role=Principal.Role.OPERATOR)
        Principal.objects.create_user(
            username="admin", password="existing-admin", role=Principal.Role.OPERATIONS_ADMIN,
        )
        evaluator = Principal(username="rule-evaluator", principal_type=Principal.PrincipalType.SERVICE_ACCOUNT)
        evaluator.set_unusable_password()
        evaluator.save()

        with patch.dict(os.environ, self._clear_password_env(), clear=True):
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)

        self.assertEqual(Principal.objects.count(), 4)
        self.assertEqual(PermissionGrant.objects.count(), 54)

    def test_full_demo_accepts_legacy_reviewer_password_env_for_new_admin(self):
        self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_OPERATOR_PASSWORD": "operator-env-only-test-password",
            "BOOTSTRAP_REVIEWER_PASSWORD": "legacy-reviewer-env-test-password",
        })

        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)

        admin = Principal.objects.get(username="admin")
        self.assertTrue(admin.check_password("legacy-reviewer-env-test-password"))

    def test_strict_separation_demo_preserves_legacy_capability_identities(self):
        self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_OPERATOR_PASSWORD": "operator-env-only-test-password",
            "BOOTSTRAP_REVIEWER_PASSWORD": "reviewer-env-only-test-password",
            "BOOTSTRAP_PUBLISHER_PASSWORD": "publisher-env-only-test-password",
        })

        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", strict_separation_demo=True, stdout=self.stdout)
            call_command("bootstrap_dogfood", strict_separation_demo=True, stdout=self.stdout)

        operator = Principal.objects.get(username="operator")
        reviewer = Principal.objects.get(username="reviewer")
        publisher = Principal.objects.get(username="publisher")
        product = Product.objects.get(product_code="PUKO")

        self.assertEqual(reviewer.role, Principal.Role.OPERATIONS_ADMIN)
        self.assertEqual(publisher.role, Principal.Role.OPERATOR)
        self.assertEqual(Principal.objects.count(), 5)
        self.assertEqual(PermissionGrant.objects.count(), 35)
        self.assertTrue(PermissionGrant.objects.filter(
            principal=reviewer,
            action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product,
        ).exists())
        self.assertFalse(PermissionGrant.objects.filter(
            principal=reviewer,
            action__in=[
                PermissionGrant.Action.CREATE_TASK,
                PermissionGrant.Action.ASSIGN_TASK,
                PermissionGrant.Action.CANCEL_TASK,
                PermissionGrant.Action.COMPLETE_TASK,
            ],
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=publisher,
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us",
            risk_level=PermissionGrant.RiskLevel.HIGH,
        ).exists())
        self.assertFalse(PermissionGrant.objects.filter(
            principal=publisher,
            action=PermissionGrant.Action.EDIT,
        ).exists())
        self.assertFalse(PermissionGrant.objects.filter(
            principal=operator,
            action=PermissionGrant.Action.PUBLISH,
        ).exists())

    def test_streamlined_demo_adds_operator_capability_without_rewriting_legacy_publisher(self):
        self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_ADMIN_PASSWORD": "admin-env-only-test-password",
            "BOOTSTRAP_OPERATOR_PASSWORD": "operator-env-only-test-password",
            "BOOTSTRAP_REVIEWER_PASSWORD": "reviewer-env-only-test-password",
            "BOOTSTRAP_PUBLISHER_PASSWORD": "publisher-env-only-test-password",
        })

        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", strict_separation_demo=True, stdout=self.stdout)
            legacy_publisher = Principal.objects.get(username="publisher")
            legacy_grant = PermissionGrant.objects.get(
                principal=legacy_publisher,
                action=PermissionGrant.Action.PUBLISH,
                scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                account_ref="puko-us",
            )
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)

        legacy_grant.refresh_from_db()
        self.assertEqual(legacy_grant.principal, legacy_publisher)
        self.assertEqual(legacy_grant.grant_status, PermissionGrant.GrantStatus.ACTIVE)
        self.assertTrue(Principal.objects.filter(username="reviewer").exists())
        self.assertTrue(Principal.objects.filter(username="publisher").exists())
        self.assertTrue(Principal.objects.filter(username="admin").exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal__username="operator",
            action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us",
            risk_level=PermissionGrant.RiskLevel.HIGH,
        ).exists())
        self.assertSetEqual(
            set(
                PermissionGrant.objects.filter(
                    principal__username__in=["owner", "admin", "operator"],
                    action=PermissionGrant.Action.PUBLISH,
                    scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
                    account_ref="puko-us",
                    risk_level=PermissionGrant.RiskLevel.HIGH,
                ).values_list("principal__username", flat=True)
            ),
            {"owner", "admin", "operator"},
        )

    def test_new_human_password_values_must_be_distinct(self):
        self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_OPERATOR_PASSWORD": "shared-test-password",
            "BOOTSTRAP_ADMIN_PASSWORD": "shared-test-password",
        })
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesMessage(CommandError, "distinct password values"):
                call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)
        self.assertEqual(Principal.objects.count(), 1)
        self.assertFalse(Product.objects.exists())
