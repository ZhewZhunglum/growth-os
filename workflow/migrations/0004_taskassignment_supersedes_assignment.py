from django.db import migrations, models
import django.db.models.deletion


def backfill_assignment_supersession(apps, schema_editor):
    TaskAssignment = apps.get_model("workflow", "TaskAssignment")
    alias = schema_editor.connection.alias
    previous = None
    previous_task_id = None
    assignments = (
        TaskAssignment.objects.using(alias)
        .all()
        .order_by("task_id", "assignment_number", "id")
    )
    for assignment in assignments.iterator():
        if assignment.task_id != previous_task_id:
            previous = None
            previous_task_id = assignment.task_id
        if previous is None:
            if assignment.assignment_number != 1:
                raise RuntimeError("Existing TaskAssignment history does not start at assignment 1.")
        else:
            if assignment.assignment_number != previous.assignment_number + 1:
                raise RuntimeError("Existing TaskAssignment history contains a sequence gap.")
            TaskAssignment.objects.using(alias).filter(pk=assignment.pk).update(
                supersedes_assignment_id=previous.pk
            )
        previous = assignment


def create_assignment_integrity_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        for suffix, operation in (("insert", "INSERT"), ("update", "UPDATE")):
            successor_guard = ""
            if operation == "UPDATE":
                successor_guard = """
                OR (
                    EXISTS (
                        SELECT 1
                        FROM workflow_taskassignment successor
                        WHERE successor.supersedes_assignment_id = OLD.id
                          AND (
                              successor.task_id <> NEW.task_id
                              OR successor.assignment_number <> NEW.assignment_number + 1
                          )
                    )
                )
                """
            schema_editor.execute(
                f"""
                CREATE TRIGGER workflow_assignment_chain_{suffix}
                BEFORE {operation} ON workflow_taskassignment
                WHEN (
                    NEW.supersedes_assignment_id IS NOT NULL
                    AND NOT EXISTS (
                        SELECT 1
                        FROM workflow_taskassignment previous
                        WHERE previous.id = NEW.supersedes_assignment_id
                          AND previous.id <> NEW.id
                          AND previous.task_id = NEW.task_id
                          AND previous.assignment_number + 1 = NEW.assignment_number
                    )
                )
                {successor_guard}
                BEGIN
                    SELECT RAISE(ABORT, 'invalid task assignment supersession chain');
                END;
                """
            )
        return

    if vendor == "postgresql":
        schema_editor.execute(
            """
            CREATE FUNCTION workflow_enforce_assignment_chain() RETURNS trigger AS $$
            BEGIN
                IF NEW.supersedes_assignment_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1
                    FROM workflow_taskassignment previous
                    WHERE previous.id = NEW.supersedes_assignment_id
                      AND previous.id <> NEW.id
                      AND previous.task_id = NEW.task_id
                      AND previous.assignment_number + 1 = NEW.assignment_number
                ) THEN
                    RAISE EXCEPTION 'invalid task assignment supersession chain'
                        USING ERRCODE = '23514';
                END IF;
                IF TG_OP = 'UPDATE' AND EXISTS (
                    SELECT 1
                    FROM workflow_taskassignment successor
                    WHERE successor.supersedes_assignment_id = OLD.id
                      AND (
                          successor.task_id <> NEW.task_id
                          OR successor.assignment_number <> NEW.assignment_number + 1
                      )
                ) THEN
                    RAISE EXCEPTION 'invalid task assignment supersession chain'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER workflow_assignment_chain_guard
            BEFORE INSERT OR UPDATE ON workflow_taskassignment
            FOR EACH ROW EXECUTE FUNCTION workflow_enforce_assignment_chain();
            """
        )


def drop_assignment_integrity_triggers(apps, schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        schema_editor.execute("DROP TRIGGER IF EXISTS workflow_assignment_chain_insert;")
        schema_editor.execute("DROP TRIGGER IF EXISTS workflow_assignment_chain_update;")
        return
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS workflow_assignment_chain_guard ON workflow_taskassignment;"
        )
        schema_editor.execute("DROP FUNCTION IF EXISTS workflow_enforce_assignment_chain();")


class Migration(migrations.Migration):

    dependencies = [
        ("workflow", "0003_task_creation_command_audit"),
    ]

    operations = [
        migrations.AddField(
            model_name="taskassignment",
            name="supersedes_assignment",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="superseded_by_assignment",
                to="workflow.taskassignment",
            ),
        ),
        migrations.RunPython(
            backfill_assignment_supersession,
            reverse_code=migrations.RunPython.noop,
        ),
        migrations.AddConstraint(
            model_name="taskassignment",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(assignment_number=1, supersedes_assignment__isnull=True)
                    | models.Q(assignment_number__gt=1, supersedes_assignment__isnull=False)
                ),
                name="workflow_assignment_supersession_shape",
            ),
        ),
        migrations.RunPython(
            create_assignment_integrity_triggers,
            drop_assignment_integrity_triggers,
        ),
    ]
