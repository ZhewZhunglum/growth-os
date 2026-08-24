import core.ids
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("accounts", "0005_secretreference_permissiongrant_supersedes_grant_and_more"),
        ("products", "0002_claimmatrixversion_and_more"),
        ("intelligence", "0002_evidenceinvalidationevent"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DailyBatchDispositionEvent",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=core.ids.uuid7,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("batch_key", models.UUIDField(unique=True)),
                (
                    "disposition",
                    models.CharField(
                        choices=[
                            ("ABANDONED", "Abandoned draft"),
                            ("ARCHIVED", "Abandoned and archived"),
                        ],
                        max_length=16,
                    ),
                ),
                ("reason", models.TextField()),
                (
                    "acting_role",
                    models.CharField(
                        choices=[
                            ("OWNER", "Owner"),
                            ("OPERATIONS_ADMIN", "Operations admin"),
                            ("OPERATOR", "Operator"),
                        ],
                        max_length=24,
                    ),
                ),
                ("command_id", models.UUIDField(unique=True)),
                ("payload_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "permission_grant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="daily_batch_disposition_events",
                        to="accounts.permissiongrant",
                    ),
                ),
                (
                    "principal",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="daily_batch_disposition_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="daily_batch_disposition_events",
                        to="products.product",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(("reason", ""), _negated=True),
                        name="daily_batch_disposition_reason_set",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("payload_hash", ""), _negated=True),
                        name="daily_batch_disposition_hash_set",
                    ),
                ]
            },
        )
    ]
