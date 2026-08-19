import hashlib
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
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
        self.grant = PermissionGrant.objects.create(
            principal=self.owner, scope_kind=PermissionGrant.ScopeKind.PRODUCT, product=self.product,
            action=PermissionGrant.Action.EDIT, valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1), granted_by_principal=self.owner,
        )

    def record_dor(self, result="PASS"):
        return TaskCheckRun.record_completed(
            task=self.task, check_kind=TaskCheckRun.Kind.DOR,
            results=[{"criterion_key": "source_ready", "result": result, "evidence": {"source": "fixture"}}],
            command_id=uuid.uuid4(), evaluator_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )

    def transition(self, to_state):
        self.task.refresh_from_db()
        event = Task.transition(
            task_id=self.task.pk,
            to_state=to_state,
            command_id=uuid.uuid4(),
            expected_state_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grant,
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
                permission_grant=self.grant, recorded_by_principal=self.owner,
            )
        with self.assertRaises(OptimisticConcurrencyConflict):
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid.uuid4(),
                expected_state_version=0, actor_principal=self.owner, acting_role=ActingRole.OWNER,
                permission_grant=self.grant, recorded_by_principal=self.owner,
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
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
        replay = TaskAssignment.record(
            task=self.task, assignee_principal=self.operator, command_id=command_id,
            expected_task_version=1, assigned_by_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
        self.assertEqual(assignment.pk, replay.pk)
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_assignee_principal, self.operator)
        self.assertEqual(self.operator.permission_grants.count(), 0)
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid.uuid4(),
            expected_state_version=1, actor_principal=self.owner, acting_role=ActingRole.OWNER,
            permission_grant=self.grant, recorded_by_principal=self.owner,
        )
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
                    permission_grant=self.grant,
                    recorded_by_principal=self.owner,
                )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.DRAFT)
        self.assertEqual(self.task.state_version, 0)

    def test_exact_facts_drive_chain_from_draft_to_approved(self):
        from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission

        review_grant = PermissionGrant.objects.create(
            principal=self.owner,
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
            permission_grant=self.grant,
            recorded_by_principal=self.owner,
        )
        self.transition(Task.State.ASSIGNED)
        self.transition(Task.State.IN_PROGRESS)

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
            evaluator_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grant,
            recorded_by_principal=self.owner,
        )
        guard_submission(self.task, dod_check_run=dod)

        asset = ContentAsset.create_idempotent(
            task=self.task,
            asset_key="quora-answer",
            title="Quora answer",
            asset_kind=ContentAsset.AssetKind.COPY,
            command_id=uuid.uuid4(),
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grant,
            recorded_by_principal=self.owner,
        )
        body = b"An exact immutable answer draft."
        version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"tasks/{self.task.pk}/answer-v1.txt",
            mime_type="text/plain",
            byte_size=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
            command_id=uuid.uuid4(),
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grant,
            recorded_by_principal=self.owner,
        )
        submission = TaskSubmission.seal(
            task=self.task,
            dod_check_run=dod,
            primary_asset_version=version,
            command_id=uuid.uuid4(),
            expected_task_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.grant,
            recorded_by_principal=self.owner,
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
            reviewer_principal=self.owner,
            acting_role=ActingRole.OWNER,
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
