from __future__ import annotations

from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
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
)
from products.models import Product, ProductProfileAssetLink
from releasegate.models import CapabilityState, ChannelAccount


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

    def test_runtime_configuration_requires_exact_global_manage_account(self):
        self.client.force_login(self.outsider)
        response = self.client.get(reverse("dashboard:runtime-configuration"))
        self.assertEqual(response.status_code, 403)
        with self.assertRaises(PermissionDenied):
            create_capability_version(
                actor=self.outsider,
                binding=None,
                capability_code=CapabilityState.MANUAL_PUBLISH,
                state=CapabilityState.State.OPEN,
            )
