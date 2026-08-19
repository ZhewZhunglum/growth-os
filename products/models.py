from __future__ import annotations

import hashlib
import json

from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from accounts.models import Principal
from core.models import TimeStampedModel, UUIDv7Model


def canonical_sha256(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Product(TimeStampedModel):
    class ProductStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        PAUSED = "PAUSED", "Paused"
        ARCHIVED = "ARCHIVED", "Archived"

    product_code = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)
    product_status = models.CharField(max_length=16, choices=ProductStatus.choices, default=ProductStatus.ACTIVE)
    current_profile_version = models.OneToOneField(
        "ProductProfileVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="current_for_product"
    )
    created_by_principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="products_created")
    updated_by_principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="products_updated")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product_code", "market_code", "language_code"], name="products_unique_code_market_language")
        ]

    def clean(self):
        if self.current_profile_version_id and self.current_profile_version.product_id != self.id:
            raise ValidationError("Current profile version must belong to this product.")

    def __str__(self) -> str:
        return f"{self.name} ({self.market_code}/{self.language_code})"


class ProductProfileVersion(UUIDv7Model):
    product = models.ForeignKey(Product, on_delete=models.PROTECT, related_name="profile_versions")
    version_number = models.PositiveIntegerField()
    market_code = models.CharField(max_length=16)
    language_code = models.CharField(max_length=16)
    audience = models.JSONField(default=dict)
    core_value_proposition = models.TextField()
    brand_voice = models.JSONField(default=dict)
    product_facts = models.JSONField(default=dict)
    prohibited_expressions = models.JSONField(default=list)
    objective_profile_key = models.CharField(max_length=100, default="VISIBILITY_AND_SEO_PROOF")
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    sealed_at = models.DateTimeField(null=True, blank=True)
    sealed_by_principal = models.ForeignKey(
        Principal, null=True, blank=True, on_delete=models.PROTECT, related_name="product_profiles_sealed"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by_principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="product_profiles_created")

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["product", "version_number"], name="products_unique_profile_version"),
            models.CheckConstraint(
                condition=(Q(sealed_at__isnull=True, sealed_by_principal__isnull=True))
                | (Q(sealed_at__isnull=False, sealed_by_principal__isnull=False) & ~Q(manifest_sha256="")),
                name="products_profile_complete_seal",
            ),
        ]

    @property
    def is_sealed(self) -> bool:
        return self.sealed_at is not None

    def manifest_payload(self) -> dict:
        return {
            "product_id": str(self.product_id), "version_number": self.version_number,
            "market_code": self.market_code, "language_code": self.language_code,
            "audience": self.audience, "core_value_proposition": self.core_value_proposition,
            "brand_voice": self.brand_voice, "product_facts": self.product_facts,
            "prohibited_expressions": self.prohibited_expressions, "objective_profile_key": self.objective_profile_key,
        }

    def seal(self, principal: Principal) -> None:
        if self.is_sealed:
            raise ValidationError("Product profile version is already sealed.")
        self.manifest_sha256 = canonical_sha256(self.manifest_payload())
        self.sealed_at = timezone.now()
        self.sealed_by_principal = principal
        self.save(update_fields=["manifest_sha256", "sealed_at", "sealed_by_principal"])

    def save(self, *args, **kwargs):
        if self.pk:
            original = ProductProfileVersion.objects.filter(pk=self.pk).first()
            if original and original.is_sealed:
                changed = {
                    field.attname for field in self._meta.concrete_fields
                    if field.attname not in {"id", "created_at"}
                    and getattr(original, field.attname) != getattr(self, field.attname)
                }
                if changed:
                    raise ValidationError("A sealed ProductProfileVersion is immutable; create a new version.")
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        state = "sealed" if self.is_sealed else "draft"
        return f"{self.product.product_code} profile v{self.version_number} ({state})"

