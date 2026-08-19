from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from django.apps import apps
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from core.ids import uuid7
from products.models import Product, ProductProfileVersion

from contentops.models import (
    ActingRole,
    ContentAsset,
    ContentAssetVersion,
    ReviewDecision,
    TaskSubmission,
    TaskSubmissionAssetLink,
)


class ContentOpsInvariantTests(TestCase):
    def setUp(self):
        self.owner = Principal.objects.create_user(
            username="owner-contentops", password="not-a-real-secret", role=Principal.Role.OWNER
        )
        self.operator = Principal.objects.create_user(
            username="operator-contentops", password="not-a-real-secret"
        )
        self.reviewer = Principal.objects.create_user(
            username="reviewer-contentops", password="not-a-real-secret",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        self.recorder = Principal.objects.create_user(
            username="recorder-contentops",
            password="not-a-real-secret",
            principal_type=Principal.PrincipalType.SERVICE_ACCOUNT,
        )
        self.product = Product.objects.create(
            product_code="PUKO_CONTENTOPS",
            name="PUKO ContentOps Fixture",
            market_code="US",
            language_code="en",
            created_by_principal=self.owner,
            updated_by_principal=self.owner,
        )
        now = timezone.now()
        self.edit_grant = PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        self.review_grant = PermissionGrant.objects.create(
            principal=self.reviewer,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.REVIEW,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        Task = apps.get_model("workflow", "Task")
        if any(field.name == "product_profile_version" for field in Task._meta.fields):
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
            TaskContractVersion = apps.get_model("workflow", "TaskContractVersion")
            self.contract = TaskContractVersion.objects.create(
                product_profile_version=self.profile,
                version_number=1,
                title="PUKO TikTok contract",
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
            self.contract_version_id = self.contract.pk
            self.task = Task.objects.create(
                product=self.product,
                product_profile_version=self.profile,
                contract_version=self.contract,
                title="Create primary TikTok asset",
                created_by_principal=self.owner,
                updated_by_principal=self.owner,
            )
            TaskCheckRun = apps.get_model("workflow", "TaskCheckRun")
            TaskAssignment = apps.get_model("workflow", "TaskAssignment")
            TaskCheckRun.record_completed(
                task=self.task,
                check_kind=TaskCheckRun.Kind.DOR,
                results=[{"criterion_key": "input", "result": "PASS", "evidence": {"ready": True}}],
                command_id=uuid7(), evaluator_principal=self.operator, acting_role="OPERATOR",
                permission_grant=self.edit_grant, recorded_by_principal=self.recorder,
            )
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.READY, command_id=uuid7(),
                expected_state_version=0, actor_principal=self.operator, acting_role="OPERATOR",
                permission_grant=self.edit_grant, recorded_by_principal=self.recorder,
            )
            self.task.refresh_from_db()
            TaskAssignment.record(
                task=self.task, assignee_principal=self.operator, command_id=uuid7(),
                expected_task_version=1, assigned_by_principal=self.operator, acting_role="OPERATOR",
                permission_grant=self.edit_grant, recorded_by_principal=self.recorder,
            )
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid7(),
                expected_state_version=1, actor_principal=self.operator, acting_role="OPERATOR",
                permission_grant=self.edit_grant, recorded_by_principal=self.recorder,
            )
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.IN_PROGRESS, command_id=uuid7(),
                expected_state_version=2, actor_principal=self.operator, acting_role="OPERATOR",
                permission_grant=self.edit_grant, recorded_by_principal=self.recorder,
            )
            self.task.refresh_from_db()
        else:
            self.contract_version_id = uuid7()
            self.task = Task.objects.create(
                product=self.product,
                contract_version_id=self.contract_version_id,
                state_version=3,
            )

    def _dod(self, attempt: int, *, result: str = "PASS", actual: int = 2):
        TaskCheckRun = apps.get_model("workflow", "TaskCheckRun")
        if hasattr(TaskCheckRun, "record_completed"):
            results = [
                {
                    "criterion_key": "primary-deliverable",
                    "result": result,
                    "evidence": {"exact": True},
                },
                {
                    "criterion_key": "claims-check",
                    "result": "PASS",
                    "evidence": {"checked": True},
                },
            ][:actual]
            return TaskCheckRun.record_completed(
                task=self.task,
                check_kind=TaskCheckRun.Kind.DOD,
                results=results,
                command_id=uuid7(),
                evaluator_principal=self.operator,
                acting_role="OPERATOR",
                permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
        return TaskCheckRun.objects.create(
            task=self.task,
            contract_version_id=self.contract_version_id,
            check_kind="DOD",
            status="COMPLETED",
            aggregate_result=result,
            expected_criterion_count=2,
            actual_criterion_count=actual,
        )

    def _asset(self) -> ContentAsset:
        return ContentAsset.create_idempotent(
            task=self.task,
            asset_key="tiktok-main",
            title="TikTok primary video",
            asset_kind=ContentAsset.AssetKind.VIDEO,
            command_id=uuid7(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )

    def _version(self, asset: ContentAsset, number: int) -> ContentAssetVersion:
        content = f"immutable content v{number}".encode()
        return ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"puko/tasks/{self.task.pk}/primary-v{number}.mp4",
            mime_type="video/mp4",
            byte_size=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            metadata={"source": "test"},
            command_id=uuid7(),
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )

    def _submission(
        self,
        *,
        version: ContentAssetVersion,
        dod,
        supersedes: TaskSubmission | None = None,
        review: ReviewDecision | None = None,
    ) -> TaskSubmission:
        return TaskSubmission.seal(
            task=self.task,
            dod_check_run=dod,
            primary_asset_version=version,
            supersedes_submission=supersedes,
            triggering_review=review,
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )

    def _review(self, submission: TaskSubmission, decision: str) -> ReviewDecision:
        Task = apps.get_model("workflow", "Task")
        if self.task.current_state == Task.State.IN_PROGRESS:
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.SUBMITTED, command_id=uuid7(),
                expected_state_version=self.task.state_version, actor_principal=self.operator,
                acting_role="OPERATOR", permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
            self.task.refresh_from_db()
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.UNDER_REVIEW, command_id=uuid7(),
                expected_state_version=self.task.state_version, actor_principal=self.operator,
                acting_role="OPERATOR", permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
            self.task.refresh_from_db()
        return ReviewDecision.record_final(
            submission=submission,
            decision=decision,
            rationale="Human-reviewed exact sealed submission.",
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=self.review_grant,
            recorded_by_principal=self.recorder,
        )

    def test_uuidv7_and_asset_version_are_append_only_and_idempotent(self):
        asset = self._asset()
        command_id = uuid7()
        content = b"v1"
        kwargs = {
            "content_asset": asset,
            "object_key": "puko/v1.mp4",
            "mime_type": "video/mp4",
            "byte_size": len(content),
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "command_id": command_id,
            "actor_principal": self.operator,
            "acting_role": ActingRole.OPERATOR,
            "permission_grant": self.edit_grant,
            "recorded_by_principal": self.recorder,
        }
        version = ContentAssetVersion.create_next(**kwargs)
        replay = ContentAssetVersion.create_next(**kwargs)
        self.assertEqual(replay.pk, version.pk)
        self.assertEqual(version.id.version, 7)
        self.assertEqual(ContentAssetVersion.objects.count(), 1)
        with self.assertRaises(ValidationError):
            ContentAssetVersion.create_next(**{**kwargs, "object_key": "puko/different.mp4"})
        version.object_key = "mutated.mp4"
        with self.assertRaises(ValidationError):
            version.save()
        with self.assertRaises(ValidationError):
            ContentAssetVersion.objects.filter(pk=version.pk).update(object_key="mutated.mp4")
        with self.assertRaises(ValidationError):
            ContentAssetVersion.objects.filter(pk=version.pk).delete()

    def test_submission_has_one_exact_primary_and_rejects_incomplete_dod(self):
        asset = self._asset()
        version = self._version(asset, 1)
        submission = self._submission(version=version, dod=self._dod(1))
        self.assertEqual(submission.primary_asset_version, version)
        self.assertEqual(submission.id.version, 7)
        self.assertEqual(submission.asset_links.count(), 1)
        self.assertEqual(submission.asset_links.get().role, TaskSubmissionAssetLink.Role.PRIMARY)
        self.assertEqual(
            [item["role"] for item in submission.asset_manifest],
            [TaskSubmissionAssetLink.Role.PRIMARY],
        )
        submission.submission_note = "attempted edit"
        with self.assertRaises(ValidationError):
            submission.save()

        incomplete_run = self._dod(2, actual=1)
        with self.assertRaises(ValidationError):
            self._submission(version=version, dod=incomplete_run)
        self.assertEqual(TaskSubmission.objects.count(), 1)

    def test_only_one_immutable_final_review_and_unauthorized_review_writes_nothing(self):
        asset = self._asset()
        submission = self._submission(version=self._version(asset, 1), dod=self._dod(1))
        Task = apps.get_model("workflow", "Task")
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.SUBMITTED, command_id=uuid7(),
            expected_state_version=self.task.state_version, actor_principal=self.operator,
            acting_role="OPERATOR", permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self.task.refresh_from_db()
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.UNDER_REVIEW, command_id=uuid7(),
            expected_state_version=self.task.state_version, actor_principal=self.operator,
            acting_role="OPERATOR", permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self.task.refresh_from_db()
        unauthorized_command = uuid7()
        with self.assertRaises(ValidationError):
            ReviewDecision.record_final(
                submission=submission,
                decision=ReviewDecision.Decision.APPROVED,
                rationale="Not authorized.",
                command_id=unauthorized_command,
                expected_task_version=self.task.state_version,
                reviewer_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
        self.assertFalse(ReviewDecision.objects.filter(command_id=unauthorized_command).exists())

        review = self._review(submission, ReviewDecision.Decision.CHANGES_REQUESTED)
        self.assertEqual(review.id.version, 7)
        with self.assertRaises(ValidationError):
            self._review(submission, ReviewDecision.Decision.APPROVED)
        review.rationale = "attempted rewrite"
        with self.assertRaises(ValidationError):
            review.save()
        with self.assertRaises(ValidationError):
            ReviewDecision.objects.filter(pk=review.pk).update(decision=ReviewDecision.Decision.APPROVED)
        self.assertEqual(ReviewDecision.objects.filter(submission=submission).count(), 1)

    def test_rework_requires_new_version_new_dod_and_new_submission(self):
        asset = self._asset()
        v1 = self._version(asset, 1)
        submission1 = self._submission(version=v1, dod=self._dod(1))
        review1 = self._review(submission1, ReviewDecision.Decision.CHANGES_REQUESTED)

        Task = apps.get_model("workflow", "Task")
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.HUMAN_REWORK, command_id=uuid7(),
            expected_state_version=self.task.state_version, actor_principal=self.operator,
            acting_role="OPERATOR", permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self.task.refresh_from_db()
        Task.transition(
            task_id=self.task.pk, to_state=Task.State.IN_PROGRESS, command_id=uuid7(),
            expected_state_version=self.task.state_version, actor_principal=self.operator,
            acting_role="OPERATOR", permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )
        self.task.refresh_from_db()

        v2 = self._version(asset, 2)
        dod2 = self._dod(2)
        submission2 = self._submission(
            version=v2,
            dod=dod2,
            supersedes=submission1,
            review=review1,
        )
        review2 = self._review(submission2, ReviewDecision.Decision.APPROVED)
        self.assertEqual(submission2.submission_number, 2)
        self.assertEqual(submission2.supersedes_submission, submission1)
        self.assertEqual(submission2.triggering_review, review1)
        self.assertNotEqual(submission2.pk, submission1.pk)
        self.assertNotEqual(submission2.dod_check_run_id, submission1.dod_check_run_id)
        self.assertGreater(v2.version_number, v1.version_number)
        self.assertEqual(review2.decision, ReviewDecision.Decision.APPROVED)

        with self.assertRaises(ValidationError):
            self._submission(
                version=v1,
                dod=self._dod(3),
                supersedes=submission2,
                review=review2,
            )
        self.assertEqual(TaskSubmission.objects.count(), 2)
