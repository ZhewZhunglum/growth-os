from django.db import migrations
from django.db.models import F


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


def _create_review_trigger(schema_editor, *, allow_owner_approval: bool):
    vendor = schema_editor.connection.vendor
    owner_exception = """
        AND NOT (
            reviewer.role = 'OWNER'
            AND reviewer.principal_type = 'HUMAN_USER'
            AND NEW.reviewer_acting_role = 'OWNER'
            AND NEW.decision = 'APPROVED'
            AND EXISTS (
                SELECT 1
                FROM accounts_permissiongrant review_grant
                WHERE review_grant.id = NEW.reviewer_grant_id
                  AND review_grant.principal_id = NEW.reviewer_principal_id
                  AND review_grant.action IN ('REVIEW', 'APPROVE')
                  AND review_grant.effect = 'ALLOW'
                  AND review_grant.grant_status = 'ACTIVE'
                  AND review_grant.scope_kind = 'PRODUCT'
                  AND review_grant.product_id IS NOT NULL
            )
        )
    """ if allow_owner_approval else ""

    if vendor == "sqlite":
        for suffix, operation in (("insert", "INSERT"), ("update", "UPDATE")):
            schema_editor.execute(
                f"""
                CREATE TRIGGER contentops_review_no_self_{suffix}
                BEFORE {operation} ON contentops_reviewdecision
                WHEN EXISTS (
                    SELECT 1
                    FROM contentops_tasksubmission submission
                    JOIN accounts_principal reviewer
                      ON reviewer.id = NEW.reviewer_principal_id
                    WHERE submission.id = NEW.submission_id
                      AND submission.submitted_by_principal_id = NEW.reviewer_principal_id
                      {owner_exception}
                )
                BEGIN
                    SELECT RAISE(
                        ABORT,
                        'only an Owner may approve their own submission'
                    );
                END;
                """
            )
        return

    if vendor == "postgresql":
        schema_editor.execute(
            f"""
            CREATE FUNCTION contentops_enforce_no_self_review() RETURNS trigger AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM contentops_tasksubmission submission
                    JOIN accounts_principal reviewer
                      ON reviewer.id = NEW.reviewer_principal_id
                    WHERE submission.id = NEW.submission_id
                      AND submission.submitted_by_principal_id = NEW.reviewer_principal_id
                      {owner_exception}
                ) THEN
                    RAISE EXCEPTION 'only an Owner may approve their own submission'
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


def allow_owner_self_approval(apps, schema_editor):
    _drop_review_trigger(schema_editor)
    _create_review_trigger(schema_editor, allow_owner_approval=True)


def restore_strict_self_review(apps, schema_editor):
    ReviewDecision = apps.get_model("contentops", "ReviewDecision")
    alias = schema_editor.connection.alias
    self_review_ids = list(
        ReviewDecision.objects.using(alias)
        .filter(reviewer_principal_id=F("submission__submitted_by_principal_id"))
        .values_list("id", flat=True)[:20]
    )
    if self_review_ids:
        raise RuntimeError(
            "Cannot restore strict self-review policy while Owner self-approvals exist: "
            f"{self_review_ids}"
        )
    _drop_review_trigger(schema_editor)
    _create_review_trigger(schema_editor, allow_owner_approval=False)


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_secretreference_permissiongrant_supersedes_grant_and_more"),
        ("contentops", "0004_contentassetversion_inline_text"),
    ]

    operations = [
        migrations.RunPython(allow_owner_self_approval, restore_strict_self_review),
    ]
