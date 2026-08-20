from __future__ import annotations

import os
from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from products.models import Product, ProductProfileVersion
from releasegate.models import (
    AccountEnvironmentBinding,
    CapabilityState,
    ChannelAccount,
    RuntimeEnvironment,
)
from workflow.models import TaskContractVersion


PASSWORD_ENV_NAMES = {
    "STAGING_OWNER_PASSWORD",
    "STAGING_ADMIN_PASSWORD",
    "STAGING_OPERATOR_PASSWORD",
}

FIRST_PASSWORDS = {
    "STAGING_OWNER_PASSWORD": "OwNer!Stage#93Alpha",
    "STAGING_ADMIN_PASSWORD": "Admin!Stage#84Beta",
    "STAGING_OPERATOR_PASSWORD": "Oper!Stage#75Gamma",
}

SECOND_PASSWORDS = {
    "STAGING_OWNER_PASSWORD": "Changed!Owner#47Delta",
    "STAGING_ADMIN_PASSWORD": "Changed!Admin#58Epsilon",
    "STAGING_OPERATOR_PASSWORD": "Changed!Oper#69Zeta",
}


@override_settings(ENVIRONMENT="staging", PASSWORD_MIN_LENGTH=12)
class ProvisionStagingStaffTests(TestCase):
    def setUp(self):
        self.stdout = StringIO()
        self.seed = Principal.objects.create_user(
            username="provisioning-context-seed",
            password="Context!Seed#28Alpha",
            role=Principal.Role.OPERATOR,
            principal_type=Principal.PrincipalType.HUMAN_USER,
        )
        self.product = Product.objects.create(
            product_code="PUKO",
            name="PUKO Nutrition",
            market_code="US",
            language_code="en",
            product_status=Product.ProductStatus.ACTIVE,
            created_by_principal=self.seed,
            updated_by_principal=self.seed,
        )
        self.profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"market": "US"},
            core_value_proposition="Frozen Staging product context.",
            brand_voice={"tone": ["clear"]},
            product_facts={"product": "PUKO"},
            prohibited_expressions=["cure"],
            created_by_principal=self.seed,
        )
        self.profile.seal(self.seed)
        self.product.current_profile_version = self.profile
        self.product.updated_by_principal = self.seed
        self.product.full_clean()
        self.product.save(update_fields=["current_profile_version", "updated_by_principal", "updated_at"])
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile,
            version_number=1,
            title="Staging V1 contract",
            dor_criteria=[{"key": "brief_ready"}],
            dod_criteria=[{"key": "asset_ready"}],
            release_gate_criteria=[{"key": "human_review"}],
            success_criteria=[{"key": "manual_publish_recorded"}],
            sealed_at=timezone.now(),
            created_by_principal=self.seed,
        )

    @staticmethod
    def _environment(passwords=None):
        environment = dict(os.environ)
        for name in PASSWORD_ENV_NAMES:
            environment.pop(name, None)
        environment.update(passwords or {})
        return environment

    def _call(self, *, passwords=FIRST_PASSWORDS, apply=True, **options):
        with patch.dict(os.environ, self._environment(passwords), clear=True):
            call_command("provision_staging_staff", stdout=self.stdout, apply=apply, **options)

    def _add_publish_context(self):
        channel = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="puko-us",
            external_account_ref="staging:puko-us",
            display_name="PUKO US Staging",
            status=ChannelAccount.Status.ACTIVE,
            created_by_principal=self.seed,
            updated_by_principal=self.seed,
        )
        environment = RuntimeEnvironment.objects.create(
            environment_code="staging-primary",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="staging-identities",
            database_namespace="staging-database",
            object_storage_namespace="staging-cos-prefix",
            status=RuntimeEnvironment.Status.ACTIVE,
            created_by_principal=self.seed,
            updated_by_principal=self.seed,
        )
        binding = AccountEnvironmentBinding.objects.create(
            channel_account=channel,
            runtime_environment=environment,
            binding_version=1,
            identity_reference="staging:manual-publisher:puko-us",
            created_by_principal=self.seed,
            recorded_by_principal=self.seed,
        )
        capability = CapabilityState.objects.create(
            account_environment_binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            reason="Staging manual-publish acceptance testing.",
            created_by_principal=self.seed,
            recorded_by_principal=self.seed,
        )
        return channel, binding, capability

    def test_staging_creates_three_non_staff_principals_and_exact_product_grants(self):
        self._call()

        owner = Principal.objects.get(username="owner")
        admin = Principal.objects.get(username="admin")
        operator = Principal.objects.get(username="operator")
        self.assertEqual(owner.role, Principal.Role.OWNER)
        self.assertEqual(admin.role, Principal.Role.OPERATIONS_ADMIN)
        self.assertEqual(operator.role, Principal.Role.OPERATOR)
        for principal in (owner, admin, operator):
            self.assertFalse(principal.is_staff)
            self.assertFalse(principal.is_superuser)
            self.assertEqual(principal.principal_type, Principal.PrincipalType.HUMAN_USER)
            self.assertEqual(principal.principal_status, Principal.PrincipalStatus.ACTIVE)

        management_actions = {
            PermissionGrant.Action.EDIT,
            PermissionGrant.Action.CREATE_TASK,
            PermissionGrant.Action.ASSIGN_TASK,
            PermissionGrant.Action.CANCEL_TASK,
            PermissionGrant.Action.COMPLETE_TASK,
            PermissionGrant.Action.REVIEW,
        }
        for principal in (owner, admin):
            self.assertSetEqual(
                set(
                    PermissionGrant.objects.filter(
                        principal=principal,
                        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                        product=self.product,
                    ).values_list("action", flat=True)
                ),
                management_actions,
            )
        self.assertSetEqual(
            set(
                PermissionGrant.objects.filter(
                    principal=operator,
                    scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                    product=self.product,
                ).values_list("action", flat=True)
            ),
            {PermissionGrant.Action.EDIT},
        )
        self.assertFalse(PermissionGrant.objects.filter(action=PermissionGrant.Action.PUBLISH).exists())
        self.assertEqual(PermissionGrant.objects.count(), 13)
        now = timezone.now()
        for grant in PermissionGrant.objects.all():
            self.assertIsNotNone(grant.valid_until)
            self.assertGreater(grant.valid_until, now + timedelta(days=29, hours=23))
            self.assertLessEqual(grant.valid_until, now + timedelta(days=30, minutes=1))
        output = self.stdout.getvalue()
        for password in FIRST_PASSWORDS.values():
            self.assertNotIn(password, output)

    def test_default_is_complete_dry_run_with_zero_committed_writes(self):
        self._call(apply=False)

        self.assertFalse(Principal.objects.filter(username__in=["owner", "admin", "operator"]).exists())
        self.assertFalse(PermissionGrant.objects.exists())
        self.assertIn("WOULD CREATE (dry run; zero committed writes)", self.stdout.getvalue())

    def test_local_and_production_are_rejected_without_staff_writes(self):
        for environment_name in ("local", "production"):
            with self.subTest(environment=environment_name):
                with override_settings(ENVIRONMENT=environment_name):
                    with patch.dict(os.environ, self._environment(FIRST_PASSWORDS), clear=True):
                        with self.assertRaisesMessage(CommandError, "staging-only"):
                            call_command("provision_staging_staff", stdout=self.stdout)
                self.assertFalse(Principal.objects.filter(username__in=["owner", "admin", "operator"]).exists())
                self.assertFalse(PermissionGrant.objects.exists())

    def test_password_policy_and_distinct_values_fail_before_writes(self):
        with self.assertRaisesMessage(CommandError, "STAGING_OWNER_PASSWORD must be supplied"):
            self._call(passwords=None)

        too_short = dict(FIRST_PASSWORDS)
        too_short["STAGING_OWNER_PASSWORD"] = "Short!12345"
        with self.assertRaisesMessage(CommandError, "Staging minimum of 12"):
            self._call(passwords=too_short)

        duplicate = dict(FIRST_PASSWORDS)
        duplicate["STAGING_ADMIN_PASSWORD"] = duplicate["STAGING_OWNER_PASSWORD"]
        with self.assertRaisesMessage(CommandError, "three distinct password values"):
            self._call(passwords=duplicate)

        self.assertFalse(Principal.objects.filter(username__in=["owner", "admin", "operator"]).exists())
        self.assertFalse(PermissionGrant.objects.exists())

    def test_conflicting_existing_grant_risk_is_not_rewritten(self):
        self._call()
        operator = Principal.objects.get(username="operator")
        conflict = PermissionGrant.objects.get(
            principal=operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
        )
        conflict.risk_level = PermissionGrant.RiskLevel.LOW
        conflict.save(update_fields=["risk_level", "updated_at"])

        with self.assertRaisesMessage(CommandError, "has risk LOW; expected MEDIUM"):
            self._call(passwords=None)

        self.assertEqual(PermissionGrant.objects.count(), 13)
        self.assertTrue(PermissionGrant.objects.filter(pk=conflict.pk).exists())
        conflict.refresh_from_db()
        self.assertEqual(conflict.risk_level, PermissionGrant.RiskLevel.LOW)

    def test_complete_existing_principal_set_without_base_grants_fails_without_expanding_authority(self):
        for username, role, password in (
            ("owner", Principal.Role.OWNER, "Existing!Owner#41Alpha"),
            ("admin", Principal.Role.OPERATIONS_ADMIN, "Existing!Admin#52Beta"),
            ("operator", Principal.Role.OPERATOR, "Existing!Oper#63Gamma"),
        ):
            Principal.objects.create_user(
                username=username,
                password=password,
                role=role,
                principal_type=Principal.PrincipalType.HUMAN_USER,
            )

        with self.assertRaisesMessage(CommandError, "will not silently expand"):
            self._call(passwords=None)

        self.assertEqual(
            Principal.objects.filter(username__in=["owner", "admin", "operator"]).count(),
            3,
        )
        self.assertFalse(PermissionGrant.objects.exists())

    def test_replay_is_idempotent_and_does_not_reset_existing_passwords(self):
        self._call(passwords=FIRST_PASSWORDS)
        first_grant_ids = set(PermissionGrant.objects.values_list("id", flat=True))

        self.stdout = StringIO()
        self._call(passwords=None)

        self.assertSetEqual(set(PermissionGrant.objects.values_list("id", flat=True)), first_grant_ids)
        for username, first_name, second_name in (
            ("owner", "STAGING_OWNER_PASSWORD", "STAGING_OWNER_PASSWORD"),
            ("admin", "STAGING_ADMIN_PASSWORD", "STAGING_ADMIN_PASSWORD"),
            ("operator", "STAGING_OPERATOR_PASSWORD", "STAGING_OPERATOR_PASSWORD"),
        ):
            principal = Principal.objects.get(username=username)
            self.assertTrue(principal.check_password(FIRST_PASSWORDS[first_name]))
            self.assertFalse(principal.check_password(SECOND_PASSWORDS[second_name]))
        self.assertIn("Passwords were not printed or reset", self.stdout.getvalue())

    def test_partial_existing_staff_set_is_rejected(self):
        Principal.objects.create_user(
            username="operator",
            password="Existing!Oper#26Theta",
            role=Principal.Role.OPERATOR,
            principal_type=Principal.PrincipalType.HUMAN_USER,
        )

        with self.assertRaisesMessage(CommandError, "complete three-account set"):
            self._call()

        self.assertFalse(Principal.objects.filter(username__in=["owner", "admin"]).exists())
        self.assertFalse(PermissionGrant.objects.exists())

    def test_publish_grant_is_operator_only_account_scoped_and_high_risk(self):
        channel, binding, capability = self._add_publish_context()

        self._call(publish_account_code=channel.account_code)

        grant = PermissionGrant.objects.get(action=PermissionGrant.Action.PUBLISH)
        self.assertEqual(grant.principal.username, "operator")
        self.assertEqual(grant.scope_kind, PermissionGrant.ScopeKind.ACCOUNT)
        self.assertEqual(grant.account_ref, channel.account_code)
        self.assertIsNone(grant.product)
        self.assertEqual(grant.risk_level, PermissionGrant.RiskLevel.HIGH)
        self.assertEqual(grant.effect, PermissionGrant.Effect.ALLOW)
        self.assertTrue(binding.is_current_at())
        self.assertTrue(capability.is_current_open_at())
        self.assertFalse(
            PermissionGrant.objects.filter(
                principal__username__in=["owner", "admin"],
                action=PermissionGrant.Action.PUBLISH,
            ).exists()
        )

    def test_missing_current_profile_or_contract_fails_without_staff_writes(self):
        self.product.current_profile_version = None
        self.product.updated_by_principal = self.seed
        self.product.save(update_fields=["current_profile_version", "updated_by_principal", "updated_at"])

        with self.assertRaisesMessage(CommandError, "no current sealed ProductProfileVersion"):
            self._call()

        self.assertFalse(Principal.objects.filter(username__in=["owner", "admin", "operator"]).exists())
        self.assertFalse(PermissionGrant.objects.exists())
