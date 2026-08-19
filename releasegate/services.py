"""Deterministic orchestration for the frozen V1 manual-release boundary.

The functions in this module only compose the append-only domain models.  They
do not connect to, post to, or otherwise mutate an external platform.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.authorization import resolve_authorization
from accounts.models import PermissionGrant, Principal
from contentops.models import TaskSubmission
from workflow.services import guard_release_gate

from .models import (
    AccountEnvironmentBinding,
    ActingRole,
    CapabilityState,
    ChannelAccount,
    PolicyVersion,
    Publication,
    PublicationEvent,
    ReleaseGateRecord,
    RuleEvaluationResult,
    RuleEvaluationRun,
    RuntimeEnvironment,
    canonical_sha256,
    release_context_hash,
    required_policy_versions_for_contract,
    validate_sha256,
)


KNOWN_V1_RULES = frozenset({"exact_release_context"})
SERVICE_COMMAND_NAMESPACE = "growth-os:releasegate:v1"


@dataclass(frozen=True, slots=True)
class V1GateOrchestrationResult:
    publication: Publication
    evaluation_run: RuleEvaluationRun
    gate: ReleaseGateRecord
    terminal_event: PublicationEvent


def _command(root_command_id: uuid.UUID, operation: str) -> uuid.UUID:
    return uuid.uuid5(root_command_id, f"{SERVICE_COMMAND_NAMESPACE}:{operation}")


def _payload(operation: str, **values) -> str:
    return canonical_sha256({"operation": operation, **values})


def _publisher_grant(*, principal: Principal, product_id, channel_account: ChannelAccount, at):
    decision = resolve_authorization(
        principal=principal,
        acting_role=principal.role,
        action=PermissionGrant.Action.PUBLISH,
        scope_kind=PermissionGrant.ScopeKind.ACCOUNT,
        product=product_id,
        platform_code=channel_account.platform_code,
        account_ref=channel_account.account_code,
        at=at,
    )
    if not decision.allowed or decision.grant is None:
        raise ValidationError({"publisher_principal": f"PUBLISH authorization denied: {decision.reason}."})
    return decision.grant


def _trusted_evaluator(*, task_contract_version, at):
    """Choose the first deterministic active service identity with REVIEW."""

    product_id = task_contract_version.product_profile_version.product_id
    candidates = Principal.objects.filter(
        principal_type__in=[
            Principal.PrincipalType.SERVICE_ACCOUNT,
            Principal.PrincipalType.SYSTEM,
        ],
        principal_status=Principal.PrincipalStatus.ACTIVE,
        is_active=True,
    ).order_by("username", "id")
    for candidate in candidates:
        decision = resolve_authorization(
            principal=candidate,
            acting_role=candidate.role,
            action=PermissionGrant.Action.REVIEW,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=product_id,
            at=at,
        )
        if decision.allowed and decision.grant is not None:
            return candidate, decision.grant
    raise ValidationError(
        {"rule_evaluator": "No active SERVICE_ACCOUNT or SYSTEM Principal has current explicit REVIEW authorization."}
    )


def _current_binding_and_capability(*, channel_account, runtime_environment):
    binding = AccountEnvironmentBinding.objects.filter(
        channel_account=channel_account,
        runtime_environment=runtime_environment,
    ).order_by("-binding_version").first()
    if binding is None:
        raise ValidationError(
            {"runtime_environment": "No account/environment binding exists for this manual release."}
        )
    capability = CapabilityState.objects.filter(
        account_environment_binding=binding,
        capability_code=CapabilityState.MANUAL_PUBLISH,
    ).order_by("-state_version").first()
    if capability is None:
        raise ValidationError(
            {"capability_state": "No MANUAL_PUBLISH capability fact exists for the current binding."}
        )
    return binding, capability


def _release_intent(
    *, submission, publisher, publisher_grant, root_command_id, recorded_by_principal
):
    command_id = _command(root_command_id, "publication-intent")
    payload_hash = _payload(
        "publication-intent",
        submission_id=str(submission.pk),
        publisher_principal_id=str(publisher.pk),
        publisher_grant_id=str(publisher_grant.pk),
    )
    commanded = Publication.objects.filter(creation_command_id=command_id).first()
    if commanded is not None:
        # Call the manager so a reused command with different inputs is rejected
        # by its authoritative payload comparison.
        return Publication.objects.create_intent(
            submission=submission,
            command_id=command_id,
            payload_hash=payload_hash,
            actor_principal=publisher,
            acting_role=publisher.role,
            permission_grant=publisher_grant,
            recorded_by_principal=recorded_by_principal,
        )

    reusable = Publication.objects.filter(
        submission=submission,
        requested_by_principal=publisher,
        requested_under_grant=publisher_grant,
    ).exclude(status=Publication.Status.MANUAL_PUBLISHED_RECORDED).order_by("-created_at", "-id").first()
    if reusable is not None:
        return reusable
    if Publication.objects.filter(
        submission=submission,
        status=Publication.Status.MANUAL_PUBLISHED_RECORDED,
    ).exists():
        raise ValidationError({"submission": "This exact submission already has recorded publication proof."})
    return Publication.objects.create_intent(
        submission=submission,
        command_id=command_id,
        payload_hash=payload_hash,
        actor_principal=publisher,
        acting_role=publisher.role,
        permission_grant=publisher_grant,
        recorded_by_principal=recorded_by_principal,
    )


def _existing_orchestration_result(*, gate, publication, expected_payload_hash):
    if gate is None:
        return None
    if gate.payload_hash != expected_payload_hash or gate.publication_id != publication.pk:
        raise ValidationError("The orchestration command_id was already used with different release inputs.")
    event_type = (
        PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH
        if gate.outcome == ReleaseGateRecord.Outcome.PASSED
        else PublicationEvent.EventType.GATE_BLOCKED
    )
    terminal = PublicationEvent.objects.filter(
        publication=publication,
        release_gate=gate,
        event_type=event_type,
    ).order_by("event_sequence").first()
    if terminal is None:
        raise ValidationError("An existing orchestration command has an incomplete Gate/event chain.")
    if gate.outcome == ReleaseGateRecord.Outcome.PASSED and gate.current_blockers():
        raise ValidationError("The prior Gate is now stale; reevaluate with a new command_id.")
    return V1GateOrchestrationResult(publication, gate.evaluation_run, gate, terminal)


@transaction.atomic
def orchestrate_v1_release_gate(
    *,
    task,
    submission: TaskSubmission,
    publisher_principal: Principal,
    channel_account: ChannelAccount,
    runtime_environment: RuntimeEnvironment,
    command_id: uuid.UUID,
) -> V1GateOrchestrationResult:
    """Evaluate the frozen V1 gate and stop at manual-publish readiness.

    A passed Gate appends READY_FOR_MANUAL_PUBLISH.  A blocked Gate appends
    GATE_BLOCKED.  No branch invokes an external platform.
    """

    try:
        root_command_id = uuid.UUID(str(command_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"command_id": "A valid UUID command_id is required."}) from exc

    task = type(task).objects.select_related("contract_version__product_profile_version").get(pk=task.pk)
    submission = TaskSubmission.objects.select_related(
        "task__contract_version__product_profile_version",
        "primary_asset_version",
    ).get(pk=submission.pk)
    publisher = Principal.objects.get(pk=publisher_principal.pk)
    channel_account = ChannelAccount.objects.get(pk=channel_account.pk)
    runtime_environment = RuntimeEnvironment.objects.get(pk=runtime_environment.pk)
    if submission.task_id != task.pk:
        raise ValidationError({"submission": "The submission does not belong to the exact Task."})
    try:
        review = submission.final_review
    except ObjectDoesNotExist as exc:
        raise ValidationError({"submission": "The exact submission has no final human review."}) from exc
    guard_release_gate(task, submission=submission, review_decision=review)

    now = timezone.now()
    publisher_grant = _publisher_grant(
        principal=publisher,
        product_id=task.product_id,
        channel_account=channel_account,
        at=now,
    )
    evaluator, evaluator_grant = _trusted_evaluator(
        task_contract_version=task.contract_version,
        at=now,
    )
    binding, capability = _current_binding_and_capability(
        channel_account=channel_account,
        runtime_environment=runtime_environment,
    )
    policies, has_contract_snapshot = required_policy_versions_for_contract(task.contract_version, at=now)
    if not has_contract_snapshot or not policies:
        raise ValidationError(
            {"policy_set": "The Task contract has no exact required PolicyVersion snapshot."}
        )

    publication = _release_intent(
        submission=submission,
        publisher=publisher,
        publisher_grant=publisher_grant,
        root_command_id=root_command_id,
        recorded_by_principal=publisher,
    )
    publication.refresh_from_db()
    context_hash = release_context_hash(
        publication=publication,
        review_decision=review,
        primary_asset_version=submission.primary_asset_version,
        task_contract_version=task.contract_version,
        publisher_principal=publisher,
        publisher_grant=publisher_grant,
        channel_account=channel_account,
        runtime_environment=runtime_environment,
        account_environment_binding=binding,
        capability_state=capability,
        policy_versions=policies,
    )
    gate_command_id = _command(root_command_id, "release-gate")
    gate_payload_hash = _payload(
        "release-gate",
        task_id=str(task.pk),
        submission_id=str(submission.pk),
        publication_id=str(publication.pk),
        publisher_principal_id=str(publisher.pk),
        publisher_grant_id=str(publisher_grant.pk),
        channel_account_id=str(channel_account.pk),
        runtime_environment_id=str(runtime_environment.pk),
        context_sha256=context_hash,
    )
    existing = ReleaseGateRecord.objects.filter(command_id=gate_command_id).select_related(
        "evaluation_run"
    ).first()
    replay = _existing_orchestration_result(
        gate=existing,
        publication=publication,
        expected_payload_hash=gate_payload_hash,
    )
    if replay is not None:
        return replay

    if publication.status == Publication.Status.MANUAL_PUBLISHED_RECORDED:
        raise ValidationError({"publication": "Manual publication proof is already recorded."})
    if publication.status == Publication.Status.READY_FOR_MANUAL_PUBLISH:
        current_gate = publication.current_gate
        requested_context_matches = bool(
            current_gate
            and current_gate.publisher_principal_id == publisher.pk
            and current_gate.publisher_grant_id == publisher_grant.pk
            and current_gate.channel_account_id == channel_account.pk
            and current_gate.runtime_environment_id == runtime_environment.pk
            and current_gate.account_environment_binding_id == binding.pk
            and current_gate.capability_state_id == capability.pk
        )
        if requested_context_matches and not current_gate.current_blockers():
            terminal = publication.events.filter(
                release_gate=current_gate,
                event_type=PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH,
            ).order_by("-event_sequence").first()
            if terminal is None:
                raise ValidationError("READY publication has no immutable READY event.")
            return V1GateOrchestrationResult(
                publication,
                current_gate.evaluation_run,
                current_gate,
                terminal,
            )
        publication.append_event(
            event_type=PublicationEvent.EventType.GATE_PENDING,
            command_id=_command(root_command_id, "recover-gate-pending"),
            payload_hash=_payload("recover-gate-pending", publication_id=str(publication.pk)),
            expected_state_version=publication.state_version,
            actor_principal=publisher,
            acting_role=publisher.role,
            permission_grant=publisher_grant,
            recorded_by_principal=publisher,
            release_gate=publication.current_gate,
        )
        publication.refresh_from_db()
    elif publication.status == Publication.Status.GATE_BLOCKED:
        publication.append_event(
            event_type=PublicationEvent.EventType.GATE_PENDING,
            command_id=_command(root_command_id, "recover-gate-pending"),
            payload_hash=_payload("recover-gate-pending", publication_id=str(publication.pk)),
            expected_state_version=publication.state_version,
            actor_principal=publisher,
            acting_role=publisher.role,
            permission_grant=publisher_grant,
            recorded_by_principal=publisher,
            release_gate=publication.current_gate,
        )
        publication.refresh_from_db()
    elif publication.status != Publication.Status.GATE_PENDING:
        raise ValidationError({"publication": "Publication is not in a gate-evaluable state."})

    run = RuleEvaluationRun.objects.start(
        publication=publication,
        policy_versions=policies,
        context_hash=context_hash,
        evaluator_key="deterministic-v1",
        command_id=_command(root_command_id, "rule-evaluation-run"),
        payload_hash=_payload(
            "rule-evaluation-run",
            publication_id=str(publication.pk),
            context_sha256=context_hash,
            policy_version_ids=[str(policy.pk) for policy in policies],
        ),
        initiated_by_principal=evaluator,
        acting_role=ActingRole.SYSTEM,
        permission_grant=evaluator_grant,
        recorded_by_principal=evaluator,
    )
    for policy in policies:
        for rule in policy.normalized_rules():
            code = rule["rule_code"]
            required = rule["required"]
            if code in KNOWN_V1_RULES:
                result = RuleEvaluationResult.Result.PASS
                detail = {"evaluator": "deterministic-v1", "context_sha256": context_hash}
            elif required:
                result = RuleEvaluationResult.Result.ERROR
                detail = {"error": "UNKNOWN_REQUIRED_RULE", "rule_code": code}
            else:
                result = RuleEvaluationResult.Result.SKIPPED
                detail = {"reason": "UNKNOWN_OPTIONAL_RULE", "rule_code": code}
            RuleEvaluationResult.objects.record(
                evaluation_run=run,
                policy_version=policy,
                rule_code=code,
                required=required,
                result=result,
                detail=detail,
                command_id=_command(root_command_id, f"rule-result:{policy.pk}:{code}"),
                payload_hash=_payload(
                    "rule-result",
                    evaluation_run_id=str(run.pk),
                    policy_version_id=str(policy.pk),
                    rule_code=code,
                    required=required,
                    result=result,
                    detail=detail,
                ),
                evaluated_by_principal=evaluator,
                recorded_by_principal=evaluator,
            )
    run = run.complete()
    gate = ReleaseGateRecord.objects.evaluate(
        publication=publication,
        task_submission=submission,
        review_decision=review,
        primary_asset_version=submission.primary_asset_version,
        task_contract_version=task.contract_version,
        publisher_principal=publisher,
        publisher_grant=publisher_grant,
        channel_account=channel_account,
        runtime_environment=runtime_environment,
        account_environment_binding=binding,
        capability_state=capability,
        evaluation_run=run,
        command_id=gate_command_id,
        payload_hash=gate_payload_hash,
        evaluated_by_principal=evaluator,
        evaluated_by_acting_role=ActingRole.SYSTEM,
        recorded_by_principal=evaluator,
    )
    terminal_type = (
        PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH
        if gate.outcome == ReleaseGateRecord.Outcome.PASSED
        else PublicationEvent.EventType.GATE_BLOCKED
    )
    terminal = publication.append_event(
        event_type=terminal_type,
        release_gate=gate,
        command_id=_command(root_command_id, f"publication-event:{terminal_type}"),
        payload_hash=_payload(
            "publication-event",
            publication_id=str(publication.pk),
            gate_id=str(gate.pk),
            event_type=terminal_type,
        ),
        expected_state_version=publication.state_version,
        actor_principal=publisher,
        acting_role=publisher.role,
        permission_grant=publisher_grant,
        recorded_by_principal=publisher,
    )
    publication.refresh_from_db()
    return V1GateOrchestrationResult(publication, run, gate, terminal)


@transaction.atomic
def record_manual_publication_proof(
    *,
    publication: Publication,
    publisher_principal: Principal,
    command_id: uuid.UUID,
    external_url: str = "",
    external_publication_id: str = "",
    proof_reference: str,
    proof_sha256: str,
) -> PublicationEvent:
    """Append external proof only; this function never publishes externally."""

    try:
        command_id = uuid.UUID(str(command_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError({"command_id": "A valid UUID command_id is required."}) from exc
    validate_sha256(proof_sha256)
    payload_hash = _payload(
        "manual-publication-proof",
        publication_id=str(publication.pk),
        external_url=external_url,
        external_publication_id=external_publication_id,
        proof_reference=proof_reference,
        proof_sha256=proof_sha256,
    )
    existing = PublicationEvent.objects.filter(command_id=command_id).first()
    if existing is not None:
        if (
            existing.payload_hash != payload_hash
            or existing.publication_id != publication.pk
            or existing.event_type != PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED
        ):
            raise ValidationError("The command_id was already used with different proof inputs.")
        return existing

    publication = Publication.objects.select_related(
        "current_gate__channel_account",
        "current_gate__publisher_grant",
        "requested_by_principal",
    ).get(pk=publication.pk)
    publisher = Principal.objects.get(pk=publisher_principal.pk)
    if publication.requested_by_principal_id != publisher.pk:
        raise ValidationError({"publisher_principal": "Only the exact authorized Publisher may record proof."})
    gate = publication.current_gate
    if gate is None:
        raise ValidationError({"publication": "Manual proof requires the exact current READY Gate."})
    grant = _publisher_grant(
        principal=publisher,
        product_id=gate.task_contract_version.product_profile_version.product_id,
        channel_account=gate.channel_account,
        at=timezone.now(),
    )
    if grant.pk != publication.requested_under_grant_id or grant.pk != gate.publisher_grant_id:
        raise ValidationError({"publisher_principal": "The current PUBLISH authorization no longer matches the Gate."})
    return publication.append_event(
        event_type=PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
        release_gate=gate,
        command_id=command_id,
        payload_hash=payload_hash,
        expected_state_version=publication.state_version,
        actor_principal=publisher,
        acting_role=publisher.role,
        permission_grant=grant,
        recorded_by_principal=publisher,
        external_publication_id=external_publication_id,
        external_url=external_url,
        proof_reference=proof_reference,
        proof_sha256=proof_sha256,
    )
