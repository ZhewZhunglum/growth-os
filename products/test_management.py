import os
from io import StringIO
from unittest.mock import patch

from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

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
            "grants": 1,
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
        self.assertEqual(CapabilityState.objects.get().state, CapabilityState.State.OPEN)
        self.assertEqual(Task.objects.count(), 0)
        self.assertFalse(Principal.objects.filter(username__in=["operator", "reviewer", "publisher"]).exists())
        self.assertIn("No Task was created", self.stdout.getvalue())

    def test_full_demo_refuses_missing_human_passwords_without_partial_writes(self):
        self._owner()
        with patch.dict(os.environ, self._clear_password_env(), clear=True):
            with self.assertRaisesMessage(CommandError, "BOOTSTRAP_OPERATOR_PASSWORD"):
                call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)
        self.assertEqual(Principal.objects.count(), 1)
        self.assertFalse(Product.objects.exists())
        self.assertFalse(Principal.objects.filter(username="rule-evaluator").exists())

    def test_full_demo_creates_scoped_identities_and_grants_then_replays(self):
        self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_OPERATOR_PASSWORD": "operator-env-only-test-password",
            "BOOTSTRAP_REVIEWER_PASSWORD": "reviewer-env-only-test-password",
            "BOOTSTRAP_PUBLISHER_PASSWORD": "publisher-env-only-test-password",
        })
        with patch.dict(os.environ, environment, clear=True):
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)

        operator = Principal.objects.get(username="operator")
        reviewer = Principal.objects.get(username="reviewer")
        publisher = Principal.objects.get(username="publisher")
        evaluator = Principal.objects.get(username="rule-evaluator")
        product = Product.objects.get(product_code="PUKO")

        self.assertTrue(operator.check_password("operator-env-only-test-password"))
        self.assertTrue(reviewer.check_password("reviewer-env-only-test-password"))
        self.assertTrue(publisher.check_password("publisher-env-only-test-password"))
        self.assertFalse(evaluator.has_usable_password())
        self.assertEqual(reviewer.role, Principal.Role.OPERATIONS_ADMIN)
        self.assertEqual(evaluator.principal_type, Principal.PrincipalType.SERVICE_ACCOUNT)
        self.assertEqual(Principal.objects.count(), 5)
        self.assertEqual(PermissionGrant.objects.count(), 6)
        self.assertTrue(PermissionGrant.objects.filter(
            principal=operator, action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=reviewer, action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=reviewer, action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=publisher, action=PermissionGrant.Action.PUBLISH,
            scope_kind=PermissionGrant.ScopeKind.ACCOUNT, account_ref="puko-us",
        ).exists())
        self.assertTrue(PermissionGrant.objects.filter(
            principal=evaluator, action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=product,
        ).exists())
        self.assertEqual(Task.objects.count(), 0)

    def test_full_demo_reuses_existing_humans_without_password_environment(self):
        self._owner()
        Principal.objects.create_user(username="operator", password="existing-operator", role=Principal.Role.OPERATOR)
        Principal.objects.create_user(
            username="reviewer", password="existing-reviewer", role=Principal.Role.OPERATIONS_ADMIN,
        )
        Principal.objects.create_user(username="publisher", password="existing-publisher", role=Principal.Role.OPERATOR)
        evaluator = Principal(username="rule-evaluator", principal_type=Principal.PrincipalType.SERVICE_ACCOUNT)
        evaluator.set_unusable_password()
        evaluator.save()

        with patch.dict(os.environ, self._clear_password_env(), clear=True):
            call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)

        self.assertEqual(Principal.objects.count(), 5)
        self.assertEqual(PermissionGrant.objects.count(), 6)

    def test_new_human_password_values_must_be_distinct(self):
        self._owner()
        environment = self._clear_password_env()
        environment.update({
            "BOOTSTRAP_OPERATOR_PASSWORD": "shared-test-password",
            "BOOTSTRAP_REVIEWER_PASSWORD": "shared-test-password",
            "BOOTSTRAP_PUBLISHER_PASSWORD": "publisher-distinct-test-password",
        })
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesMessage(CommandError, "distinct password values"):
                call_command("bootstrap_dogfood", full_demo=True, stdout=self.stdout)
        self.assertEqual(Principal.objects.count(), 1)
        self.assertFalse(Product.objects.exists())
