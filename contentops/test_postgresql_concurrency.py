from __future__ import annotations

import hashlib
import threading
from datetime import timedelta
from queue import Queue
from typing import Callable
from unittest import skipUnless

from django.core.exceptions import ValidationError
from django.db import IntegrityError, close_old_connections, connection, connections, models, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from accounts.services import revoke_permission_grant
from contentops.models import (
    ActingRole,
    ContentAsset,
    ContentAssetVersion,
    ReviewDecision,
    TaskSubmission,
    canonical_sha256,
)
from core.ids import uuid7
from products.models import Product, ProductProfileVersion
from workflow.models import Task, TaskAssignment, TaskCheckRun, TaskContractVersion, TaskStateEvent


@skipUnless(
    connection.vendor == "postgresql",
    "PostgreSQL-only: requires real row locks, independent connections, and PostgreSQL triggers.",
)
class PostgreSQLConcurrencyAcceptanceTests(TransactionTestCase):
    """Acceptance proof for V1 concurrency boundaries on a real PostgreSQL database.

    TransactionTestCase is intentional: TestCase's enclosing transaction would hide
    fixture rows from the independent connections opened by worker threads.
    """

    databases = {"default"}

    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="pg-concurrency-owner",
            password="not-a-real-secret",
            role=Principal.Role.OWNER,
        )
        self.operator = Principal.objects.create_user(
            username="pg-concurrency-operator",
            password="not-a-real-secret",
            role=Principal.Role.OPERATOR,
        )
        self.replacement_operator = Principal.objects.create_user(
            username="pg-concurrency-replacement-operator",
            password="not-a-real-secret",
            role=Principal.Role.OPERATOR,
        )
        self.reviewer_a = Principal.objects.create_user(
            username="pg-concurrency-reviewer-a",
            password="not-a-real-secret",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.reviewer_b = Principal.objects.create_user(
            username="pg-concurrency-reviewer-b",
            password="not-a-real-secret",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.recorder = Principal.objects.create_user(
            username="pg-concurrency-recorder",
            password="not-a-real-secret",
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
        )
        self.product = Product.objects.create(
            product_code="PUKO_PG_CONCURRENCY",
            name="PUKO PostgreSQL Concurrency Fixture",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        grant_window = {
            "scope_kind": PermissionGrant.ScopeKind.PRODUCT,
            "product": self.product,
            "valid_from": now - timedelta(minutes=1),
            "valid_until": now + timedelta(hours=1),
            "granted_by_principal": self.owner,
        }
        self.edit_grant = PermissionGrant.objects.create(
            principal=self.operator,
            action=PermissionGrant.Action.EDIT,
            **grant_window,
        )
        self.cancel_grant = PermissionGrant.objects.create(
            principal=self.operator,
            action=PermissionGrant.Action.CANCEL_TASK,
            **grant_window,
        )
        self.assign_grant = PermissionGrant.objects.create(
            principal=self.operator,
            action=PermissionGrant.Action.ASSIGN_TASK,
            **grant_window,
        )
        self.owner_assign_grant = PermissionGrant.objects.create(
            principal=self.owner,
            action=PermissionGrant.Action.ASSIGN_TASK,
            **grant_window,
        )
        self.owner_manage_account_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.GLOBAL,
            action=PermissionGrant.Action.MANAGE_ACCOUNT,
            valid_from=grant_window["valid_from"],
            valid_until=grant_window["valid_until"],
            granted_by_principal=self.owner,
        )
        self.replacement_edit_grant = PermissionGrant.objects.create(
            principal=self.replacement_operator,
            action=PermissionGrant.Action.EDIT,
            **grant_window,
        )
        self.operator_review_grant = PermissionGrant.objects.create(
            principal=self.operator,
            action=PermissionGrant.Action.REVIEW,
            **grant_window,
        )
        self.review_grant_a = PermissionGrant.objects.create(
            principal=self.reviewer_a,
            action=PermissionGrant.Action.REVIEW,
            **grant_window,
        )
        self.review_grant_b = PermissionGrant.objects.create(
            principal=self.reviewer_b,
            action=PermissionGrant.Action.REVIEW,
            **grant_window,
        )

        self.profile = ProductProfileVersion.objects.create(
            product=self.product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"primary": "US wellness consumers"},
            core_value_proposition="Evidence-informed daily wellness products.",
            brand_voice={"tone": ["clear", "measured"]},
            product_facts={"business_mode": "B2C"},
            prohibited_expressions=["cure"],
            created_by_principal=self.owner,
        )
        self.profile.seal(self.owner)
        self.contract = TaskContractVersion.objects.create(
            product_profile_version=self.profile,
            version_number=1,
            title="PostgreSQL concurrency contract",
            dor_criteria=[{"key": "input", "required": True}],
            dod_criteria=[
                {"key": "primary-deliverable", "required": True},
                {"key": "claims-check", "required": True},
            ],
            release_gate_criteria=[{"key": "human-review", "required": True}],
            success_criteria=[{"key": "manual-proof", "required": True}],
            sealed_at=timezone.now(),
            created_by_principal=self.owner,
        )
        self.task = Task.objects.create(
            product=self.product,
            product_profile_version=self.profile,
            contract_version=self.contract,
            title="PostgreSQL concurrency acceptance task",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        TaskCheckRun.record_completed(
            task=self.task,
            check_kind=TaskCheckRun.Kind.DOR,
            results=[{"criterion_key": "input", "result": "PASS", "evidence": {"ready": True}}],
            command_id=uuid7(),
            evaluator_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self._transition(Task.State.READY, self.edit_grant)
        TaskAssignment.record(
            task=self.task,
            assignee_principal=self.operator,
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.owner_assign_grant,
            recorded_by_principal=self.recorder,
        )
        self._transition(Task.State.ASSIGNED, self.assign_grant)
        self._transition(Task.State.IN_PROGRESS, self.edit_grant)

        asset = ContentAsset.create_idempotent(
            task=self.task,
            asset_key="primary",
            title="Primary deliverable",
            asset_kind=ContentAsset.AssetKind.VIDEO,
            command_id=uuid7(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        payload = b"postgresql concurrency acceptance payload"
        asset_version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"https://drafts.example.com/acceptance/tasks/{self.task.pk}/primary-v1.mp4",
            mime_type="video/mp4",
            byte_size=len(payload),
            content_sha256=hashlib.sha256(payload).hexdigest(),
            metadata={"source": "postgresql-concurrency-test"},
            command_id=uuid7(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        dod = TaskCheckRun.record_completed(
            task=self.task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[
                {"criterion_key": "primary-deliverable", "result": "PASS", "evidence": {"exact": True}},
                {"criterion_key": "claims-check", "result": "PASS", "evidence": {"checked": True}},
            ],
            command_id=uuid7(),
            evaluator_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self.submission = TaskSubmission.seal(
            task=self.task,
            dod_check_run=dod,
            primary_asset_version=asset_version,
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self._transition(Task.State.SUBMITTED, self.edit_grant)
        self._transition(Task.State.UNDER_REVIEW, self.edit_grant)

    def _transition(self, to_state: str, grant: PermissionGrant) -> None:
        Task.transition(
            task_id=self.task.pk,
            to_state=to_state,
            command_id=uuid7(),
            expected_state_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=grant,
            recorded_by_principal=self.recorder,
        )
        self.task.refresh_from_db()

    def _create_assignment_race_fixture(self, suffix: str) -> dict[str, object]:
        """Create an IN_PROGRESS task with a sealed-ready deliverable but no submission."""

        task = Task.objects.create(
            product=self.product,
            product_profile_version=self.profile,
            contract_version=self.contract,
            title=f"PostgreSQL assignment race {suffix}",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        dor = TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOR,
            results=[{"criterion_key": "input", "result": "PASS", "evidence": {"ready": True}}],
            command_id=uuid7(),
            evaluator_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self.assertEqual(dor.aggregate_result, TaskCheckRun.Result.PASS)

        def transition(to_state: str, grant: PermissionGrant) -> None:
            nonlocal task
            Task.transition(
                task_id=task.pk,
                to_state=to_state,
                command_id=uuid7(),
                expected_state_version=task.state_version,
                actor_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=grant,
                recorded_by_principal=self.recorder,
            )
            task.refresh_from_db()

        transition(Task.State.READY, self.edit_grant)
        assignment = TaskAssignment.record(
            task=task,
            assignee_principal=self.operator,
            command_id=uuid7(),
            expected_task_version=task.state_version,
            assigned_by_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=self.owner_assign_grant,
            recorded_by_principal=self.recorder,
        )
        transition(Task.State.ASSIGNED, self.assign_grant)
        transition(Task.State.IN_PROGRESS, self.edit_grant)

        asset = ContentAsset.create_idempotent(
            task=task,
            asset_key=f"assignment-race-{suffix}",
            title=f"Assignment race deliverable {suffix}",
            asset_kind=ContentAsset.AssetKind.VIDEO,
            command_id=uuid7(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        payload = f"postgresql assignment race {suffix}".encode()
        asset_version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"https://drafts.example.com/assignment-races/{task.pk}/v1.mp4",
            mime_type="video/mp4",
            byte_size=len(payload),
            content_sha256=hashlib.sha256(payload).hexdigest(),
            metadata={"source": "postgresql-assignment-race"},
            command_id=uuid7(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        dod = TaskCheckRun.record_completed(
            task=task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[
                {"criterion_key": "primary-deliverable", "result": "PASS", "evidence": {"exact": True}},
                {"criterion_key": "claims-check", "result": "PASS", "evidence": {"checked": True}},
            ],
            command_id=uuid7(),
            evaluator_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        return {
            "task": task,
            "assignment": assignment,
            "asset_version": asset_version,
            "dod": dod,
        }

    @staticmethod
    def _postgres_backend_pid(alias: str = "default") -> int:
        with connections[alias].cursor() as cursor:
            cursor.execute("SELECT pg_backend_pid()")
            return cursor.fetchone()[0]

    def _run_two_connection_race(
        self,
        actions: dict[str, Callable[[], object]],
    ) -> dict[str, tuple[bool, object, int]]:
        barrier = threading.Barrier(len(actions))
        outcomes: Queue[tuple[str, bool, object, int]] = Queue()

        def worker(label: str, action) -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    backend_pid = self._postgres_backend_pid()
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                        cursor.execute("SET LOCAL statement_timeout = '10s'")
                    barrier.wait(timeout=10)
                    result = action()
                outcomes.put((label, True, result, backend_pid))
            except BaseException as error:  # Captured and asserted in the test thread.
                outcomes.put((label, False, error, locals().get("backend_pid", -1)))
            finally:
                connections["default"].close()

        threads = [
            threading.Thread(target=worker, args=(label, action), daemon=True)
            for label, action in actions.items()
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        alive = [thread.name for thread in threads if thread.is_alive()]
        if alive:
            barrier.abort()
            self.fail(f"Concurrent PostgreSQL workers did not finish: {alive}")

        collected = [outcomes.get_nowait() for _ in threads]
        results = {label: (succeeded, value, backend_pid) for label, succeeded, value, backend_pid in collected}
        self.assertEqual(set(results), set(actions))
        self.assertEqual(len({item[2] for item in results.values()}), len(actions))
        return results

    def _record_review(self, reviewer_id, grant_id, *, rationale: str) -> str:
        submission = TaskSubmission.objects.select_related("task").get(pk=self.submission.pk)
        reviewer = Principal.objects.get(pk=reviewer_id)
        grant = PermissionGrant.objects.select_related("principal").get(pk=grant_id)
        recorder = Principal.objects.get(pk=self.recorder.pk)
        review = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale=rationale,
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=grant,
            recorded_by_principal=recorder,
        )
        return str(review.pk)

    def test_select_for_update_blocks_a_second_real_connection(self):
        start = threading.Barrier(2)
        holder_locked = threading.Event()
        release_holder = threading.Event()
        contender_attempting = threading.Event()
        contender_acquired = threading.Event()
        outcomes: Queue[tuple[str, object]] = Queue()

        def holder() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    pid = self._postgres_backend_pid()
                    start.wait(timeout=10)
                    Task.objects.select_for_update().get(pk=self.task.pk)
                    holder_locked.set()
                    if not release_holder.wait(timeout=10):
                        raise TimeoutError("Test did not release the PostgreSQL row lock.")
                outcomes.put(("holder", pid))
            except BaseException as error:
                outcomes.put(("holder-error", error))
            finally:
                connections["default"].close()

        def contender() -> None:
            close_old_connections()
            try:
                with transaction.atomic():
                    pid = self._postgres_backend_pid()
                    with connections["default"].cursor() as cursor:
                        cursor.execute("SET LOCAL lock_timeout = '5s'")
                    start.wait(timeout=10)
                    if not holder_locked.wait(timeout=10):
                        raise TimeoutError("Lock holder never acquired the Task row.")
                    contender_attempting.set()
                    Task.objects.select_for_update().get(pk=self.task.pk)
                    contender_acquired.set()
                outcomes.put(("contender", pid))
            except BaseException as error:
                outcomes.put(("contender-error", error))
            finally:
                connections["default"].close()

        threads = [
            threading.Thread(target=holder, daemon=True),
            threading.Thread(target=contender, daemon=True),
        ]
        for thread in threads:
            thread.start()
        try:
            self.assertTrue(holder_locked.wait(timeout=10), "Holder did not acquire the Task row lock.")
            self.assertTrue(
                contender_attempting.wait(timeout=10),
                "The second PostgreSQL connection never attempted to lock the Task row.",
            )
            self.assertFalse(
                contender_acquired.wait(timeout=0.4),
                "The second PostgreSQL connection acquired a row that should still be locked.",
            )
        finally:
            release_holder.set()
        for thread in threads:
            thread.join(timeout=15)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        results = dict(outcomes.get_nowait() for _ in threads)
        self.assertEqual(set(results), {"holder", "contender"}, results)
        self.assertNotEqual(results["holder"], results["contender"])
        self.assertTrue(contender_acquired.is_set())

    def test_withdrawal_and_review_are_serialized_to_exactly_one_winner(self):
        expected_version = self.task.state_version

        def withdraw() -> str:
            actor = Principal.objects.get(pk=self.operator.pk)
            grant = PermissionGrant.objects.select_related("principal").get(pk=self.edit_grant.pk)
            recorder = Principal.objects.get(pk=self.recorder.pk)
            event = Task.withdraw_submission(
                task_id=self.task.pk,
                submission_id=self.submission.pk,
                command_id=uuid7(),
                expected_state_version=expected_version,
                actor_principal=actor,
                acting_role=ActingRole.OPERATOR,
                permission_grant=grant,
                recorded_by_principal=recorder,
                reason="PostgreSQL race acceptance.",
            )
            return str(event.pk)

        results = self._run_two_connection_race(
            {
                "withdraw": withdraw,
                "review": lambda: self._record_review(
                    self.reviewer_a.pk,
                    self.review_grant_a.pk,
                    rationale="PostgreSQL race acceptance.",
                ),
            }
        )
        winners = [label for label, (succeeded, _value, _pid) in results.items() if succeeded]
        losers = [value for succeeded, value, _pid in results.values() if not succeeded]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIsInstance(losers[0], ValidationError)

        review_count = ReviewDecision.objects.filter(submission=self.submission).count()
        withdrawal_count = TaskStateEvent.objects.filter(
            event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
            submission=self.submission,
        ).count()
        self.assertEqual(review_count + withdrawal_count, 1)
        self.task.refresh_from_db()
        if review_count:
            self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        else:
            self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)

    def test_abandonment_and_review_are_serialized_to_exactly_one_winner(self):
        expected_version = self.task.state_version

        def abandon() -> str:
            actor = Principal.objects.get(pk=self.operator.pk)
            grant = PermissionGrant.objects.select_related("principal").get(
                pk=self.cancel_grant.pk
            )
            recorder = Principal.objects.get(pk=self.recorder.pk)
            event = Task.cancel_task(
                task_id=self.task.pk,
                submission_id=self.submission.pk,
                command_id=uuid7(),
                expected_state_version=expected_version,
                actor_principal=actor,
                acting_role=ActingRole.OPERATOR,
                permission_grant=grant,
                recorded_by_principal=recorder,
                reason="PostgreSQL abandon vs review acceptance.",
            )
            return str(event.pk)

        results = self._run_two_connection_race(
            {
                "abandon": abandon,
                "review": lambda: self._record_review(
                    self.reviewer_a.pk,
                    self.review_grant_a.pk,
                    rationale="PostgreSQL abandon vs review acceptance.",
                ),
            }
        )
        winners = [label for label, (succeeded, _value, _pid) in results.items() if succeeded]
        losers = [value for succeeded, value, _pid in results.values() if not succeeded]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIsInstance(losers[0], ValidationError)

        review_count = ReviewDecision.objects.filter(submission=self.submission).count()
        abandonment_count = TaskStateEvent.objects.filter(
            event_type=TaskStateEvent.EventType.SUBMISSION_ABANDONED,
            submission=self.submission,
        ).count()
        self.assertEqual(review_count + abandonment_count, 1)
        self.task.refresh_from_db()
        if review_count:
            self.assertEqual(self.task.current_state, Task.State.UNDER_REVIEW)
        else:
            self.assertEqual(self.task.current_state, Task.State.CANCELLED)

    def test_reassignment_and_original_assignee_submission_have_exactly_one_winner(self):
        fixture = self._create_assignment_race_fixture("submission")
        task = fixture["task"]
        original_assignment = fixture["assignment"]
        asset_version = fixture["asset_version"]
        dod = fixture["dod"]
        expected_version = task.state_version

        def reassign() -> str:
            persisted_task = Task.objects.get(pk=task.pk)
            replacement = Principal.objects.get(pk=self.replacement_operator.pk)
            owner = Principal.objects.get(pk=self.owner.pk)
            grant = PermissionGrant.objects.get(pk=self.owner_assign_grant.pk)
            recorder = Principal.objects.get(pk=self.recorder.pk)
            assignment = TaskAssignment.record(
                task=persisted_task,
                assignee_principal=replacement,
                command_id=uuid7(),
                expected_task_version=expected_version,
                expected_current_assignment_id=original_assignment.pk,
                assigned_by_principal=owner,
                acting_role=ActingRole.OWNER,
                permission_grant=grant,
                recorded_by_principal=recorder,
            )
            return str(assignment.pk)

        def seal_submission() -> str:
            persisted_task = Task.objects.get(pk=task.pk)
            persisted_dod = TaskCheckRun.objects.get(pk=dod.pk)
            persisted_asset_version = ContentAssetVersion.objects.get(pk=asset_version.pk)
            operator = Principal.objects.get(pk=self.operator.pk)
            grant = PermissionGrant.objects.get(pk=self.edit_grant.pk)
            recorder = Principal.objects.get(pk=self.recorder.pk)
            submission = TaskSubmission.seal(
                task=persisted_task,
                dod_check_run=persisted_dod,
                primary_asset_version=persisted_asset_version,
                command_id=uuid7(),
                expected_task_version=expected_version,
                actor_principal=operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=grant,
                recorded_by_principal=recorder,
            )
            return str(submission.pk)

        results = self._run_two_connection_race(
            {"reassign": reassign, "seal-submission": seal_submission}
        )
        winners = [label for label, (succeeded, _value, _pid) in results.items() if succeeded]
        losers = [value for succeeded, value, _pid in results.values() if not succeeded]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIsInstance(losers[0], ValidationError)

        task.refresh_from_db()
        reassignment = TaskAssignment.objects.filter(task=task, assignment_number=2).first()
        submission = TaskSubmission.objects.filter(task=task).first()
        self.assertEqual(int(reassignment is not None) + int(submission is not None), 1)
        if reassignment is not None:
            self.assertEqual(TaskAssignment.objects.filter(task=task).count(), 2)
            self.assertEqual(reassignment.supersedes_assignment_id, original_assignment.pk)
            self.assertEqual(task.current_assignee_principal_id, self.replacement_operator.pk)
            self.assertIsNone(submission)
        else:
            self.assertEqual(TaskAssignment.objects.filter(task=task).count(), 1)
            self.assertEqual(submission.submitted_by_principal_id, self.operator.pk)
            self.assertEqual(task.current_assignee_principal_id, self.operator.pk)

    def test_reassignment_and_exact_assign_grant_revocation_are_serialized(self):
        fixture = self._create_assignment_race_fixture("grant-revocation")
        task = fixture["task"]
        original_assignment = fixture["assignment"]
        expected_version = task.state_version

        def reassign() -> str:
            persisted_task = Task.objects.get(pk=task.pk)
            replacement = Principal.objects.get(pk=self.replacement_operator.pk)
            owner = Principal.objects.get(pk=self.owner.pk)
            grant = PermissionGrant.objects.get(pk=self.owner_assign_grant.pk)
            recorder = Principal.objects.get(pk=self.recorder.pk)
            assignment = TaskAssignment.record(
                task=persisted_task,
                assignee_principal=replacement,
                command_id=uuid7(),
                expected_task_version=expected_version,
                expected_current_assignment_id=original_assignment.pk,
                assigned_by_principal=owner,
                acting_role=ActingRole.OWNER,
                permission_grant=grant,
                recorded_by_principal=recorder,
            )
            return str(assignment.pk)

        def revoke_assign_grant() -> str:
            owner = Principal.objects.get(pk=self.owner.pk)
            revoked = revoke_permission_grant(
                actor=owner,
                grant_id=self.owner_assign_grant.pk,
                reason="PostgreSQL assignment authority race acceptance.",
            )
            return str(revoked.pk)

        results = self._run_two_connection_race(
            {"reassign": reassign, "revoke-grant": revoke_assign_grant}
        )
        self.assertTrue(results["revoke-grant"][0], results)

        self.owner_assign_grant.refresh_from_db()
        self.assertEqual(
            self.owner_assign_grant.grant_status,
            PermissionGrant.GrantStatus.REVOKED,
        )
        self.assertIsNotNone(self.owner_assign_grant.revoked_at)
        task.refresh_from_db()
        reassignment = TaskAssignment.objects.filter(task=task, assignment_number=2).first()
        if results["reassign"][0]:
            self.assertIsNotNone(reassignment)
            self.assertEqual(reassignment.permission_grant_id, self.owner_assign_grant.pk)
            self.assertLessEqual(reassignment.assigned_at, self.owner_assign_grant.revoked_at)
            self.assertEqual(task.current_assignee_principal_id, self.replacement_operator.pk)
        else:
            self.assertIsInstance(results["reassign"][1], ValidationError)
            self.assertIsNone(reassignment)
            self.assertEqual(TaskAssignment.objects.filter(task=task).count(), 1)
            self.assertEqual(task.current_assignee_principal_id, self.operator.pk)

        self.assertFalse(
            TaskAssignment.objects.filter(
                task=task,
                assignment_number=2,
                assigned_at__gte=self.owner_assign_grant.revoked_at,
            ).exists(),
            "A reassignment must never be recorded under a Grant that was already revoked.",
        )

    def test_two_reviewers_compete_for_one_final_decision(self):
        results = self._run_two_connection_race(
            {
                "reviewer-a": lambda: self._record_review(
                    self.reviewer_a.pk,
                    self.review_grant_a.pk,
                    rationale="Reviewer A PostgreSQL race.",
                ),
                "reviewer-b": lambda: self._record_review(
                    self.reviewer_b.pk,
                    self.review_grant_b.pk,
                    rationale="Reviewer B PostgreSQL race.",
                ),
            }
        )
        winners = [label for label, (succeeded, _value, _pid) in results.items() if succeeded]
        losers = [value for succeeded, value, _pid in results.values() if not succeeded]
        self.assertEqual(len(winners), 1, results)
        self.assertEqual(len(losers), 1, results)
        self.assertIsInstance(losers[0], ValidationError)
        self.assertEqual(ReviewDecision.objects.filter(submission=self.submission).count(), 1)

    def test_postgresql_trigger_rejects_self_review_when_orm_guard_is_bypassed(self):
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*)
                FROM pg_trigger
                WHERE tgname = 'contentops_review_no_self_guard'
                  AND NOT tgisinternal
                """
            )
            self.assertEqual(cursor.fetchone()[0], 1)

        raw = ReviewDecision(
            submission=self.submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Raw PostgreSQL self-review bypass attempt.",
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.operator,
            reviewer_acting_role=ActingRole.OPERATOR,
            reviewer_grant=self.operator_review_grant,
            recorded_by_principal=self.recorder,
            decided_at=timezone.now(),
        )
        raw.payload_hash = canonical_sha256(raw.command_payload())
        raw.decision_sha256 = raw.payload_hash
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                models.Model.save_base(raw, raw=True, force_insert=True, using="default")
        self.assertFalse(ReviewDecision.objects.filter(submission=self.submission).exists())
