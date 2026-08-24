import hashlib
import uuid
from datetime import timedelta

from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from core.ids import uuid7
from products.models import Product, ProductProfileVersion
from workflow.exceptions import (
    CheckGateRejected,
    CommandReplayConflict,
    IllegalTaskTransition,
    OptimisticConcurrencyConflict,
)
from workflow.models import ActingRole, Task, TaskAssignment, TaskCheckRun, TaskContractVersion
from workflow.services import guard_release_gate, guard_review, guard_submission


class WorkflowFoundationTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="owner", password="local-test-only", role=Principal.Role.OWNER
        )
        self.operator = Principal.objects.create_user(username="operator", password="local-test-only")
        self.product = Product.objects.create(
            product_code="PUKO", name="PUKO", market_code="US", language_code="en",
            created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.profile = ProductProfileVersion.objects.create(
            product=self.product, version_number=1, market_code="US", language_code="en",
            audience={}, core_value_proposition="Evidence-informed wellness.", brand_voice={},
            product_facts={}, prohibited_expressions=[], created_by_principal=self.owner,
        )
        self.profile.seal(self.owner)
        self.product.current_profile_version = self.profile
        self.product.save(update_fields=["current_profile_version", "updated_at"])
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile, version_number=1, title="Content production",
            dor_criteria=[{"key": "source_ready", "required": True}],
            dod_criteria=[{"key": "copy_complete", "required": True}],
            release_gate_criteria=[{"key": "policy_pass", "required": True}],
            success_criteria=[{"key": "result_observed", "required": False}],
            sealed_at=timezone.now(), created_by_principal=self.owner,
        )
        self.task = Task.objects.create(
            product=self.product, product_profile_version=self.profile, contract_version=self.contract,
            title="Draft one answer", created_by_principal=self.owner, updated_by_principal=self.owner,
        )
        self.grants = {
            action: PermissionGrant.objects.create(
                principal=self.owner,
                scope_kind=PermissionGrant.ScopeKind.PRODUCT,
                product=self.product,
                action=action,
                valid_from=timezone.now() - timedelta(minutes=1),
                valid_until=timezone.now() + timedelta(hours=1),
                granted_by_principal=self.owner,
            )
            for action in (
                PermissionGrant.Action.EDIT,
                PermissionGrant.Action.CREATE_TASK,
                PermissionGrant.Action.ASSIGN_TASK,
                PermissionGrant.Action.CANCEL_TASK,
                PermissionGrant.Action.COMPLETE_TASK,
            )
        }
        self.grant = self.grants[PermissionGrant.Action.EDIT]
        self.operator_edit_grant = PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )

    def create_draft(self, *, task_id=None, command_id=None, title="A controlled draft"):
        return Task.create_draft(
            task_id=task_id or uuid7(),
            command_id=command_id or uuid.uuid4(),
            product_profile_version_id=self.profile.pk,
            contract_version_id=self.contract.pk,
            title=title,
            description="Created through the audited aggregate command.",
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
        )

    def test_create_draft_records_exact_grant_and_replays_only_same_payload(self):
        task_id = uuid7()
        command_id = uuid.uuid4()
        created, was_created = self.create_draft(task_id=task_id, command_id=command_id)
        replayed, replay_created = self.create_draft(task_id=task_id, command_id=command_id)

        self.assertTrue(was_created)
        self.assertFalse(replay_created)
        self.assertEqual(replayed.pk, created.pk)
        self.assertEqual(created.creation_command_id, command_id)
        self.assertEqual(
            created.created_under_grant_id,
            self.grants[PermissionGrant.Action.CREATE_TASK].pk,
        )
        self.assertEqual(created.created_by_acting_role, ActingRole.OWNER)
        self.assertEqual(Task.objects.filter(creation_command_id=command_id).count(), 1)

        with self.assertRaises(CommandReplayConflict):
            self.create_draft(
                task_id=task_id,
                command_id=command_id,
                title="Different payload under the same command",
            )
        self.assertEqual(Task.objects.filter(creation_command_id=command_id).count(), 1)

    def test_create_draft_fails_closed_after_create_grant_revocation_without_partial_task(self):
        create_grant = self.grants[PermissionGrant.Action.CREATE_TASK]
        create_grant.grant_status = PermissionGrant.GrantStatus.REVOKED
        create_grant.revoked_at = timezone.now()
        create_grant.revoked_by_principal = self.owner
        create_grant.revocation_reason = "Revoked before command execution."
        create_grant.save()
        task_id = uuid7()

        with self.assertRaises(PermissionDenied):
            self.create_draft(task_id=task_id)
        self.assertFalse(Task.objects.filter(pk=task_id).exists())

    def test_create_draft_rejects_edit_grant_used_as_create_permission(self):
        task_id = uuid7()
        with self.assertRaises(PermissionDenied):
            Task.create_draft(
                task_id=task_id,
                command_id=uuid.uuid4(),
                product_profile_version_id=self.profile.pk,
                contract_version_id=self.contract.pk,
                title="EDIT is not CREATE_TASK",
                description="Wrong actions must fail closed.",
                actor_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
            )
        self.assertFalse(Task.objects.filter(pk=task_id).exists())

    def test_create_draft_rejects_non_latest_contract_without_partial_task(self):
        latest = TaskContractVersion.objects.create(
            product_profile_version=self.profile,
            version_number=2,
            title="New exact contract",
            dor_criteria=[{"key": "source_ready", "required": True}],
            dod_criteria=[{"key": "copy_complete", "required": True}],
            release_gate_criteria=[{"key": "policy_pass", "required": True}],
            success_criteria=[{"key": "result_observed", "required": False}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        task_id = uuid7()
        with self.assertRaises(ValidationError):
            self.create_draft(task_id=task_id)
        self.assertFalse(Task.objects.filter(pk=task_id).exists())
        self.assertIsNotNone(latest.pk)

    def record_dor(self, result="PASS"):
        return TaskCheckRun.record_completed(
            task=self.task, check_kind=TaskCheckRun.Kind.DOR,
            results=[{"criterion_key": "source_ready", "result": result, "evidence": {"source": "fixture"}}],
            command_id=uuid.uuid4(), evaluator_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )

    def transition(self, to_state):
        self.task.refresh_from_db()
        grant = {
            Task.State.ASSIGNED: self.grants[PermissionGrant.Action.ASSIGN_TASK],
            Task.State.CANCELLED: self.grants[PermissionGrant.Action.CANCEL_TASK],
            Task.State.DONE: self.grants[PermissionGrant.Action.COMPLETE_TASK],
        }.get(to_state, self.grant)
        event = Task.transition(
            task_id=self.task.pk,
            to_state=to_state,
            command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        return event

    def test_ready_is_fail_closed_until_dor_passes(self):
        self.record_dor(TaskCheckRun.Result.BLOCKED)
        with self.assertRaises(CheckGateRejected):
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.READY, command_id=uuid.uuid4(),
                expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
                permission_grant=self.grant, recorded_by_principal=self.owner,
            )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.DRAFT)
        self.assertEqual(self.task.state_events.count(), 0)

    def test_transition_is_idempotent_and_optimistically_locked(self):
        self.record_dor()
        command_id = uuid.uuid4()
        event = Task.transition(
            task_id=self.task.pk, to_state=Task.State.READY, command_id=command_id,
            expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
        replay = Task.transition(
            task_id=self.task.pk, to_state=Task.State.READY, command_id=command_id,
            expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
        self.assertEqual(event.pk, replay.pk)
        self.assertEqual(self.task.state_events.count(), 1)
        with self.assertRaises(CommandReplayConflict):
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.CANCELLED, command_id=command_id,
                expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
                permission_grant=self.grants[PermissionGrant.Action.CANCEL_TASK], recorded_by_principal=self.owner,
            )
        with self.assertRaises(OptimisticConcurrencyConflict):
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid.uuid4(),
                expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
                permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK], recorded_by_principal=self.owner,
            )

    def test_completed_check_facts_are_immutable(self):
        run = self.record_dor()
        run.aggregate_result = TaskCheckRun.Result.FAIL
        with self.assertRaises(ValidationError):
            run.save()
        with self.assertRaises(ValidationError):
            run.results.all().delete()

    def test_assignment_is_explicit_idempotent_and_does_not_grant_review_or_publish(self):
        self.record_dor()
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.READY, command_id=uuid.uuid4(),
            expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        command_id = uuid.uuid4()
        assignment = TaskAssignment.record(
            task=self.task, assignee_principal=self.operator, command_id=command_id,
            expected_task_version=1, assigned_by_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK], recorded_by_principal=self.owner,
        )
        replay = TaskAssignment.record(
            task=self.task, assignee_principal=self.operator, command_id=command_id,
            expected_task_version=1, assigned_by_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK], recorded_by_principal=self.owner,
        )
        self.assertEqual(assignment.pk, replay.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_assignee_principal, self.operator)
        self.assertEqual(
            set(self.operator.permission_grants.values_list("action", flat=True)),
            {PermissionGrant.Action.EDIT},
        )
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid.uuid4(),
            expected_state_version=1, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK], recorded_by_principal=self.owner,
        )
        self.assertEqual(self.task.assignments.count(), 1)

    def test_assignment_rejects_an_assignee_without_current_edit_permission(self):
        outsider = Principal.objects.create_user(
            username="unqualified-assignee", password="local-test-only"
        )
        self.record_dor()
        self.transition(Task.State.READY)

        with self.assertRaises(ValidationError):
            TaskAssignment.record(
                task=self.task,
                assignee_principal=outsider,
                command_id=uuid.uuid4(),
                expected_task_version=self.task.state_version,
                assigned_by_principal=self.owner,
                acting_role=ActingRole.OWNER,
                permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
                recorded_by_principal=self.owner,
            )

        self.task.refresh_from_db()
        self.assertIsNone(self.task.current_assignee_principal_id)
        self.assertFalse(self.task.assignments.exists())

    def test_reassignment_appends_a_new_assignment_and_rejects_a_stale_manager_tab(self):
        second_operator = Principal.objects.create_user(
            username="second-operator",
            password="local-test-only",
            role=Principal.Role.OPERATOR,
        )
        second_operator_edit = PermissionGrant.objects.create(
            principal=second_operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        self.assertIsNotNone(second_operator_edit.pk)
        self.record_dor()
        self.transition(Task.State.READY)
        first = TaskAssignment.record(
            task=self.task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.ASSIGNED)

        second = TaskAssignment.record(
            task=self.task,
            assignee_principal=second_operator,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            expected_current_assignment_id=first.pk,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.ASSIGNED)
        self.assertEqual(self.task.current_assignee_principal_id, second_operator.pk)
        self.assertEqual(second.assignment_number, 2)
        self.assertEqual(second.supersedes_assignment_id, first.pk)
        self.assertEqual(self.task.assignments.count(), 2)

        with self.assertRaises(OptimisticConcurrencyConflict):
            TaskAssignment.record(
                task=self.task,
                assignee_principal=self.operator,
                command_id=uuid.uuid4(),
                expected_task_version=self.task.state_version,
                expected_current_assignment_id=first.pk,
                assigned_by_principal=self.owner,
                acting_role=ActingRole.OWNER,
                permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
                recorded_by_principal=self.owner,
            )
        self.assertEqual(self.task.assignments.count(), 2)

    def test_operator_cannot_assign_and_admin_can_assign_only_operators(self):
        admin = Principal.objects.create_user(
            username="assignment-admin",
            password="local-test-only",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        now = timezone.now()
        admin_assign = PermissionGrant.objects.create(
            principal=admin,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.ASSIGN_TASK,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        operator_assign = PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.ASSIGN_TASK,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        self.record_dor()
        self.transition(Task.State.READY)

        with self.assertRaises(PermissionDenied):
            TaskAssignment.record(
                task=self.task,
                assignee_principal=self.owner,
                command_id=uuid.uuid4(),
                expected_task_version=self.task.state_version,
                assigned_by_principal=admin,
                acting_role=ActingRole.OPERATIONS_ADMIN,
                permission_grant=admin_assign,
                recorded_by_principal=admin,
            )
        with self.assertRaises(PermissionDenied):
            TaskAssignment.record(
                task=self.task,
                assignee_principal=self.operator,
                command_id=uuid.uuid4(),
                expected_task_version=self.task.state_version,
                assigned_by_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=operator_assign,
                recorded_by_principal=self.operator,
            )
        self.assertFalse(self.task.assignments.exists())

        # Admin may manage Operator work, but may not take a task away from an
        # Owner/Admin even when the proposed replacement is an Operator.
        first = TaskAssignment.record(
            task=self.task,
            assignee_principal=self.owner,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.ASSIGNED)
        with self.assertRaises(PermissionDenied):
            TaskAssignment.record(
                task=self.task,
                assignee_principal=self.operator,
                command_id=uuid.uuid4(),
                expected_task_version=self.task.state_version,
                expected_current_assignment_id=first.pk,
                assigned_by_principal=admin,
                acting_role=ActingRole.OPERATIONS_ADMIN,
                permission_grant=admin_assign,
                recorded_by_principal=admin,
            )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_assignee_principal_id, self.owner.pk)
        self.assertEqual(self.task.assignments.count(), 1)

    def test_reassignment_chain_must_link_the_immediately_previous_assignment(self):
        self.record_dor()
        self.transition(Task.State.READY)
        first = TaskAssignment.record(
            task=self.task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        with self.assertRaises(ValidationError):
            TaskAssignment.objects.create(
                task=self.task,
                assignee_principal=self.operator,
                assignment_number=2,
                command_id=uuid.uuid4(),
                payload_hash="a" * 64,
                expected_task_version=self.task.state_version,
                assigned_by_principal=self.owner,
                acting_role=ActingRole.OWNER,
                permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
                recorded_by_principal=self.owner,
                assigned_at=timezone.now(),
                supersedes_assignment=None,
            )
        self.assertEqual(self.task.assignments.get().pk, first.pk)

    def test_only_current_assignee_can_start_and_record_dod(self):
        self.record_dor()
        self.transition(Task.State.READY)
        TaskAssignment.record(
            task=self.task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.ASSIGNED)

        with self.assertRaises(ValidationError):
            self.transition(Task.State.IN_PROGRESS)

        self.task.refresh_from_db()
        Task.transition(
            task_id=self.task.pk,
            to_state=Task.State.IN_PROGRESS,
            command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit_grant,
            recorded_by_principal=self.operator,
        )
        self.task.refresh_from_db()

        with self.assertRaises(ValidationError):
            TaskCheckRun.record_completed(
                task=self.task,
                check_kind=TaskCheckRun.Kind.DOD,
                results=[
                    {
                        "criterion_key": "copy_complete",
                        "result": TaskCheckRun.Result.PASS,
                        "evidence": {"asset": "wrong actor"},
                    }
                ],
                command_id=uuid.uuid4(),
                evaluator_principal=self.owner,
                acting_role=ActingRole.OWNER,
                permission_grant=self.grant,
                recorded_by_principal=self.owner,
            )
        self.assertFalse(
            self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).exists()
        )

    def test_unblocking_to_assigned_uses_edit_not_a_new_assignment_grant(self):
        self.record_dor()
        self.transition(Task.State.READY)
        TaskAssignment.record(
            task=self.task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.ASSIGNED)
        self.transition(Task.State.BLOCKED)
        self.task.refresh_from_db()

        Task.transition(
            task_id=self.task.pk,
            to_state=Task.State.ASSIGNED,
            command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grant,
            recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.ASSIGNED)
        self.assertEqual(self.task.assignments.count(), 1)

    def test_blocked_task_can_only_return_to_recorded_prior_state(self):
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.BLOCKED, command_id=uuid.uuid4(),
            expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner, reason="Missing source",
        )
        self.task.refresh_from_db()
        with self.assertRaises(IllegalTaskTransition):
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.IN_PROGRESS, command_id=uuid.uuid4(),
                expected_state_version=1, actor_principal=self.owner, acting_role=ActingRole.OWNER,
                permission_grant=self.grant, recorded_by_principal=self.owner,
            )
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.DRAFT, command_id=uuid.uuid4(),
            expected_state_version=1, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.DRAFT)
        self.assertEqual(list(self.task.state_events.values_list("to_state", flat=True)), ["BLOCKED", "DRAFT"])

    def test_dod_is_rejected_until_task_is_in_progress(self):
        with self.assertRaises(IllegalTaskTransition):
            TaskCheckRun.record_completed(
                task=self.task,
                check_kind=TaskCheckRun.Kind.DOD,
                results=[
                    {
                        "criterion_key": "copy_complete",
                        "result": TaskCheckRun.Result.PASS,
                        "evidence": {"draft": "fixture"},
                    }
                ],
                command_id=uuid.uuid4(),
                evaluator_principal=self.owner,
                acting_role=ActingRole.OWNER,
                permission_grant=self.grant,
                recorded_by_principal=self.owner,
            )
        self.assertFalse(self.task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).exists())

    def test_draft_cannot_jump_to_publish_adjacent_states(self):
        for target in (
            Task.State.SUBMITTED,
            Task.State.UNDER_REVIEW,
            Task.State.APPROVED,
            Task.State.DONE,
        ):
            with self.subTest(target=target), self.assertRaises(IllegalTaskTransition):
                Task.transition(
                    task_id=self.task.pk,
                    to_state=target,
                    command_id=uuid.uuid4(),
                    expected_state_version=0,
                    actor_principal=self.owner,
                    acting_role=ActingRole.OWNER,
                    permission_grant=(
                        self.grants[PermissionGrant.Action.COMPLETE_TASK]
                        if target == Task.State.DONE
                        else self.grant
                    ),
                    recorded_by_principal=self.owner,
                )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.DRAFT)
        self.assertEqual(self.task.state_version, 0)

    def test_exact_facts_drive_chain_from_draft_to_approved(self):
        from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission

        reviewer = Principal.objects.create_user(
            username="workflow-reviewer",
            password="local-test-only",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        review_grant = PermissionGrant.objects.create(
            principal=reviewer,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.REVIEW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )

        self.record_dor()
        self.transition(Task.State.READY)
        TaskAssignment.record(
            task=self.task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grants[PermissionGrant.Action.ASSIGN_TASK],
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.ASSIGNED)
        self.task.refresh_from_db()
        Task.transition(
            task_id=self.task.pk,
            to_state=Task.State.IN_PROGRESS,
            command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit_grant,
            recorded_by_principal=self.operator,
        )
        self.task.refresh_from_db()

        dod = TaskCheckRun.record_completed(
            task=self.task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[
                {
                    "criterion_key": "copy_complete",
                    "result": TaskCheckRun.Result.PASS,
                    "evidence": {"asset": "fixture"},
                }
            ],
            command_id=uuid.uuid4(),
            evaluator_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit_grant,
            recorded_by_principal=self.operator,
        )
        guard_submission(self.task, dod_check_run=dod)

        asset = ContentAsset.create_idempotent(
            task=self.task,
            asset_key="quora-answer",
            title="Quora answer",
            asset_kind=ContentAsset.AssetKind.COPY,
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit_grant,
            recorded_by_principal=self.operator,
        )
        body = b"An exact immutable answer draft."
        version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"https://assets.example.com/tasks/{self.task.pk}/answer-v1.txt",
            mime_type="text/plain",
            byte_size=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit_grant,
            recorded_by_principal=self.operator,
        )
        submission = TaskSubmission.seal(
            task=self.task,
            dod_check_run=dod,
            primary_asset_version=version,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit_grant,
            recorded_by_principal=self.operator,
        )
        self.transition(Task.State.SUBMITTED)
        self.transition(Task.State.UNDER_REVIEW)
        guard_review(self.task, submission=submission)

        review = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Exact sealed submission approved by a human.",
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            reviewer_principal=reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=review_grant,
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.APPROVED)
        guard_release_gate(self.task, submission=submission, review_decision=review)

        with self.assertRaises(CheckGateRejected):
            self.transition(Task.State.DONE)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.APPROVED)
        self.assertEqual(
            list(self.task.state_events.values_list("to_state", flat=True)),
            ["READY", "ASSIGNED", "IN_PROGRESS", "SUBMITTED", "UNDER_REVIEW", "APPROVED"],
        )
