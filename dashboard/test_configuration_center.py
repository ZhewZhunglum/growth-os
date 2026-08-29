from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from dashboard.config_forms import ChannelAccountForm, RuntimeEnvironmentForm
from dashboard.config_services import (
    add_profile_evidence_link,
    create_binding_version,
    create_capability_version,
    create_channel_account,
    create_claim_matrix,
    create_evidence_library,
    create_objective_profile,
    create_product_profile,
    create_runtime_environment,
    seal_and_activate_profile,
    set_current_account_environment,
)
from products.models import Product, ProductProfileAssetLink
from releasegate.models import AccountEnvironmentBinding, CapabilityState, ChannelAccount, RuntimeEnvironment
from releasegate.runtime import ManualPublishReadiness, inspect_manual_publish_context


class ConfigurationCenterTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="config-owner", password="safe-test-password-123", role=Principal.Role.OWNER
        )
        self.outsider = Principal.objects.create_user(
            username="config-outsider", password="safe-test-password-123", role=Principal.Role.OPERATOR
        )
        self.product = Product.objects.create(
            product_code="PUKO-CONFIG",
            name="PUKO Config",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        self.product_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.MEDIUM,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=30),
            granted_by_principal=self.owner,
        )
        self.runtime_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            effect=PermissionGrant.Effect.ALLOW,
            risk_level=PermissionGrant.RiskLevel.HIGH,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(days=30),
            granted_by_principal=self.owner,
        )

    def contexts(self):
        objective = create_objective_profile(
            actor=self.owner,
            product=self.product,
            objective_key="PUKO_DAILY",
            primary_objectives=["EXPOSURE", "SEO", "GEO"],
            secondary_objectives=["ENGAGEMENT"],
            retained_metrics=["PURCHASE"],
            priority_rules={"first": "safety"},
            strategy_boundaries={"commerce_not_demand": True},
        )
        matrix = create_claim_matrix(
            actor=self.owner,
            product=self.product,
            claim={
                "claim_key": "focus-support",
                "claim_type": "RESTRICTED",
                "market_code": "US",
                "platform_code": "",
                "evidence_level": "HIGH",
                "wording": "Supports normal daily focus.",
            },
        )
        library = create_evidence_library(
            actor=self.owner,
            product=self.product,
            evidence={
                "evidence_key": "study-1",
                "title": "Controlled source",
                "provider": "Publisher",
                "external_url": "https://example.com/study/v1",
                "version_reference": "v1",
                "summary": "A controlled external reference.",
            },
        )
        return objective, matrix, library

    def test_unauthorized_user_cannot_open_or_write_product_configuration(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("dashboard:product-configuration", args=[self.product.pk]))
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(PermissionDenied):
            create_objective_profile(
                actor=self.outsider,
                product=self.product,
                objective_key="NOPE",
                primary_objectives=[], secondary_objectives=[], retained_metrics=[],
                priority_rules={}, strategy_boundaries={},
            )

    def test_exact_sealed_context_builds_and_activates_immutable_profile(self):
        objective, matrix, library = self.contexts()
        profile = create_product_profile(
            actor=self.owner,
            product=self.product,
            objective_profile_version=objective,
            claim_matrix_version=matrix,
            evidence_library_version=library,
            audience={"primary": "US consumers"},
            core_value_proposition="Evidence-informed daily wellness.",
            brand_voice={"tone": ["clear"]},
            product_facts={"format": "supplement"},
            prohibited_expressions=["cure"],
        )
        evidence = library.items.get().controlled_evidence_item_version
        add_profile_evidence_link(
            actor=self.owner,
            profile=profile,
            evidence=evidence,
            asset_kind=ProductProfileAssetLink.AssetKind.LITERATURE_LINK,
        )
        seal_and_activate_profile(actor=self.owner, profile=profile)
        self.product.refresh_from_db()
        self.assertEqual(self.product.current_profile_version_id, profile.pk)
        profile.refresh_from_db()
        profile.core_value_proposition = "Changed after approval"
        with self.assertRaises(ValidationError):
            profile.save()

    def test_cross_product_link_is_rejected(self):
        objective, matrix, library = self.contexts()
        profile = create_product_profile(
            actor=self.owner, product=self.product,
            objective_profile_version=objective, claim_matrix_version=matrix,
            evidence_library_version=library, audience={}, core_value_proposition="Value",
            brand_voice={}, product_facts={}, prohibited_expressions=[],
        )
        other = Product.objects.create(
            product_code="OTHER", name="Other", market_code="US", language_code="en",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        PermissionGrant.objects.create(
            principal=self.owner, scope_kind="PRODUCT", product=other, action="EDIT", effect="ALLOW",
            risk_level="LOW", valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(days=1), granted_by_principal=self.owner,
        )
        other_library = create_evidence_library(
            actor=self.owner, product=other,
            evidence={"evidence_key": "other", "title": "Other", "provider": "Publisher",
                      "external_url": "https://example.com/other", "version_reference": "v1", "summary": ""},
        )
        with self.assertRaises(ValidationError):
            add_profile_evidence_link(
                actor=self.owner, profile=profile,
                evidence=other_library.items.get().controlled_evidence_item_version,
                asset_kind=ProductProfileAssetLink.AssetKind.LITERATURE_LINK,
            )

    def test_binding_and_capability_changes_append_versions(self):
        account = create_channel_account(
            actor=self.owner, platform_code="PINTEREST", account_code="puko-pin",
            external_account_ref="external-1", display_name="PUKO Pinterest", status="ACTIVE",
        )
        environment = create_runtime_environment(
            actor=self.owner, environment_code="staging-us", environment_type="STAGING",
            identity_namespace="puko-staging", database_namespace="growth_os_staging",
            object_storage_namespace="DISABLED_LINK_ONLY", status="ACTIVE",
        )
        binding_v1 = create_binding_version(
            actor=self.owner, channel_account=account, runtime_environment=environment,
            identity_reference="pinterest-profile-a",
        )
        binding_v2 = create_binding_version(
            actor=self.owner, channel_account=account, runtime_environment=environment,
            identity_reference="pinterest-profile-b",
        )
        self.assertEqual(binding_v2.binding_version, 2)
        self.assertEqual(binding_v2.supersedes_id, binding_v1.pk)
        with self.assertRaises(ValidationError):
            create_capability_version(
                actor=self.owner, binding=binding_v1, capability_code="MANUAL_PUBLISH",
                state="OPEN", reason="stale binding must not reopen publishing",
            )
        state_v1 = create_capability_version(
            actor=self.owner, binding=binding_v2, capability_code="MANUAL_PUBLISH",
            state="UNKNOWN", reason="not checked",
        )
        state_v2 = create_capability_version(
            actor=self.owner, binding=binding_v2, capability_code="MANUAL_PUBLISH",
            state="OPEN", reason="manual check passed",
        )
        self.assertEqual(state_v2.state_version, 2)
        self.assertEqual(state_v2.supersedes_id, state_v1.pk)
        state_v1.reason = "silently changed"
        with self.assertRaises(ValidationError):
            state_v1.save()

        with self.assertRaises(ValidationError):
            create_channel_account(
                actor=self.owner, platform_code="UNKNOWN_PLATFORM", account_code="unknown",
                external_account_ref="unknown", display_name="Unknown", status="ACTIVE",
            )

    def test_set_current_environment_recovers_multiple_bindings_and_is_idempotent(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="TIKTOK",
            account_code="switch-tiktok",
            external_account_ref="switch-tiktok-external",
            display_name="PUKO TikTok Switch",
            status=ChannelAccount.Status.ACTIVE,
        )
        environment_a = create_runtime_environment(
            actor=self.owner,
            environment_code="switch-staging-a",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="switch-a-identities",
            database_namespace="switch-a-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        environment_b = create_runtime_environment(
            actor=self.owner,
            environment_code="switch-staging-b",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="switch-b-identities",
            database_namespace="switch-b-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        future_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="switch-staging-future",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="switch-future-identities",
            database_namespace="switch-future-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        binding_a = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=environment_a,
            identity_reference="switch-profile-a",
        )
        binding_b = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=environment_b,
            identity_reference="switch-profile-b",
        )
        capability_b = create_capability_version(
            actor=self.owner,
            binding=binding_b,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.OPEN,
            reason="target already checked",
        )
        future_binding = AccountEnvironmentBinding.objects.create(
            channel_account=account,
            runtime_environment=future_environment,
            binding_version=1,
            status=AccountEnvironmentBinding.Status.ACTIVE,
            identity_reference="switch-profile-future",
            valid_from=timezone.now() + timedelta(days=1),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.assertEqual(
            inspect_manual_publish_context(account).readiness,
            ManualPublishReadiness.MULTIPLE_ENVIRONMENTS,
        )

        change = set_current_account_environment(
            actor=self.owner,
            channel_account=account,
            runtime_environment=environment_b,
            identity_reference="",
        )

        self.assertFalse(change.created_target_version)
        self.assertEqual(change.revoked_count, 2)
        self.assertEqual(change.binding.pk, binding_b.pk)
        latest_a = AccountEnvironmentBinding.objects.filter(
            channel_account=account,
            runtime_environment=environment_a,
        ).order_by("-binding_version").first()
        self.assertEqual(latest_a.status, AccountEnvironmentBinding.Status.REVOKED)
        self.assertEqual(latest_a.supersedes_id, binding_a.pk)
        binding_a.refresh_from_db()
        self.assertEqual(binding_a.status, AccountEnvironmentBinding.Status.ACTIVE)
        latest_future = AccountEnvironmentBinding.objects.filter(
            channel_account=account,
            runtime_environment=future_environment,
        ).order_by("-binding_version").first()
        self.assertEqual(latest_future.status, AccountEnvironmentBinding.Status.REVOKED)
        self.assertEqual(latest_future.supersedes_id, future_binding.pk)
        inspection = inspect_manual_publish_context(account)
        self.assertEqual(inspection.readiness, ManualPublishReadiness.READY)
        self.assertEqual(inspection.binding.pk, binding_b.pk)
        self.assertEqual(inspection.capability.pk, capability_b.pk)

        binding_count = AccountEnvironmentBinding.objects.filter(channel_account=account).count()
        repeated = set_current_account_environment(
            actor=self.owner,
            channel_account=account,
            runtime_environment=environment_b,
            identity_reference="",
        )
        self.assertFalse(repeated.created_target_version)
        self.assertEqual(repeated.revoked_count, 0)
        self.assertEqual(
            AccountEnvironmentBinding.objects.filter(channel_account=account).count(),
            binding_count,
        )

    def test_set_current_environment_switches_atomically_and_requires_capability_recheck(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="PINTEREST",
            account_code="switch-new-pinterest",
            external_account_ref="switch-new-pinterest-external",
            display_name="PUKO Pinterest Switch",
            status=ChannelAccount.Status.ACTIVE,
        )
        old_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="switch-old",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="switch-old-identities",
            database_namespace="switch-old-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        new_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="switch-new",
            environment_type=RuntimeEnvironment.EnvironmentType.PRODUCTION,
            identity_namespace="switch-new-identities",
            database_namespace="switch-new-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        old_binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=old_environment,
            identity_reference="switch-old-profile",
        )
        create_capability_version(
            actor=self.owner,
            binding=old_binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.OPEN,
            reason="old environment checked",
        )

        change = set_current_account_environment(
            actor=self.owner,
            channel_account=account,
            runtime_environment=new_environment,
            identity_reference="switch-new-profile",
        )

        self.assertTrue(change.created_target_version)
        self.assertEqual(change.revoked_count, 1)
        self.assertEqual(change.binding.runtime_environment_id, new_environment.pk)
        self.assertFalse(old_binding.is_current_at())
        inspection = inspect_manual_publish_context(account)
        self.assertEqual(inspection.readiness, ManualPublishReadiness.NO_CAPABILITY)
        self.assertEqual(inspection.binding.pk, change.binding.pk)
        create_capability_version(
            actor=self.owner,
            binding=change.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.OPEN,
            reason="new environment checked",
        )
        self.assertEqual(inspect_manual_publish_context(account).readiness, ManualPublishReadiness.READY)

        new_environment.status = RuntimeEnvironment.Status.LOCKED
        new_environment.save(update_fields=["status", "updated_at"])
        before = AccountEnvironmentBinding.objects.filter(channel_account=account).count()
        with self.assertRaises(ValidationError):
            set_current_account_environment(
                actor=self.owner,
                channel_account=account,
                runtime_environment=new_environment,
                identity_reference="should-not-write",
            )
        self.assertEqual(AccountEnvironmentBinding.objects.filter(channel_account=account).count(), before)

    def test_binding_action_requires_confirmation_before_switching_current_environment(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="QUORA",
            account_code="switch-view-quora",
            external_account_ref="switch-view-quora-external",
            display_name="PUKO Quora Switch",
            status=ChannelAccount.Status.ACTIVE,
        )
        environment = create_runtime_environment(
            actor=self.owner,
            environment_code="switch-view-environment",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="switch-view-identities",
            database_namespace="switch-view-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        url = reverse("dashboard:runtime-configuration-action", args=["binding"])
        payload = {
            "channel_account": account.pk,
            "runtime_environment": environment.pk,
            "identity_reference": "switch-view-profile",
        }
        self.client.force_login(self.owner)

        rejected = self.client.post(url, payload)
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, "其他当前或预定连接", status_code=400)
        self.assertFalse(AccountEnvironmentBinding.objects.filter(channel_account=account).exists())

        accepted = self.client.post(url, {**payload, "confirm_replace": "on"})
        binding = AccountEnvironmentBinding.objects.get(channel_account=account)
        self.assertEqual(binding.runtime_environment_id, environment.pk)
        expected_query = (
            f"?account={account.pk}&binding={binding.pk}&step=capability#advanced-capability"
        )
        self.assertEqual(
            accepted["Location"],
            f'{reverse("dashboard:runtime-configuration-advanced")}{expected_query}',
        )

    def test_multiple_environment_recovery_is_direct_safe_and_http_complete(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="TIKTOK",
            account_code="http-recovery-tiktok",
            external_account_ref="http-recovery-external",
            display_name="PUKO TikTok Recovery",
            status=ChannelAccount.Status.ACTIVE,
        )
        local_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="local-http-recovery",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="local-http-recovery-identities",
            database_namespace="local-http-recovery-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        staging_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="staging-http-recovery",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="staging-http-recovery-identities",
            database_namespace="staging-http-recovery-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        local_binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=local_environment,
            identity_reference="local-http-profile",
        )
        staging_binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=staging_environment,
            identity_reference="staging-http-profile",
        )
        capability = create_capability_version(
            actor=self.owner,
            binding=staging_binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.OPEN,
            reason="keep this checked context",
        )
        self.assertEqual(
            inspect_manual_publish_context(account).readiness,
            ManualPublishReadiness.MULTIPLE_ENVIRONMENTS,
        )

        self.client.force_login(self.owner)
        summary_url = reverse("dashboard:runtime-configuration")
        advanced_url = reverse("dashboard:runtime-configuration-advanced")
        action_url = reverse("dashboard:runtime-configuration-action", args=["binding"])

        summary = self.client.get(summary_url)
        self.assertContains(summary, "本地练习（local-http-recovery）")
        self.assertContains(summary, "测试环境（staging-http-recovery）")
        self.assertContains(summary, "选择唯一使用场景")
        self.assertContains(
            summary,
            f'{advanced_url}?account={account.pk}#advanced-binding',
        )

        advanced = self.client.get(f"{advanced_url}?account={account.pk}")
        self.assertContains(
            advanced,
            'id="advanced-binding" class="advanced-tool" open',
        )
        self.assertContains(advanced, "正在处理：PUKO TikTok Recovery")
        self.assertContains(advanced, f'value="{account.pk}" selected')
        self.assertContains(advanced, "本地练习（local-http-recovery）")
        self.assertContains(advanced, "测试环境（staging-http-recovery）")

        payload = {
            "channel_account": account.pk,
            "runtime_environment": staging_environment.pk,
            "identity_reference": "",
        }
        binding_count = AccountEnvironmentBinding.objects.filter(channel_account=account).count()
        rejected = self.client.post(action_url, payload)
        self.assertEqual(rejected.status_code, 400)
        self.assertContains(rejected, "其他当前或预定连接", status_code=400)
        self.assertEqual(
            AccountEnvironmentBinding.objects.filter(channel_account=account).count(),
            binding_count,
        )

        accepted = self.client.post(action_url, {**payload, "confirm_replace": "on"})
        self.assertEqual(
            accepted["Location"],
            f"{summary_url}?platform=TIKTOK#platform-tiktok",
        )
        latest_local = AccountEnvironmentBinding.objects.filter(
            channel_account=account,
            runtime_environment=local_environment,
        ).order_by("-binding_version").first()
        latest_staging = AccountEnvironmentBinding.objects.filter(
            channel_account=account,
            runtime_environment=staging_environment,
        ).order_by("-binding_version").first()
        self.assertEqual(latest_local.status, AccountEnvironmentBinding.Status.REVOKED)
        self.assertEqual(latest_local.supersedes_id, local_binding.pk)
        self.assertEqual(latest_staging.pk, staging_binding.pk)
        inspection = inspect_manual_publish_context(account)
        self.assertEqual(inspection.readiness, ManualPublishReadiness.READY)
        self.assertEqual(inspection.binding.pk, staging_binding.pk)
        self.assertEqual(inspection.capability.pk, capability.pk)

        recovered = self.client.get(accepted["Location"].split("#", 1)[0])
        self.assertContains(recovered, "可以使用")
        self.assertContains(recovered, 'id="platform-tiktok"')
        self.assertNotContains(recovered, "系统不会替你猜选")

        audit = self.client.get(advanced_url)
        self.assertContains(audit, "当前连接")
        self.assertContains(audit, "最新记录（非当前连接）")
        self.assertContains(audit, "历史版本")
        self.assertContains(
            audit,
            "http-recovery-tiktok → staging-http-recovery",
        )

        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        english = self.client.get(f"{advanced_url}?account={account.pk}")
        self.assertContains(english, "Working on: PUKO TikTok Recovery")
        self.assertContains(english, "Set the account’s current usage context")
        self.assertContains(english, "other current or scheduled connections")

    def test_recovery_routes_to_availability_check_when_preserved_binding_is_not_ready(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="QUORA",
            account_code="http-unchecked-quora",
            external_account_ref="http-unchecked-external",
            display_name="PUKO Quora Unchecked",
            status=ChannelAccount.Status.ACTIVE,
        )
        old_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="unchecked-old",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="unchecked-old-identities",
            database_namespace="unchecked-old-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        target_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="unchecked-target",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="unchecked-target-identities",
            database_namespace="unchecked-target-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=old_environment,
            identity_reference="unchecked-old-profile",
        )
        target_binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=target_environment,
            identity_reference="unchecked-target-profile",
        )
        self.client.force_login(self.owner)

        response = self.client.post(
            reverse("dashboard:runtime-configuration-action", args=["binding"]),
            {
                "channel_account": account.pk,
                "runtime_environment": target_environment.pk,
                "identity_reference": "",
                "confirm_replace": "on",
            },
        )

        expected = (
            f'{reverse("dashboard:runtime-configuration-advanced")}'
            f"?account={account.pk}&binding={target_binding.pk}&step=capability#advanced-capability"
        )
        self.assertEqual(response["Location"], expected)
        guided = self.client.get(response["Location"].split("#", 1)[0])
        self.assertContains(guided, 'id="advanced-capability" class="advanced-tool" open')
        self.assertContains(guided, "请继续第 4 步确认人工发布是否可用")
        self.assertContains(guided, f'value="{target_binding.pk}" selected')

        summary = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertContains(summary, "尚未检查")
        self.assertContains(summary, "检查人工发布状态")
        self.assertContains(
            summary,
            f"account={account.pk}&amp;binding={target_binding.pk}&amp;step=capability",
        )

    def test_local_named_production_is_rejected_and_never_presented_as_local_practice(self):
        with self.assertRaises(ValidationError):
            create_runtime_environment(
                actor=self.owner,
                environment_code="local-invalid-production",
                environment_type=RuntimeEnvironment.EnvironmentType.PRODUCTION,
                identity_namespace="invalid-production-identities",
                database_namespace="invalid-production-database",
                object_storage_namespace="DISABLED_LINK_ONLY",
                status=RuntimeEnvironment.Status.ACTIVE,
            )

        legacy_environment = RuntimeEnvironment.objects.create(
            environment_code="local-legacy-production",
            environment_type=RuntimeEnvironment.EnvironmentType.PRODUCTION,
            identity_namespace="legacy-production-identities",
            database_namespace="legacy-production-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        account = create_channel_account(
            actor=self.owner,
            platform_code="SHOPIFY",
            account_code="legacy-production-shopify",
            external_account_ref="legacy-production-external",
            display_name="PUKO Legacy Production",
            status=ChannelAccount.Status.ACTIVE,
        )
        binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=legacy_environment,
            identity_reference="legacy-production-profile",
        )
        create_capability_version(
            actor=self.owner,
            binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.OPEN,
            reason="legacy production check",
        )
        self.client.force_login(self.owner)

        summary = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertContains(summary, "正式环境")
        self.assertNotContains(summary, "本地练习可用")
        advanced = self.client.get(reverse("dashboard:runtime-configuration-advanced"))
        self.assertContains(advanced, "正式环境 · local-legacy-production")

    def test_unsupported_legacy_platform_cannot_enter_current_binding_flow(self):
        account = ChannelAccount.objects.create(
            platform_code="LEGACY_UNKNOWN",
            account_code="legacy-unknown-account",
            external_account_ref="legacy-unknown-external",
            display_name="Legacy Unknown Account",
            status=ChannelAccount.Status.ACTIVE,
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        environment = create_runtime_environment(
            actor=self.owner,
            environment_code="legacy-unknown-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="legacy-unknown-identities",
            database_namespace="legacy-unknown-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )

        with self.assertRaises(ValidationError):
            set_current_account_environment(
                actor=self.owner,
                channel_account=account,
                runtime_environment=environment,
                identity_reference="legacy-unknown-profile",
            )
        self.assertFalse(AccountEnvironmentBinding.objects.filter(channel_account=account).exists())

        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:runtime-configuration-action", args=["binding"]),
            {
                "channel_account": account.pk,
                "runtime_environment": environment.pk,
                "identity_reference": "legacy-unknown-profile",
                "confirm_replace": "on",
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "选择一个有效的选项", status_code=400)
        self.assertFalse(AccountEnvironmentBinding.objects.filter(channel_account=account).exists())

    def test_forms_refuse_file_or_secret_fields(self):
        self.client.force_login(self.owner)
        response = self.client.post(
            reverse("dashboard:runtime-configuration-action", args=["account"]),
            {
                "platform_code": "tiktok", "account_code": "puko-tiktok",
                "external_account_ref": "external-2", "display_name": "PUKO TikTok",
                "status": "ACTIVE", "api_secret": "must-not-be-stored",
                "upload": SimpleUploadedFile("secret.txt", b"secret"),
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(ChannelAccount.objects.filter(account_code="puko-tiktok").exists())

    def test_channel_account_form_localizes_english_platform_choices(self):
        form = ChannelAccountForm(language_code="en")

        self.assertIn(("SHOPIFY", "Shopify store"), list(form.fields["platform_code"].choices))
        self.assertNotIn(("SHOPIFY", "Shopify / 独立站"), list(form.fields["platform_code"].choices))
        self.assertTrue(form.fields["status"].widget.is_hidden)
        self.assertEqual(list(form.fields["status"].choices), [(ChannelAccount.Status.ACTIVE, "Active")])

        environment_form = RuntimeEnvironmentForm(language_code="en")
        self.assertTrue(environment_form.fields["status"].widget.is_hidden)
        self.assertEqual(
            list(environment_form.fields["status"].choices),
            [(RuntimeEnvironment.Status.ACTIVE, "Active")],
        )

        with self.assertRaises(ValidationError):
            create_channel_account(
                actor=self.owner,
                platform_code="TIKTOK",
                account_code="cannot-start-paused",
                external_account_ref="cannot-start-paused-external",
                display_name="Cannot Start Paused",
                status=ChannelAccount.Status.SUSPENDED,
            )
        with self.assertRaises(ValidationError):
            create_runtime_environment(
                actor=self.owner,
                environment_code="cannot-start-locked",
                environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
                identity_namespace="cannot-start-locked-identities",
                database_namespace="cannot-start-locked-database",
                object_storage_namespace="DISABLED_LINK_ONLY",
                status=RuntimeEnvironment.Status.LOCKED,
            )

    def test_runtime_configuration_requires_exact_global_manage_account(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="SHOPIFY",
            account_code="permission-switch-shopify",
            external_account_ref="permission-switch-external",
            display_name="PUKO Shopify Permission Check",
            status=ChannelAccount.Status.ACTIVE,
        )
        environment = create_runtime_environment(
            actor=self.owner,
            environment_code="permission-switch-environment",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="permission-switch-identities",
            database_namespace="permission-switch-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status=RuntimeEnvironment.Status.ACTIVE,
        )
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertEqual(response.status_code, 403)
        advanced = self.client.get(reverse("dashboard:runtime-configuration-advanced"))
        self.assertEqual(advanced.status_code, 403)
        with self.assertRaises(PermissionDenied):
            create_capability_version(
                actor=self.outsider,
                binding=None,
                capability_code=CapabilityState.MANUAL_PUBLISH,
                state=CapabilityState.State.OPEN,
            )
        with self.assertRaises(PermissionDenied):
            set_current_account_environment(
                actor=self.outsider,
                channel_account=account,
                runtime_environment=environment,
                identity_reference="must-not-write",
            )
        self.assertFalse(AccountEnvironmentBinding.objects.filter(channel_account=account).exists())

    def test_runtime_summary_is_plain_language_and_advanced_keeps_audit_detail(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="TIKTOK",
            account_code="plain-summary-tiktok",
            external_account_ref="plain-summary-external",
            display_name="PUKO TikTok Summary",
            status="ACTIVE",
        )
        environment = create_runtime_environment(
            actor=self.owner,
            environment_code="plain-summary-staging",
            environment_type="STAGING",
            identity_namespace="plain-summary-identities",
            database_namespace="plain-summary-database",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status="ACTIVE",
        )
        binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=environment,
            identity_reference="plain-summary-reference",
        )
        create_capability_version(
            actor=self.owner,
            binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.UNKNOWN,
            reason="not checked yet",
        )

        self.client.force_login(self.owner)
        summary = self.client.get(
            f'{reverse("dashboard:runtime-configuration")}?platform=TIKTOK'
        )
        self.assertEqual(summary.status_code, 200)
        self.assertContains(summary, 'id="platform-tiktok"')
        self.assertContains(summary, 'id="platform-pinterest"')
        self.assertContains(summary, 'id="platform-quora"')
        self.assertContains(summary, 'id="platform-shopify"')
        self.assertContains(summary, "PUKO TikTok Summary")
        self.assertContains(summary, "尚未检查")
        self.assertContains(summary, "测试环境")
        self.assertContains(summary, "只用于分析")
        self.assertNotContains(summary, "plain-summary-tiktok")
        self.assertNotContains(summary, "plain-summary-reference")
        self.assertNotContains(summary, "追加账号与环境绑定")
        self.assertNotContains(summary, CapabilityState.MANUAL_PUBLISH)

        advanced = self.client.get(reverse("dashboard:runtime-configuration-advanced"))
        self.assertContains(advanced, "高级设置与审计")
        self.assertContains(advanced, "设置账号的当前使用场景")
        self.assertContains(advanced, "plain-summary-tiktok")
        self.assertContains(advanced, "plain-summary-reference")
        self.assertContains(advanced, CapabilityState.MANUAL_PUBLISH)
        self.assertContains(advanced, "PUKO TikTok Summary · TikTok")
        self.assertContains(advanced, "测试环境 · plain-summary-staging")
        self.assertContains(advanced, "版本 1")
        self.assertNotContains(advanced, "ChannelAccount object (")
        self.assertNotContains(advanced, "RuntimeEnvironment object (")
        self.assertNotContains(advanced, "AccountEnvironmentBinding object (")

    def test_runtime_summary_uses_latest_fail_closed_state(self):
        account = create_channel_account(
            actor=self.owner,
            platform_code="PINTEREST",
            account_code="readiness-pinterest",
            external_account_ref="readiness-external",
            display_name="PUKO Pinterest Readiness",
            status="ACTIVE",
        )
        environment = create_runtime_environment(
            actor=self.owner,
            environment_code="local-readiness",
            environment_type="STAGING",
            identity_namespace="local-readiness",
            database_namespace="local-readiness",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status="ACTIVE",
        )
        binding = create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=environment,
            identity_reference="local-readiness-profile",
        )
        create_capability_version(
            actor=self.owner,
            binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.OPEN,
            reason="local flow only",
        )

        self.client.force_login(self.owner)
        ready = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertContains(ready, "本地练习可用")
        self.assertContains(ready, "不会自动替你发布到真实平台")

        create_capability_version(
            actor=self.owner,
            binding=binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state=CapabilityState.State.CLOSED,
            reason="paused",
        )
        closed = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertContains(closed, "已关闭")
        self.assertNotContains(closed, "本地练习可用")

        second_environment = create_runtime_environment(
            actor=self.owner,
            environment_code="readiness-second-staging",
            environment_type="STAGING",
            identity_namespace="readiness-second",
            database_namespace="readiness-second",
            object_storage_namespace="DISABLED_LINK_ONLY",
            status="ACTIVE",
        )
        create_binding_version(
            actor=self.owner,
            channel_account=account,
            runtime_environment=second_environment,
            identity_reference="readiness-second-profile",
        )
        ambiguous = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertContains(ambiguous, "需要管理员处理")
        self.assertContains(ambiguous, "系统不会替你猜选")
