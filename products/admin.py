from django.contrib import admin

from products.models import (
    ClaimEvidenceLink,
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
    ProductProfilePolicyLink,
    ProductProfileVersion,
)


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ("product_code", "name", "market_code", "language_code", "product_status", "current_profile_version")
    list_filter = ("product_status", "market_code", "language_code")
    search_fields = ("product_code", "name")


@admin.register(ProductProfileVersion)
class ProductProfileVersionAdmin(admin.ModelAdmin):
    list_display = ("product", "version_number", "market_code", "language_code", "sealed_at", "manifest_sha256")
    list_filter = ("market_code", "language_code")
    readonly_fields = ("manifest_sha256", "sealed_at", "sealed_by_principal", "created_at")


for model in (
    ObjectiveProfileVersion,
    ObjectiveMetricLink,
    ClaimMatrixVersion,
    ProductClaimVersion,
    ClaimMatrixItem,
    ControlledEvidenceItemVersion,
    ClaimEvidenceLink,
    EvidenceLibraryVersion,
    EvidenceLibraryItem,
    ProductProfilePolicyLink,
    ProductProfileAssetLink,
):
    admin.site.register(model)

