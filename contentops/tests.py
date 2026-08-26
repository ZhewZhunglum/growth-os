from __future__ import annotations

import hashlib
import importlib
import uuid
from datetime import timedelta

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import IntegrityError, models, transaction
from django.test import TestCase
from django.utils import timezone

from accounts.models import PermissionGrant, Principal
from core.ids import uuid7
from products.models import Product, ProductProfileVersion
from workflow.exceptions import CheckGateRejected, IllegalTaskTransition

from contentops.models import (
    ActingRole,
    ContentAsset,
    ContentAssetVersion,
    ReviewDecision,
    TaskSubmission,
    TaskSubmissionAssetLink,
    canonical_sha256,
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
        self.assign_grant = PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.ASSIGN_TASK,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        self.owner_assign_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.ASSIGN_TASK,
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
                expected_task_version=1, assigned_by_principal=self.owner, acting_role="OWNER",
                permission_grant=self.owner_assign_grant, recorded_by_principal=self.recorder,
            )
            Task.transition(
                task_id=self.task.pk, to_state=Task.State.ASSIGNED, command_id=uuid7(),
                expected_state_version=1, actor_principal=self.operator, acting_role="OPERATOR",
                permission_grant=self.assign_grant, recorded_by_principal=self.recorder,
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
            object_key=f"https://drafts.example.com/tasks/{self.task.pk}/primary-v{number}.mp4",
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
            self._move_to_review()
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

    def _move_to_review(self) -> None:
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

    def test_uuidv7_and_asset_version_are_append_only_and_idempotent(self):
        asset = self._asset()
        command_id = uuid7()
        content = b"v1"
        kwargs = {
            "content_asset": asset,
            "object_key": "https://drafts.example.com/puko/v1.mp4",
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
            ContentAssetVersion.create_next(
                **{**kwargs, "object_key": "https://drafts.example.com/puko/different.mp4"}
            )
        version.object_key = "https://drafts.example.com/puko/mutated.mp4"
        with self.assertRaises(ValidationError):
            version.save()
        with self.assertRaises(ValidationError):
            ContentAssetVersion.objects.filter(pk=version.pk).update(
                object_key="https://drafts.example.com/puko/mutated.mp4"
            )
        with self.assertRaises(ValidationError):
            ContentAssetVersion.objects.filter(pk=version.pk).delete()

    def test_inline_text_version_hashes_body_and_metadata_and_is_idempotent(self):
        asset = self._asset()
        command_id = uuid7()
        body = "Hook: Keep your afternoon focused.\nCTA: Learn more."
        metadata = {
            "template_key": "tiktok-script",
            "template_version": 1,
            "evidence_manifest_sha256": "a" * 64,
        }
        kwargs = {
            "content_asset": asset,
            "representation_kind": ContentAssetVersion.RepresentationKind.INLINE_TEXT,
            "inline_content": body,
            "mime_type": "text/plain; charset=utf-8",
            "metadata": metadata,
            "command_id": command_id,
            "actor_principal": self.operator,
            "acting_role": ActingRole.OPERATOR,
            "permission_grant": self.edit_grant,
            "recorded_by_principal": self.recorder,
        }

        version = ContentAssetVersion.create_next(**kwargs)
        replay = ContentAssetVersion.create_next(**kwargs)

        body_bytes = body.encode("utf-8")
        expected_payload = {
            "content_asset_id": str(asset.pk),
            "payload_schema_version": ContentAssetVersion.PayloadSchemaVersion.V2,
            "representation_kind": ContentAssetVersion.RepresentationKind.INLINE_TEXT,
            "object_key": "",
            "inline_content": body,
            "mime_type": "text/plain; charset=utf-8",
            "byte_size": len(body_bytes),
            "content_sha256": hashlib.sha256(body_bytes).hexdigest(),
            "metadata": metadata,
        }
        self.assertEqual(replay.pk, version.pk)
        self.assertEqual(version.object_key, "")
        self.assertEqual(version.byte_size, len(body_bytes))
        self.assertEqual(version.content_sha256, hashlib.sha256(body_bytes).hexdigest())
        self.assertEqual(version.creation_payload_hash, canonical_sha256(expected_payload))
        self.assertEqual(
            version.manifest_sha256,
            canonical_sha256({**expected_payload, "version_number": version.version_number}),
        )

    def test_legacy_v1_object_key_keeps_its_original_payload_and_manifest_hashes(self):
        asset = self._asset()
        command_id = uuid7()
        legacy_object_key = f"legacy/tasks/{self.task.pk}/primary-v1.txt"
        legacy_bytes = b"legacy immutable content"
        metadata = {"source": "pre-inline-text-migration"}
        legacy_payload = {
            "content_asset_id": str(asset.pk),
            "object_key": legacy_object_key,
            "mime_type": "text/plain",
            "byte_size": len(legacy_bytes),
            "content_sha256": hashlib.sha256(legacy_bytes).hexdigest(),
            "metadata": metadata,
        }
        original_payload_hash = canonical_sha256(legacy_payload)
        original_manifest_hash = canonical_sha256({**legacy_payload, "version_number": 1})
        legacy = ContentAssetVersion.objects.create(
            content_asset=asset,
            version_number=1,
            payload_schema_version=ContentAssetVersion.PayloadSchemaVersion.V1,
            representation_kind=ContentAssetVersion.RepresentationKind.EXTERNAL_URL,
            object_key=legacy_object_key,
            inline_content="",
            mime_type="text/plain",
            byte_size=len(legacy_bytes),
            content_sha256=hashlib.sha256(legacy_bytes).hexdigest(),
            metadata=metadata,
            manifest_sha256=original_manifest_hash,
            creation_command_id=command_id,
            creation_payload_hash=original_payload_hash,
            created_by_principal=self.operator,
            created_by_acting_role=ActingRole.OPERATOR,
            created_under_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )

        replay = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=legacy_object_key,
            mime_type="text/plain",
            byte_size=len(legacy_bytes),
            content_sha256=hashlib.sha256(legacy_bytes).hexdigest(),
            metadata=metadata,
            command_id=command_id,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
        )

        self.assertEqual(replay.pk, legacy.pk)
        self.assertEqual(legacy.command_payload(), legacy_payload)
        self.assertNotIn("representation_kind", legacy.command_payload())
        self.assertNotIn("inline_content", legacy.command_payload())
        self.assertEqual(legacy.creation_payload_hash, original_payload_hash)
        self.assertEqual(legacy.manifest_sha256, original_manifest_hash)

    def test_content_representation_rejects_invalid_url_and_mixed_or_blank_inline_text(self):
        asset = self._asset()
        common = {
            "content_asset": asset,
            "mime_type": "text/plain",
            "command_id": uuid7(),
            "actor_principal": self.operator,
            "acting_role": ActingRole.OPERATOR,
            "permission_grant": self.edit_grant,
            "recorded_by_principal": self.recorder,
        }
        payload = b"draft"

        with self.assertRaisesMessage(ValidationError, "HTTP(S)"):
            ContentAssetVersion.create_next(
                **common,
                object_key="legacy/draft.txt",
                byte_size=len(payload),
                content_sha256=hashlib.sha256(payload).hexdigest(),
            )
        for credential_url in (
            "https://embedded-user@example.com/draft",
            "https://embedded-user:embedded-password@example.com/draft",
        ):
            with self.subTest(credential_url=credential_url):
                with self.assertRaisesMessage(ValidationError, "must not embed credentials"):
                    ContentAssetVersion.create_next(
                        **{**common, "command_id": uuid7()},
                        object_key=credential_url,
                        byte_size=len(payload),
                        content_sha256=hashlib.sha256(payload).hexdigest(),
                    )
        with self.assertRaisesMessage(ValidationError, "cannot be blank"):
            ContentAssetVersion.create_next(
                **{**common, "command_id": uuid7()},
                representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
                inline_content="   ",
            )
        with self.assertRaisesMessage(ValidationError, "cannot also contain"):
            ContentAssetVersion.create_next(
                **{**common, "command_id": uuid7()},
                representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
                object_key="https://drafts.example.com/mixed",
                inline_content="draft",
            )
        with self.assertRaisesMessage(ValidationError, "hash does not match"):
            ContentAssetVersion.create_next(
                **{**common, "command_id": uuid7()},
                representation_kind=ContentAssetVersion.RepresentationKind.INLINE_TEXT,
                inline_content="draft",
                content_sha256="0" * 64,
            )
        self.assertFalse(ContentAssetVersion.objects.filter(content_asset=asset).exists())

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

    def test_only_current_assignee_can_seal_submission_after_reassignment(self):
        replacement = Principal.objects.create_user(
            username="replacement-operator-contentops",
            password="not-a-real-secret",
            role=Principal.Role.OPERATOR,
        )
        asset = self._asset()
        version = self._version(asset, 1)
        dod = self._dod(1)
        # Simulate the assignee changing after the caller prepared the
        # submission but before seal() obtains the task lock.
        type(self.task).objects.filter(pk=self.task.pk).update(
            current_assignee_principal=replacement,
            updated_by_principal=self.owner,
        )
        self.task.refresh_from_db()

        with self.assertRaisesMessage(ValidationError, "current assignee"):
            self._submission(version=version, dod=dod)
        self.assertFalse(TaskSubmission.objects.exists())

    def test_review_payload_v1_replays_without_rehashing_and_new_reviews_use_v2(self):
        asset = self._asset()
        submission = self._submission(version=self._version(asset, 1), dod=self._dod(1))
        self._move_to_review()
        submission.task = self.task
        command_id = uuid7()
        legacy = ReviewDecision(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Legacy approved review.",
            criteria_results={"primary-deliverable": "PASS"},
            command_id=command_id,
            payload_schema_version=ReviewDecision.PayloadSchemaVersion.V1,
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            reviewer_acting_role=ActingRole.OPERATIONS_ADMIN,
            reviewer_grant=self.review_grant,
            recorded_by_principal=self.recorder,
            decided_at=timezone.now(),
        )
        legacy.payload_hash = canonical_sha256(legacy.command_payload())
        legacy.decision_sha256 = legacy.payload_hash
        legacy._record_final_authorized = True
        legacy.save()
        original_hash = legacy.payload_hash
        self.assertNotIn("reviewer_principal_id", legacy.command_payload())

        replay = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Legacy approved review.",
            criteria_results={"primary-deliverable": "PASS"},
            command_id=command_id,
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=self.review_grant,
            recorded_by_principal=self.recorder,
        )
        legacy.refresh_from_db()
        self.assertEqual(replay.pk, legacy.pk)
        self.assertEqual(legacy.payload_schema_version, ReviewDecision.PayloadSchemaVersion.V1)
        self.assertEqual(legacy.payload_hash, original_hash)
        self.assertEqual(legacy.decision_sha256, original_hash)

        # A fresh command is always reviewer-bound V2.
        fresh = ReviewDecision()
        self.assertEqual(fresh.payload_schema_version, ReviewDecision.PayloadSchemaVersion.V2)
        self.assertIn("payload_schema_version", fresh.command_payload())
        self.assertIn("reviewer_principal_id", fresh.command_payload())

    def test_legacy_integrity_audit_detects_self_review_and_invalid_chain(self):
        migration = importlib.import_module(
            "contentops.migrations.0003_tasksubmission_contentops_submission_cannot_supersede_self"
        )
        task_a = uuid7()
        task_b = uuid7()
        first = uuid7()
        invalid = uuid7()
        failures = migration.legacy_integrity_failures(
            self_review_ids=[uuid7()],
            submission_rows=[
                {
                    "id": first,
                    "task_id": task_a,
                    "submission_number": 1,
                    "supersedes_submission_id": None,
                },
                {
                    "id": invalid,
                    "task_id": task_b,
                    "submission_number": 2,
                    "supersedes_submission_id": first,
                },
            ],
        )
        self.assertEqual(len(failures), 2)
        self.assertIn("self-review", failures[0])
        self.assertIn("invalid TaskSubmission chain", failures[1])

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

    def test_owner_submitter_with_exact_review_grant_can_approve_own_submission(self):
        """The explicit Owner path may approve, while preserving exact audit facts."""

        now = timezone.now()
        owner_edit_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.EDIT,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        owner_review_grant = PermissionGrant.objects.create(
            principal=self.owner,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.REVIEW,
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=self.owner,
        )

        # Fixture-only projection adjustment: the test is about self-review,
        # so make Owner the exact current assignee before creating any facts.
        type(self.task).objects.filter(pk=self.task.pk).update(
            current_assignee_principal=self.owner,
            updated_by_principal=self.owner,
        )
        self.task.refresh_from_db()

        asset = ContentAsset.create_idempotent(
            task=self.task,
            asset_key="owner-authored-copy",
            title="Owner-authored primary copy",
            asset_kind=ContentAsset.AssetKind.COPY,
            command_id=uuid7(),
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=owner_edit_grant,
            recorded_by_principal=self.recorder,
        )
        content = b"immutable owner-authored content"
        version = ContentAssetVersion.create_next(
            content_asset=asset,
            object_key=f"https://drafts.example.com/tasks/{self.task.pk}/owner-v1",
            mime_type="text/plain",
            byte_size=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            metadata={"source": "owner-self-review-negative"},
            command_id=uuid7(),
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=owner_edit_grant,
            recorded_by_principal=self.recorder,
        )
        TaskCheckRun = apps.get_model("workflow", "TaskCheckRun")
        dod = TaskCheckRun.record_completed(
            task=self.task,
            check_kind=TaskCheckRun.Kind.DOD,
            results=[
                {
                    "criterion_key": "primary-deliverable",
                    "result": "PASS",
                    "evidence": {"exact": True},
                },
                {
                    "criterion_key": "claims-check",
                    "result": "PASS",
                    "evidence": {"checked": True},
                },
            ],
            command_id=uuid7(),
            evaluator_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=owner_edit_grant,
            recorded_by_principal=self.recorder,
        )
        submission = TaskSubmission.seal(
            task=self.task,
            dod_check_run=dod,
            primary_asset_version=version,
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=owner_edit_grant,
            recorded_by_principal=self.recorder,
        )

        Task = apps.get_model("workflow", "Task")
        for next_state in (Task.State.SUBMITTED, Task.State.UNDER_REVIEW):
            Task.transition(
                task_id=self.task.pk,
                to_state=next_state,
                command_id=uuid7(),
                expected_state_version=self.task.state_version,
                actor_principal=self.owner,
                acting_role=ActingRole.OWNER,
                permission_grant=owner_edit_grant,
                recorded_by_principal=self.recorder,
            )
            self.task.refresh_from_db()

        self.assertFalse(
            ReviewDecision.owner_self_approval_allowed(
                submission=submission,
                decision=ReviewDecision.Decision.CHANGES_REQUESTED,
                reviewer_principal=self.owner,
                acting_role=ActingRole.OWNER,
            )
        )

        # The database exception is intentionally as narrow as the service
        # rule.  Raw SQL/ORM bypasses cannot label an EDIT grant as REVIEW
        # authority merely because the submitter happens to be an Owner.
        raw_wrong_grant = ReviewDecision(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Raw Owner approval with the wrong grant must fail.",
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.owner,
            reviewer_acting_role=ActingRole.OWNER,
            reviewer_grant=owner_edit_grant,
            recorded_by_principal=self.owner,
            decided_at=timezone.now(),
        )
        raw_wrong_grant.payload_hash = canonical_sha256(
            raw_wrong_grant.command_payload()
        )
        raw_wrong_grant.decision_sha256 = raw_wrong_grant.payload_hash
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Model.save_base(
                raw_wrong_grant,
                raw=True,
                force_insert=True,
                using="default",
            )

        review = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Owner explicitly approved their own final content.",
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=owner_review_grant,
            recorded_by_principal=self.owner,
        )
        self.assertEqual(review.reviewer_principal, self.owner)
        self.assertEqual(review.submission.submitted_by_principal, self.owner)
        self.assertEqual(review.reviewer_grant, owner_review_grant)
        self.assertEqual(review.reviewer_acting_role, ActingRole.OWNER)

        Task.transition(
            task_id=self.task.pk,
            to_state=Task.State.APPROVED,
            command_id=uuid7(),
            expected_state_version=self.task.state_version,
            actor_principal=self.owner,
            acting_role=ActingRole.OWNER,
            permission_grant=owner_edit_grant,
            recorded_by_principal=self.owner,
            reason="Owner final approval recorded.",
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.APPROVED)

    def test_self_review_is_rejected_and_record_final_is_the_only_orm_write_path(self):
        asset = self._asset()
        submission = self._submission(version=self._version(asset, 1), dod=self._dod(1))
        self._move_to_review()
        operator_review_grant = PermissionGrant.objects.create(
            principal=self.operator,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.REVIEW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        with self.assertRaisesMessage(ValidationError, "cannot review their own"):
            ReviewDecision.record_final(
                submission=submission,
                decision=ReviewDecision.Decision.APPROVED,
                rationale="Self review must fail.",
                command_id=uuid7(),
                expected_task_version=self.task.state_version,
                reviewer_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=operator_review_grant,
                recorded_by_principal=self.recorder,
            )
        self.assertFalse(ReviewDecision.objects.filter(submission=submission).exists())

        direct = ReviewDecision(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Bypass attempt.",
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            reviewer_acting_role=ActingRole.OPERATIONS_ADMIN,
            reviewer_grant=self.review_grant,
            recorded_by_principal=self.recorder,
            decided_at=timezone.now(),
        )
        with self.assertRaisesMessage(ValidationError, "through record_final"):
            direct.save()

        raw = ReviewDecision(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Raw database bypass attempt.",
            command_id=uuid7(),
            expected_task_version=self.task.state_version,
            reviewer_principal=self.operator,
            reviewer_acting_role=ActingRole.OPERATOR,
            reviewer_grant=operator_review_grant,
            recorded_by_principal=self.recorder,
            decided_at=timezone.now(),
        )
        raw.payload_hash = canonical_sha256(raw.command_payload())
        raw.decision_sha256 = raw.payload_hash
        with self.assertRaises(IntegrityError), transaction.atomic():
            models.Model.save_base(raw, raw=True, force_insert=True, using="default")

    def test_review_replay_binds_exact_reviewer_role_and_grant(self):
        asset = self._asset()
        submission = self._submission(version=self._version(asset, 1), dod=self._dod(1))
        self._move_to_review()
        command_id = uuid7()
        review = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Approved once.",
            command_id=command_id,
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=self.review_grant,
            recorded_by_principal=self.recorder,
        )
        replay = ReviewDecision.record_final(
            submission=submission,
            decision=ReviewDecision.Decision.APPROVED,
            rationale="Approved once.",
            command_id=command_id,
            expected_task_version=self.task.state_version,
            reviewer_principal=self.reviewer,
            acting_role=ActingRole.OPERATIONS_ADMIN,
            permission_grant=self.review_grant,
            recorded_by_principal=self.recorder,
        )
        self.assertEqual(replay.pk, review.pk)

        second_reviewer = Principal.objects.create_user(
            username="second-reviewer-contentops",
            password="not-a-real-secret",
            role=Principal.Role.OPERATIONS_ADMIN,
        )
        second_grant = PermissionGrant.objects.create(
            principal=second_reviewer,
            scope_kind=PermissionGrant.ScopeKind.PRODUCT,
            product=self.product,
            action=PermissionGrant.Action.REVIEW,
            valid_from=timezone.now() - timedelta(minutes=1),
            valid_until=timezone.now() + timedelta(hours=1),
            granted_by_principal=self.owner,
        )
        with self.assertRaisesMessage(ValidationError, "different review command"):
            ReviewDecision.record_final(
                submission=submission,
                decision=ReviewDecision.Decision.APPROVED,
                rationale="Approved once.",
                command_id=command_id,
                expected_task_version=self.task.state_version,
                reviewer_principal=second_reviewer,
                acting_role=ActingRole.OPERATIONS_ADMIN,
                permission_grant=second_grant,
                recorded_by_principal=self.recorder,
            )

    def test_withdrawal_is_exact_idempotent_and_allows_a_new_submission_version(self):
        Task = apps.get_model("workflow", "Task")
        TaskStateEvent = apps.get_model("workflow", "TaskStateEvent")
        asset = self._asset()
        v1 = self._version(asset, 1)
        submission1 = self._submission(version=v1, dod=self._dod(1))
        self._move_to_review()
        command_id = uuid7()
        event = Task.withdraw_submission(
            task_id=self.task.pk,
            submission_id=submission1.pk,
            command_id=command_id,
            expected_state_version=self.task.state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
            reason="Wrong file selected.",
        )
        self.task.refresh_from_db()
        self.assertEqual(self.task.current_state, Task.State.IN_PROGRESS)
        self.assertEqual(event.event_type, TaskStateEvent.EventType.SUBMISSION_WITHDRAWN)
        self.assertEqual(event.submission_id, submission1.pk)
        replay = Task.withdraw_submission(
            task_id=self.task.pk,
            submission_id=submission1.pk,
            command_id=command_id,
            expected_state_version=event.expected_state_version,
            actor_principal=self.operator,
            acting_role=ActingRole.OPERATOR,
            permission_grant=self.edit_grant,
            recorded_by_principal=self.recorder,
            reason="Wrong file selected.",
        )
        self.assertEqual(replay.pk, event.pk)
        self.assertEqual(
            TaskStateEvent.objects.filter(
                event_type=TaskStateEvent.EventType.SUBMISSION_WITHDRAWN,
                submission=submission1,
            ).count(),
            1,
        )

        with self.assertRaises(IllegalTaskTransition):
            Task.withdraw_submission(
                task_id=self.task.pk,
                submission_id=submission1.pk,
                command_id=uuid7(),
                expected_state_version=self.task.state_version,
                actor_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
        with self.assertRaises(ValidationError):
            ReviewDecision.record_final(
                submission=submission1,
                decision=ReviewDecision.Decision.APPROVED,
                rationale="Stale review.",
                command_id=uuid7(),
                expected_task_version=self.task.state_version,
                reviewer_principal=self.reviewer,
                acting_role=ActingRole.OPERATIONS_ADMIN,
                permission_grant=self.review_grant,
                recorded_by_principal=self.recorder,
            )

        v2 = self._version(asset, 2)
        submission2 = self._submission(
            version=v2,
            dod=self._dod(2),
            supersedes=submission1,
        )
        self.assertEqual(submission2.submission_number, 2)
        self.assertEqual(submission2.supersedes_submission_id, submission1.pk)
        self.assertIsNone(submission2.triggering_review_id)

    def test_review_wins_then_withdrawal_fails_and_generic_transition_cannot_withdraw(self):
        Task = apps.get_model("workflow", "Task")
        asset = self._asset()
        submission = self._submission(version=self._version(asset, 1), dod=self._dod(1))
        self._move_to_review()
        with self.assertRaises(IllegalTaskTransition):
            Task.transition(
                task_id=self.task.pk,
                to_state=Task.State.IN_PROGRESS,
                command_id=uuid7(),
                expected_state_version=self.task.state_version,
                actor_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
        self._review(submission, ReviewDecision.Decision.APPROVED)
        with self.assertRaises(CheckGateRejected):
            Task.withdraw_submission(
                task_id=self.task.pk,
                submission_id=submission.pk,
                command_id=uuid7(),
                expected_state_version=self.task.state_version,
                actor_principal=self.operator,
                acting_role=ActingRole.OPERATOR,
                permission_grant=self.edit_grant,
                recorded_by_principal=self.recorder,
            )
