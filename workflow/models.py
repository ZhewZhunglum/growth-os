from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from core.models import TimeStampedModel, UUIDv7Model
from workflow.exceptions import (
    CheckGateRejected,
    CommandReplayConflict,
    IllegalTaskTransition,
    OptimisticConcurrencyConflict,
)
from workflow.services import guard_check_run, guard_transition_prerequisites


def payload_sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ActingRole(models.TextChoices):
    OWNER = "OWNER", "Owner"
    OPERATIONS_ADMIN = "OPERATIONS_ADMIN", "Operations admin"
    OPERATOR = "OPERATOR", "Operator"
    SYSTEM = "SYSTEM", "System"


class AppendOnlyQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("Append-only workflow facts cannot be updated.")

    def delete(self):
        raise ValidationError("Append-only workflow facts cannot be deleted.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("Append-only workflow facts cannot be bulk-updated.")

    def bulk_create(
        self,
        objs,
        batch_size=None,
        ignore_conflicts=False,
        update_conflicts=False,
        update_fields=None,
        unique_fields=None,
    ):
        raise ValidationError("Bulk creation bypasses workflow-fact authorization and validation.")


class AppendOnlyManager(models.Manager.from_queryset(AppendOnlyQuerySet)):
    pass


class AppendOnlyFact(UUIDv7Model):
    objects = AppendOnlyManager()

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError(f"{self.__class__.__name__} is immutable; create a new fact.")
        self.full_clean()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(f"{self.__class__.__name__} is append-only.")


def validate_current_grant(grant, *, principal, acting_role: str, action: str, product_id) -> None:
    from accounts.authorization import resolve_authorization

    decision = resolve_authorization(
        principal=principal,
        acting_role=acting_role,
        action=action,
        scope_kind="PRODUCT",
        product=product_id,
    )
    if not decision.allowed or decision.grant is None:
        raise ValidationError(f"Workflow authorization denied: {decision.reason}.")
    if decision.grant.pk != grant.pk:
        raise ValidationError("The command must record the centrally resolved PermissionGrant.")


class TaskContractVersion(AppendOnlyFact):
    product_profile_version = models.ForeignKey(
        "products.ProductProfileVersion", on_delete=models.PROTECT, related_name="task_contract_versions"
    )
    version_number = models.PositiveIntegerField()
    title = models.CharField(max_length=240)
    dor_criteria = models.JSONField(default=list)
    dod_criteria = models.JSONField(default=list)
    release_gate_criteria = models.JSONField(default=list)
    success_criteria = models.JSONField(default=list)
    manifest_sha256 = models.CharField(max_length=64, blank=True)
    sealed_at = models.DateTimeField()
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_contract_versions_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["product_profile_version", "version_number"], name="workflow_unique_contract_version"
            )
        ]

    def manifest_payload(self) -> dict[str, Any]:
        return {
            "product_profile_version_id": str(self.product_profile_version_id),
            "version_number": self.version_number,
            "title": self.title,
            "dor_criteria": self.dor_criteria,
            "dod_criteria": self.dod_criteria,
            "release_gate_criteria": self.release_gate_criteria,
            "success_criteria": self.success_criteria,
        }

    def clean(self):
        super().clean()
        if self.product_profile_version_id and not self.product_profile_version.is_sealed:
            raise ValidationError("A task contract requires a sealed ProductProfileVersion.")
        for field_name in ("dor_criteria", "dod_criteria", "release_gate_criteria", "success_criteria"):
            value = getattr(self, field_name)
            if not isinstance(value, list):
                raise ValidationError({field_name: "Criteria must be a list."})
            keys = [item.get("key") for item in value if isinstance(item, dict)]
            if len(keys) != len(value) or any(not key for key in keys) or len(keys) != len(set(keys)):
                raise ValidationError({field_name: "Every criterion needs a unique non-empty key."})
        expected = payload_sha256(self.manifest_payload())
        if not self.manifest_sha256:
            self.manifest_sha256 = expected
        elif self.manifest_sha256 != expected:
            raise ValidationError("Task contract manifest hash does not match its exact content.")

    def __str__(self) -> str:
        return f"{self.title} v{self.version_number}"


class TaskContractPolicyLink(AppendOnlyFact):
    task_contract_version = models.ForeignKey(
        TaskContractVersion, on_delete=models.PROTECT, related_name="policy_links"
    )
    policy_version = models.ForeignKey(
        "releasegate.PolicyVersion", on_delete=models.PROTECT, related_name="task_contract_links"
    )
    required = models.BooleanField(default=True)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_contract_policy_links_created"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task_contract_version", "policy_version"], name="workflow_unique_contract_policy_link"
            )
        ]


class Task(TimeStampedModel):
    class State(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        BLOCKED = "BLOCKED", "Blocked"
        READY = "READY", "Ready"
        ASSIGNED = "ASSIGNED", "Assigned"
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        SUBMITTED = "SUBMITTED", "Submitted"
        UNDER_REVIEW = "UNDER_REVIEW", "Under review"
        HUMAN_REWORK = "HUMAN_REWORK", "Human rework"
        APPROVED = "APPROVED", "Approved"
        DONE = "DONE", "Done"
        CANCELLED = "CANCELLED", "Cancelled"

    TRANSITIONS = {
        State.DRAFT: {State.BLOCKED, State.READY, State.CANCELLED},
        State.BLOCKED: {
            State.DRAFT,
            State.READY,
            State.ASSIGNED,
            State.IN_PROGRESS,
            State.HUMAN_REWORK,
            State.CANCELLED,
        },
        State.READY: {State.ASSIGNED, State.CANCELLED},
        State.ASSIGNED: {State.IN_PROGRESS, State.BLOCKED, State.CANCELLED},
        State.IN_PROGRESS: {State.SUBMITTED, State.BLOCKED, State.CANCELLED},
        State.SUBMITTED: {State.UNDER_REVIEW},
        State.UNDER_REVIEW: {State.HUMAN_REWORK, State.APPROVED},
        State.HUMAN_REWORK: {State.IN_PROGRESS, State.CANCELLED},
        State.APPROVED: {State.DONE},
        State.DONE: set(),
        State.CANCELLED: set(),
    }

    product = models.ForeignKey("products.Product", on_delete=models.PROTECT, related_name="tasks")
    product_profile_version = models.ForeignKey(
        "products.ProductProfileVersion", on_delete=models.PROTECT, related_name="tasks"
    )
    contract_version = models.ForeignKey(TaskContractVersion, on_delete=models.PROTECT, related_name="tasks")
    title = models.CharField(max_length=240)
    description = models.TextField(blank=True)
    current_state = models.CharField(max_length=24, choices=State.choices, default=State.DRAFT)
    state_version = models.PositiveIntegerField(default=0)
    blocked_from_state = models.CharField(max_length=24, choices=State.choices, blank=True)
    current_assignee_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="currently_assigned_tasks",
    )
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks_created"
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks_updated"
    )

    def clean(self):
        super().clean()
        if self._state.adding and (
            self.current_state != self.State.DRAFT
            or self.state_version != 0
            or self.blocked_from_state
            or self.current_assignee_principal_id is not None
        ):
            raise ValidationError(
                "A Task must begin unassigned in DRAFT at state version 0."
            )
        if self.product_profile_version_id and self.product_profile_version.product_id != self.product_id:
            raise ValidationError("Task profile version belongs to another product.")
        if self.contract_version_id and self.contract_version.product_profile_version_id != self.product_profile_version_id:
            raise ValidationError("Task contract must bind the task's exact ProductProfileVersion.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)

    def _latest_complete_passing_check(self, kind: str) -> bool:
        run = self.check_runs.filter(check_kind=kind, status=TaskCheckRun.Status.COMPLETED).order_by("-attempt_number").first()
        return bool(
            run
            and run.aggregate_result == TaskCheckRun.Result.PASS
            and run.actual_criterion_count == run.expected_criterion_count
            and run.contract_version_id == self.contract_version_id
        )

    @classmethod
    def transition(
        cls,
        *,
        task_id,
        to_state: str,
        command_id: uuid.UUID,
        expected_state_version: int,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
        reason: str = "",
    ) -> TaskStateEvent:
        with transaction.atomic():
            task = cls.objects.select_for_update().get(pk=task_id)
            payload = {
                "task_id": str(task.pk),
                "to_state": to_state,
                "expected_state_version": expected_state_version,
                "reason": reason,
            }
            digest = payload_sha256(payload)
            existing = TaskStateEvent.objects.filter(command_id=command_id).first()
            if existing:
                if existing.payload_hash != digest:
                    raise CommandReplayConflict("command_id was already used with a different payload.")
                return existing
            if task.state_version != expected_state_version:
                raise OptimisticConcurrencyConflict("Task version is stale; reread before retrying.")
            if (
                task.current_state == cls.State.BLOCKED
                and to_state != cls.State.CANCELLED
                and to_state != task.blocked_from_state
            ):
                raise IllegalTaskTransition(
                    f"A blocked task must return to its recorded prior state ({task.blocked_from_state})."
                )
            if to_state not in cls.TRANSITIONS.get(task.current_state, set()):
                raise IllegalTaskTransition(f"{task.current_state} cannot transition to {to_state}.")
            validate_current_grant(
                permission_grant,
                principal=actor_principal,
                acting_role=acting_role,
                action="EDIT",
                product_id=task.product_id,
            )
            if to_state == cls.State.READY and not task._latest_complete_passing_check(TaskCheckRun.Kind.DOR):
                raise CheckGateRejected("READY requires the latest complete passing DoR run.")
            if to_state == cls.State.ASSIGNED:
                latest_assignment = task.assignments.order_by("-assignment_number").first()
                if (
                    task.current_assignee_principal_id is None
                    or latest_assignment is None
                    or latest_assignment.assignee_principal_id != task.current_assignee_principal_id
                ):
                    raise CheckGateRejected("ASSIGNED requires an explicit current TaskAssignment fact.")
            guard_transition_prerequisites(task, to_state)
            previous = task.state_events.order_by("-event_sequence").first()
            next_version = task.state_version + 1
            event = TaskStateEvent.objects.create(
                task=task,
                from_state=task.current_state,
                to_state=to_state,
                command_id=command_id,
                payload_hash=digest,
                expected_state_version=expected_state_version,
                resulting_state_version=next_version,
                event_sequence=next_version,
                previous_event=previous,
                reason=reason,
                actor_principal=actor_principal,
                acting_role=acting_role,
                permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
                event_at=timezone.now(),
            )
            if to_state == cls.State.BLOCKED:
                task.blocked_from_state = task.current_state
            elif task.current_state == cls.State.BLOCKED:
                task.blocked_from_state = ""
            task.current_state = to_state
            task.state_version = next_version
            task.updated_by_principal = recorded_by_principal
            task.save(update_fields=["current_state", "state_version", "blocked_from_state", "updated_by_principal", "updated_at"])
            return event


class TaskAssignment(AppendOnlyFact):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="assignments")
    assignee_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_assignments_received"
    )
    assignment_number = models.PositiveIntegerField()
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    expected_task_version = models.PositiveIntegerField()
    assigned_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_assignments_made"
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="task_assignments"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_assignments_recorded"
    )
    assigned_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "assignment_number"], name="workflow_unique_assignment_number")
        ]

    @classmethod
    def record(
        cls,
        *,
        task: Task,
        assignee_principal,
        command_id: uuid.UUID,
        expected_task_version: int,
        assigned_by_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
    ) -> TaskAssignment:
        payload = {
            "task_id": str(task.pk),
            "assignee_principal_id": str(assignee_principal.pk),
            "expected_task_version": expected_task_version,
        }
        digest = payload_sha256(payload)
        existing = cls.objects.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != digest:
                raise CommandReplayConflict("command_id was already used for a different assignment.")
            return existing

        with transaction.atomic():
            locked_task = Task.objects.select_for_update().get(pk=task.pk)
            if locked_task.state_version != expected_task_version:
                raise OptimisticConcurrencyConflict("Task version is stale; reread before assigning.")
            if locked_task.current_state not in {
                Task.State.READY,
                Task.State.ASSIGNED,
                Task.State.IN_PROGRESS,
                Task.State.HUMAN_REWORK,
            }:
                raise IllegalTaskTransition("This task state does not allow assignment.")
            validate_current_grant(
                permission_grant,
                principal=assigned_by_principal,
                acting_role=acting_role,
                action="EDIT",
                product_id=locked_task.product_id,
            )
            latest = cls.objects.filter(task=locked_task).order_by("-assignment_number").first()
            assignment = cls.objects.create(
                task=locked_task,
                assignee_principal=assignee_principal,
                assignment_number=1 if latest is None else latest.assignment_number + 1,
                command_id=command_id,
                payload_hash=digest,
                expected_task_version=expected_task_version,
                assigned_by_principal=assigned_by_principal,
                acting_role=acting_role,
                permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
                assigned_at=timezone.now(),
            )
            Task.objects.filter(pk=locked_task.pk).update(
                current_assignee_principal=assignee_principal,
                updated_by_principal=recorded_by_principal,
                updated_at=timezone.now(),
            )
            return assignment


class TaskStateEvent(AppendOnlyFact):
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="state_events")
    from_state = models.CharField(max_length=24, choices=Task.State.choices)
    to_state = models.CharField(max_length=24, choices=Task.State.choices)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    expected_state_version = models.PositiveIntegerField()
    resulting_state_version = models.PositiveIntegerField()
    event_sequence = models.PositiveIntegerField()
    previous_event = models.OneToOneField(
        "self", null=True, blank=True, on_delete=models.PROTECT, related_name="next_event"
    )
    reason = models.TextField(blank=True)
    actor_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_state_events_acted"
    )
    acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="task_state_events"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_state_events_recorded"
    )
    event_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "event_sequence"], name="workflow_unique_event_sequence"),
            models.UniqueConstraint(fields=["task", "resulting_state_version"], name="workflow_unique_result_state_version"),
            models.CheckConstraint(
                condition=Q(resulting_state_version=models.F("expected_state_version") + 1),
                name="workflow_event_advances_one_version",
            ),
        ]

    def clean(self):
        super().clean()
        if self.event_sequence != self.resulting_state_version:
            raise ValidationError("Task event sequence must equal the resulting Task state version.")
        if self.event_sequence == 1:
            if self.previous_event_id:
                raise ValidationError("The first TaskStateEvent cannot have a previous event.")
            if self.expected_state_version != 0:
                raise ValidationError("The first TaskStateEvent must expect Task state version 0.")
        elif (
            not self.previous_event_id
            or self.previous_event.task_id != self.task_id
            or self.previous_event.event_sequence != self.event_sequence - 1
            or self.previous_event.to_state != self.from_state
        ):
            raise ValidationError("TaskStateEvent must append to the exact prior event without a fork.")


class TaskCheckRun(AppendOnlyFact):
    class Kind(models.TextChoices):
        DOR = "DOR", "Definition of Ready"
        DOD = "DOD", "Definition of Done"

    class Status(models.TextChoices):
        COMPLETED = "COMPLETED", "Completed"

    class Result(models.TextChoices):
        PASS = "PASS", "Pass"
        FAIL = "FAIL", "Fail"
        BLOCKED = "BLOCKED", "Blocked"
        ERROR = "ERROR", "Error"

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="check_runs")
    contract_version = models.ForeignKey(TaskContractVersion, on_delete=models.PROTECT, related_name="check_runs")
    check_kind = models.CharField(max_length=8, choices=Kind.choices)
    attempt_number = models.PositiveIntegerField()
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.COMPLETED)
    aggregate_result = models.CharField(max_length=16, choices=Result.choices)
    expected_criterion_count = models.PositiveIntegerField()
    actual_criterion_count = models.PositiveIntegerField()
    context_sha256 = models.CharField(max_length=64)
    command_id = models.UUIDField(unique=True)
    payload_hash = models.CharField(max_length=64)
    evaluator_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_checks_evaluated"
    )
    evaluator_acting_role = models.CharField(max_length=24, choices=ActingRole.choices)
    permission_grant = models.ForeignKey(
        "accounts.PermissionGrant", on_delete=models.PROTECT, related_name="task_check_runs"
    )
    recorded_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_checks_recorded"
    )
    completed_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["task", "check_kind", "attempt_number"], name="workflow_unique_check_attempt"
            ),
            models.CheckConstraint(
                condition=Q(actual_criterion_count__lte=models.F("expected_criterion_count")),
                name="workflow_check_actual_lte_expected",
            ),
        ]

    @classmethod
    def record_completed(
        cls,
        *,
        task: Task,
        check_kind: str,
        results: list[dict[str, Any]],
        command_id: uuid.UUID,
        evaluator_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
    ) -> TaskCheckRun:
        criteria = task.contract_version.dor_criteria if check_kind == cls.Kind.DOR else task.contract_version.dod_criteria
        criterion_map = {item["key"]: item for item in criteria}
        normalized = sorted(results, key=lambda item: item["criterion_key"])
        payload = {
            "task_id": str(task.pk), "contract_version_id": str(task.contract_version_id),
            "check_kind": check_kind, "results": normalized,
        }
        digest = payload_sha256(payload)
        existing = cls.objects.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != digest:
                raise CommandReplayConflict("command_id was already used with different check results.")
            return existing
        validate_current_grant(
            permission_grant,
            principal=evaluator_principal,
            acting_role=acting_role,
            action="EDIT",
            product_id=task.product_id,
        )
        supplied = {item["criterion_key"] for item in normalized}
        if len(supplied) != len(normalized) or not supplied.issubset(criterion_map):
            raise ValidationError("Check results contain duplicate or unknown criterion keys.")
        required_failures = [
            item["result"] for item in normalized
            if criterion_map[item["criterion_key"]].get("required", True) and item["result"] != cls.Result.PASS
        ]
        complete = len(normalized) == len(criteria)
        if complete and not required_failures:
            aggregate = cls.Result.PASS
        elif cls.Result.ERROR in required_failures:
            aggregate = cls.Result.ERROR
        elif cls.Result.BLOCKED in required_failures:
            aggregate = cls.Result.BLOCKED
        else:
            aggregate = cls.Result.FAIL
        with transaction.atomic():
            locked_task = Task.objects.select_for_update().get(pk=task.pk)
            guard_check_run(locked_task, check_kind)
            latest = cls.objects.filter(task=locked_task, check_kind=check_kind).order_by("-attempt_number").first()
            attempt = 1 if latest is None else latest.attempt_number + 1
            run = cls.objects.create(
                task=locked_task, contract_version=locked_task.contract_version, check_kind=check_kind,
                attempt_number=attempt, aggregate_result=aggregate,
                expected_criterion_count=len(criteria), actual_criterion_count=len(normalized),
                context_sha256=payload_sha256({"task_version": locked_task.state_version, "contract": task.contract_version.manifest_sha256}),
                command_id=command_id, payload_hash=digest, evaluator_principal=evaluator_principal,
                evaluator_acting_role=acting_role, permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal, completed_at=timezone.now(),
            )
            for sequence, item in enumerate(normalized, start=1):
                TaskCheckResult.objects.create(
                    check_run=run, criterion_key=item["criterion_key"], criterion_sequence=sequence,
                    required=criterion_map[item["criterion_key"]].get("required", True),
                    result=item["result"], evidence=item.get("evidence", {}),
                    created_by_principal=recorded_by_principal, created_at=timezone.now(),
                )
            return run


class TaskCheckResult(AppendOnlyFact):
    check_run = models.ForeignKey(TaskCheckRun, on_delete=models.PROTECT, related_name="results")
    criterion_key = models.CharField(max_length=120)
    criterion_sequence = models.PositiveIntegerField()
    required = models.BooleanField(default=True)
    result = models.CharField(max_length=16, choices=TaskCheckRun.Result.choices)
    evidence = models.JSONField(default=dict)
    created_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="task_check_results_created"
    )
    created_at = models.DateTimeField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["check_run", "criterion_key"], name="workflow_unique_check_criterion"),
            models.UniqueConstraint(fields=["check_run", "criterion_sequence"], name="workflow_unique_check_sequence"),
        ]
