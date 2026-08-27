from django.db import migrations, models


EXECUTION_PLATFORM_CODES = {"TIKTOK", "PINTEREST", "QUORA", "SHOPIFY"}


def preflight_channel_plans(apps, schema_editor):
    """Refuse ambiguous legacy data before adding hard invariants.

    ChannelPlan did not previously have a database-level platform invariant.
    Even case/whitespace-only rewrites would mutate an event-projected fact and
    could desynchronise its creation hash. Unsupported codes, non-canonical
    spellings, account mismatches, and duplicate historical plans therefore
    require an explicit audited repair; this migration never guesses, rewrites,
    merges, cancels, or deletes historical rows.
    """

    ChannelPlan = apps.get_model("intelligence", "ChannelPlan")
    ChannelAccount = apps.get_model("releasegate", "ChannelAccount")
    rows = list(
        ChannelPlan.objects.order_by("initiative_id", "platform_code", "id").values(
            "id", "initiative_id", "platform_code", "channel_account_id"
        )
    )
    account_platforms = dict(
        ChannelAccount.objects.filter(
            pk__in=[row["channel_account_id"] for row in rows if row["channel_account_id"]]
        ).values_list("id", "platform_code")
    )
    invalid_rows = []
    noncanonical_rows = []
    account_conflicts = []
    groups = {}

    for row in rows:
        original = str(row["platform_code"] or "")
        canonical = original.strip().upper()
        if canonical not in EXECUTION_PLATFORM_CODES:
            invalid_rows.append((str(row["id"]), original))
            continue
        if original != canonical:
            noncanonical_rows.append((str(row["id"]), original, canonical))
            continue
        account_id = row["channel_account_id"]
        if account_id:
            account_platform = str(account_platforms.get(account_id, ""))
            if account_platform != canonical:
                account_conflicts.append(
                    (str(row["id"]), str(account_id), original, account_platform, canonical)
                )
                continue
        groups.setdefault((row["initiative_id"], canonical), []).append(str(row["id"]))

    duplicate_groups = {
        (str(initiative_id), platform_code): plan_ids
        for (initiative_id, platform_code), plan_ids in groups.items()
        if len(plan_ids) > 1
    }
    if invalid_rows or noncanonical_rows or account_conflicts or duplicate_groups:
        raise RuntimeError(
            "Cannot enforce the ChannelPlan execution-platform invariant without an explicit "
            "history repair. Unsupported rows: "
            f"{invalid_rows!r}; non-canonical rows: {noncanonical_rows!r}; "
            f"plan/account platform conflicts: {account_conflicts!r}; "
            f"duplicate initiative/platform groups: {duplicate_groups!r}. "
            "Do not delete or silently merge these plans; resolve them with an audited repair "
            "before rerunning this migration."
        )

class Migration(migrations.Migration):

    dependencies = [
        ("intelligence", "0002_evidenceinvalidationevent"),
    ]

    operations = [
        migrations.RunPython(preflight_channel_plans, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="channelplan",
            constraint=models.CheckConstraint(
                condition=models.Q(platform_code__in=("TIKTOK", "PINTEREST", "QUORA", "SHOPIFY")),
                name="intel_plan_exec_platform_code",
            ),
        ),
        migrations.AddConstraint(
            model_name="channelplan",
            constraint=models.UniqueConstraint(
                fields=("initiative", "platform_code"),
                name="intel_plan_init_platform_uniq",
            ),
        ),
    ]
