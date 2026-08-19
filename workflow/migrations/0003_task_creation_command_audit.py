import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0004_alter_permissiongrant_action"),
        ("workflow", "0002_taskstateevent_event_type_taskstateevent_submission_and_more"),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="created_by_acting_role",
            field=models.CharField(
                blank=True,
                choices=[
                    ("OWNER", "Owner"),
                    ("OPERATIONS_ADMIN", "Operations admin"),
                    ("OPERATOR", "Operator"),
                    ("SYSTEM", "System"),
                ],
                max_length=24,
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="created_under_grant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="tasks_created",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="creation_command_id",
            field=models.UUIDField(blank=True, null=True, unique=True),
        ),
        migrations.AddField(
            model_name="task",
            name="creation_payload_hash",
            field=models.CharField(blank=True, max_length=64),
        ),
        migrations.AddConstraint(
            model_name="task",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(
                        creation_command_id__isnull=True,
                        creation_payload_hash="",
                        created_by_acting_role="",
                        created_under_grant__isnull=True,
                    )
                    | (
                        models.Q(
                            creation_command_id__isnull=False,
                            created_under_grant__isnull=False,
                        )
                        & ~models.Q(creation_payload_hash="")
                        & ~models.Q(created_by_acting_role="")
                    )
                ),
                name="workflow_task_creation_audit_complete",
            ),
        ),
    ]
