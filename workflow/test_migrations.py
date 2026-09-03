import uuid
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class TaskAssignmentSupersessionMigrationTests(TransactionTestCase):
    """Prove legacy assignment history upgrades into a DB-enforced chain."""

    databases = {"default"}
    migrate_from = ("workflow", "0003_task_creation_command_audit")
    migrate_to = ("workflow", "0004_taskassignment_supersedes_assignment")

    def setUp(self):
        super().setUp()
        executor = MigrationExecutor(connection)
        self.latest_targets = executor.loader.graph.leaf_nodes()
        self.addCleanup(self._restore_latest_schema)
        # contentops.0006 is deliberately installed after workflow.0006
        # because its database trigger joins workflow_task.  Roll both apps
        # back to the last graph-consistent state before this workflow upgrade;
        # otherwise keeping the contentops leaf would pull workflow.0006 back
        # in and the historical ORM would no longer match the physical table.
        from_targets = [
            target
            for target in self.latest_targets
            if target[0] not in {"workflow", "contentops"}
        ] + [
            ("contentops", "0005_owner_self_approval_policy"),
            self.migrate_from,
        ]
        executor.migrate(from_targets)
        old_apps = executor.loader.project_state(from_targets).apps
        self.legacy_ids = self._create_legacy_assignment_history(old_apps)

        executor = MigrationExecutor(connection)
        executor.migrate([self.migrate_to])
        self.migrated_apps = executor.loader.project_state([self.migrate_to]).apps

    def _restore_latest_schema(self):
        MigrationExecutor(connection).migrate(self.latest_targets)

    def _create_legacy_assignment_history(self, apps):
        Principal = apps.get_model("accounts", "Principal")
        PermissionGrant = apps.get_model("accounts", "PermissionGrant")
        Product = apps.get_model("products", "Product")
        ProductProfileVersion = apps.get_model("products", "ProductProfileVersion")
        Task = apps.get_model("workflow", "Task")
        TaskAssignment = apps.get_model("workflow", "TaskAssignment")
        TaskContractVersion = apps.get_model("workflow", "TaskContractVersion")

        now = timezone.now()
        owner = Principal.objects.create(
            username="assignment-migration-owner",
            password="!",
            display_name="Migration Owner",
            role="OWNER",
        )
        operator = Principal.objects.create(
            username="assignment-migration-operator",
            password="!",
            display_name="Migration Operator",
            role="OPERATOR",
        )
        product = Product.objects.create(
            product_code="ASSIGNMENT_MIGRATION",
            name="Assignment migration fixture",
            market_code="US",
            language_code="en",
            created_by_principal=owner,
            updated_by_principal=owner,
        )
        profile = ProductProfileVersion.objects.create(
            product=product,
            version_number=1,
            market_code="US",
            language_code="en",
            core_value_proposition="Migration fixture.",
            created_by_principal=owner,
        )
        contract = TaskContractVersion.objects.create(
            product_profile_version=profile,
            version_number=1,
            title="Migration contract",
            manifest_sha256="a" * 64,
            sealed_at=now,
            created_by_principal=owner,
        )
        grant = PermissionGrant.objects.create(
            principal=owner,
            scope_kind="PRODUCT",
            product=product,
            action="ASSIGN_TASK",
            valid_from=now - timedelta(minutes=1),
            valid_until=now + timedelta(hours=1),
            granted_by_principal=owner,
        )

        def create_task(title):
            return Task.objects.create(
                product=product,
                product_profile_version=profile,
                contract_version=contract,
                title=title,
                created_by_principal=owner,
                updated_by_principal=owner,
            )

        first_task = create_task("Legacy task one")
        second_task = create_task("Legacy task two")

        def create_assignment(task, number, assigned_at):
            return TaskAssignment.objects.create(
                task=task,
                assignee_principal=operator,
                assignment_number=number,
                command_id=uuid.uuid4(),
                payload_hash=(str(number) * 64)[:64],
                expected_task_version=0,
                assigned_by_principal=owner,
                acting_role="OWNER",
                permission_grant=grant,
                recorded_by_principal=owner,
                assigned_at=assigned_at,
            )

        # Deliberately reverse the timestamps.  The immutable business sequence,
        # not wall-clock time, is the only deterministic predecessor ordering.
        first = create_assignment(first_task, 1, now + timedelta(minutes=3))
        second = create_assignment(first_task, 2, now + timedelta(minutes=2))
        third = create_assignment(first_task, 3, now + timedelta(minutes=1))
        other_first = create_assignment(second_task, 1, now)
        return {
            "first": first.pk,
            "second": second.pk,
            "third": third.pk,
            "other_first": other_first.pk,
        }

    def test_upgrade_backfills_immediate_predecessors_and_database_rejects_bypasses(self):
        TaskAssignment = self.migrated_apps.get_model("workflow", "TaskAssignment")
        first = TaskAssignment.objects.get(pk=self.legacy_ids["first"])
        second = TaskAssignment.objects.get(pk=self.legacy_ids["second"])
        third = TaskAssignment.objects.get(pk=self.legacy_ids["third"])
        other_first = TaskAssignment.objects.get(pk=self.legacy_ids["other_first"])

        self.assertIsNone(first.supersedes_assignment_id)
        self.assertEqual(second.supersedes_assignment_id, first.pk)
        self.assertEqual(third.supersedes_assignment_id, second.pk)
        self.assertIsNone(other_first.supersedes_assignment_id)

        # Historical migration models do not have the current append-only
        # manager, so this ORM update reaches the database trigger directly.
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TaskAssignment.objects.filter(pk=second.pk).update(
                    supersedes_assignment_id=third.pk
                )

        adapted_second_id = second.pk.hex if connection.vendor == "sqlite" else second.pk
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE workflow_taskassignment "
                        "SET assignment_number = %s WHERE id = %s",
                        [4, adapted_second_id],
                    )

        second.refresh_from_db()
        self.assertEqual(second.assignment_number, 2)
        self.assertEqual(second.supersedes_assignment_id, first.pk)
