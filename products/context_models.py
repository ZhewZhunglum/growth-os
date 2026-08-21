from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import UUIDv7Model


def _canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ImmutableQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Versioned product context cannot be updated in bulk.")

    def delete(self):
        raise ValidationError("Versioned product context cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Versioned product context cannot be bulk-updated.")


class ImmutableManager(models.Manager.from_queryset(ImmutableQuerySet)):
    pass


class SealableVersion(UUIDv7Model):
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    sealed_at = models.DateTimeField(null=True, blank=True)
    sealed_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    objects = ImmutableManager()

    class Meta:
        abstract = True

    @property
    def is_sealed(self) -> bool:
        return self.sealed_at is not None

    def manifest_payload(self) -> dict:
        raise NotImplementedError

    def clean(self):
        super().clean()
        if self.sealed_at is None:
            if self.sealed_by_principal_id or self.manifest_sha256:
                raise ValidationError("A draft version cannot carry seal metadata.")
            return
        if not self.sealed_by_principal_id:
            raise ValidationError("A sealed version requires the sealing Principal.")
        expected = _canonical_sha256(self.manifest_payload())
        if not self.manifest_sha256:
            self.manifest_sha256 = expected
        elif self.manifest_sha256 != expected:
            raise ValidationError("Manifest hash does not match the exact version content.")

    def seal(self, *, principal) -> None:
        if self.is_sealed:
            raise ValidationError("This version is already sealed.")
        self.sealed_at = timezone.now()
        self.sealed_by_principal = principal
        self.manifest_sha256 = _canonical_sha256(self.manifest_payload())
        self.save(update_fields=["sealed_at", "sealed_by_principal", "manifest_sha256"])

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).first()
            if original and original.is_sealed:
                raise ValidationError(f"{type(self).__name__} is sealed; create a new version.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{type(self).__name__} is retained for audit.")


class AppendOnlyFact(UUIDv7Model):
    objects = ImmutableManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{type(self).__name__} is immutable.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{type(self).__name__} is append-only.")


class ObjectiveProfileVersion(SealableVersion):
    objective_key = models.CharField(max_length=100)
    version_number = models.PositiveIntegerField()
    primary_objectives = models.JSONField(default=list)
    secondary_objectives = models.JSONField(default=list)
    retained_metrics = models.JSONField(default=list)
    priority_rules = models.JSONField(default=dict)
    strategy_boundaries = models.JSONField(default=dict)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["objective_key", "version_number"],
                name="products_unique_objective_profile_version",
            ),
            models.CheckConstraint(
                condition=Q(version_number__gte=1),
                name="products_objective_version_gte_one",
            ),
        ]

    def manifest_payload(self) -> dict:
        payload = {
            "objective_key": self.objective_key,
            "version_number": self.version_number,
            "primary_objectives": self.primary_objectives,
            "secondary_objectives": self.secondary_objectives,
            "retained_metrics": self.retained_metrics,
            "priority_rules": self.priority_rules,
            "strategy_boundaries": self.strategy_boundaries,
        }
        if self.pk:
            payload["metric_links"] = [
                {
                    "metric_definition_id": str(metric_definition_id),
                    "metric_role": metric_role,
                }
                for metric_definition_id, metric_role in self.metric_links.order_by(
                    "metric_definition_id", "metric_role"
                ).values_list("metric_definition_id", "metric_role")
            ]
        return payload


class ObjectiveMetricLink(AppendOnlyFact):
    class MetricRole(models.TextChoices):
        PRIMARY = "PRIMARY", "Primary"
        SECONDARY = "SECONDARY", "Secondary"
        GUARDRAIL = "GUARDRAIL", "Guardrail"
        RETAIN_ONLY = "RETAIN_ONLY", "Retain only"

    objective_profile_version = models.ForeignKey(
        ObjectiveProfileVersion,
        on_delete=models.PROTECT,
        related_name="metric_links",
    )
    metric_definition = models.ForeignKey(
        "insights.MetricDefinition",
        on_delete=models.PROTECT,
        related_name="objective_links",
    )
    metric_role = models.CharField(max_length=16, choices=MetricRole.choices)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["objective_profile_version", "metric_definition", "metric_role"],
                name="products_unique_objective_metric_role",
            )
        ]

    def clean(self):
        super().clean()
        if self.objective_profile_version_id and self.objective_profile_version.is_sealed:
            raise ValidationError("A sealed ObjectiveProfileVersion cannot receive new metrics.")


class ClaimMatrixVersion(SealableVersion):
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="claim_matrices")
    version_number = models.PositiveIntegerField()
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "version_number"],
                name="products_unique_claim_matrix_version",
            )
        ]

    def manifest_payload(self) -> dict:
        payload = {
            "product_id": str(self.product_id),
            "version_number": self.version_number,
            "market_code": self.market_code,
            "language_code": self.language_code,
        }
        if self.pk:
            payload["claim_version_ids"] = [
                str(claim_id)
                for claim_id in self.items.order_by("product_claim_version_id").values_list(
                    "product_claim_version_id", flat=True
                )
            ]
        return payload


class ProductClaimVersion(AppendOnlyFact):
    class ClaimType(models.TextChoices):
        ALLOWED = "ALLOWED", "Allowed"
        RESTRICTED = "RESTRICTED", "Restricted"
        PROHIBITED = "PROHIBITED", "Prohibited"

    class EvidenceLevel(models.TextChoices):
        HIGH = "HIGH", "High"
        MEDIUM = "MEDIUM", "Medium"
        LOW = "LOW", "Low"
        UNSUPPORTED = "UNSUPPORTED", "Unsupported"

    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="claim_versions")
    claim_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    claim_type = models.CharField(max_length=16, choices=ClaimType.choices)
    market_code = models.CharField(max_length=16)
    platform_code = models.CharField(max_length=64, blank=True)
    evidence_level = models.CharField(max_length=16, choices=EvidenceLevel.choices)
    wording = models.TextField()
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "claim_key", "version_number"],
                name="products_unique_claim_version",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="products_claim_valid_window",
            ),
        ]


class ClaimMatrixItem(AppendOnlyFact):
    claim_matrix_version = models.ForeignKey(
        ClaimMatrixVersion,
        on_delete=models.PROTECT,
        related_name="items",
    )
    product_claim_version = models.ForeignKey(
        ProductClaimVersion,
        on_delete=models.PROTECT,
        related_name="matrix_items",
    )
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["claim_matrix_version", "product_claim_version"],
                name="products_unique_claim_matrix_item",
            )
        ]

    def clean(self):
        super().clean()
        if self.claim_matrix_version_id and self.claim_matrix_version.is_sealed:
            raise ValidationError("A sealed ClaimMatrixVersion cannot receive new claims.")
        if (
            self.claim_matrix_version_id
            and self.product_claim_version_id
            and self.claim_matrix_version.product_id != self.product_claim_version.product_id
        ):
            raise ValidationError("A claim matrix can contain claims only for the same Product.")


class ControlledEvidenceItemVersion(AppendOnlyFact):
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="controlled_evidence")
    evidence_key = models.CharField(max_length=120)
    version_number = models.PositiveIntegerField()
    title = models.CharField(max_length=240)
    provider = models.CharField(max_length=120)
    external_url = models.URLField(max_length=2048)
    version_reference = models.CharField(max_length=255, blank=True)
    summary = models.TextField(blank=True)
    content_sha256 = models.CharField(max_length=64, blank=True)
    valid_from = models.DateTimeField(default=timezone.now)
    valid_until = models.DateTimeField(null=True, blank=True)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "evidence_key", "version_number"],
                name="products_unique_controlled_evidence_version",
            ),
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="products_evidence_valid_window",
            ),
        ]


class ClaimEvidenceLink(AppendOnlyFact):
    product_claim_version = models.ForeignKey(
        ProductClaimVersion,
        on_delete=models.PROTECT,
        related_name="evidence_links",
    )
    controlled_evidence_item_version = models.ForeignKey(
        ControlledEvidenceItemVersion,
        on_delete=models.PROTECT,
        related_name="claim_links",
    )
    evidence_reference = models.TextField(blank=True)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product_claim_version", "controlled_evidence_item_version"],
                name="products_unique_claim_evidence_link",
            )
        ]

    def clean(self):
        super().clean()
        if (
            self.product_claim_version_id
            and self.controlled_evidence_item_version_id
            and self.product_claim_version.product_id != self.controlled_evidence_item_version.product_id
        ):
            raise ValidationError("Claim evidence must belong to the same Product.")


class EvidenceLibraryVersion(SealableVersion):
    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="evidence_libraries")
    version_number = models.PositiveIntegerField()
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product", "version_number"],
                name="products_unique_evidence_library_version",
            )
        ]

    def manifest_payload(self) -> dict:
        payload = {
            "product_id": str(self.product_id),
            "version_number": self.version_number,
            "market_code": self.market_code,
            "language_code": self.language_code,
        }
        if self.pk:
            payload["evidence_version_ids"] = [
                str(evidence_id)
                for evidence_id in self.items.order_by(
                    "controlled_evidence_item_version_id"
                ).values_list("controlled_evidence_item_version_id", flat=True)
            ]
        return payload


class EvidenceLibraryItem(AppendOnlyFact):
    evidence_library_version = models.ForeignKey(
        EvidenceLibraryVersion,
        on_delete=models.PROTECT,
        related_name="items",
    )
    controlled_evidence_item_version = models.ForeignKey(
        ControlledEvidenceItemVersion,
        on_delete=models.PROTECT,
        related_name="library_items",
    )
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["evidence_library_version", "controlled_evidence_item_version"],
                name="products_unique_evidence_library_item",
            )
        ]

    def clean(self):
        super().clean()
        if self.evidence_library_version_id and self.evidence_library_version.is_sealed:
            raise ValidationError("A sealed EvidenceLibraryVersion cannot receive new evidence.")
        if (
            self.evidence_library_version_id
            and self.controlled_evidence_item_version_id
            and self.evidence_library_version.product_id != self.controlled_evidence_item_version.product_id
        ):
            raise ValidationError("An evidence library can contain evidence only for the same Product.")


class ProductProfilePolicyLink(AppendOnlyFact):
    class PolicyRole(models.TextChoices):
        APPLICABLE = "APPLICABLE", "Applicable"
        OVERRIDE = "OVERRIDE", "Override"
        EXPERIMENTAL = "EXPERIMENTAL", "Experimental"

    product_profile_version = models.ForeignKey(
        "products.ProductProfileVersion",
        on_delete=models.PROTECT,
        related_name="policy_links",
    )
    policy_version = models.ForeignKey(
        "releasegate.PolicyVersion",
        on_delete=models.PROTECT,
        related_name="product_profile_links",
    )
    policy_role = models.CharField(max_length=16, choices=PolicyRole.choices)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product_profile_version", "policy_version", "policy_role"],
                name="products_unique_profile_policy_link",
            )
        ]

    def clean(self):
        super().clean()
        if self.product_profile_version_id and self.product_profile_version.is_sealed:
            raise ValidationError("A sealed ProductProfileVersion cannot receive new policy links.")


class ProductProfileAssetLink(AppendOnlyFact):
    class AssetKind(models.TextChoices):
        LOGO = "LOGO", "Logo"
        VISUAL_SPEC = "VISUAL_SPEC", "Visual specification"
        PRODUCT_IMAGE_LINK = "PRODUCT_IMAGE_LINK", "Product image link"
        LITERATURE_LINK = "LITERATURE_LINK", "Literature link"
        CONTROLLED_EVIDENCE = "CONTROLLED_EVIDENCE", "Controlled evidence"

    product_profile_version = models.ForeignKey(
        "products.ProductProfileVersion",
        on_delete=models.PROTECT,
        related_name="asset_links",
    )
    asset_kind = models.CharField(max_length=32, choices=AssetKind.choices)
    content_asset_version = models.ForeignKey(
        "contentops.ContentAssetVersion",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="product_profile_links",
    )
    controlled_evidence_item_version = models.ForeignKey(
        ControlledEvidenceItemVersion,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="product_profile_links",
    )
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(content_asset_version__isnull=False, controlled_evidence_item_version__isnull=True)
                    | Q(content_asset_version__isnull=True, controlled_evidence_item_version__isnull=False)
                ),
                name="products_profile_asset_exactly_one_target",
            )
        ]

    def clean(self):
        super().clean()
        if self.product_profile_version_id and self.product_profile_version.is_sealed:
            raise ValidationError("A sealed ProductProfileVersion cannot receive new asset links.")
        if self.content_asset_version_id and self.product_profile_version_id:
            if self.content_asset_version.asset.product_id != self.product_profile_version.product_id:
                raise ValidationError("Profile content assets must belong to the same Product.")
        if self.controlled_evidence_item_version_id and self.product_profile_version_id:
            if self.controlled_evidence_item_version.product_id != self.product_profile_version.product_id:
                raise ValidationError("Profile evidence must belong to the same Product.")
