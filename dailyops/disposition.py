from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from accounts.authorization import require_authorization
from accounts.models import PermissionGrant, Principal
from core.audit_notes import tag_optional_audit_note
from dailyops.models import DailyBatchDispositionEvent
from intelligence.exceptions import CommandReplayConflict
from intelligence.models import CollectionRun, Initiative, ProductOpportunity, canonical_sha256
from products.models import Product


@dataclass(frozen=True, slots=True)
class DailyBatchDispositionResult:
    event: DailyBatchDispositionEvent
    created: bool


def batch_disposition(*, batch_key, product: Product) -> DailyBatchDispositionEvent | None:
    return DailyBatchDispositionEvent.objects.filter(
        batch_key=batch_key,
        product=product,
    ).first()


def _has_formal_execution(*, batch_key, product: Product) -> bool:
    opportunity = ProductOpportunity.objects.filter(
        opportunity_key=f"daily-{batch_key.hex}",
        product=product,
    ).first()
    return bool(opportunity and Initiative.objects.filter(opportunity=opportunity).exists())


def lock_daily_batch_runs(*, batch_key, product: Product) -> tuple[CollectionRun, ...]:
    """Lock the immutable collection rows used as the batch concurrency mutex."""

    runs = tuple(
        CollectionRun.objects.select_for_update()
        .filter(batch_key=batch_key)
        .order_by("created_at", "id")
    )
    if not runs:
        raise ValidationError("这次工作不存在。")
    if any(str(run.query_spec.get("product_id", "")) != str(product.pk) for run in runs):
        raise ValidationError("这次工作不属于当前产品。")
    return runs


@transaction.atomic
def dispose_daily_batch(
    *,
    batch_key: uuid.UUID,
    product: Product,
    command_id: uuid.UUID,
    reason: str,
    principal: Principal,
    acting_role: str,
) -> DailyBatchDispositionResult:
    """Hide a run from active work without deleting or rewriting history."""

    replay = DailyBatchDispositionEvent.objects.filter(command_id=command_id).first()
    normalized_reason = tag_optional_audit_note(
        reason,
        default="未填写额外说明；由系统记录本次隐藏操作。",
        existing_value=replay.reason if replay is not None else None,
    )

    if principal.role not in {Principal.Role.OWNER, Principal.Role.OPERATIONS_ADMIN}:
        raise PermissionDenied("ONLY_OWNER_OR_ADMIN_CAN_HIDE_DAILY_WORK")

    # All downstream mutations use these rows as the first database lock too,
    # so a mutation and a disposition cannot both pass their active check.
    lock_daily_batch_runs(batch_key=batch_key, product=product)

    grant = require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.CANCEL_TASK,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )
    if grant.scope_kind != PermissionGrant.ScopeKind.PRODUCT or grant.product_id != product.pk:
        raise PermissionDenied("EXACT_PRODUCT_CANCEL_TASK_GRANT_REQUIRED")
    # Serialize against grant revocation.  If revocation wins, the refreshed
    # authorization check below fails; if this lock wins, the recorded
    # decision completes before revocation can take effect.
    grant = PermissionGrant.objects.select_for_update().get(pk=grant.pk)
    refreshed_grant = require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.CANCEL_TASK,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )
    if refreshed_grant.pk != grant.pk:
        raise PermissionDenied("EXACT_PRODUCT_CANCEL_TASK_GRANT_CHANGED")

    disposition = (
        DailyBatchDispositionEvent.Disposition.ARCHIVED
        if _has_formal_execution(batch_key=batch_key, product=product)
        else DailyBatchDispositionEvent.Disposition.ABANDONED
    )
    payload_hash = canonical_sha256(
        {
            "batch_key": str(batch_key),
            "product_id": str(product.pk),
            "disposition": disposition,
            "reason": normalized_reason,
        }
    )

    if replay is not None:
        if replay.payload_hash != payload_hash or replay.principal_id != principal.pk:
            raise CommandReplayConflict("The command ID was already used for another Daily batch decision.")
        return DailyBatchDispositionResult(event=replay, created=False)

    existing = DailyBatchDispositionEvent.objects.filter(batch_key=batch_key).first()
    if existing is not None:
        if existing.payload_hash == payload_hash and existing.principal_id == principal.pk:
            return DailyBatchDispositionResult(event=existing, created=False)
        raise ValidationError("这次工作已经删除草稿或归档，不能重复改变处理结论。")

    event = DailyBatchDispositionEvent.objects.create(
        batch_key=batch_key,
        product=product,
        disposition=disposition,
        reason=normalized_reason,
        principal=principal,
        acting_role=acting_role,
        permission_grant=grant,
        command_id=command_id,
        payload_hash=payload_hash,
    )
    return DailyBatchDispositionResult(event=event, created=True)
