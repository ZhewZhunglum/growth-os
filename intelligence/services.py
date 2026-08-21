from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TypeVar

from django.db import transaction

from accounts.authorization import require_authorization
from accounts.models import PermissionGrant, Principal
from intelligence.exceptions import CommandReplayConflict, IllegalStateTransition, StateVersionConflict
from intelligence.models import (
    AvailabilityState,
    ChannelPlan,
    ChannelPlanStateEvent,
    CollectionRun,
    Initiative,
    InitiativeStateEvent,
    OpportunityStateEvent,
    ProductOpportunity,
    SourceRegistry,
    canonical_sha256,
)


AggregateT = TypeVar("AggregateT", ProductOpportunity, Initiative, ChannelPlan)
EventT = TypeVar("EventT", OpportunityStateEvent, InitiativeStateEvent, ChannelPlanStateEvent)


@dataclass(frozen=True, slots=True)
class TransitionResult:
    aggregate: ProductOpportunity | Initiative | ChannelPlan
    event: OpportunityStateEvent | InitiativeStateEvent | ChannelPlanStateEvent
    created: bool


@dataclass(frozen=True, slots=True)
class CollectionRunResult:
    run: CollectionRun
    created: bool


def record_collection_run(
    *,
    source: SourceRegistry,
    batch_key: uuid.UUID,
    attempt_number: int,
    operation_key: uuid.UUID,
    query_spec: dict,
    status: str,
    availability_state: str,
    started_at,
    completed_at,
    result_summary: dict,
    error_code: str,
    principal: Principal,
    acting_role: str,
) -> CollectionRunResult:
    """Append one terminal collection attempt with replay-safe provenance."""

    if status not in CollectionRun.Status.values:
        raise ValueError(f"Unknown terminal collection status: {status}.")
    if availability_state not in AvailabilityState.values:
        raise ValueError(f"Unknown availability state: {availability_state}.")
    request_hash = canonical_sha256(query_spec)
    operation_hash = canonical_sha256(
        {
            "source_id": str(source.pk),
            "batch_key": str(batch_key),
            "attempt_number": attempt_number,
            "query_spec": query_spec,
            "status": status,
            "availability_state": availability_state,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
            "result_summary": result_summary,
            "error_code": error_code,
        }
    )
    replay = CollectionRun.objects.filter(operation_key=operation_key).first()
    if replay is not None:
        if replay.operation_payload_hash != operation_hash:
            raise CommandReplayConflict("The operation key was already used with a different collection payload.")
        return CollectionRunResult(replay, False)

    with transaction.atomic():
        grant = require_authorization(
            principal=principal,
            acting_role=acting_role,
            action=PermissionGrant.Action.COLLECT_READ_ONLY,
            scope_kind=PermissionGrant.ScopeKind.PLATFORM,
            platform_code=source.platform_code,
        )
        replay = CollectionRun.objects.filter(operation_key=operation_key).first()
        if replay is not None:
            if replay.operation_payload_hash != operation_hash:
                raise CommandReplayConflict("The operation key was already used with a different collection payload.")
            return CollectionRunResult(replay, False)
        run = CollectionRun.objects.create(
            source=source,
            batch_key=batch_key,
            attempt_number=attempt_number,
            operation_key=operation_key,
            request_payload_hash=request_hash,
            operation_payload_hash=operation_hash,
            query_spec=query_spec,
            status=status,
            availability_state=availability_state,
            started_at=started_at,
            completed_at=completed_at,
            result_summary=result_summary,
            error_code=error_code,
            executed_by_principal=principal,
            permission_grant=grant,
        )
        return CollectionRunResult(run, True)


def _transition_payload(*, aggregate_id, to_state: str, expected_version: int, reason: str) -> dict:
    return {
        "aggregate_id": str(aggregate_id),
        "to_state": to_state,
        "expected_version": expected_version,
        "reason": reason,
    }


def _event_replay(*, event_model, command_id: uuid.UUID, payload_hash: str) -> TransitionResult | None:
    event = event_model.objects.filter(command_id=command_id).first()
    if event is None:
        return None
    if event.payload_hash != payload_hash:
        raise CommandReplayConflict("The command ID was already used with a different transition payload.")
    aggregate_field = next(
        name for name in ("opportunity", "initiative", "channel_plan") if hasattr(event, f"{name}_id")
    )
    return TransitionResult(getattr(event, aggregate_field), event, False)


def _transition(
    *,
    aggregate_model,
    event_model,
    aggregate_field: str,
    aggregate_id,
    to_state: str,
    expected_version: int,
    command_id: uuid.UUID,
    reason: str,
    principal: Principal,
    acting_role: str,
) -> TransitionResult:
    if to_state not in aggregate_model.State.values:
        raise IllegalStateTransition(f"Unknown target state: {to_state}.")
    payload_hash = canonical_sha256(
        _transition_payload(
            aggregate_id=aggregate_id,
            to_state=to_state,
            expected_version=expected_version,
            reason=reason,
        )
    )
    replay = _event_replay(event_model=event_model, command_id=command_id, payload_hash=payload_hash)
    if replay is not None:
        return replay

    with transaction.atomic():
        aggregate = aggregate_model.objects.select_for_update().get(pk=aggregate_id)
        replay = _event_replay(event_model=event_model, command_id=command_id, payload_hash=payload_hash)
        if replay is not None:
            return replay
        if aggregate.state_version != expected_version:
            raise StateVersionConflict(
                f"Expected state version {expected_version}, found {aggregate.state_version}."
            )
        allowed = aggregate_model.TRANSITIONS.get(aggregate.current_state, set())
        if to_state not in allowed:
            raise IllegalStateTransition(f"{aggregate.current_state} cannot transition to {to_state}.")

        grant = require_authorization(
            principal=principal,
            acting_role=acting_role,
            action=PermissionGrant.Action.EDIT,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=aggregate.product_id,
        )
        next_version = aggregate.state_version + 1
        event = event_model.objects.create(
            **{
                aggregate_field: aggregate,
                "sequence": next_version,
                "from_state": aggregate.current_state,
                "to_state": to_state,
                "command_id": command_id,
                "payload_hash": payload_hash,
                "reason": reason,
                "principal": principal,
                "acting_role": acting_role,
                "permission_grant": grant,
            }
        )
        aggregate.current_state = to_state
        aggregate.state_version = next_version
        aggregate.updated_by_principal = principal
        aggregate._allow_projection_save = True
        aggregate.save(update_fields=["current_state", "state_version", "updated_by_principal", "updated_at"])
        del aggregate._allow_projection_save
        return TransitionResult(aggregate, event, True)


def transition_opportunity(
    *, opportunity_id, to_state: str, expected_version: int, command_id: uuid.UUID,
    reason: str, principal: Principal, acting_role: str,
) -> TransitionResult:
    return _transition(
        aggregate_model=ProductOpportunity,
        event_model=OpportunityStateEvent,
        aggregate_field="opportunity",
        aggregate_id=opportunity_id,
        to_state=to_state,
        expected_version=expected_version,
        command_id=command_id,
        reason=reason,
        principal=principal,
        acting_role=acting_role,
    )


def transition_initiative(
    *, initiative_id, to_state: str, expected_version: int, command_id: uuid.UUID,
    reason: str, principal: Principal, acting_role: str,
) -> TransitionResult:
    return _transition(
        aggregate_model=Initiative,
        event_model=InitiativeStateEvent,
        aggregate_field="initiative",
        aggregate_id=initiative_id,
        to_state=to_state,
        expected_version=expected_version,
        command_id=command_id,
        reason=reason,
        principal=principal,
        acting_role=acting_role,
    )


def transition_channel_plan(
    *, channel_plan_id, to_state: str, expected_version: int, command_id: uuid.UUID,
    reason: str, principal: Principal, acting_role: str,
) -> TransitionResult:
    return _transition(
        aggregate_model=ChannelPlan,
        event_model=ChannelPlanStateEvent,
        aggregate_field="channel_plan",
        aggregate_id=channel_plan_id,
        to_state=to_state,
        expected_version=expected_version,
        command_id=command_id,
        reason=reason,
        principal=principal,
        acting_role=acting_role,
    )
