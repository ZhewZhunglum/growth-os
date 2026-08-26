from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("contentops", "0003_tasksubmission_contentops_submission_cannot_supersede_self"),
    ]

    operations = [
        migrations.AddField(
            model_name="contentassetversion",
            name="representation_kind",
            field=models.CharField(
                choices=[("EXTERNAL_URL", "External URL"), ("INLINE_TEXT", "Inline text")],
                default="EXTERNAL_URL",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="contentassetversion",
            name="inline_content",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="contentassetversion",
            name="payload_schema_version",
            field=models.PositiveSmallIntegerField(
                choices=[(1, "Legacy object-key payload"), (2, "Representation-aware payload")],
                default=1,
            ),
            preserve_default=False,
        ),
        migrations.AlterField(
            model_name="contentassetversion",
            name="object_key",
            field=models.CharField(blank=True, default="", max_length=1024),
        ),
        migrations.RemoveConstraint(
            model_name="contentassetversion",
            name="contentops_object_key_not_empty",
        ),
        migrations.AddConstraint(
            model_name="contentassetversion",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(representation_kind="EXTERNAL_URL")
                    & ~models.Q(object_key="")
                    & models.Q(inline_content="")
                )
                | (
                    models.Q(representation_kind="INLINE_TEXT")
                    & models.Q(object_key="")
                    & ~models.Q(inline_content="")
                ),
                name="contentops_asset_version_representation_valid",
            ),
        ),
        migrations.AlterField(
            model_name="contentassetversion",
            name="payload_schema_version",
            field=models.PositiveSmallIntegerField(
                choices=[(1, "Legacy object-key payload"), (2, "Representation-aware payload")],
                default=2,
            ),
        ),
    ]
