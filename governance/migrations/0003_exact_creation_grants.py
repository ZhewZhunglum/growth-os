import django.db.models.deletion
from django.db import migrations, models
from django.db.models import Q


def _grant_at(PermissionGrant, *, principal_id, action, action_at):
    window = (
        Q(valid_from__lte=action_at)
        & (Q(valid_until__isnull=True) | Q(valid_until__gt=action_at))
        & (Q(revoked_at__isnull=True) | Q(revoked_at__gt=action_at))
        & Q(grant_status__in=["ACTIVE", "REVOKED"])
    )
    common = PermissionGrant.objects.filter(
        principal_id=principal_id,
        scope_kind="GLOBAL",
        action=action,
    ).filter(window)
    if common.filter(effect="DENY").exists():
        return None
    return common.filter(effect="ALLOW").order_by("-valid_from", "-created_at", "-pk").first()


def backfill_exact_creation_grants(apps, schema_editor):
    PermissionGrant = apps.get_model("accounts", "PermissionGrant")
    specifications = (
        (apps.get_model("governance", "Issue"), "EDIT"),
        (apps.get_model("governance", "Meeting"), "APPROVE"),
        (apps.get_model("governance", "MeetingDecision"), "APPROVE"),
        (apps.get_model("governance", "RuleProposalVersion"), "EDIT"),
    )
    for model, action in specifications:
        for fact in model.objects.filter(permission_grant__isnull=True).iterator():
            grant = _grant_at(
                PermissionGrant,
                principal_id=fact.created_by_principal_id,
                action=action,
                action_at=fact.created_at,
            )
            if grant is None:
                raise RuntimeError(
                    f"Cannot backfill {model._meta.label} {fact.pk}: "
                    f"no exact historical GLOBAL ALLOW/{action} Grant."
                )
            fact.permission_grant_id = grant.pk
            fact.save(update_fields=["permission_grant"])

    IssueDecisionLink = apps.get_model("governance", "IssueDecisionLink")
    for link in IssueDecisionLink.objects.filter(permission_grant__isnull=True).select_related(
        "meeting_decision"
    ).iterator():
        link.permission_grant_id = link.meeting_decision.permission_grant_id
        link.save(update_fields=["permission_grant"])

    RuleProposalSourceLink = apps.get_model("governance", "RuleProposalSourceLink")
    for link in RuleProposalSourceLink.objects.filter(permission_grant__isnull=True).select_related(
        "rule_proposal_version"
    ).iterator():
        link.permission_grant_id = link.rule_proposal_version.permission_grant_id
        link.save(update_fields=["permission_grant"])


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0005_secretreference_permissiongrant_supersedes_grant_and_more"),
        ("governance", "0002_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="issue",
            name="permission_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AddField(
            model_name="meeting",
            name="permission_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AddField(
            model_name="meetingdecision",
            name="permission_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AddField(
            model_name="issuedecisionlink",
            name="permission_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AddField(
            model_name="ruleproposalversion",
            name="permission_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AddField(
            model_name="ruleproposalsourcelink",
            name="permission_grant",
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.RunPython(backfill_exact_creation_grants, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="issue",
            name="permission_grant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AlterField(
            model_name="meeting",
            name="permission_grant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AlterField(
            model_name="meetingdecision",
            name="permission_grant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AlterField(
            model_name="issuedecisionlink",
            name="permission_grant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AlterField(
            model_name="ruleproposalversion",
            name="permission_grant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
        migrations.AlterField(
            model_name="ruleproposalsourcelink",
            name="permission_grant",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="+",
                to="accounts.permissiongrant",
            ),
        ),
    ]
