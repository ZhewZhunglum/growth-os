from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import Principal
from insights.models import DataDomain, MetricDefinition
from products.models import (
    ClaimMatrixItem,
    ClaimMatrixVersion,
    ControlledEvidenceItemVersion,
    EvidenceLibraryItem,
    EvidenceLibraryVersion,
    ObjectiveMetricLink,
    ObjectiveProfileVersion,
    Product,
    ProductClaimVersion,
    ProductProfileAssetLink,
    ProductProfileVersion,
)


class ProductContextVersionTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="context-owner",
            password="safe-test-password-123",
            role=Principal.Role.OWNER,
        )
        self.product = Product.objects.create(
            product_code="PUKO-CONTEXT",
            name="PUKO Context",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )

    def objective_version(self):
        objective = ObjectiveProfileVersion.objects.create(
            objective_key="DAILY_VISIBILITY",
            version_number=1,
            primary_objectives=["EXPOSURE", "SEO", "GEO", "ACCOUNT_VISIT"],
            secondary_objectives=["ENGAGEMENT"],
            retained_metrics=["PRODUCT_VIEW", "PURCHASE", "REVENUE"],
            priority_rules={"first": "safety"},
            strategy_boundaries={"commerce_not_demand": True},
            created_by_principal=self.owner,
        )
        metric = MetricDefinition.objects.create(
            metric_key="search-impressions",
            version_number=1,
            name="Search impressions",
            data_domain=DataDomain.SEARCH_VISIBILITY,
            value_kind=MetricDefinition.ValueKind.COUNT,
            unit="count",
            created_by_principal=self.owner,
        )
        ObjectiveMetricLink.objects.create(
            objective_profile_version=objective,
            metric_definition=metric,
            metric_role=ObjectiveMetricLink.MetricRole.PRIMARY,
            created_by_principal=self.owner,
        )
        objective.seal(principal=self.owner)
        return objective, metric

    def claim_matrix(self):
        claim = ProductClaimVersion.objects.create(
            product=self.product,
            claim_key="structure-function",
            version_number=1,
            claim_type=ProductClaimVersion.ClaimType.RESTRICTED,
            market_code="US",
            evidence_level=ProductClaimVersion.EvidenceLevel.HIGH,
            wording="Supports normal daily focus.",
            created_by_principal=self.owner,
        )
        matrix = ClaimMatrixVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        ClaimMatrixItem.objects.create(
            claim_matrix_version=matrix,
            product_claim_version=claim,
            created_by_principal=self.owner,
        )
        matrix.seal(principal=self.owner)
        return matrix, claim

    def evidence_library(self):
        evidence = ControlledEvidenceItemVersion.objects.create(
            product=self.product,
            evidence_key="study-focus-001",
            version_number=1,
            title="Controlled study reference",
            provider="publisher",
            external_url="https://example.com/studies/focus-v1",
            version_reference="v1",
            created_by_principal=self.owner,
        )
        library = EvidenceLibraryVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
        )
        EvidenceLibraryItem.objects.create(
            evidence_library_version=library,
            controlled_evidence_item_version=evidence,
            created_by_principal=self.owner,
        )
        library.seal(principal=self.owner)
        return library, evidence

    def test_sealed_version_manifest_locks_its_exact_links(self):
        objective, metric = self.objective_version()
        self.assertEqual(len(objective.manifest_sha256), 64)
        self.assertIn(str(metric.pk), str(objective.manifest_payload()))

        another_metric = MetricDefinition.objects.create(
            metric_key="geo-mentions",
            version_number=1,
            name="GEO mentions",
            data_domain=DataDomain.GEO,
            value_kind=MetricDefinition.ValueKind.COUNT,
            unit="count",
            created_by_principal=self.owner,
        )
        with self.assertRaises(ValidationError):
            ObjectiveMetricLink.objects.create(
                objective_profile_version=objective,
                metric_definition=another_metric,
                metric_role=ObjectiveMetricLink.MetricRole.SECONDARY,
                created_by_principal=self.owner,
            )

    def test_profile_seal_requires_and_records_exact_sealed_context(self):
        objective, _ = self.objective_version()
        matrix, _ = self.claim_matrix()
        library, evidence = self.evidence_library()
        profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"primary": "US wellness consumers"},
            core_value_proposition="Evidence-informed daily wellness.",
            brand_voice={"tone": ["clear"]},
            product_facts={"business_mode": "B2C"},
            prohibited_expressions=["cure"],
            objective_profile_version=objective,
            claim_matrix_version=matrix,
            evidence_library_version=library,
            created_by_principal=self.owner,
        )
        ProductProfileAssetLink.objects.create(
            product_profile_version=profile,
            asset_kind=ProductProfileAssetLink.AssetKind.CONTROLLED_EVIDENCE,
            controlled_evidence_item_version=evidence,
            created_by_principal=self.owner,
        )
        profile.seal(self.owner)

        payload = profile.manifest_payload()
        self.assertEqual(payload["objective_profile_version_id"], str(objective.pk))
        self.assertEqual(payload["claim_matrix_version_id"], str(matrix.pk))
        self.assertEqual(payload["evidence_library_version_id"], str(library.pk))
        self.assertEqual(payload["asset_links"][0]["controlled_evidence_item_version_id"], str(evidence.pk))
        with self.assertRaises(ValidationError):
            ProductProfileAssetLink.objects.create(
                product_profile_version=profile,
                asset_kind=ProductProfileAssetLink.AssetKind.LITERATURE_LINK,
                controlled_evidence_item_version=evidence,
                created_by_principal=self.owner,
            )

    def test_profile_refuses_unsealed_or_cross_product_context(self):
        draft_objective = ObjectiveProfileVersion.objects.create(
            objective_key="DRAFT",
            version_number=1,
            primary_objectives=["EXPOSURE"],
            secondary_objectives=["ENGAGEMENT"],
            retained_metrics=["PURCHASE"],
            priority_rules={"first": "safety"},
            strategy_boundaries={"commerce_not_demand": True},
            created_by_principal=self.owner,
        )
        profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={},
            core_value_proposition="Draft",
            brand_voice={},
            product_facts={},
            prohibited_expressions=[],
            objective_profile_version=draft_objective,
            created_by_principal=self.owner,
        )
        with self.assertRaises(ValidationError):
            profile.seal(self.owner)
