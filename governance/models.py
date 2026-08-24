from __future__ import annotations

import hashlib
import json

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

from core.models import UUIDv7Model


def canonical_sha256(payload) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_creation_grant(*, grant, principal_id, expected_action: str, occurred_at=None) -> None:
    """Validate the exact Grant that authorized an immutable governance fact.

    Validation is evaluated at the fact's recorded time.  That keeps an old
    fact valid after its Grant is later revoked, while still rejecting a Grant
    that was already revoked or outside its validity window when the fact was
    created.
    """

    action_at = occurred_at or timezone.now()
    errors = []
    if grant.principal_id != principal_id:
        errors.append("Grant principal must be the Principal who created the fact.")
    if grant.scope_kind != "GLOBAL":
        errors.append("Governance creation requires an exact GLOBAL Grant.")
    if grant.action != expected_action:
        errors.append(f"Governance creation requires an exact {expected_action} Grant.")
    if grant.effect != "ALLOW":
        errors.append("Governance creation requires an ALLOW Grant.")
    if grant.valid_from > action_at or (grant.valid_until is not None and grant.valid_until <= action_at):
        errors.append("Grant was outside its validity window at creation time.")
    if grant.revoked_at is not None and grant.revoked_at <= action_at:
        errors.append("Grant had already been revoked at creation time.")
    if grant.grant_status not in {"ACTIVE", "REVOKED"}:
        errors.append("Grant was not active at creation time.")
    if errors:
        raise ValidationError({"permission_grant": errors})


class ActingRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN", "Operations admin"
    OPERATOR = "OPERATOR", "Operator"
    SYSTEM = "SYSTEM", "System"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Governance facts are append-only.")

    def delete(self):
        raise ValidationError("Governance facts cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Governance facts cannot be bulk-updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Bulk creation bypasses governance validation.")


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyFact(UUIDv7Model):
    objects = AppendOnlyManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{self.__class__.__name__} is immutable; append a new fact.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self.__class__.__name__} is append-only.")


class Issue(UUIDv7Model):
    class IssueType(models.TextChoices):
        OPERATIONAL = "OPERATIONAL", "Operational"
        BLOCKER = "BLOCKER", "Blocker"
        SAFETY_EVENT = "SAFETY_EVENT", "Safety event"
        RULE_CONFLICT = "RULE_CONFLICT", "Rule conflict"

    class Severity(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    class State(models.TextChoices):
        OPEN = "OPEN", "Open"
        TRIAGED = "TRIAGED", "Triaged"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        RESOLVED = "RESOLVED", "Resolved"
        CLOSED = "CLOSED", "Closed"
        ESCALATED_TO_MEETING = "ESCALATED_TO_MEETING", "Escalated to meeting"

    issue_key = models.CharField(max_length=160, unique=True)
    issue_type = models.CharField(max_length=24, choices=IssueType.choices)
    severity = models.CharField(max_length=16, choices=Severity.choices)
    title = models.CharField(max_length=240)
    description = models.TextField()
    current_state = models.CharField(max_length=32, choices=State.choices, default=State.OPEN)
    state_version = models.PositiveIntegerField(default=0)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="issues_created")
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        if self.permission_grant_id and self.created_by_principal_id:
            validate_creation_grant(
                grant=self.permission_grant,
                principal_id=self.created_by_principal_id,
                expected_action="EDIT",
                occurred_at=self.created_at,
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            original = type(self).objects.get(pk=self.pk)
            if original.current_state != self.current_state or original.state_version != self.state_version:
                raise ValidationError("Issue state is an IssueEvent projection and cannot be edited directly.")
        self.full_clean()
        return super().save(*args, **kwargs)


class IssueEvent(AppendOnlyFact):
    issue = models.ForeignKey(Issue, on_delete=models.PROTECT, related_name="events")
    from_state = models.CharField(max_length=32, choices=Issue.State.choices)
    to_state = models.CharField(max_length=32, choices=Issue.State.choices)
    event_sequence = models.PositiveIntegerField()
    expected_state_version = models.PositiveIntegerField()
    resulting_state_version = models.PositiveIntegerField()
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    reason = models.TextField()
    actor_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["issue", "event_sequence"], name="governance_unique_issue_event_sequence"),
            models.CheckConstraint(condition=Q(event_sequence__gte=1), name="governance_issue_event_sequence_gte_one"),
            models.CheckConstraint(condition=Q(resulting_state_version=models.F("expected_state_version") + 1), name="governance_issue_event_advances_one"),
        ]


class IssueSourceLink(AppendOnlyFact):
    class SourceKind(models.TextChoices):
        TASK = "TASK", "Task"
        PUBLICATION = "PUBLICATION", "Publication"
        METRIC_COLLECTION_RUN = "METRIC_COLLECTION_RUN", "Metric collection run"
        GEO_PROBE_RUN = "GEO_PROBE_RUN", "GEO probe run"
        MANUAL = "MANUAL", "Manual"

    issue = models.ForeignKey(Issue, on_delete=models.PROTECT, related_name="source_links")
    source_kind = models.CharField(max_length=32, choices=SourceKind.choices)
    task = models.ForeignKey("workflow.Task", null=True, blank=True, on_delete=models.PROTECT, related_name="issue_links")
    publication = models.ForeignKey("releasegate.Publication", null=True, blank=True, on_delete=models.PROTECT, related_name="issue_links")
    metric_collection_run = models.ForeignKey("insights.MetricCollectionRun", null=True, blank=True, on_delete=models.PROTECT, related_name="issue_links")
    geo_probe_run = models.ForeignKey("insights.GEOProbeRun", null=True, blank=True, on_delete=models.PROTECT, related_name="issue_links")
    source_note = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(source_kind="TASK", task__isnull=False, publication__isnull=True, metric_collection_run__isnull=True, geo_probe_run__isnull=True)
                    | Q(source_kind="PUBLICATION", task__isnull=True, publication__isnull=False, metric_collection_run__isnull=True, geo_probe_run__isnull=True)
                    | Q(source_kind="METRIC_COLLECTION_RUN", task__isnull=True, publication__isnull=True, metric_collection_run__isnull=False, geo_probe_run__isnull=True)
                    | Q(source_kind="GEO_PROBE_RUN", task__isnull=True, publication__isnull=True, metric_collection_run__isnull=True, geo_probe_run__isnull=False)
                    | Q(source_kind="MANUAL", task__isnull=True, publication__isnull=True, metric_collection_run__isnull=True, geo_probe_run__isnull=True)
                ),
                name="governance_issue_typed_source",
            ),
        ]


class Meeting(AppendOnlyFact):
    class MeetingType(models.TextChoices):
        OPERATIONAL_REVIEW = "OPERATIONAL_REVIEW", "Operational review"
        RULE_GOVERNANCE = "RULE_GOVERNANCE", "Rule governance"
        SAFETY_INCIDENT = "SAFETY_INCIDENT", "Safety incident"

    meeting_key = models.CharField(max_length=160, unique=True)
    meeting_type = models.CharField(max_length=32, choices=MeetingType.choices)
    title = models.CharField(max_length=240)
    summary = models.TextField(blank=True)
    occurred_at = models.DateTimeField()
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        if self.permission_grant_id and self.created_by_principal_id:
            validate_creation_grant(
                grant=self.permission_grant,
                principal_id=self.created_by_principal_id,
                expected_action="APPROVE",
                occurred_at=self.created_at,
            )


class MeetingParticipant(AppendOnlyFact):
    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="participants")
    principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="meeting_participations")
    participant_role = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["meeting", "principal"], name="governance_unique_meeting_participant"),
        ]


class MeetingDecision(AppendOnlyFact):
    class DecisionType(models.TextChoices):
        RULE_PROPOSAL = "RULE_PROPOSAL", "Rule proposal"
        OPERATIONAL_FIX = "OPERATIONAL_FIX", "Operational fix"
        NO_ACTION = "NO_ACTION", "No action"

    meeting = models.ForeignKey(Meeting, on_delete=models.PROTECT, related_name="decisions")
    decision_key = models.CharField(max_length=160)
    decision_type = models.CharField(max_length=24, choices=DecisionType.choices)
    decision = models.TextField()
    impact_scope = models.JSONField(default=dict, blank=True)
    owner_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="meeting_decisions_owned")
    due_at = models.DateTimeField(null=True, blank=True)
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["meeting", "decision_key"], name="governance_unique_meeting_decision"),
        ]

    def clean(self):
        super().clean()
        if self.permission_grant_id and self.created_by_principal_id:
            validate_creation_grant(
                grant=self.permission_grant,
                principal_id=self.created_by_principal_id,
                expected_action="APPROVE",
                occurred_at=self.created_at,
            )


class IssueDecisionLink(AppendOnlyFact):
    issue = models.ForeignKey(Issue, on_delete=models.PROTECT, related_name="decision_links")
    meeting_decision = models.ForeignKey(MeetingDecision, on_delete=models.PROTECT, related_name="issue_links")
    linkage_role = models.CharField(max_length=16, choices=[("PRIMARY", "Primary"), ("RELATED", "Related")])
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["issue", "meeting_decision"], name="governance_unique_issue_decision_link"),
        ]

    def clean(self):
        super().clean()
        if (
            self.permission_grant_id
            and self.meeting_decision_id
            and self.permission_grant_id != self.meeting_decision.permission_grant_id
        ):
            raise ValidationError(
                {"permission_grant": "Issue link must retain the MeetingDecision's exact creation Grant."}
            )


class RuleProposalVersion(AppendOnlyFact):
    class ChangeEffect(models.TextChoices):
        TIGHTEN = "TIGHTEN", "Tighten"
        RELAX = "RELAX", "Relax"
        CLARIFY = "CLARIFY", "Clarify"
        ADD = "ADD", "Add"
        RETIRE = "RETIRE", "Retire"

    class RiskLevel(models.TextChoices):
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        HIGH = "HIGH", "High"
        CRITICAL = "CRITICAL", "Critical"

    proposal_key = models.CharField(max_length=160)
    version_number = models.PositiveIntegerField()
    target_policy_definition = models.ForeignKey("releasegate.PolicyDefinition", on_delete=models.PROTECT, related_name="rule_proposal_versions")
    candidate_policy_version = models.ForeignKey("releasegate.PolicyVersion", on_delete=models.PROTECT, related_name="rule_proposals")
    change_effect = models.CharField(max_length=16, choices=ChangeEffect.choices)
    risk_level = models.CharField(max_length=16, choices=RiskLevel.choices)
    affected_scope = models.JSONField(default=dict, blank=True)
    rationale = models.TextField()
    supersedes_version = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="superseded_by_version")
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["proposal_key", "version_number"], name="governance_unique_rule_proposal_version"),
            models.CheckConstraint(condition=Q(version_number__gte=1), name="governance_rule_proposal_version_gte_one"),
        ]

    def clean(self):
        super().clean()
        if self.permission_grant_id and self.created_by_principal_id:
            validate_creation_grant(
                grant=self.permission_grant,
                principal_id=self.created_by_principal_id,
                expected_action="EDIT",
                occurred_at=self.created_at,
            )
        if self.candidate_policy_version_id and self.target_policy_definition_id and self.candidate_policy_version.policy_definition_id != self.target_policy_definition_id:
            raise ValidationError({"candidate_policy_version": "Candidate version must belong to the target definition."})
        if self.supersedes_version_id and (
            self.supersedes_version.proposal_key != self.proposal_key
            or self.supersedes_version.target_policy_definition_id != self.target_policy_definition_id
            or self.supersedes_version.version_number >= self.version_number
        ):
            raise ValidationError({"supersedes_version": "Proposal revision must extend the same proposal chain."})


class RuleProposalSourceLink(AppendOnlyFact):
    class SourceKind(models.TextChoices):
        MEETING_DECISION = "MEETING_DECISION", "Meeting decision"
        LEARNING = "LEARNING", "Learning"
        ISSUE = "ISSUE", "Issue"
        OFFICIAL_POLICY = "OFFICIAL_POLICY", "Official policy"
        MANUAL = "MANUAL", "Manual"

    rule_proposal_version = models.ForeignKey(RuleProposalVersion, on_delete=models.PROTECT, related_name="source_links")
    source_kind = models.CharField(max_length=24, choices=SourceKind.choices)
    meeting_decision = models.ForeignKey(MeetingDecision, null=True, blank=True, on_delete=models.PROTECT, related_name="rule_proposal_links")
    learning_version = models.ForeignKey("insights.LearningVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="rule_proposal_links")
    issue = models.ForeignKey(Issue, null=True, blank=True, on_delete=models.PROTECT, related_name="rule_proposal_links")
    official_policy_version = models.ForeignKey("releasegate.PolicyVersion", null=True, blank=True, on_delete=models.PROTECT, related_name="proposal_source_links")
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(source_kind="MEETING_DECISION", meeting_decision__isnull=False, learning_version__isnull=True, issue__isnull=True, official_policy_version__isnull=True)
                    | Q(source_kind="LEARNING", meeting_decision__isnull=True, learning_version__isnull=False, issue__isnull=True, official_policy_version__isnull=True)
                    | Q(source_kind="ISSUE", meeting_decision__isnull=True, learning_version__isnull=True, issue__isnull=False, official_policy_version__isnull=True)
                    | Q(source_kind="OFFICIAL_POLICY", meeting_decision__isnull=True, learning_version__isnull=True, issue__isnull=True, official_policy_version__isnull=False)
                    | Q(source_kind="MANUAL", meeting_decision__isnull=True, learning_version__isnull=True, issue__isnull=True, official_policy_version__isnull=True)
                ),
                name="governance_rule_proposal_typed_source",
            ),
        ]

    def clean(self):
        super().clean()
        if (
            self.permission_grant_id
            and self.rule_proposal_version_id
            and self.permission_grant_id != self.rule_proposal_version.permission_grant_id
        ):
            raise ValidationError(
                {"permission_grant": "Proposal source must retain the RuleProposalVersion's exact creation Grant."}
            )


class RuleValidationRun(AppendOnlyFact):
    class ValidationType(models.TextChoices):
        HISTORICAL_REPLAY = "HISTORICAL_REPLAY", "Historical replay"
        SHADOW = "SHADOW", "Shadow"
        CANARY = "CANARY", "Canary"

    class Result(models.TextChoices):
        PASSED = "PASSED", "Passed"
        FAILED = "FAILED", "Failed"
        PARTIAL = "PARTIAL", "Partial"
        ERROR = "ERROR", "Error"

    rule_proposal_version = models.ForeignKey(RuleProposalVersion, on_delete=models.PROTECT, related_name="validation_runs")
    validation_type = models.CharField(max_length=24, choices=ValidationType.choices)
    policy_version = models.ForeignKey("releasegate.PolicyVersion", on_delete=models.PROTECT, related_name="governance_validation_runs")
    input_version_hash = models.CharField(max_length=64)
    data_window_start = models.DateTimeField()
    data_window_end = models.DateTimeField()
    parameters = models.JSONField(default=dict, blank=True)
    result = models.CharField(max_length=16, choices=Result.choices)
    false_positive_count = models.PositiveIntegerField(default=0)
    false_negative_count = models.PositiveIntegerField(default=0)
    risk_events = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField()
    created_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["rule_proposal_version", "validation_type", "input_version_hash"], name="governance_unique_validation_input"),
            models.CheckConstraint(condition=Q(data_window_end__gt=models.F("data_window_start")), name="governance_validation_window_valid"),
            models.CheckConstraint(condition=Q(completed_at__gte=models.F("started_at")), name="governance_validation_time_valid"),
        ]

    def clean(self):
        super().clean()
        if self.policy_version_id and self.rule_proposal_version_id and self.policy_version_id != self.rule_proposal_version.candidate_policy_version_id:
            raise ValidationError({"policy_version": "Validation must use the proposal's exact candidate PolicyVersion."})


class RuleApprovalDecision(AppendOnlyFact):
    class Decision(models.TextChoices):
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CHANGES_REQUESTED = "CHANGES_REQUESTED", "Changes requested"

    rule_proposal_version = models.ForeignKey(RuleProposalVersion, on_delete=models.PROTECT, related_name="approval_decisions")
    decision = models.CharField(max_length=24, choices=Decision.choices)
    approver_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="rule_approval_decisions")
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="rule_approval_decisions")
    rationale = models.TextField()
    decided_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        errors = {}
        if (
            self.approver_principal_id
            and self.approver_principal.principal_type != "HUMAN_USER"
        ):
            errors["approver_principal"] = (
                "Rule approval must be an explicit decision by a human Principal."
            )
        if self.permission_grant_id and self.approver_principal_id:
            if self.permission_grant.principal_id != self.approver_principal_id or self.permission_grant.action != "APPROVE" or self.permission_grant.effect != "ALLOW" or not self.permission_grant.is_current:
                errors["permission_grant"] = "Approval requires the approver's current exact ALLOW/APPROVE Grant."
        if self.approver_principal_id:
            try:
                self.approver_principal.validate_acting_role(self.acting_role)
            except PermissionDenied as exc:
                errors["acting_role"] = str(exc)
        if self.rule_proposal_version_id and self.rule_proposal_version.change_effect == RuleProposalVersion.ChangeEffect.RELAX:
            if self.approver_principal.role != "OWNER" or self.acting_role != ActingRole.OWNER:
                errors["approver_principal"] = "RELAX requires explicit Owner approval acting as Owner."
        if errors:
            raise ValidationError(errors)


class PolicyActivation(AppendOnlyFact):
    rule_proposal_version = models.OneToOneField(RuleProposalVersion, on_delete=models.PROTECT, related_name="policy_activation")
    policy_version = models.ForeignKey("releasegate.PolicyVersion", on_delete=models.PROTECT, related_name="governance_activations")
    activation_scope = models.JSONField(default=dict, blank=True)
    effective_from = models.DateTimeField()
    activated_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        if self.policy_version_id and self.rule_proposal_version_id and self.policy_version_id != self.rule_proposal_version.candidate_policy_version_id:
            raise ValidationError({"policy_version": "Activation must bind the proposal's exact candidate PolicyVersion."})

    def save(self, *args, **kwargs):
        if not getattr(self, "_activation_service_authorized", False):
            raise ValidationError("PolicyActivation must be written through activate_policy().")
        return super().save(*args, **kwargs)


class PolicyActivationEvent(AppendOnlyFact):
    class EventType(models.TextChoices):
        ACTIVATED = "ACTIVATED", "Activated"
        MONITORING = "MONITORING", "Monitoring"
        SUPERSEDED = "SUPERSEDED", "Superseded"
        ROLLED_BACK = "ROLLED_BACK", "Rolled back"

    policy_activation = models.ForeignKey(PolicyActivation, on_delete=models.PROTECT, related_name="events")
    event_type = models.CharField(max_length=16, choices=EventType.choices)
    event_sequence = models.PositiveIntegerField()
    previous_event = models.OneToOneField("self", null=True, blank=True, on_delete=models.PROTECT, related_name="next_event")
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    reason = models.TextField(blank=True)
    actor_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["policy_activation", "event_sequence"], name="governance_unique_activation_event_sequence"),
            models.CheckConstraint(condition=Q(event_sequence__gte=1), name="governance_activation_event_sequence_gte_one"),
        ]

    def clean(self):
        super().clean()
        if self.event_sequence == 1 and (self.previous_event_id or self.event_type != self.EventType.ACTIVATED):
            raise ValidationError("The first activation event must be ACTIVATED without a predecessor.")
        if self.event_sequence > 1 and (
            not self.previous_event_id
            or self.previous_event.policy_activation_id != self.policy_activation_id
            or self.previous_event.event_sequence != self.event_sequence - 1
        ):
            raise ValidationError("Activation events must append to the exact prior event.")

    def save(self, *args, **kwargs):
        if not getattr(self, "_activation_service_authorized", False):
            raise ValidationError("PolicyActivationEvent must be written through governance services.")
        return super().save(*args, **kwargs)


class PolicyRollbackEvent(AppendOnlyFact):
    policy_activation = models.OneToOneField(PolicyActivation, on_delete=models.PROTECT, related_name="rollback_event")
    activation_event = models.OneToOneField(PolicyActivationEvent, on_delete=models.PROTECT, related_name="rollback_detail")
    rollback_to_policy_version = models.ForeignKey("releasegate.PolicyVersion", on_delete=models.PROTECT, related_name="rollback_targets")
    reason = models.TextField()
    rollback_by_principal = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey("accounts.PermissionGrant", on_delete=models.PROTECT, related_name="+")
    rollback_at = models.DateTimeField(default=timezone.now)

    def clean(self):
        super().clean()
        if self.activation_event_id and (
            self.activation_event.policy_activation_id != self.policy_activation_id
            or self.activation_event.event_type != PolicyActivationEvent.EventType.ROLLED_BACK
        ):
            raise ValidationError({"activation_event": "Rollback detail requires the exact ROLLED_BACK activation event."})
        if self.rollback_to_policy_version_id and self.policy_activation_id and self.rollback_to_policy_version.policy_definition_id != self.policy_activation.policy_version.policy_definition_id:
            raise ValidationError({"rollback_to_policy_version": "Rollback target must be from the same PolicyDefinition."})

    def save(self, *args, **kwargs):
        if not getattr(self, "_activation_service_authorized", False):
            raise ValidationError("PolicyRollbackEvent must be written through rollback_policy().")
        return super().save(*args, **kwargs)
