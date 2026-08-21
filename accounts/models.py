from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.ids import uuid7
from core.models import TimeStampedModel


class Principal(AbstractUser):
    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        OPERATIONS_ADMIN = "OPERATIONS_ADMIN", "Operations admin"
        OPERATOR = "OPERATOR", "Operator"

    class PrincipalType(models.TextChoices):
        HUMAN_USER = "HUMAN_USER", "Human user"
        SERVICE_ACCOUNT = "SERVICE_ACCOUNT", "Service account"
        API_CLIENT = "API_CLIENT", "API client"
        SYSTEM = "SYSTEM", "System"

    class PrincipalStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        SUSPENDED = "SUSPENDED", "Suspended"
        LOCKED = "LOCKED", "Locked"
        DECOMMISSIONED = "DECOMMISSIONED", "Decommissioned"

    class MfaStatus(models.TextChoices):
        NOT_ENROLLED = "NOT_ENROLLED", "Not enrolled"
        ENROLLED = "ENROLLED", "Enrolled"
        EXEMPT = "EXEMPT", "Exempt"

    id = models.UUIDField(primary_key=True, default=uuid7, editable=False)
    role = models.CharField(max_length=24, choices=Role.choices, default=Role.OPERATOR)
    principal_type = models.CharField(max_length=24, choices=PrincipalType.choices, default=PrincipalType.HUMAN_USER)
    display_name = models.CharField(max_length=150, blank=True)
    auth_provider = models.CharField(max_length=64, default="internal")
    auth_subject = models.CharField(max_length=255, blank=True)
    mfa_status = models.CharField(max_length=20, choices=MfaStatus.choices, default=MfaStatus.NOT_ENROLLED)
    principal_status = models.CharField(max_length=20, choices=PrincipalStatus.choices, default=PrincipalStatus.ACTIVE)
    decommissioned_at = models.DateTimeField(null=True, blank=True)
    must_change_password = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["auth_provider", "auth_subject"],
                condition=~Q(auth_subject=""),
                name="accounts_unique_provider_subject",
            ),
            models.CheckConstraint(
                condition=Q(principal_status="ACTIVE") | Q(is_active=False),
                name="accounts_inactive_status_cannot_login",
            ),
        ]

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).only("role", "principal_type").first()
            if original and (original.role != self.role or original.principal_type != self.principal_type):
                raise ValidationError("Principal role and type are immutable; create a new identity instead.")
        forced_fields: set[str] = set()
        if self.principal_status != self.PrincipalStatus.ACTIVE:
            self.is_active = False
            forced_fields.add("is_active")
        if self.principal_status == self.PrincipalStatus.DECOMMISSIONED and self.decommissioned_at is None:
            self.decommissioned_at = timezone.now()
            forced_fields.add("decommissioned_at")
        if kwargs.get("update_fields") is not None:
            kwargs["update_fields"] = set(kwargs["update_fields"]) | forced_fields
        super().save(*args, **kwargs)

    @property
    def can_authenticate(self) -> bool:
        return self.principal_status == self.PrincipalStatus.ACTIVE and self.is_active

    def validate_acting_role(self, acting_role: str) -> "Principal":
        from accounts.authorization import validate_acting_role

        return validate_acting_role(self, acting_role)

    def __str__(self) -> str:
        return self.display_name or self.get_full_name() or self.username


class SecretReference(TimeStampedModel):
    """Metadata-only pointer to a deployment secret.

    The secret value is deliberately never stored in Growth OS.  Runtime
    adapters resolve the named ``*_FILE`` mount or an external secret manager.
    """

    class Backend(models.TextChoices):
        FILE_MOUNT = "FILE_MOUNT", "Read-only file mount"
        EXTERNAL_MANAGER = "EXTERNAL_MANAGER", "External secret manager"

    class EnvironmentScope(models.TextChoices):
        LOCAL = "LOCAL", "Local"
        STAGING = "STAGING", "Staging"
        PRODUCTION = "PRODUCTION", "Production"

    class Status(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        ROTATION_REQUIRED = "ROTATION_REQUIRED", "Rotation required"
        REVOKED = "REVOKED", "Revoked"

    secret_key = models.CharField(max_length=120)
    provider_code = models.CharField(max_length=64)
    backend = models.CharField(max_length=24, choices=Backend.choices)
    reference_name = models.CharField(
        max_length=255,
        help_text="Environment variable base name or external secret identifier; never the value.",
    )
    environment_scope = models.CharField(max_length=16, choices=EnvironmentScope.choices)
    purpose = models.CharField(max_length=240)
    status = models.CharField(max_length=24, choices=Status.choices, default=Status.ACTIVE)
    last_rotated_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by_principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="secret_references_created")
    updated_by_principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="secret_references_updated")

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["secret_key", "environment_scope"],
                name="accounts_unique_secret_reference_scope",
            ),
            models.CheckConstraint(
                condition=Q(expires_at__isnull=True) | Q(last_rotated_at__isnull=True) | Q(expires_at__gt=models.F("last_rotated_at")),
                name="accounts_secret_expiry_after_rotation",
            ),
        ]

    def clean(self):
        super().clean()
        forbidden_fragments = ("BEGIN PRIVATE", "sk-", "Bearer ")
        if any(fragment.lower() in self.reference_name.lower() for fragment in forbidden_fragments):
            raise ValidationError({"reference_name": "Store only a reference name, never secret material."})
        if self.backend == self.Backend.FILE_MOUNT and self.reference_name.upper().endswith("_FILE"):
            raise ValidationError(
                {
                    "reference_name": (
                        "Use the environment-variable base name without _FILE; "
                        "the runtime adds that suffix when resolving the read-only mount."
                    )
                }
            )

    def __str__(self) -> str:
        return f"{self.provider_code}:{self.secret_key}@{self.environment_scope}"


class PermissionGrant(TimeStampedModel):
    class ScopeKind(models.TextChoices):
        GLOBAL = "GLOBAL", "Global"
        PRODUCT = "PRODUCT", "Product"
        PLATFORM = "PLATFORM", "Platform"
        ACCOUNT = "ACCOUNT", "Account"
        SURFACE = "SURFACE", "Surface"

    class Effect(models.TextChoices):
        ALLOW = "ALLOW", "Allow"
        DENY = "DENY", "Deny"

    class Action(models.TextChoices):
        VIEW = "VIEW", "View"
        EDIT = "EDIT", "Edit"
        CREATE_TASK = "CREATE_TASK", "Create task"
        ASSIGN_TASK = "ASSIGN_TASK", "Assign task"
        CANCEL_TASK = "CANCEL_TASK", "Cancel task"
        COMPLETE_TASK = "COMPLETE_TASK", "Complete task"
        REVIEW = "REVIEW", "Review"
        APPROVE = "APPROVE", "Approve"
        PUBLISH = "PUBLISH", "Publish"
        MANAGE_ACCOUNT = "MANAGE_ACCOUNT", "Manage account"
        EMERGENCY_STOP = "EMERGENCY_STOP", "Emergency stop"
        COLLECT_READ_ONLY = "COLLECT_READ_ONLY", "Collect read-only"

    class RiskLevel(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    class GrantStatus(models.TextChoices):
        ACTIVE = "ACTIVE", "Active"
        REVOKED = "REVOKED", "Revoked"
        EXPIRED = "EXPIRED", "Expired"
        SUSPENDED = "SUSPENDED", "Suspended"

    principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="permission_grants")
    scope_kind = models.CharField(max_length=16, choices=ScopeKind.choices)
    product = models.ForeignKey(
        "products.Product", null=True, blank=True, on_delete=models.PROTECT, related_name="permission_grants"
    )
    platform_code = models.CharField(max_length=64, blank=True)
    account_ref = models.CharField(max_length=255, blank=True)
    surface_ref = models.CharField(max_length=255, blank=True)
    action = models.CharField(max_length=32, choices=Action.choices)
    effect = models.CharField(max_length=8, choices=Effect.choices, default=Effect.ALLOW)
    risk_level = models.CharField(max_length=16, choices=RiskLevel.choices, default=RiskLevel.LOW)
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    grant_status = models.CharField(max_length=16, choices=GrantStatus.choices, default=GrantStatus.ACTIVE)
    granted_by_principal = models.ForeignKey(Principal, on_delete=models.PROTECT, related_name="grants_issued")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by_principal = models.ForeignKey(
        Principal, null=True, blank=True, on_delete=models.PROTECT, related_name="grants_revoked"
    )
    revocation_reason = models.TextField(blank=True)
    supersedes_grant = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="renewed_by_grant",
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(valid_until__isnull=True) | Q(valid_until__gt=models.F("valid_from")),
                name="accounts_grant_valid_window",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        scope_kind="GLOBAL", product__isnull=True, platform_code="",
                        account_ref="", surface_ref="",
                    )
                    | Q(
                        scope_kind="PRODUCT", product__isnull=False, platform_code="",
                        account_ref="", surface_ref="",
                    )
                    | (
                        Q(
                            scope_kind="PLATFORM", product__isnull=True,
                            account_ref="", surface_ref="",
                        )
                        & ~Q(platform_code="")
                    )
                    | (
                        Q(
                            scope_kind="ACCOUNT", product__isnull=True, platform_code="",
                            surface_ref="",
                        )
                        & ~Q(account_ref="")
                    )
                    | (
                        Q(
                            scope_kind="SURFACE", product__isnull=True, platform_code="",
                            account_ref="",
                        )
                        & ~Q(surface_ref="")
                    )
                ),
                name="accounts_grant_explicit_scope",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        grant_status="REVOKED",
                        revoked_at__isnull=False,
                        revoked_by_principal__isnull=False,
                    )
                    & ~Q(revocation_reason="")
                )
                | (
                    ~Q(grant_status="REVOKED")
                    & Q(
                        revoked_at__isnull=True,
                        revoked_by_principal__isnull=True,
                        revocation_reason="",
                    )
                ),
                name="accounts_grant_revocation_metadata",
            ),
        ]

    def clean(self):
        super().clean()
        if self.supersedes_grant_id:
            prior = self.supersedes_grant
            immutable_fields = (
                "principal_id",
                "scope_kind",
                "product_id",
                "platform_code",
                "account_ref",
                "surface_ref",
                "action",
                "effect",
                "risk_level",
            )
            if any(getattr(self, field) != getattr(prior, field) for field in immutable_fields):
                raise ValidationError("A renewal must preserve the exact Principal, scope, action, effect, and risk.")
            if self.valid_from < prior.valid_from:
                raise ValidationError("A renewal cannot begin before the original grant.")

    def save(self, *args, **kwargs):
        if self.pk:
            original = type(self).objects.filter(pk=self.pk).first()
            if original:
                authority_fields = {
                    "principal_id",
                    "scope_kind",
                    "product_id",
                    "platform_code",
                    "account_ref",
                    "surface_ref",
                    "action",
                    "effect",
                    "risk_level",
                    "valid_from",
                    "valid_until",
                    "granted_by_principal_id",
                    "created_at",
                    "supersedes_grant_id",
                }
                changed_authority = {
                    field
                    for field in authority_fields
                    if getattr(self, field) != getattr(original, field)
                }
                if changed_authority:
                    raise ValidationError(
                        "A PermissionGrant authority record is immutable; revoke it and create a new grant."
                    )
                lifecycle_changed = any(
                    getattr(self, field) != getattr(original, field)
                    for field in (
                        "grant_status",
                        "revoked_at",
                        "revoked_by_principal_id",
                        "revocation_reason",
                    )
                )
                if lifecycle_changed:
                    if original.grant_status != self.GrantStatus.ACTIVE:
                        raise ValidationError("Only an ACTIVE grant can be revoked.")
                    if self.grant_status != self.GrantStatus.REVOKED:
                        raise ValidationError("PermissionGrant lifecycle is one-way ACTIVE to REVOKED.")
        self.full_clean()
        return super().save(*args, **kwargs)

    @property
    def is_current(self) -> bool:
        now = timezone.now()
        return (
            self.grant_status == self.GrantStatus.ACTIVE
            and self.valid_from <= now
            and (self.valid_until is None or self.valid_until > now)
            and self.principal.principal_status == Principal.PrincipalStatus.ACTIVE
            and self.principal.is_active
        )
