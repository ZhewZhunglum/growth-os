from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import ValidationError
from django.db import transaction

from accounts.authorization import require_authorization
from accounts.models import PermissionGrant, Principal
from dailyops.disposition import batch_disposition, lock_daily_batch_runs
from intelligence.exceptions import CommandReplayConflict
from intelligence.models import (
    AssessmentMethod,
    EvidenceInvalidationEvent,
    ExternalEvidenceItem,
    ProductOpportunity,
    SignalAssessment,
    canonical_sha256,
)
from products.models import Product


@dataclass(frozen=True, slots=True)
class EvidenceInvalidationResult:
    event: EvidenceInvalidationEvent
    created: bool


def ensure_batch_active(*, batch_key, product: Product) -> None:
    """Reject any active-work command after the batch was hidden or archived."""

    if batch_disposition(batch_key=batch_key, product=product) is not None:
        raise ValidationError(
            "这次工作已经删除草稿或归档，只能查看历史，不能继续采集或修改线索。"
        )


def ensure_batch_evidence_editable(*, batch_key, product: Product) -> None:
    """Keep evidence changes before the first durable human planning decision."""

    ensure_batch_active(batch_key=batch_key, product=product)
    assessment_key = f"daily-analysis-{batch_key.hex}"
    human_decision_exists = SignalAssessment.objects.filter(
        assessment_key=assessment_key,
        method=AssessmentMethod.HUMAN,
    ).exists()
    opportunity_exists = ProductOpportunity.objects.filter(
        opportunity_key=f"daily-{batch_key.hex}",
        product=product,
    ).exists()
    if human_decision_exists or opportunity_exists:
        raise ValidationError(
            "这次建议已经由人工采用，不能再采集、补录、更正或移除线索。"
            "请返回机会与计划，新开一轮工作。"
        )


def _payload_hash(*, evidence_id, product_id, reason: str) -> str:
    return canonical_sha256(
        {
            "evidence_item_id": str(evidence_id),
            "product_id": str(product_id),
            "reason": reason,
        }
    )


@transaction.atomic
def invalidate_evidence(
    *,
    evidence_id,
    product: Product,
    batch_key,
    command_id: uuid.UUID,
    reason: str,
    principal: Principal,
    acting_role: str,
) -> EvidenceInvalidationResult:
    """Remove evidence from future decisions without rewriting history."""

    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValidationError("请简单说明为什么移除这条来源。")
    expected_hash = _payload_hash(
        evidence_id=evidence_id,
        product_id=product.pk,
        reason=normalized_reason,
    )
    grant = require_authorization(
        principal=principal,
        acting_role=acting_role,
        action=PermissionGrant.Action.EDIT,
        scope_kind=PermissionGrant.ScopeKind.PRODUCT,
        product=product,
    )
    replay = (
        EvidenceInvalidationEvent.objects.filter(command_id=command_id)
        .select_related("evidence_item__collection_run")
        .first()
    )
    if replay is not None:
        if replay.payload_hash != expected_hash:
            raise CommandReplayConflict("The command ID was already used for another evidence removal.")
        if replay.invalidated_by_principal_id != principal.pk:
            raise CommandReplayConflict("The command ID belongs to another Principal.")
        if str(replay.evidence_item.collection_run.batch_key) != str(batch_key):
            raise CommandReplayConflict("The command ID belongs to another Daily Operations batch.")
        return EvidenceInvalidationResult(event=replay, created=False)

    candidate = (
        ExternalEvidenceItem.objects
        .select_related("collection_run")
        .get(pk=evidence_id)
    )
    batch_product_id = str(candidate.collection_run.query_spec.get("product_id", ""))
    if batch_product_id != str(product.pk):
        raise ValidationError("这条来源不属于当前产品，不能移除。")
    if str(candidate.collection_run.batch_key) != str(batch_key):
        raise ValidationError("这条来源不属于本次分析，不能从这里移除。")

    # Share the same immutable batch mutex as proposal acceptance and every
    # collection path.  The immutable candidate can be validated first so a
    # cross-batch request keeps its precise error, then locked for the write.
    lock_daily_batch_runs(batch_key=batch_key, product=product)
    ensure_batch_evidence_editable(batch_key=batch_key, product=product)
    evidence = (
        ExternalEvidenceItem.objects.select_for_update()
        .select_related("collection_run")
        .get(pk=candidate.pk)
    )

    existing = EvidenceInvalidationEvent.objects.filter(evidence_item=evidence).first()
    if existing is not None:
        return EvidenceInvalidationResult(event=existing, created=False)

    event = EvidenceInvalidationEvent.objects.create(
        evidence_item=evidence,
        product=product,
        command_id=command_id,
        payload_hash=expected_hash,
        reason=normalized_reason,
        invalidated_by_principal=principal,
        acting_role=acting_role,
        permission_grant=grant,
    )
    return EvidenceInvalidationResult(event=event, created=True)
