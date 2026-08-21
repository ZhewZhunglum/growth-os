from __future__ import annotations

from datetime import timezone as datetime_timezone

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from accounts.authorization import require_authorization, resolve_authorization
from accounts.models import PermissionGrant, Principal
from governance.models import (
    Issue,
    IssueDecisionLink,
    Meeting,
    MeetingDecision,
    MeetingParticipant,
    RuleApprovalDecision,
    RuleProposalSourceLink,
    RuleProposalVersion,
    RuleValidationRun,
    canonical_sha256,
)
from releasegate.models import PolicyDefinition, PolicyVersion


def require_global_grant(actor: Principal, action: str) -> PermissionGrant:
    return require_authorization(
        principal=actor,
        acting_role=actor.role,
        action=action,
        scope_kind=PermissionGrant.ScopeKind.GLOBAL,
    )


def require_governance_view(actor: Principal) -> PermissionGrant:
    return require_global_grant(actor, PermissionGrant.Action.VIEW)


def _normalized_policy_rules(rules) -> list[dict]:
    """Return the exact rule manifest accepted by ``PolicyVersion``.

    String values such as ``"false"`` are rejected instead of being silently
    coerced to ``True`` by Python truthiness.
    """

    if not isinstance(rules, list) or not rules:
        raise ValidationError({"rules": "至少需要一条规则。"})
    normalized = []
    seen = set()
    for position, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            raise ValidationError({"rules": f"第 {position} 条规则必须是 JSON 对象。"})
        rule_code = str(rule.get("rule_code", rule.get("code", ""))).strip()
        if not rule_code:
            raise ValidationError({"rules": f"第 {position} 条规则缺少 rule_code。"})
        if rule_code in seen:
            raise ValidationError({"rules": f"规则编号重复：{rule_code}。"})
        required = rule.get("required", True)
        if not isinstance(required, bool):
            raise ValidationError({"rules": f"{rule_code} 的 required 只能是 true 或 false。"})
        seen.add(rule_code)
        normalized.append({"rule_code": rule_code, "required": required})
    return sorted(normalized, key=lambda item: item["rule_code"])


@transaction.atomic
def create_policy_definition(
    *,
    actor: Principal,
    policy_code: str,
    name: str,
    description: str = "",
    is_mandatory: bool = True,
) -> PolicyDefinition:
    """Create an active definition through an exact GLOBAL APPROVE Grant.

    The legacy release model has no Grant column, so web writes are restricted
    to this controlled transaction.  The later RuleProposal retains its exact
    Grant in the append-only governance chain.
    """

    require_global_grant(actor, PermissionGrant.Action.APPROVE)
    code = (policy_code or "").strip()
    title = (name or "").strip()
    if not code:
        raise ValidationError({"policy_code": "规则编号不能为空。"})
    if not title:
        raise ValidationError({"name": "规则名称不能为空。"})
    existing = PolicyDefinition.objects.select_for_update().filter(policy_code=code).first()
    expected = {
        "name": title,
        "description": (description or "").strip(),
        "is_mandatory": bool(is_mandatory),
        "status": PolicyDefinition.Status.ACTIVE,
    }
    if existing is not None:
        if all(getattr(existing, field) == value for field, value in expected.items()):
            return existing
        raise ValidationError({"policy_code": "该规则编号已存在，且内容不同；请使用新的编号。"})
    return PolicyDefinition.objects.create(
        policy_code=code,
        created_by_principal=actor,
        updated_by_principal=actor,
        **expected,
    )


@transaction.atomic
def append_policy_version(
    *,
    actor: Principal,
    policy_definition: PolicyDefinition,
    rules,
    effective_from,
    effective_until=None,
) -> PolicyVersion:
    """Append the next immutable candidate version, never update an old one."""

    require_global_grant(actor, PermissionGrant.Action.APPROVE)
    locked_definition = PolicyDefinition.objects.select_for_update().get(pk=policy_definition.pk)
    if locked_definition.status != PolicyDefinition.Status.ACTIVE:
        raise ValidationError({"policy_definition": "已停用的规则不能追加候选版本。"})
    normalized_rules = _normalized_policy_rules(rules)
    # Persist and hash the same UTC representation Django will read back from
    # PostgreSQL/SQLite; otherwise equal instants with different offsets yield
    # a manifest that no longer verifies after a database round trip.
    if timezone.is_aware(effective_from):
        effective_from = effective_from.astimezone(datetime_timezone.utc)
    if effective_until is not None and timezone.is_aware(effective_until):
        effective_until = effective_until.astimezone(datetime_timezone.utc)
    versions = PolicyVersion.objects.filter(policy_definition=locked_definition).order_by("-version_number")
    latest = versions.first()
    if latest is not None and (
        latest.rules == normalized_rules
        and latest.effective_from == effective_from
        and latest.effective_until == effective_until
    ):
        return latest
    version = PolicyVersion(
        policy_definition=locked_definition,
        version_number=(latest.version_number + 1) if latest else 1,
        rules=normalized_rules,
        effective_from=effective_from,
        effective_until=effective_until,
        created_by_principal=actor,
        recorded_by_principal=actor,
    )
    version.save()
    return version


def create_issue(*, actor: Principal, **data) -> Issue:
    grant = require_global_grant(actor, PermissionGrant.Action.EDIT)
    return Issue.objects.create(
        created_by_principal=actor,
        permission_grant=grant,
        **data,
    )


@transaction.atomic
def create_meeting(*, actor: Principal, participants, **data) -> Meeting:
    grant = require_global_grant(actor, PermissionGrant.Action.APPROVE)
    meeting = Meeting.objects.create(
        created_by_principal=actor,
        permission_grant=grant,
        **data,
    )
    participant_ids = {principal.pk for principal in participants}
    participant_ids.add(actor.pk)
    for principal in Principal.objects.filter(pk__in=participant_ids).order_by("pk"):
        MeetingParticipant.objects.create(
            meeting=meeting,
            principal=principal,
            participant_role=principal.role,
        )
    return meeting


@transaction.atomic
def create_meeting_decision(
    *,
    actor: Principal,
    meeting: Meeting,
    issue: Issue | None = None,
    linkage_role: str = "RELATED",
    **data,
) -> MeetingDecision:
    grant = require_global_grant(actor, PermissionGrant.Action.APPROVE)
    decision = MeetingDecision.objects.create(
        meeting=meeting,
        created_by_principal=actor,
        permission_grant=grant,
        **data,
    )
    if issue is not None:
        IssueDecisionLink.objects.create(
            issue=issue,
            meeting_decision=decision,
            linkage_role=linkage_role or "RELATED",
            permission_grant=grant,
        )
    return decision


@transaction.atomic
def create_rule_proposal(*, actor: Principal, source_kind: str, source, **data) -> RuleProposalVersion:
    grant = require_global_grant(actor, PermissionGrant.Action.EDIT)
    proposal_key = data["proposal_key"]
    versions = RuleProposalVersion.objects.select_for_update().filter(proposal_key=proposal_key).order_by("-version_number")
    prior = versions.first()
    source_fields = {
        RuleProposalSourceLink.SourceKind.MEETING_DECISION: "meeting_decision",
        RuleProposalSourceLink.SourceKind.LEARNING: "learning_version",
        RuleProposalSourceLink.SourceKind.ISSUE: "issue",
        RuleProposalSourceLink.SourceKind.OFFICIAL_POLICY: "official_policy_version",
    }
    source_field = source_fields.get(source_kind)
    if source_field and source is None:
        raise ValidationError("必须绑定确切的提案来源。")
    if source_kind not in {*source_fields, RuleProposalSourceLink.SourceKind.MANUAL}:
        raise ValidationError("未知提案来源。")
    if prior and prior.created_by_principal_id == actor.pk:
        exact_fields = (
            "target_policy_definition_id",
            "candidate_policy_version_id",
            "change_effect",
            "risk_level",
            "affected_scope",
            "rationale",
        )
        same_payload = all(
            getattr(prior, field) == getattr(data.get(field[:-3]), "pk", data.get(field[:-3]))
            if field.endswith("_id")
            else getattr(prior, field) == data.get(field)
            for field in exact_fields
        )
        prior_source = prior.source_links.order_by("created_at").first()
        same_source = (
            prior_source is not None
            and prior_source.source_kind == source_kind
            and (
                source_field is None
                or getattr(prior_source, f"{source_field}_id") == getattr(source, "pk", None)
            )
        )
        if same_payload and same_source:
            return prior
    proposal = RuleProposalVersion.objects.create(
        version_number=(prior.version_number + 1) if prior else 1,
        supersedes_version=prior,
        created_by_principal=actor,
        permission_grant=grant,
        **data,
    )
    link_data = {}
    if source_field:
        link_data[source_field] = source
    RuleProposalSourceLink.objects.create(
        rule_proposal_version=proposal,
        source_kind=source_kind,
        permission_grant=grant,
        **link_data,
    )
    return proposal


def _latest_pass(proposal: RuleProposalVersion, validation_type: str):
    return proposal.validation_runs.filter(
        validation_type=validation_type,
        result=RuleValidationRun.Result.PASSED,
    ).order_by("-completed_at", "-id").first()


def _validate_offline_stage(proposal: RuleProposalVersion, validation_type: str, started_at) -> None:
    if validation_type == RuleValidationRun.ValidationType.HISTORICAL_REPLAY:
        return
    replay = _latest_pass(proposal, RuleValidationRun.ValidationType.HISTORICAL_REPLAY)
    if replay is None or replay.completed_at > started_at:
        raise ValidationError("必须先完成 Historical Replay 并通过。")
    if validation_type == RuleValidationRun.ValidationType.SHADOW:
        return
    shadow = _latest_pass(proposal, RuleValidationRun.ValidationType.SHADOW)
    if shadow is None or shadow.completed_at > started_at:
        raise ValidationError("必须先完成 Shadow 并通过。")
    approval = proposal.approval_decisions.order_by("-decided_at", "-id").first()
    if (
        approval is None
        or approval.decision != RuleApprovalDecision.Decision.APPROVED
        or approval.approver_principal.principal_type != Principal.PrincipalType.HUMAN_USER
        or approval.decided_at > started_at
    ):
        raise ValidationError("Canary 前必须有一条明确的人工批准。")


def record_offline_validation(*, actor: Principal, proposal: RuleProposalVersion, **data) -> RuleValidationRun:
    require_global_grant(actor, PermissionGrant.Action.APPROVE)
    started_at = timezone.now()
    completed_at = started_at
    validation_type = data["validation_type"]
    _validate_offline_stage(proposal, validation_type, started_at)
    payload = {
        "proposal_id": str(proposal.pk),
        "type": validation_type,
        "result": data["result"],
        "window_start": data["data_window_start"].isoformat(),
        "window_end": data["data_window_end"].isoformat(),
        "parameters": data.get("parameters") or {},
        "false_positive_count": data.get("false_positive_count", 0),
        "false_negative_count": data.get("false_negative_count", 0),
        "risk_events": data.get("risk_events") or [],
        "recorded_by": str(actor.pk),
    }
    input_hash = canonical_sha256(payload)
    replay = RuleValidationRun.objects.filter(
        rule_proposal_version=proposal,
        validation_type=validation_type,
        input_version_hash=input_hash,
    ).first()
    if replay is not None:
        return replay
    return RuleValidationRun.objects.create(
        rule_proposal_version=proposal,
        policy_version=proposal.candidate_policy_version,
        input_version_hash=input_hash,
        started_at=started_at,
        completed_at=completed_at,
        created_by_principal=actor,
        **data,
    )


def record_rule_approval(*, actor: Principal, proposal: RuleProposalVersion, decision: str, rationale: str):
    grant = require_global_grant(actor, PermissionGrant.Action.APPROVE)
    if decision == RuleApprovalDecision.Decision.APPROVED:
        replay = _latest_pass(proposal, RuleValidationRun.ValidationType.HISTORICAL_REPLAY)
        shadow = _latest_pass(proposal, RuleValidationRun.ValidationType.SHADOW)
        if replay is None or shadow is None or replay.completed_at > shadow.started_at:
            raise ValidationError("人工批准前必须依次通过 Historical Replay 和 Shadow。")
    latest = proposal.approval_decisions.order_by("-decided_at", "-id").first()
    if (
        latest is not None
        and latest.approver_principal_id == actor.pk
        and latest.permission_grant_id == grant.pk
        and latest.decision == decision
        and latest.rationale == rationale
    ):
        return latest
    return RuleApprovalDecision.objects.create(
        rule_proposal_version=proposal,
        decision=decision,
        approver_principal=actor,
        acting_role=actor.role,
        permission_grant=grant,
        rationale=rationale,
    )


def resolve_rollback_grant(actor: Principal) -> PermissionGrant:
    for action in (PermissionGrant.Action.EMERGENCY_STOP, PermissionGrant.Action.APPROVE):
        decision = resolve_authorization(
            principal=actor,
            acting_role=actor.role,
            action=action,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
        )
        if decision.allowed and decision.grant is not None:
            return decision.grant
    raise PermissionDenied("需要当前有效的批准或紧急停止权限。")
