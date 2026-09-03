from __future__ import annotations

import hashlib
import uuid
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class OwnerSelfApprovalMigrationTests(TransactionTestCase):
    """Prove migration 0006 safely tightens, rolls back, and reinstalls its trigger."""

    databases = {"default"}
    migrate_from = ("contentops", "0005_owner_self_approval_policy")
    migrate_to = ("contentops", "0006_harden_owner_self_approval_policy")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.latest_targets = executor.loader.graph.leaf_nodes()
        self.from_targets = [
            target for target in self.latest_targets if target[0] != "contentops"
        ] + [self.migrate_from]
        self.addCleanup(self._restore_latest_schema)

        executor.migrate(self.from_targets)
        legacy_apps = executor.loader.project_state(self.from_targets).apps
        self.fixture = self._create_fixture(legacy_apps)

    def _restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_targets)

    def _migrate(self, targets):
        executor = MigrationExecutor(connection)
        executor.migrate(targets)
        return MigrationExecutor(connection).loader.project_state(targets).apps

    def _create_fixture(self, apps):
        Principal = apps.get_model("accounts", "Principal")
        PermissionGrant = apps.get_model("accounts", "PermissionGrant")
        Product = apps.get_model("products", "Product")
        ProductProfileVersion = apps.get_model("products", "ProductProfileVersion")
        Task = apps.get_model("workflow", "Task")
        TaskCheckRun = apps.get_model("workflow", "TaskCheckRun")
        TaskContractVersion = apps.get_model("workflow", "TaskContractVersion")
        ContentAsset = apps.get_model("contentops", "ContentAsset")
        ContentAssetVersion = apps.get_model("contentops", "ContentAssetVersion")
        TaskSubmission = apps.get_model("contentops", "TaskSubmission")

        now = timezone.now()
        owners = []
        for suffix in ("first", "rollback", "reinstalled"):
            owners.append(
                Principal.objects.create(
                    username=f"owner-self-approval-migration-{suffix}",
                    password="!",
                    display_name=f"Migration Owner {suffix}",
                    role="OWNER",
                )
            )

        product = Product.objects.create(
            product_code="OWNER_SELF_APPROVAL_MIGRATION",
            name="Owner self-approval migration fixture",
            market_code="US",
            language_code="en",
            created_by_principal=owners[0],
            updated_by_principal=owners[0],
        )
        profile = ProductProfileVersion.objects.create(
            product=product,
            version_number=1,
            market_code="US",
            language_code="en",
            audience={"fixture": True},
            core_value_proposition="Migration fixture.",
            brand_voice={},
            product_facts={},
            prohibited_expressions=[],
            manifest_sha256="a" * 64,
            sealed_at=now,
            sealed_by_principal=owners[0],
            created_by_principal=owners[0],
        )
        contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="Owner self-approval migration contract",
            dor_criteria=[],
            dod_criteria=[],
            release_gate_criteria=[],
            success_criteria=[],
            manifest_sha256="b" * 64,
            sealed_at=now,
            created_by_principal=owners[0],
        )

        review_grants = []
        edit_grants = []
        submissions = []
        for sequence, owner in enumerate(owners, start=1):
            review_grant = PermissionGrant.objects.create(
                principal=owner,
                scope_kind="PRODUCT",
                product=product,
                action="REVIEW",
                effect="ALLOW",
                valid_from=now - timedelta(minutes=1),
                valid_until=now + timedelta(hours=1),
                granted_by_principal=owners[0],
            )
            review_grants.append(review_grant)
            edit_grants.append(
                PermissionGrant.objects.create(
                    principal=owner,
                    scope_kind="PRODUCT",
                    product=product,
                    action="EDIT",
                    effect="ALLOW",
                    valid_from=now - timedelta(minutes=1),
                    valid_until=now + timedelta(hours=1),
                    granted_by_principal=owners[0],
                )
            )

            task = Task.objects.create(
                product=product,
                product_profile_version=profile,
                contract_version=contract,
                title=f"Owner self-approval migration task {sequence}",
                description="Migration-only fixture.",
                current_state="UNDER_REVIEW",
                state_version=1,
                current_assignee_principal=owner,
                created_by_principal=owner,
                creation_command_id=uuid.uuid4(),
                creation_payload_hash=(str(sequence) * 64)[:64],
                created_by_acting_role="OWNER",
                created_under_grant=review_grant,
                updated_by_principal=owner,
            )
            check = TaskCheckRun.objects.create(
                task=task,
                contract_version=contract,
                check_kind="DOD",
                attempt_number=1,
                status="COMPLETED",
                aggregate_result="PASS",
                expected_criterion_count=0,
                actual_criterion_count=0,
                context_sha256=(str(sequence + 3) * 64)[:64],
                command_id=uuid.uuid4(),
                payload_hash=(str(sequence + 6) * 64)[:64],
                evaluator_principal=owner,
                evaluator_acting_role="OWNER",
                permission_grant=review_grant,
                recorded_by_principal=owner,
                completed_at=now,
            )
            asset = ContentAsset.objects.create(
                task=task,
                asset_key="primary-copy",
                title="Migration primary copy",
                asset_kind="COPY",
                description="Migration-only fixture.",
                creation_command_id=uuid.uuid4(),
                creation_payload_hash=(str(sequence + 9) * 64)[:64],
                created_by_principal=owner,
                created_by_acting_role="OWNER",
                created_under_grant=review_grant,
                recorded_by_principal=owner,
            )
            inline_content = f"immutable migration content {sequence}"
            content_sha256 = hashlib.sha256(inline_content.encode()).hexdigest()
            version = ContentAssetVersion.objects.create(
                content_asset=asset,
                version_number=1,
                payload_schema_version=2,
                representation_kind="INLINE_TEXT",
                object_key="",
                inline_content=inline_content,
                mime_type="text/plain",
                byte_size=len(inline_content.encode()),
                content_sha256=content_sha256,
                metadata={"fixture": True},
                manifest_sha256=content_sha256,
                creation_command_id=uuid.uuid4(),
                creation_payload_hash=content_sha256,
                created_by_principal=owner,
                created_by_acting_role="OWNER",
                created_under_grant=review_grant,
                recorded_by_principal=owner,
            )
            submissions.append(
                TaskSubmission.objects.create(
                    task=task,
                    dod_check_run=check,
                    submission_number=1,
                    primary_asset_version=version,
                    asset_manifest={"primary": str(version.pk)},
                    submission_note="Migration-only fixture.",
                    manifest_sha256=content_sha256,
                    command_id=uuid.uuid4(),
                    payload_hash=content_sha256,
                    expected_task_version=1,
                    submitted_by_principal=owner,
                    submitted_by_acting_role="OWNER",
                    submitted_under_grant=review_grant,
                    recorded_by_principal=owner,
                    sealed_at=now,
                )
            )

        return {
            "owner_ids": [owner.pk for owner in owners],
            "product_id": product.pk,
            "review_grant_ids": [grant.pk for grant in review_grants],
            "edit_grant_ids": [grant.pk for grant in edit_grants],
            "submission_ids": [submission.pk for submission in submissions],
        }

    def _insert_owner_approval(self, apps, index, *, bind_exact_edit=False):
        ReviewDecision = apps.get_model("contentops", "ReviewDecision")
        has_exact_edit_field = any(
            field.name == "owner_edit_grant" for field in ReviewDecision._meta.fields
        )
        values = {
            "submission_id": self.fixture["submission_ids"][index],
            "decision": "APPROVED",
            "rationale": "Explicit migration-test Owner approval.",
            "criteria_results": {},
            "decision_sha256": (str(index + 2) * 64)[:64],
            "command_id": uuid.uuid4(),
            "payload_hash": (str(index + 5) * 64)[:64],
            "payload_schema_version": 3 if has_exact_edit_field else 2,
            "expected_task_version": 1,
            "reviewer_principal_id": self.fixture["owner_ids"][index],
            "reviewer_acting_role": "OWNER",
            "reviewer_grant_id": self.fixture["review_grant_ids"][index],
            "recorded_by_principal_id": self.fixture["owner_ids"][index],
            "decided_at": timezone.now(),
        }
        if bind_exact_edit and has_exact_edit_field:
            values["owner_edit_grant_id"] = self.fixture["edit_grant_ids"][index]
        return ReviewDecision.objects.create(**values)

    def test_forward_rollback_and_reinstall_preserve_the_expected_trigger_policy(self):
        hardened_apps = self._migrate(self.latest_targets)

        # 0006 adds the exact current Product EDIT requirement. REVIEW alone
        # must no longer be sufficient for a raw database write.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._insert_owner_approval(hardened_apps, 0)

        first_review = self._insert_owner_approval(
            hardened_apps,
            0,
            bind_exact_edit=True,
        )

        # Rolling 0006 back reinstalls 0005's narrower trigger. A different
        # Owner may use the legacy exception without binding the exact EDIT
        # grant on the review row, proving the reverse operation really ran.
        legacy_apps = self._migrate(self.from_targets)
        rollback_review = self._insert_owner_approval(legacy_apps, 1)

        # SQLite rebuilds the review table while reversing AddField.  The
        # legacy trigger must be restored *after* that rebuild; otherwise a
        # non-Owner self-review raw write would silently succeed in the
        # rollback schema.
        Principal = legacy_apps.get_model("accounts", "Principal")
        Principal.objects.filter(pk=self.fixture["owner_ids"][2]).update(
            role="OPERATIONS_ADMIN"
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._insert_owner_approval(legacy_apps, 2)
        Principal.objects.filter(pk=self.fixture["owner_ids"][2]).update(role="OWNER")

        # Reapplying 0006 must preserve both immutable historical decisions,
        # reinstall the hardened trigger, and reject REVIEW-only self-approval
        # again for a fresh submission.
        reinstalled_apps = self._migrate(self.latest_targets)
        ReviewDecision = reinstalled_apps.get_model("contentops", "ReviewDecision")
        self.assertTrue(ReviewDecision.objects.filter(pk=first_review.pk).exists())
        self.assertTrue(ReviewDecision.objects.filter(pk=rollback_review.pk).exists())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                self._insert_owner_approval(reinstalled_apps, 2)
