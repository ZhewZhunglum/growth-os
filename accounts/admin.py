from django.contrib import admin
from django.contrib.auth.admin import UserAdmin

from accounts.models import PermissionGrant, Principal, SecretReference


@admin.register(Principal)
class PrincipalAdmin(UserAdmin):
    fieldsets = UserAdmin.fieldsets + (
        ("Growth OS identity", {"fields": ("role", "principal_type", "display_name", "auth_provider", "auth_subject", "mfa_status", "principal_status", "decommissioned_at", "must_change_password")}),
    )
    list_display = ("username", "display_name", "role", "principal_type", "principal_status", "is_staff")
    list_filter = ("role", "principal_type", "principal_status", "mfa_status", "is_staff")


@admin.register(PermissionGrant)
class PermissionGrantAdmin(admin.ModelAdmin):
    list_display = ("principal", "scope_kind", "action", "effect", "risk_level", "grant_status", "valid_until")
    list_filter = ("scope_kind", "action", "effect", "risk_level", "grant_status")
    search_fields = (
        "principal__username", "principal__display_name", "platform_code", "account_ref", "surface_ref"
    )


@admin.register(SecretReference)
class SecretReferenceAdmin(admin.ModelAdmin):
    list_display = ("secret_key", "provider_code", "environment_scope", "backend", "status", "expires_at")
    list_filter = ("provider_code", "environment_scope", "backend", "status")
    search_fields = ("secret_key", "provider_code", "reference_name", "purpose")
