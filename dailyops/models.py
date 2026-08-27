from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from intelligence.models import ActingRole, ImmutableFact, canonical_sha256


class DailyBatchDispositionEvent(ImmutableFact):
    """Append-only decision to remove a Daily Operations run from active UI.

    A Daily batch is represented by immutable CollectionRun rows sharing a
    ``batch_key``.  This event deliberately leaves those rows, their evidence,
    and any downstream planning/execution facts untouched.
    """

    class Disposition(models.TextChoices):
        ABANDONED = "ABANDONED", "Abandoned draft"
        ARCHIVED = "ARCHIVED", "Abandoned and archived"

    batch_key = models.UUIDField(unique=True)
    product = models.ForeignKey(
        "products.Product",
        on_delete=models.PROTECT,
        related_name="daily_batch_disposition_events",
    )
    disposition = models.CharField(max_length=16, choices=Disposition.choices)
    reason = models.TextField()
    principal = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="daily_batch_disposition_events",
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant",
        on_delete=models.PROTECT,
        related_name="daily_batch_disposition_events",
    )
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(reason=""),
                name="daily_batch_disposition_reason_set",
            ),
            models.CheckConstraint(
                condition=~models.Q(payload_hash=""),
                name="daily_batch_disposition_hash_set",
            ),
        ]

    def payload(self) -> dict[str, Any]:
        return {
            "batch_key": str(self.batch_key),
            "product_id": str(self.product_id),
            "disposition": self.disposition,
            "reason": self.reason,
        }

    def clean(self):
        super().clean()
        if not self.reason.strip():
            raise ValidationError({"reason": "A plain-language reason is required."})
        if self.principal_id and self.principal.role not in {"OWNER", "OPERATIONS_ADMIN"}:
            raise ValidationError(
                {"principal": "Only an Owner or Operations Admin may hide shared Daily work."}
            )
        if self.permission_grant_id and self.principal_id:
            grant = self.permission_grant
            if grant.principal_id != self.principal_id:
                raise ValidationError({"permission_grant": "The grant must belong to the acting Principal."})
            if grant.action != "CANCEL_TASK" or grant.effect != "ALLOW":
                raise ValidationError({"permission_grant": "An ALLOW CANCEL_TASK grant is required."})
            if grant.scope_kind != "PRODUCT" or grant.product_id != self.product_id:
                raise ValidationError(
                    {"permission_grant": "The CANCEL_TASK grant must cover the exact Product."}
                )
            if self.acting_role != self.principal.role:
                raise ValidationError({"acting_role": "The acting role must match the Principal."})
            if not grant.is_current:
                raise ValidationError({"permission_grant": "The exact grant must still be active."})

            from accounts.authorization import resolve_authorization
            from accounts.models import PermissionGrant

            decision = resolve_authorization(
                principal=self.principal,
                acting_role=self.acting_role,
                action=PermissionGrant.Action.CANCEL_TASK,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=self.product,
            )
            if not decision.allowed or decision.grant is None or decision.grant.pk != grant.pk:
                raise ValidationError(
                    {"permission_grant": "The exact grant must be the current fail-closed authorization."}
                )
        expected = canonical_sha256(self.payload())
        if not self.payload_hash:
            self.payload_hash = expected
        elif self.payload_hash != expected:
            raise ValidationError({"payload_hash": "Daily batch disposition payload hash mismatch."})
