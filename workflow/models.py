from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, models, transaction
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
        State.APPROVED: {State.HUMAN_REWORK, State.DONE, State.CANCELLED},
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
    creation_command_id = models.UUIDField(null=True, blank=True, unique=True)
    creation_payload_hash = models.CharField(max_length=64, blank=True)
    created_by_acting_role = models.CharField(max_length=24, choices=ActingRole.choices, blank=True)
    created_under_grant = models.ForeignKey(
        "accounts.PermissionGrant",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="tasks_created",
    )
    updated_by_principal = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="tasks_updated"
    )

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        creation_command_id__isnull=True,
                        creation_payload_hash="",
                        created_by_acting_role="",
                        created_under_grant__isnull=True,
                    )
                    | (
                        Q(creation_command_id__isnull=False, created_under_grant__isnull=False)
                        & ~Q(creation_payload_hash="")
                        & ~Q(created_by_acting_role="")
                    )
                ),
                name="workflow_task_creation_audit_complete",
            )
        ]

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

    @classmethod
    def create_draft(
        cls,
        *,
        task_id: uuid.UUID,
        command_id: uuid.UUID,
        product_profile_version_id,
        contract_version_id,
        title: str,
        description: str,
        actor_principal,
        acting_role: str,
    ) -> tuple[Task, bool]:
        """Create one audited DRAFT through the sole interactive runtime command.

        Direct ORM creation remains available for migrations and historical test
        fixtures.  Interactive callers must use this command so authorization,
        exact configuration binding, and idempotency are checked in one
        transaction.
        """

        payload = {
            "task_id": str(task_id),
            "product_profile_version_id": str(product_profile_version_id),
            "contract_version_id": str(contract_version_id),
            "title": title,
            "description": description,
            "actor_principal_id": str(getattr(actor_principal, "pk", "")),
            "acting_role": acting_role,
        }
        digest = payload_sha256(payload)

        def resolve_existing(existing: Task) -> tuple[Task, bool]:
            if existing.creation_command_id != command_id or existing.creation_payload_hash != digest:
                raise CommandReplayConflict(
                    "该任务 ID 已用于另一份创建内容；请刷新页面生成新的任务 ID。"
                )
            return existing, False

        from accounts.authorization import resolve_authorization
        from accounts.models import PermissionGrant, Principal
        from products.models import Product, ProductProfileVersion

        with transaction.atomic():
            existing = cls.objects.select_for_update().filter(creation_command_id=command_id).first()
            if existing is not None:
                return resolve_existing(existing)
            existing = cls.objects.select_for_update().filter(pk=task_id).first()
            if existing is not None:
                return resolve_existing(existing)

            principal = Principal.objects.select_for_update().filter(pk=getattr(actor_principal, "pk", None)).first()
            if (
                principal is None
                or principal.principal_type != Principal.PrincipalType.HUMAN_USER
                or not principal.can_authenticate
            ):
                raise PermissionDenied("ACTIVE_HUMAN_PRINCIPAL_REQUIRED")
            if acting_role not in ActingRole.values or acting_role == ActingRole.SYSTEM or principal.role != acting_role:
                raise PermissionDenied("ACTING_ROLE_MISMATCH")

            profile_ref = ProductProfileVersion.objects.filter(pk=product_profile_version_id).values(
                "product_id"
            ).first()
            if profile_ref is None:
                raise ValidationError("The selected ProductProfileVersion does not exist.")
            product = Product.objects.select_for_update().get(pk=profile_ref["product_id"])
            profile = ProductProfileVersion.objects.select_for_update().get(pk=product_profile_version_id)
            contract = TaskContractVersion.objects.select_for_update().filter(pk=contract_version_id).first()

            if product.product_status != Product.ProductStatus.ACTIVE:
                raise ValidationError("Tasks may only be created for an active Product.")
            if product.current_profile_version_id != profile.pk or not profile.is_sealed:
                raise ValidationError("Task creation requires the current sealed ProductProfileVersion.")
            if contract is None or contract.product_profile_version_id != profile.pk or not contract.sealed_at:
                raise ValidationError("Task creation requires a sealed contract for the exact profile.")
            latest_contract_id = (
                TaskContractVersion.objects.filter(product_profile_version=profile)
                .order_by("-version_number", "-created_at", "-id")
                .values_list("pk", flat=True)
                .first()
            )
            if latest_contract_id != contract.pk:
                raise ValidationError("Task creation requires the latest contract for the exact profile.")

            decision = resolve_authorization(
                principal=principal,
                acting_role=acting_role,
                action=PermissionGrant.Action.CREATE_TASK,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=product,
            )
            if not decision.allowed or decision.grant is None:
                raise PermissionDenied(decision.reason)
            locked_grant = PermissionGrant.objects.select_for_update().get(pk=decision.grant.pk)
            current = resolve_authorization(
                principal=principal,
                acting_role=acting_role,
                action=PermissionGrant.Action.CREATE_TASK,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=product,
            )
            if not current.allowed or current.grant is None or current.grant.pk != locked_grant.pk:
                raise PermissionDenied(current.reason if not current.allowed else "GRANT_CHANGED_DURING_COMMAND")

            task = cls(
                id=task_id,
                product=product,
                product_profile_version=profile,
                contract_version=contract,
                title=title,
                description=description,
                created_by_principal=principal,
                creation_command_id=command_id,
                creation_payload_hash=digest,
                created_by_acting_role=acting_role,
                created_under_grant=locked_grant,
                updated_by_principal=principal,
            )
            try:
                with transaction.atomic():
                    task.save(force_insert=True)
            except IntegrityError:
                winner = cls.objects.select_for_update().filter(creation_command_id=command_id).first()
                if winner is None:
                    winner = cls.objects.select_for_update().filter(pk=task_id).first()
                if winner is None:
                    raise
                return resolve_existing(winner)
            return task, True

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
                if (
                    existing.event_type != TaskStateEvent.EventType.STATE_TRANSITION
                    or existing.payload_hash != digest
                ):
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
            if task.current_state == cls.State.APPROVED and to_state == cls.State.HUMAN_REWORK:
                raise IllegalTaskTransition(
                    "Approved content must return through the exact approved-submission rework command."
                )
            if task.current_state == cls.State.APPROVED and to_state == cls.State.CANCELLED:
                if acting_role not in {ActingRole.OWNER, ActingRole.OPERATIONS_ADMIN}:
                    raise PermissionDenied("ONLY_OWNER_OR_ADMIN_CAN_STOP_PUBLICATION")
                if not reason.strip():
                    raise ValidationError("Stopping publication requires a reason.")
            # Projecting a newly-created assignment from READY is the
            # privileged assignment action.  Returning a BLOCKED task to its
            # recorded ASSIGNED state is only an unblock operation and must
            # not require a second assignment grant.
            required_action = {
                cls.State.CANCELLED: "CANCEL_TASK",
                cls.State.DONE: "COMPLETE_TASK",
            }.get(to_state, "EDIT")
            if task.current_state == cls.State.READY and to_state == cls.State.ASSIGNED:
                required_action = "ASSIGN_TASK"
            validate_current_grant(
                permission_grant,
                principal=actor_principal,
                acting_role=acting_role,
                action=required_action,
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
            if (
                to_state == cls.State.IN_PROGRESS
                and task.current_assignee_principal_id != actor_principal.pk
            ):
                raise ValidationError("Only the task's current assignee may start or resume work.")
            guard_transition_prerequisites(task, to_state)
            previous = task.state_events.order_by("-event_sequence").first()
            next_version = task.state_version + 1
            event = TaskStateEvent.objects.create(
                task=task,
                event_type=TaskStateEvent.EventType.STATE_TRANSITION,
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

    @classmethod
    def stop_publication(
        cls,
        *,
        task_id,
        command_id: uuid.UUID,
        expected_state_version: int,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
        reason: str,
    ) -> TaskStateEvent:
        """Append an audited stop for an approved task without deleting facts."""

        if acting_role not in {ActingRole.OWNER, ActingRole.OPERATIONS_ADMIN}:
            raise PermissionDenied("ONLY_OWNER_OR_ADMIN_CAN_STOP_PUBLICATION")
        if not reason.strip():
            raise ValidationError("Stopping publication requires a reason.")
        return cls.transition(
            task_id=task_id,
            to_state=cls.State.CANCELLED,
            command_id=command_id,
            expected_state_version=expected_state_version,
            actor_principal=actor_principal,
            acting_role=acting_role,
            permission_grant=permission_grant,
            recorded_by_principal=recorded_by_principal,
            reason=reason.strip(),
        )

    @classmethod
    def return_approved_submission_for_rework(
        cls,
        *,
        task_id,
        submission_id,
        command_id: uuid.UUID,
        expected_state_version: int,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
        reason: str,
    ) -> TaskStateEvent:
        """Return the exact approved link-only submission for a new inline version.

        The old Submission, ReviewDecision, Gate and Publication intent remain
        immutable.  The task lock serializes this command with final publication.
        """

        from contentops.models import ContentAssetVersion, ReviewDecision, TaskSubmission

        with transaction.atomic():
            task = cls.objects.select_for_update().get(pk=task_id)
            submission = TaskSubmission.objects.select_for_update().select_related(
                "primary_asset_version"
            ).get(pk=submission_id)
            payload = {
                "task_id": str(task.pk),
                "submission_id": str(submission.pk),
                "expected_state_version": expected_state_version,
                "actor_principal_id": str(actor_principal.pk),
                "acting_role": acting_role,
                "permission_grant_id": str(permission_grant.pk),
                "reason": reason.strip(),
            }
            digest = payload_sha256(payload)
            existing = TaskStateEvent.objects.filter(command_id=command_id).first()
            if existing:
                if (
                    existing.event_type
                    != TaskStateEvent.EventType.APPROVED_REWORK_REQUESTED
                    or existing.submission_id != submission.pk
                    or existing.payload_hash != digest
                ):
                    raise CommandReplayConflict(
                        "command_id was already used with a different payload."
                    )
                return existing

            if acting_role not in {ActingRole.OWNER, ActingRole.OPERATIONS_ADMIN}:
                raise PermissionDenied("ONLY_OWNER_OR_ADMIN_CAN_RETURN_APPROVED_CONTENT")
            if not reason.strip():
                raise ValidationError("Returning approved content requires a reason.")
            if task.state_version != expected_state_version:
                raise OptimisticConcurrencyConflict(
                    "Task version is stale; reread before returning content."
                )
            if task.current_state != cls.State.APPROVED:
                raise IllegalTaskTransition(
                    "Only an APPROVED task may return for complete inline content."
                )
            if submission.task_id != task.pk:
                raise CheckGateRejected("The submission belongs to another task.")
            latest = TaskSubmission.objects.filter(task=task).order_by(
                "-submission_number"
            ).first()
            if latest is None or latest.pk != submission.pk:
                raise CheckGateRejected("Only the latest exact submission may return for rework.")
            try:
                final_review = submission.final_review
            except ReviewDecision.DoesNotExist:
                final_review = None
            if final_review is None or final_review.decision != ReviewDecision.Decision.APPROVED:
                raise CheckGateRejected(
                    "Returning content requires the exact final APPROVED human review."
                )
            if (
                submission.primary_asset_version.representation_kind
                != ContentAssetVersion.RepresentationKind.EXTERNAL_URL
            ):
                raise CheckGateRejected(
                    "Only a link-only approved submission needs this rework path."
                )
            if TaskStateEvent.objects.filter(
                event_type=TaskStateEvent.EventType.APPROVED_REWORK_REQUESTED,
                submission=submission,
            ).exists():
                raise CheckGateRejected("This approved submission was already returned.")
            validate_current_grant(
                permission_grant,
                principal=actor_principal,
                acting_role=acting_role,
                action="EDIT",
                product_id=task.product_id,
            )

            previous = task.state_events.order_by("-event_sequence").first()
            next_version = task.state_version + 1
            event = TaskStateEvent.objects.create(
                task=task,
                event_type=TaskStateEvent.EventType.APPROVED_REWORK_REQUESTED,
                submission=submission,
                from_state=cls.State.APPROVED,
                to_state=cls.State.HUMAN_REWORK,
                command_id=command_id,
                payload_hash=digest,
                expected_state_version=expected_state_version,
                resulting_state_version=next_version,
                event_sequence=next_version,
                previous_event=previous,
                reason=reason.strip(),
                actor_principal=actor_principal,
                acting_role=acting_role,
                permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
                event_at=timezone.now(),
            )
            task.current_state = cls.State.HUMAN_REWORK
            task.state_version = next_version
            task.updated_by_principal = recorded_by_principal
            task.save(
                update_fields=[
                    "current_state",
                    "state_version",
                    "updated_by_principal",
                    "updated_at",
                ]
            )
            return event

    @classmethod
    def withdraw_submission(
        cls,
        *,
        task_id,
        submission_id,
        command_id: uuid.UUID,
        expected_state_version: int,
        actor_principal,
        acting_role: str,
        permission_grant,
        recorded_by_principal,
        reason: str = "",
    ) -> TaskStateEvent:
        """Withdraw the latest unreviewed submission through one serialized command.

        The task row is deliberately locked before the submission row.  Human review
        uses the same order, so a withdrawal and a final decision cannot both win.
        """

        from contentops.models import ReviewDecision, TaskSubmission

        with transaction.atomic():
            task = cls.objects.select_for_update().get(pk=task_id)
            submission = TaskSubmission.objects.select_for_update().get(pk=submission_id)
            payload = {
                "task_id": str(task.pk),
                "submission_id": str(submission.pk),
                "expected_state_version": expected_state_version,
                "actor_principal_id": str(actor_principal.pk),
                "acting_role": acting_role,
                "permission_grant_id": str(permission_grant.pk),
                "reason": reason,
            }
            digest = payload_sha256(payload)
            existing = TaskStateEvent.objects.filter(command_id=command_id).first()
            if existing:
                if (
                    existing.event_type != TaskStateEvent.EventType.SUBMISSION_WITHDRAWN
                    or existing.submission_id != submission.pk
                    or existing.payload_hash != digest
                ):
                    raise CommandReplayConflict("command_id was already used with a different payload.")
                return existing

            if task.state_version != expected_state_version:
                raise OptimisticConcurrencyConflict("Task version is stale; reread before withdrawing.")
            if task.current_state != cls.State.UNDER_REVIEW:
                raise IllegalTaskTransition("Only an UNDER_REVIEW task may withdraw its submission.")
            if submission.task_id != task.pk:
                raise CheckGateRejected("The submission belongs to another task.")
            if task.current_assignee_principal_id != actor_principal.pk:
                raise ValidationError("Only the task's current assignee may withdraw the submission.")
            validate_current_grant(
                permission_grant,
                principal=actor_principal,
                acting_role=acting_role,
                action="EDIT",
                product_id=task.product_id,
            )
            latest = TaskSubmission.objects.filter(task=task).order_by("-submission_number").first()
            if latest is None or latest.pk != submission.pk:
                raise CheckGateRejected("Only the latest exact submission may be withdrawn.")
            if ReviewDecision.objects.filter(submission=submission).exists():
                raise CheckGateRejected("A submission with a final review decision cannot be withdrawn.")
            if TaskStateEvent.objects.filter(
                event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
                submission=submission,
            ).exists():
                raise CheckGateRejected("This submission has already been withdrawn.")

            previous = task.state_events.order_by("-event_sequence").first()
            next_version = task.state_version + 1
            event = TaskStateEvent.objects.create(
                task=task,
                event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
                submission=submission,
                from_state=cls.State.UNDER_REVIEW,
                to_state=cls.State.IN_PROGRESS,
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
            task.current_state = cls.State.IN_PROGRESS
            task.state_version = next_version
            task.updated_by_principal = recorded_by_principal
            task.save(
                update_fields=[
                    "current_state",
                    "state_version",
                    "updated_by_principal",
                    "updated_at",
                ]
            )
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
    supersedes_assignment = models.OneToOneField(
        "self",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="superseded_by_assignment",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["task", "assignment_number"], name="workflow_unique_assignment_number"),
            models.CheckConstraint(
                condition=(
                    Q(assignment_number=1, supersedes_assignment__isnull=True)
                    | Q(assignment_number__gt=1, supersedes_assignment__isnull=False)
                ),
                name="workflow_assignment_supersession_shape",
            ),
        ]

    def clean(self):
        super().clean()
        if self.assignment_number == 1 and self.supersedes_assignment_id:
            raise ValidationError(
                {"supersedes_assignment": "The first assignment cannot supersede another assignment."}
            )
        if self.assignment_number > 1 and not self.supersedes_assignment_id:
            raise ValidationError(
                {"supersedes_assignment": "A reassignment must link to the immediately previous assignment."}
            )
        if self.supersedes_assignment_id and (
            self.supersedes_assignment.task_id != self.task_id
            or self.supersedes_assignment.assignment_number != self.assignment_number - 1
        ):
            raise ValidationError(
                {"supersedes_assignment": "A reassignment must supersede the immediately older assignment for the same task."}
            )

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
        expected_current_assignment_id=None,
    ) -> TaskAssignment:
        payload = {
            "task_id": str(task.pk),
            "assignee_principal_id": str(assignee_principal.pk),
            "expected_task_version": expected_task_version,
            "expected_current_assignment_id": (
                str(expected_current_assignment_id) if expected_current_assignment_id else ""
            ),
            "assigned_by_principal_id": str(getattr(assigned_by_principal, "pk", "")),
            "acting_role": acting_role,
            "permission_grant_id": str(getattr(permission_grant, "pk", "")),
        }
        digest = payload_sha256(payload)
        existing = cls.objects.filter(command_id=command_id).first()
        if existing:
            if existing.payload_hash != digest:
                raise CommandReplayConflict("command_id was already used for a different assignment.")
            return existing

        with transaction.atomic():
            locked_task = Task.objects.select_for_update().get(pk=task.pk)
            existing = cls.objects.filter(command_id=command_id).first()
            if existing:
                if existing.payload_hash != digest:
                    raise CommandReplayConflict("command_id was already used for a different assignment.")
                return existing
            if locked_task.state_version != expected_task_version:
                raise OptimisticConcurrencyConflict("Task version is stale; reread before assigning.")

            from accounts.authorization import resolve_authorization
            from accounts.models import PermissionGrant, Principal

            persisted_assigner = Principal.objects.filter(pk=getattr(assigned_by_principal, "pk", None)).first()
            if (
                persisted_assigner is None
                or persisted_assigner.principal_type != Principal.PrincipalType.HUMAN_USER
                or not persisted_assigner.can_authenticate
                or persisted_assigner.role != acting_role
            ):
                raise PermissionDenied("ACTIVE_ASSIGNMENT_MANAGER_REQUIRED")
            if persisted_assigner.role not in {
                Principal.Role.OWNER,
                Principal.Role.OPERATIONS_ADMIN,
            }:
                raise PermissionDenied("ONLY_OWNER_OR_ADMIN_CAN_ASSIGN")
            # Lock the exact authority row after the Task.  Grant revocation
            # uses the same row lock, so either revocation wins and this
            # command fails closed, or this audited assignment commits first.
            permission_grant = PermissionGrant.objects.select_for_update().get(
                pk=getattr(permission_grant, "pk", None)
            )
            validate_current_grant(
                permission_grant,
                principal=persisted_assigner,
                acting_role=acting_role,
                action="ASSIGN_TASK",
                product_id=locked_task.product_id,
            )
            latest = cls.objects.filter(task=locked_task).order_by("-assignment_number").first()

            is_initial_assignment = locked_task.current_state == Task.State.READY
            is_reassignment = locked_task.current_state in {
                Task.State.ASSIGNED,
                Task.State.IN_PROGRESS,
            }
            if not is_initial_assignment and not is_reassignment:
                raise IllegalTaskTransition(
                    "Assignments may change only before submission; an item under review cannot be reassigned."
                )
            if locked_task.submissions.exists():
                raise IllegalTaskTransition("A task with a sealed submission cannot be reassigned.")
            if is_initial_assignment:
                if latest is not None or locked_task.current_assignee_principal_id is not None:
                    raise CheckGateRejected("The READY task already has an assignment record.")
                if expected_current_assignment_id is not None:
                    raise OptimisticConcurrencyConflict("The expected assignment is stale; refresh and retry.")
            else:
                if latest is None or locked_task.current_assignee_principal_id is None:
                    raise CheckGateRejected("The task has no current assignment to replace.")
                if latest.pk != expected_current_assignment_id:
                    raise OptimisticConcurrencyConflict("The current assignee changed; refresh and retry.")
                if latest.assignee_principal_id != locked_task.current_assignee_principal_id:
                    raise CheckGateRejected("The assignment projection is inconsistent; reassignment was stopped.")
                if persisted_assigner.role == Principal.Role.OPERATIONS_ADMIN:
                    current_assignee = Principal.objects.filter(
                        pk=locked_task.current_assignee_principal_id
                    ).first()
                    if current_assignee is None or current_assignee.role != Principal.Role.OPERATOR:
                        raise PermissionDenied("ADMIN_MAY_REASSIGN_ONLY_OPERATOR_WORK")

            persisted_assignee = type(assignee_principal).objects.filter(
                pk=assignee_principal.pk
            ).first()
            if (
                persisted_assignee is None
                or persisted_assignee.principal_type != Principal.PrincipalType.HUMAN_USER
                or not persisted_assignee.can_authenticate
            ):
                raise ValidationError("The assignee must be an active human Principal.")
            if (
                persisted_assigner.role == Principal.Role.OPERATIONS_ADMIN
                and persisted_assignee.role != Principal.Role.OPERATOR
            ):
                raise PermissionDenied("ADMIN_MAY_ASSIGN_ONLY_OPERATORS")
            if is_reassignment and persisted_assignee.pk == locked_task.current_assignee_principal_id:
                raise ValidationError("The selected person is already the current assignee.")
            assignee_decision = resolve_authorization(
                principal=persisted_assignee,
                acting_role=persisted_assignee.role,
                action="EDIT",
                scope_kind="PRODUCT",
                product=locked_task.product_id,
            )
            if not assignee_decision.allowed:
                raise ValidationError(
                    f"The assignee is not currently allowed to execute this product task: "
                    f"{assignee_decision.reason}."
                )
            assignment = cls.objects.create(
                task=locked_task,
                assignee_principal=persisted_assignee,
                assignment_number=(latest.assignment_number + 1 if latest else 1),
                command_id=command_id,
                payload_hash=digest,
                expected_task_version=expected_task_version,
                assigned_by_principal=assigned_by_principal,
                acting_role=acting_role,
                permission_grant=permission_grant,
                recorded_by_principal=recorded_by_principal,
                assigned_at=timezone.now(),
                supersedes_assignment=latest,
            )
            Task.objects.filter(pk=locked_task.pk).update(
                current_assignee_principal=persisted_assignee,
                updated_by_principal=recorded_by_principal,
                updated_at=timezone.now(),
            )
            return assignment


class TaskStateEvent(AppendOnlyFact):
    class EventType(models.TextChoices):
        STATE_TRANSITION = "STATE_TRANSITION", "State transition"
        SUBMISSION_WITHDRAWN = "SUBMISSION_WITHDRAWN", "Submission withdrawn"
        APPROVED_REWORK_REQUESTED = (
            "APPROVED_REWORK_REQUESTED",
            "Approved submission returned for rework",
        )

    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="state_events")
    event_type = models.CharField(
        max_length=32,
        choices=EventType.choices,
        default=EventType.STATE_TRANSITION,
    )
    submission = models.ForeignKey(
        "contentops.TaskSubmission",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="withdrawal_events",
    )
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
            models.CheckConstraint(
                condition=(
                    Q(event_type="STATE_TRANSITION", submission__isnull=True)
                    | Q(
                        event_type="SUBMISSION_WITHDRAWN",
                        submission__isnull=False,
                        from_state=Task.State.UNDER_REVIEW,
                        to_state=Task.State.IN_PROGRESS,
                    )
                    | Q(
                        event_type="APPROVED_REWORK_REQUESTED",
                        submission__isnull=False,
                        from_state=Task.State.APPROVED,
                        to_state=Task.State.HUMAN_REWORK,
                    )
                ),
                name="workflow_event_type_payload_shape",
            ),
            models.UniqueConstraint(
                fields=["submission"],
                condition=Q(event_type="SUBMISSION_WITHDRAWN"),
                name="workflow_one_withdrawal_per_submission",
            ),
            models.UniqueConstraint(
                fields=["submission"],
                condition=Q(event_type="APPROVED_REWORK_REQUESTED"),
                name="workflow_one_approved_rework_per_submission",
            ),
        ]

    def clean(self):
        super().clean()
        if self.event_type == self.EventType.STATE_TRANSITION:
            if self.submission_id is not None:
                raise ValidationError("A normal state transition cannot reference a submission.")
        elif self.event_type == self.EventType.SUBMISSION_WITHDRAWN:
            if self.submission_id is None:
                raise ValidationError("A withdrawal event must reference the exact submission.")
            if (
                self.from_state != Task.State.UNDER_REVIEW
                or self.to_state != Task.State.IN_PROGRESS
            ):
                raise ValidationError("A withdrawal event must move UNDER_REVIEW to IN_PROGRESS.")
            if self.task_id and self.submission.task_id != self.task_id:
                raise ValidationError("The withdrawn submission belongs to another task.")
        elif self.event_type == self.EventType.APPROVED_REWORK_REQUESTED:
            if self.submission_id is None:
                raise ValidationError(
                    "An approved rework event must reference the exact submission."
                )
            if (
                self.from_state != Task.State.APPROVED
                or self.to_state != Task.State.HUMAN_REWORK
            ):
                raise ValidationError(
                    "An approved rework event must move APPROVED to HUMAN_REWORK."
                )
            if self.task_id and self.submission.task_id != self.task_id:
                raise ValidationError(
                    "The returned submission belongs to another task."
                )
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
            if (
                check_kind == cls.Kind.DOD
                and locked_task.current_assignee_principal_id != evaluator_principal.pk
            ):
                raise ValidationError("Only the task's current assignee may record its DoD result.")
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
