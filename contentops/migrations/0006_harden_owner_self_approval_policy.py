import django.db.models.deletion
from django.db import migrations, models


def _drop_review_trigger(schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        for name in (
            "contentops_review_no_self_insert",
            "contentops_review_no_self_update",
        ):
            schema_editor.execute(f"DROP TRIGGER IF EXISTS {name};")
        return
    if vendor == "postgresql":
        schema_editor.execute(
            "DROP TRIGGER IF EXISTS contentops_review_no_self_guard "
            "ON contentops_reviewdecision;"
        )
        schema_editor.execute(
            "DROP FUNCTION IF EXISTS contentops_enforce_no_self_review();"
        )


def _invalid_owner_edit_binding_sql(now_sql: str) -> str:
    current = (
        "grant_status = 'ACTIVE' "
        f"AND valid_from <= {now_sql} "
        f"AND (valid_until IS NULL OR valid_until > {now_sql})"
    )
    return f"""
        AND (
          (
            submission.submitted_by_principal_id = NEW.reviewer_principal_id
            AND NOT (
              reviewer.role = 'OWNER'
            AND reviewer.principal_type = 'HUMAN_USER'
            AND reviewer.principal_status = 'ACTIVE'
            AND reviewer.is_active
            AND NEW.reviewer_acting_role = 'OWNER'
            AND NEW.decision = 'APPROVED'
            AND NEW.payload_schema_version = 3
            AND TRIM(COALESCE(NEW.rationale, '')) <> ''
            AND EXISTS (
                SELECT 1
                FROM accounts_permissiongrant review_grant
                WHERE review_grant.id = NEW.reviewer_grant_id
                  AND review_grant.principal_id = NEW.reviewer_principal_id
                  AND review_grant.action = 'REVIEW'
                  AND review_grant.effect = 'ALLOW'
                  AND review_grant.{current}
                  AND review_grant.scope_kind = 'PRODUCT'
                  AND review_grant.product_id = task.product_id
            )
            AND NOT EXISTS (
                SELECT 1
                FROM accounts_permissiongrant review_deny
                WHERE review_deny.principal_id = NEW.reviewer_principal_id
                  AND review_deny.action = 'REVIEW'
                  AND review_deny.effect = 'DENY'
                  AND review_deny.{current}
                  AND (
                      review_deny.scope_kind = 'GLOBAL'
                      OR (
                          review_deny.scope_kind = 'PRODUCT'
                          AND review_deny.product_id = task.product_id
                      )
                  )
            )
            AND EXISTS (
                SELECT 1
                FROM accounts_permissiongrant edit_grant
                WHERE edit_grant.id = NEW.owner_edit_grant_id
                  AND edit_grant.principal_id = NEW.reviewer_principal_id
                  AND edit_grant.action = 'EDIT'
                  AND edit_grant.effect = 'ALLOW'
                  AND edit_grant.{current}
                  AND edit_grant.scope_kind = 'PRODUCT'
                  AND edit_grant.product_id = task.product_id
            )
            AND NOT EXISTS (
                SELECT 1
                FROM accounts_permissiongrant edit_deny
                WHERE edit_deny.principal_id = NEW.reviewer_principal_id
                  AND edit_deny.action = 'EDIT'
                  AND edit_deny.effect = 'DENY'
                  AND edit_deny.{current}
                  AND (
                      edit_deny.scope_kind = 'GLOBAL'
                      OR (
                          edit_deny.scope_kind = 'PRODUCT'
                          AND edit_deny.product_id = task.product_id
                      )
                )
            )
            )
          )
          OR (
            submission.submitted_by_principal_id <> NEW.reviewer_principal_id
            AND NEW.owner_edit_grant_id IS NOT NULL
          )
        )
    """


def _create_hardened_review_trigger(schema_editor):
    vendor = schema_editor.connection.vendor
    if vendor == "sqlite":
        invalid_binding = _invalid_owner_edit_binding_sql("CURRENT_TIMESTAMP")
        for suffix, operation in (("insert", "INSERT"), ("update", "UPDATE")):
            schema_editor.execute(
                f"""
                CREATE TRIGGER contentops_review_no_self_{suffix}
                BEFORE {operation} ON contentops_reviewdecision
                WHEN EXISTS (
                    SELECT 1
                    FROM contentops_tasksubmission submission
                    JOIN workflow_task task ON task.id = submission.task_id
                    JOIN accounts_principal reviewer
                      ON reviewer.id = NEW.reviewer_principal_id
                    WHERE submission.id = NEW.submission_id
                      {invalid_binding}
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'Owner self-approval requires exact current Product REVIEW and EDIT authorization'
                    );
                END;
                """
            )
        return

    if vendor == "postgresql":
        invalid_binding = _invalid_owner_edit_binding_sql("CURRENT_TIMESTAMP")
        schema_editor.execute(
            f"""
            CREATE FUNCTION contentops_enforce_no_self_review() RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM contentops_tasksubmission submission
                    JOIN workflow_task task ON task.id = submission.task_id
                    JOIN accounts_principal reviewer
                      ON reviewer.id = NEW.reviewer_principal_id
                    WHERE submission.id = NEW.submission_id
                      {invalid_binding}
                ) THEN
                    RAISE EXCEPTION
                        'Owner self-approval requires exact current Product REVIEW and EDIT authorization'
                        USING ERRCODE = '23514';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;
            """
        )
        schema_editor.execute(
            """
            CREATE TRIGGER contentops_review_no_self_guard
            BEFORE INSERT OR UPDATE ON contentops_reviewdecision
            FOR EACH ROW EXECUTE FUNCTION contentops_enforce_no_self_review();
            """
        )


def harden_owner_self_approval(apps, schema_editor):
    _drop_review_trigger(schema_editor)
    _create_hardened_review_trigger(schema_editor)


def drop_review_trigger(apps, schema_editor):
    """Drop either trigger version before a schema-changing table remake.

    SQLite recreates the table for AddField/RemoveField and does not preserve
    custom triggers.  Keeping the drop/restore steps on opposite sides of the
    schema operations makes both the forward and reverse migration finish with
    the trigger version that belongs to that schema state.
    """

    _drop_review_trigger(schema_editor)


def restore_previous_owner_self_approval(apps, schema_editor):
    previous = __import__(
        "contentops.migrations.0005_owner_self_approval_policy",
        fromlist=["_create_review_trigger"],
    )
    _drop_review_trigger(schema_editor)
    previous._create_review_trigger(schema_editor, allow_owner_approval=True)


class Migration(migrations.Migration):

    dependencies = [
        ("contentops", "0005_owner_self_approval_policy"),
        ("workflow", "0006_remove_taskstateevent_workflow_event_type_payload_shape_and_more"),
    ]

    operations = [
        migrations.RunPython(
            drop_review_trigger,
            restore_previous_owner_self_approval,
        ),
        migrations.AddField(
            model_name="reviewdecision",
            name="owner_edit_grant",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="owner_self_approvals_made",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AlterField(
            model_name="reviewdecision",
            name="payload_schema_version",
            field=models.PositiveSmallIntegerField(
                choices=[
                    (1, "Legacy review command"),
                    (2, "Reviewer-bound review command"),
                    (3, "Reviewer and Owner-edit-bound review command"),
                ],
                default=3,
            ),
        ),
        migrations.RunPython(
            harden_owner_self_approval,
            drop_review_trigger,
        ),
    ]
