from __future__ import annotations

import uuid

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from .models import (
    Issue,
    IssueEvent,
    PolicyActivation,
    PolicyActivationEvent,
    PolicyRollbackEvent,
    RuleApprovalDecision,
    RuleProposalVersion,
    RuleValidationRun,
    canonical_sha256,
)


ISSUE_TRANSITIONS = {
    Issue.State.OPEN: {Issue.State.TRIAGED, Issue.State.ESCALATED_TO_MEETING, Issue.State.CLOSED},
    Issue.State.TRIAGED: {Issue.State.IN_PROGRESS, Issue.State.ESCALATED_TO_MEETING, Issue.State.CLOSED},
    Issue.State.IN_PROGRESS: {Issue.State.RESOLVED, Issue.State.ESCALATED_TO_MEETING},
    Issue.State.RESOLVED: {Issue.State.CLOSED, Issue.State.OPEN},
    Issue.State.CLOSED: {Issue.State.OPEN},
    Issue.State.ESCALATED_TO_MEETING: {Issue.State.IN_PROGRESS, Issue.State.RESOLVED, Issue.State.CLOSED},
}


def _require_grant(*, principal, acting_role: str, grant, actions: set[str]) -> None:
    principal.validate_acting_role(acting_role)
    if (
        grant.principal_id != principal.pk
        or grant.action not in actions
        or grant.effect != "ALLOW"
        or not grant.is_current
    ):
        raise ValidationError({"permission_grant": "The exact current Grant does not authorize this action."})


def transition_issue(
    *,
    issue: Issue,
    to_state: str,
    reason: str,
    command_id: uuid.UUID,
    expected_state_version: int,
    actor_principal,
    acting_role: str,
    permission_grant,
) -> IssueEvent:
    payload_hash = canonical_sha256(
        {
            "issue_id": str(issue.pk),
            "to_state": to_state,
            "reason": reason,
            "expected_state_version": expected_state_version,
            "actor_principal_id": str(actor_principal.pk),
            "acting_role": acting_role,
            "permission_grant_id": str(permission_grant.pk),
        }
    )
    existing = IssueEvent.objects.filter(command_id=command_id).first()
    if existing:
        if existing.payload_hash != payload_hash:
            raise ValidationError("command_id was already used with another Issue transition.")
        return existing
    _require_grant(
        principal=actor_principal,
        acting_role=acting_role,
        grant=permission_grant,
        actions={"EDIT", "EMERGENCY_STOP"},
    )
    with transaction.atomic():
        current = Issue.objects.select_for_update().get(pk=issue.pk)
        if current.state_version != expected_state_version:
            raise ValidationError("Issue version conflict; reread before retrying.")
        if to_state not in ISSUE_TRANSITIONS[current.current_state]:
            raise ValidationError(f"Invalid Issue transition: {current.current_state} -> {to_state}.")
        event = IssueEvent.objects.create(
            issue=current,
            from_state=current.current_state,
            to_state=to_state,
            event_sequence=current.state_version + 1,
            expected_state_version=current.state_version,
            resulting_state_version=current.state_version + 1,
            command_id=command_id,
            payload_hash=payload_hash,
            reason=reason,
            actor_principal=actor_principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
        )
        Issue.objects.filter(pk=current.pk).update(
            current_state=to_state,
            state_version=current.state_version + 1,
        )
        return event


def _passing_validations(proposal: RuleProposalVersion) -> dict[str, RuleValidationRun]:
    passing = {
        run.validation_type: run
        for run in proposal.validation_runs.filter(result=RuleValidationRun.Result.PASSED).order_by("completed_at")
    }
    required = [
        RuleValidationRun.ValidationType.HISTORICAL_REPLAY,
        RuleValidationRun.ValidationType.SHADOW,
        RuleValidationRun.ValidationType.CANARY,
    ]
    if any(kind not in passing for kind in required):
        raise ValidationError("Activation requires passing Historical Replay, Shadow, and Canary runs.")
    if not (
        passing[required[0]].completed_at <= passing[required[1]].started_at
        and passing[required[1]].completed_at <= passing[required[2]].started_at
    ):
        raise ValidationError("Rule validations must run in Replay -> Shadow -> Canary order.")
    return passing


def _current_approval(proposal: RuleProposalVersion) -> RuleApprovalDecision:
    decision = proposal.approval_decisions.order_by("-decided_at", "-id").first()
    if decision is None or decision.decision != RuleApprovalDecision.Decision.APPROVED:
        raise ValidationError("Policy activation requires a current explicit human approval.")
    if decision.approver_principal.principal_type != "HUMAN_USER":
        raise ValidationError("Policy activation requires a current explicit human approval.")
    if proposal.change_effect == RuleProposalVersion.ChangeEffect.RELAX and (
        decision.approver_principal.role != "OWNER" or decision.acting_role != "OWNER"
    ):
        raise ValidationError("RELAX activation requires an explicit Owner approval.")
    return decision


def activate_policy(
    *,
    proposal: RuleProposalVersion,
    activation_scope: dict,
    effective_from,
    command_id: uuid.UUID,
    actor_principal,
    acting_role: str,
    permission_grant,
) -> PolicyActivation:
    if actor_principal.principal_type != "HUMAN_USER":
        raise ValidationError(
            {"actor_principal": "Policy activation requires an explicit human action."}
        )
    _require_grant(
        principal=actor_principal,
        acting_role=acting_role,
        grant=permission_grant,
        actions={"APPROVE"},
    )
    if proposal.change_effect == RuleProposalVersion.ChangeEffect.RELAX and (
        actor_principal.role != "OWNER" or acting_role != "OWNER"
    ):
        raise ValidationError("RELAX can only be activated by an Owner acting as Owner.")
    if RuleProposalVersion.objects.filter(supersedes_version=proposal).exists():
        raise ValidationError("A superseded RuleProposalVersion cannot be activated.")
    passing = _passing_validations(proposal)
    approval = _current_approval(proposal)
    canary = passing[RuleValidationRun.ValidationType.CANARY]
    if approval.decided_at > canary.started_at:
        raise ValidationError("Human approval must precede Canary validation.")
    payload_hash = canonical_sha256(
        {
            "proposal_id": str(proposal.pk),
            "policy_version_id": str(proposal.candidate_policy_version_id),
            "activation_scope": activation_scope,
            "effective_from": effective_from.isoformat(),
            "actor_principal_id": str(actor_principal.pk),
            "acting_role": acting_role,
            "permission_grant_id": str(permission_grant.pk),
        }
    )
    replay = PolicyActivationEvent.objects.filter(command_id=command_id).select_related("policy_activation").first()
    if replay:
        if replay.payload_hash != payload_hash:
            raise ValidationError("command_id was already used with another activation.")
        return replay.policy_activation
    with transaction.atomic():
        locked = RuleProposalVersion.objects.select_for_update().get(pk=proposal.pk)
        if PolicyActivation.objects.filter(rule_proposal_version=locked).exists():
            raise ValidationError("This exact proposal version already has an activation.")
        activation = PolicyActivation(
            rule_proposal_version=locked,
            policy_version=locked.candidate_policy_version,
            activation_scope=activation_scope,
            effective_from=effective_from,
            activated_by_principal=actor_principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
        )
        activation._activation_service_authorized = True
        activation.save()
        event = PolicyActivationEvent(
            policy_activation=activation,
            event_type=PolicyActivationEvent.EventType.ACTIVATED,
            event_sequence=1,
            command_id=command_id,
            payload_hash=payload_hash,
            reason="Approved rule proposal activated after Replay, Shadow, and Canary.",
            actor_principal=actor_principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
        )
        event._activation_service_authorized = True
        event.save()
        return activation


def rollback_policy(
    *,
    activation: PolicyActivation,
    rollback_to_policy_version,
    reason: str,
    command_id: uuid.UUID,
    actor_principal,
    acting_role: str,
    permission_grant,
) -> PolicyRollbackEvent:
    _require_grant(
        principal=actor_principal,
        acting_role=acting_role,
        grant=permission_grant,
        actions={"APPROVE", "EMERGENCY_STOP"},
    )
    payload_hash = canonical_sha256(
        {
            "activation_id": str(activation.pk),
            "rollback_to_policy_version_id": str(rollback_to_policy_version.pk),
            "reason": reason,
            "actor_principal_id": str(actor_principal.pk),
            "acting_role": acting_role,
            "permission_grant_id": str(permission_grant.pk),
        }
    )
    existing = PolicyActivationEvent.objects.filter(command_id=command_id).first()
    if existing:
        if existing.payload_hash != payload_hash or not hasattr(existing, "rollback_detail"):
            raise ValidationError("command_id was already used with another activation event.")
        return existing.rollback_detail
    with transaction.atomic():
        locked = PolicyActivation.objects.select_for_update().get(pk=activation.pk)
        prior = locked.events.order_by("-event_sequence").first()
        if prior is None or prior.event_type in {
            PolicyActivationEvent.EventType.ROLLED_BACK,
            PolicyActivationEvent.EventType.SUPERSEDED,
        }:
            raise ValidationError("Only a currently active policy can be rolled back.")
        event = PolicyActivationEvent(
            policy_activation=locked,
            event_type=PolicyActivationEvent.EventType.ROLLED_BACK,
            event_sequence=prior.event_sequence + 1,
            previous_event=prior,
            command_id=command_id,
            payload_hash=payload_hash,
            reason=reason,
            actor_principal=actor_principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
            occurred_at=timezone.now(),
        )
        event._activation_service_authorized = True
        event.save()
        rollback = PolicyRollbackEvent(
            policy_activation=locked,
            activation_event=event,
            rollback_to_policy_version=rollback_to_policy_version,
            reason=reason,
            rollback_by_principal=actor_principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
        )
        rollback._activation_service_authorized = True
        rollback.save()
        return rollback
