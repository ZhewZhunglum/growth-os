"""Frozen Dogfood V1 acceptance paths AC-01 through AC-05.

These tests deliberately cross application boundaries.  They are not a
replacement for the smaller domain tests; they prove that the exact immutable
facts can be composed into (or rejected from) the frozen manual-publishing
workflow.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from contentops.models import ContentAsset, ContentAssetVersion, ReviewDecision, TaskSubmission
from products.models import Product, ProductProfileVersion
from releasegate.models import (
    AccountEnvironmentBinding,
    ActingRole as ReleaseActingRole,
    CapabilityState,
    ChannelAccount,
    PolicyDefinition,
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
)
from workflow.exceptions import (
    CheckGateRejected,
    CommandReplayConflict,
    OptimisticConcurrencyConflict,
)
from workflow.models import (
    ActingRole,
    Task,
    TaskAssignment,
    TaskCheckRun,
    TaskContractPolicyLink,
    TaskContractVersion,
)


def unique_digest(label: str) -> str:
    return canonical_sha256({"label": label, "nonce": str(uuid.uuid4())})


class FrozenV1AcceptanceTests(TestCase):
    """Executable sign-off evidence for the five frozen acceptance paths."""

    def setUp(self):
        self.now = timezone.now()
        self.owner = Principal.objects.create_user(
            username="acceptance-owner",
            password="test-only-password",
            role=Principal.Role.OWNER,
        )
        self.operator = Principal.objects.create_user(
            username="acceptance-operator",
            password="test-only-password",
            role=Principal.Role.OPERATOR,
        )
        self.reviewer = Principal.objects.create_user(
            username="acceptance-reviewer",
            password="test-only-password",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.publisher = Principal.objects.create_user(
            username="acceptance-publisher",
            password="test-only-password",
            role=Principal.Role.OPERATOR,
        )
        self.rule_evaluator = Principal.objects.create_user(
            username="acceptance-rule-evaluator",
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
            role=Principal.Role.OPERATOR,
        )

        self.product = Product.objects.create(
            product_code="PUKO-ACCEPTANCE",
            name="PUKO Nutrition",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"market": "US", "mode": "B2C"},
            core_value_proposition="Evidence-informed daily wellness.",
            brand_voice={"tone": "clear and measured"},
            product_facts={"pilot": "PUKO"},
            prohibited_expressions=["cure", "treat disease"],
            created_by_principal=self.owner,
        )
        self.profile.seal(self.owner)

        self.policy_definition = PolicyDefinition.objects.create(
            policy_code="V1_ACCEPTANCE_RELEASE",
            name="Frozen V1 release checks",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.policy_version = PolicyVersion.objects.create(
            policy_definition=self.policy_definition,
            version_number=1,
            rules=[{"rule_code": "exact_release_context", "required": True}],
            effective_from=self.now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile,
            version_number=1,
            title="PUKO manual publication",
            dor_criteria=[{"key": "source_ready", "required": True}],
            dod_criteria=[{"key": "deliverable_complete", "required": True}],
            release_gate_criteria=[{"key": "exact_release_context", "required": True}],
            success_criteria=[{"key": "proof_recorded", "required": True}],
            sealed_at=self.now,
            created_by_principal=self.owner,
        )
        TaskContractPolicyLink.objects.create(
            task_contract_version=self.contract,
            policy_version=self.policy_version,
            required=True,
            created_by_principal=self.owner,
        )

        self.owner_edit = self._grant(
            self.owner,
            PermissionGrant.Action.EDIT,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.owner_assign = self._grant(
            self.owner,
            PermissionGrant.Action.ASSIGN_TASK,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.owner_cancel = self._grant(
            self.owner,
            PermissionGrant.Action.CANCEL_TASK,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.owner_complete = self._grant(
            self.owner,
            PermissionGrant.Action.COMPLETE_TASK,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.operator_edit = self._grant(
            self.operator,
            PermissionGrant.Action.EDIT,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.reviewer_review = self._grant(
            self.reviewer,
            PermissionGrant.Action.REVIEW,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )
        self.publisher_publish = self._grant(
            self.publisher,
            PermissionGrant.Action.PUBLISH,
            PermissionGrant.ScopeKind.ACCOUNT,
            account_ref="puko-us-acceptance",
        )
        self.evaluator_review = self._grant(
            self.rule_evaluator,
            PermissionGrant.Action.REVIEW,
            PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
        )

        self.channel_account = ChannelAccount.objects.create(
            platform_code="TIKTOK",
            account_code="puko-us-acceptance",
            external_account_ref="puko-us-acceptance",
            display_name="PUKO US Acceptance",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.environment = RuntimeEnvironment.objects.create(
            environment_code="acceptance-staging",
            environment_type=RuntimeEnvironment.EnvironmentType.STAGING,
            identity_namespace="acceptance-identity",
            database_namespace="acceptance-database",
            object_storage_namespace="acceptance-objects",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.binding = AccountEnvironmentBinding.objects.create(
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            binding_version=1,
            identity_reference="acceptance-publisher-identity",
            valid_from=self.now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self.open_capability = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=1,
            state=CapabilityState.State.OPEN,
            effective_from=self.now - timedelta(minutes=5),
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )

    def _grant(self, principal, action, scope_kind, **scope):
        return PermissionGrant.objects.create(
            principal=principal,
            scope_kind=scope_kind,
            action=action,
            effect=PermissionGrant.Effect.ALLOW,
            valid_from=self.now - timedelta(minutes=5),
            valid_until=self.now + timedelta(hours=2),
            granted_by_principal=self.owner,
            **scope,
        )

    def _task(self, title: str) -> Task:
        return Task.objects.create(
            product=self.product,
            product_profile_version=self.profile,
            contract_version=self.contract,
            title=title,
            description="Frozen V1 acceptance fixture",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )

    def _transition(self, task: Task, to_state: str, *, command_id=None, expected_version=None):
        task.refresh_from_db()
        grant = {
            Task.State.ASSIGNED: (
                self.owner_edit
                if task.current_state == Task.State.BLOCKED
                else self.owner_assign
            ),
            Task.State.CANCELLED: self.owner_cancel,
            Task.State.DONE: self.owner_complete,
        }.get(to_state, self.owner_edit)
        actor = self.operator if to_state == Task.State.IN_PROGRESS else self.owner
        acting_role = (
            ActingRole.OPERATOR if to_state == Task.State.IN_PROGRESS else ActingRole.OWNER
        )
        if to_state == Task.State.IN_PROGRESS:
            grant = self.operator_edit
        event = Task.transition(
            task_id=task.pk,
            to_state=to_state,
            command_id=command_id or uuid.uuid4(),
            expected_state_version=task.state_version if expected_version is None else expected_version,
            actor_principal=actor,
            acting_role=acting_role,
            permission_grant=grant,
            recorded_by_principal=actor,
        )
        task.refresh_from_db()
        return event

    def _check(self, task: Task, kind: str, result: str, *, evidence: dict | None = None):
        key = "source_ready" if kind == TaskCheckRun.Kind.DOR else "deliverable_complete"
        principal = self.owner if kind == TaskCheckRun.Kind.DOR else self.operator
        role = ActingRole.OWNER if kind == TaskCheckRun.Kind.DOR else ActingRole.OPERATOR
        grant = self.owner_edit if kind == TaskCheckRun.Kind.DOR else self.operator_edit
        return TaskCheckRun.record_completed(
            task=task,
            check_kind=kind,
            results=[
                {
                    "criterion_key": key,
                    "result": result,
                    "evidence": evidence or {"fixture": key},
                }
            ],
            command_id=uuid.uuid4(),
            evaluator_principal=principal,
            acting_role=role,
            permission_grant=grant,
            recorded_by_principal=principal,
        )

    def _enter_work(self, task: Task) -> None:
        self._check(task, TaskCheckRun.Kind.DOR, TaskCheckRun.Result.PASS)
        self._transition(task, Task.State.READY)
        TaskAssignment.record(
            task=task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.owner_assign,
            recorded_by_principal=self.owner,
        )
        task.refresh_from_db()
        self._transition(task, Task.State.ASSIGNED)
        self._transition(task, Task.State.IN_PROGRESS)

    def _asset_version(self, task: Task, *, asset: ContentAsset | None = None, label: str = "v1"):
        if asset is None:
            asset = ContentAsset.create_idempotent(
                task=task,
                asset_key="primary-copy",
                title="Primary deliverable",
                asset_kind=ContentAsset.AssetKind.COPY,
                command_id=uuid.uuid4(),
                actor_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.operator_edit,
                recorded_by_principal=self.operator,
            )
        body = f"Exact immutable acceptance content {label}".encode()
        version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"https://assets.example.com/acceptance/{task.pk}/primary-{label}.txt",
            mime_type="text/plain",
            byte_size=len(body),
            content_sha256=hashlib.sha256(body).hexdigest(),
            command_id=uuid.uuid4(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )
        return asset, version

    def _submission(self, task: Task, version: ContentAssetVersion, *, supersedes=None, triggering_review=None):
        dod = self._check(
            task,
            TaskCheckRun.Kind.DOD,
            TaskCheckRun.Result.PASS,
            evidence={"asset_version_id": str(version.pk)},
        )
        task.refresh_from_db()
        return TaskSubmission.seal(
            task=task,
            dod_check_run=dod,
            primary_asset_version=version,
            supersedes_submission=supersedes,
            triggering_review=triggering_review,
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.operator_edit,
            recorded_by_principal=self.operator,
        )

    def _enter_review(self, task: Task, submission: TaskSubmission) -> None:
        self._transition(task, Task.State.SUBMITTED)
        self._transition(task, Task.State.UNDER_REVIEW)

    def _review(self, task: Task, submission: TaskSubmission, decision: str):
        task.refresh_from_db()
        return ReviewDecision.record_final(
            submission=submission,
            decision=decision,
            rationale=f"Acceptance decision: {decision}",
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=self.reviewer_review,
            recorded_by_principal=self.reviewer,
        )

    def _approved_submission(self, task: Task):
        self._enter_work(task)
        asset, version = self._asset_version(task)
        submission = self._submission(task, version)
        self._enter_review(task, submission)
        review = self._review(task, submission, ReviewDecision.Decision.APPROVED)
        self._transition(task, Task.State.APPROVED)
        return asset, version, submission, review

    def _publication(self, submission: TaskSubmission) -> Publication:
        # Rehydrate the immutable submission so its related Task reflects the
        # current projection.  The submission instance is intentionally kept
        # immutable and may otherwise retain the IN_PROGRESS task object that
        # was cached when it was sealed.
        submission = TaskSubmission.objects.select_related("task").get(pk=submission.pk)
        return Publication.objects.create_intent(
            submission=submission,
            command_id=uuid.uuid4(),
            payload_hash=unique_digest("publication-intent"),
            actor_principal=self.publisher,
            acting_role=ReleaseActingRole.OPERATOR,
            permission_grant=self.publisher_publish,
            recorded_by_principal=self.publisher,
        )

    def _gate(
        self,
        publication: Publication,
        submission: TaskSubmission,
        review: ReviewDecision,
        version: ContentAssetVersion,
        capability: CapabilityState,
        *,
        rule_result: str = RuleEvaluationResult.Result.PASS,
    ):
        policies, has_contract_snapshot = required_policy_versions_for_contract(self.contract)
        self.assertTrue(has_contract_snapshot)
        context_hash = release_context_hash(
            publication=publication,
            review_decision=review,
            primary_asset_version=version,
            task_contract_version=self.contract,
            publisher_principal=self.publisher,
            publisher_grant=self.publisher_publish,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            account_environment_binding=self.binding,
            capability_state=capability,
            policy_versions=policies,
        )
        run = RuleEvaluationRun.objects.start(
            publication=publication,
            policy_versions=policies,
            context_hash=context_hash,
            evaluator_key="acceptance-deterministic-v1",
            command_id=uuid.uuid4(),
            payload_hash=unique_digest("rule-run"),
            initiated_by_principal=self.rule_evaluator,
            acting_role=ReleaseActingRole.SYSTEM,
            permission_grant=self.evaluator_review,
            recorded_by_principal=self.rule_evaluator,
        )
        for policy in policies:
            for rule in policy.normalized_rules():
                RuleEvaluationResult.objects.record(
                    evaluation_run=run,
                    policy_version=policy,
                    rule_code=rule["rule_code"],
                    required=rule["required"],
                    result=rule_result,
                    command_id=uuid.uuid4(),
                    payload_hash=unique_digest(f"rule-result-{rule['rule_code']}"),
                    evaluated_by_principal=self.rule_evaluator,
                    recorded_by_principal=self.rule_evaluator,
                )
        run.complete()
        gate = ReleaseGateRecord.objects.evaluate(
            publication=publication,
            task_submission=submission,
            review_decision=review,
            primary_asset_version=version,
            task_contract_version=self.contract,
            publisher_principal=self.publisher,
            publisher_grant=self.publisher_publish,
            channel_account=self.channel_account,
            runtime_environment=self.environment,
            account_environment_binding=self.binding,
            capability_state=capability,
            evaluation_run=run,
            command_id=uuid.uuid4(),
            payload_hash=unique_digest("release-gate"),
            evaluated_by_principal=self.rule_evaluator,
            evaluated_by_acting_role=ReleaseActingRole.SYSTEM,
            recorded_by_principal=self.rule_evaluator,
        )
        return run, gate

    def _publication_event(self, publication, event_type, expected_version, gate, **proof):
        event = publication.append_event(
            event_type=event_type,
            release_gate=gate,
            command_id=uuid.uuid4(),
            payload_hash=unique_digest(event_type),
            expected_state_version=expected_version,
            actor_principal=self.publisher,
            acting_role=ReleaseActingRole.OPERATOR,
            permission_grant=self.publisher_publish,
            recorded_by_principal=self.publisher,
            **proof,
        )
        publication.refresh_from_db()
        return event

    def test_ac01_normal_manual_publication_has_one_traceable_chain(self):
        task = self._task("AC-01 normal")
        _asset, version, submission, review = self._approved_submission(task)
        publication = self._publication(submission)
        run, gate = self._gate(publication, submission, review, version, self.open_capability)
        self.assertEqual(gate.outcome, ReleaseGateRecord.Outcome.PASSED)
        self.assertEqual(gate.evaluation_links.count(), run.actual_result_count)

        ready = self._publication_event(
            publication,
            PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH,
            1,
            gate,
        )
        published = self._publication_event(
            publication,
            PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
            2,
            gate,
            external_publication_id="tiktok-ac01",
            external_url="https://www.tiktok.com/@puko/video/ac01",
        )
        self._transition(task, Task.State.DONE)

        self.assertEqual(task.current_state, Task.State.DONE)
        self.assertEqual(
            list(task.state_events.values_list("to_state", flat=True)),
            [
                Task.State.READY,
                Task.State.ASSIGNED,
                Task.State.IN_PROGRESS,
                Task.State.SUBMITTED,
                Task.State.UNDER_REVIEW,
                Task.State.APPROVED,
                Task.State.DONE,
            ],
        )
        self.assertEqual(submission.primary_asset_version_id, version.pk)
        self.assertEqual(review.submission_id, submission.pk)
        self.assertEqual(gate.task_submission_id, submission.pk)
        self.assertEqual(gate.primary_asset_version_id, version.pk)
        self.assertEqual(gate.task_contract_version.product_profile_version_id, self.profile.pk)
        self.assertEqual(self.profile.product_id, self.product.pk)
        self.assertEqual(published.previous_event_id, ready.pk)
        self.assertEqual(publication.events.count(), 3)

    def test_ac02_dor_blocked_requires_new_run_and_recorded_recovery(self):
        task = self._task("AC-02 blocked DoR")
        failed = self._check(
            task,
            TaskCheckRun.Kind.DOR,
            TaskCheckRun.Result.BLOCKED,
            evidence={"missing": "source"},
        )
        with self.assertRaises(CheckGateRejected):
            self._transition(task, Task.State.READY)
        self.assertEqual(task.state_events.count(), 0)
        self._transition(task, Task.State.BLOCKED)
        self._transition(task, Task.State.DRAFT)

        self.assertEqual(task.check_runs.count(), 1)
        self.assertFalse(task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).exists())
        self.assertFalse(task.submissions.exists())
        self.assertEqual(ReleaseGateRecord.objects.count(), 0)
        self.assertEqual(Publication.objects.count(), 0)

        passed = self._check(
            task,
            TaskCheckRun.Kind.DOR,
            TaskCheckRun.Result.PASS,
            evidence={"source": "provided"},
        )
        self._transition(task, Task.State.READY)
        self.assertEqual((failed.attempt_number, failed.aggregate_result), (1, TaskCheckRun.Result.BLOCKED))
        self.assertEqual((passed.attempt_number, passed.aggregate_result), (2, TaskCheckRun.Result.PASS))
        self.assertEqual(
            list(task.state_events.values_list("to_state", flat=True)),
            [Task.State.BLOCKED, Task.State.DRAFT, Task.State.READY],
        )

    def test_ac03_rework_creates_new_facts_and_old_chain_cannot_gate(self):
        task = self._task("AC-03 human rework")
        self._enter_work(task)
        asset, version1 = self._asset_version(task, label="v1")
        submission1 = self._submission(task, version1)
        self._enter_review(task, submission1)
        review1 = self._review(task, submission1, ReviewDecision.Decision.CHANGES_REQUESTED)
        self._transition(task, Task.State.HUMAN_REWORK)
        self._transition(task, Task.State.IN_PROGRESS)

        version1.object_key = "mutated.txt"
        with self.assertRaises(ValidationError):
            version1.save()
        review1.rationale = "rewritten"
        with self.assertRaises(ValidationError):
            review1.save()

        _asset, version2 = self._asset_version(task, asset=asset, label="v2")
        submission2 = self._submission(
            task,
            version2,
            supersedes=submission1,
            triggering_review=review1,
        )
        self._enter_review(task, submission2)
        review2 = self._review(task, submission2, ReviewDecision.Decision.APPROVED)
        self._transition(task, Task.State.APPROVED)

        self.assertEqual(version2.version_number, version1.version_number + 1)
        self.assertEqual(submission2.submission_number, 2)
        self.assertEqual(submission2.supersedes_submission_id, submission1.pk)
        self.assertEqual(submission2.triggering_review_id, review1.pk)
        self.assertEqual(task.check_runs.filter(check_kind=TaskCheckRun.Kind.DOD).count(), 2)
        self.assertEqual(task.submissions.count(), 2)

        with self.assertRaises((CheckGateRejected, ValidationError)):
            self._publication(submission1)
        self.assertFalse(Publication.objects.filter(submission=submission1).exists())
        self.assertEqual(review2.submission_id, submission2.pk)

    def test_ac04_blocked_gate_is_immutable_and_recovery_requires_new_gate(self):
        task = self._task("AC-04 stale gate")
        _asset, version, submission, review = self._approved_submission(task)
        publication = self._publication(submission)
        closed = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=2,
            state=CapabilityState.State.CLOSED,
            reason="Acceptance emergency stop",
            supersedes=self.open_capability,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        _blocked_run, blocked_gate = self._gate(publication, submission, review, version, closed)
        self.assertEqual(blocked_gate.outcome, ReleaseGateRecord.Outcome.BLOCKED)
        self.assertIn("MANUAL_PUBLISH_CAPABILITY_NOT_OPEN", blocked_gate.failure_reasons)
        self._publication_event(
            publication,
            PublicationEvent.EventType.GATE_BLOCKED,
            1,
            blocked_gate,
        )
        with self.assertRaises(ValidationError):
            self._publication_event(
                publication,
                PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH,
                2,
                blocked_gate,
            )
        self.assertFalse(
            publication.events.filter(event_type=PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH).exists()
        )

        restored = CapabilityState.objects.create(
            account_environment_binding=self.binding,
            capability_code=CapabilityState.MANUAL_PUBLISH,
            state_version=3,
            state=CapabilityState.State.OPEN,
            supersedes=closed,
            created_by_principal=self.owner,
            recorded_by_principal=self.owner,
        )
        self._publication_event(
            publication,
            PublicationEvent.EventType.GATE_PENDING,
            2,
            blocked_gate,
        )
        _new_run, new_gate = self._gate(publication, submission, review, version, restored)
        self.assertEqual(new_gate.outcome, ReleaseGateRecord.Outcome.PASSED)
        self.assertNotEqual(new_gate.pk, blocked_gate.pk)
        self.assertNotEqual(new_gate.context_sha256, blocked_gate.context_sha256)
        with self.assertRaises(ValidationError):
            self._publication_event(
                publication,
                PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH,
                3,
                blocked_gate,
            )
        self._publication_event(
            publication,
            PublicationEvent.EventType.READY_FOR_MANUAL_PUBLISH,
            3,
            new_gate,
        )
        self._publication_event(
            publication,
            PublicationEvent.EventType.MANUAL_PUBLISHED_RECORDED,
            4,
            new_gate,
            external_publication_id="tiktok-ac04",
        )
        self.assertEqual(publication.gate_records.count(), 2)
        self.assertEqual(publication.current_gate_id, new_gate.pk)

    def test_ac05_replay_authorization_and_optimistic_conflict_leave_no_partial_facts(self):
        task = self._task("AC-05 command safety")
        self._check(task, TaskCheckRun.Kind.DOR, TaskCheckRun.Result.PASS)
        command_id = uuid.uuid4()
        first = self._transition(task, Task.State.READY, command_id=command_id, expected_version=0)
        replay = self._transition(task, Task.State.READY, command_id=command_id, expected_version=0)
        self.assertEqual(first.pk, replay.pk)
        self.assertEqual(task.state_events.count(), 1)
        with self.assertRaises(CommandReplayConflict):
            self._transition(task, Task.State.CANCELLED, command_id=command_id, expected_version=0)
        self.assertEqual(task.state_events.count(), 1)

        TaskAssignment.record(
            task=task,
            assignee_principal=self.operator,
            command_id=uuid.uuid4(),
            expected_task_version=task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.owner_assign,
            recorded_by_principal=self.owner,
        )
        task.refresh_from_db()
        self._transition(task, Task.State.ASSIGNED)
        self._transition(task, Task.State.IN_PROGRESS)
        _asset, version = self._asset_version(task)
        submission = self._submission(task, version)
        self._enter_review(task, submission)

        unauthorized_command = uuid.uuid4()
        with self.assertRaises(ValidationError):
            ReviewDecision.record_final(
                submission=submission,
                decision=ReviewDecision.Decision.APPROVED,
                rationale="Operator attempted self approval",
                command_id=unauthorized_command,
                expected_task_version=task.state_version,
                reviewer_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.operator_edit,
                recorded_by_principal=self.operator,
            )
        self.assertFalse(ReviewDecision.objects.filter(command_id=unauthorized_command).exists())
        review = self._review(task, submission, ReviewDecision.Decision.APPROVED)
        self.assertEqual(review.submission_id, submission.pk)

        stale_version = task.state_version
        winner = self._transition(task, Task.State.APPROVED, expected_version=stale_version)
        with self.assertRaises(OptimisticConcurrencyConflict):
            self._transition(task, Task.State.APPROVED, expected_version=stale_version)
        task.refresh_from_db()
        self.assertEqual(task.current_state, Task.State.APPROVED)
        self.assertEqual(
            task.state_events.filter(resulting_state_version=winner.resulting_state_version).count(),
            1,
        )
        self.assertEqual(task.state_events.count(), task.state_version)
        self.assertEqual(ReviewDecision.objects.filter(submission=submission).count(), 1)
