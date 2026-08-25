import django.db.models.deletion
from django.db import migrations, models


def create_approved_rework_integrity_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        for suffix, operation in (("insert", "INSERT"), ("update", "UPDATE")):
            schema_editor.execute(
                f"""
                CREATE TRIGGER workflow_approved_rework_task_match_{suffix}
                BEFORE {operation} ON workflow_taskstateevent
                WHEN NEW.event_type = 'APPROVED_REWORK_REQUESTED'
                     AND NOT EXISTS (
                         SELECT 1
                         FROM contentops_tasksubmission submission
                         WHERE submission.id = NEW.submission_id
                           AND submission.task_id = NEW.task_id
                     )
                BEGIN
                    SELECT RAISE(ABORT, 'approved rework submission belongs to another task');
                END;
                """
            )
        return
    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE FUNCTION workflow_enforce_approved_rework_task_match() RETURNS trigger AS $$
            BEGIN
                IF NEW.event_type = 'APPROVED_REWORK_REQUESTED' AND NOT EXISTS (
                    SELECT 1
                    FROM contentops_tasksubmission submission
                    WHERE submission.id = NEW.submission_id
                      AND submission.task_id = NEW.task_id
                ) THEN
                    RAISE EXCEPTION 'approved rework submission belongs to another task'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER workflow_approved_rework_task_match_guard
            BEFORE INSERT OR UPDATE ON workflow_taskstateevent
            FOR EACH ROW EXECUTE FUNCTION workflow_enforce_approved_rework_task_match();
            """
        )


def drop_approved_rework_integrity_trigger(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS workflow_approved_rework_task_match_insert;"
        )
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS workflow_approved_rework_task_match_update;"
        )
        return
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS workflow_approved_rework_task_match_guard "
            "ON workflow_taskstateevent;"
        )
        schema_editor.execute(
            "DROP FUNCTION IF EXISTS workflow_enforce_approved_rework_task_match();"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("contentops", "0004_contentassetversion_inline_text"),
        ("workflow", "0004_taskassignment_supersedes_assignment"),
    ]

    operations = [
        migrations.AlterField(
            model_name="taskstateevent",
            name="event_type",
            field=models.CharField(
                choices=[
                    ("STATE_TRANSITION", "State transition"),
                    ("SUBMISSION_WITHDRAWN", "Submission withdrawn"),
                    (
                        "APPROVED_REWORK_REQUESTED",
                        "Approved submission returned for rework",
                    ),
                ],
                default="STATE_TRANSITION",
                max_length=32,
            ),
        ),
        migrations.RemoveConstraint(
            model_name="taskstateevent",
            name="workflow_event_type_payload_shape",
        ),
        migrations.AddConstraint(
            model_name="taskstateevent",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(event_type="STATE_TRANSITION", submission__isnull=True)
                    | models.Q(
                        event_type="SUBMISSION_WITHDRAWN",
                        submission__isnull=False,
                        from_state="UNDER_REVIEW",
                        to_state="IN_PROGRESS",
                    )
                    | models.Q(
                        event_type="APPROVED_REWORK_REQUESTED",
                        submission__isnull=False,
                        from_state="APPROVED",
                        to_state="HUMAN_REWORK",
                    )
                ),
                name="workflow_event_type_payload_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="taskstateevent",
            constraint=models.UniqueConstraint(
                condition=models.Q(event_type="APPROVED_REWORK_REQUESTED"),
                fields=("submission",),
                name="workflow_one_approved_rework_per_submission",
            ),
        ),
        migrations.RunPython(
            create_approved_rework_integrity_trigger,
            drop_approved_rework_integrity_trigger,
        ),
    ]
