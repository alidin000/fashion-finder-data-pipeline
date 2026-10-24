# Additive: refresh-scheduling fields on the existing offer model (no data changes).
# ruff: noqa: RUF012

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("catalogue", "0003_daily_metrics"),
    ]

    operations = [
        migrations.AddField(
            model_name="retaileroffer",
            name="last_checked_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="retaileroffer",
            name="next_refresh_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="retaileroffer",
            name="last_error",
            field=models.CharField(blank=True, max_length=255, null=True),
        ),
        migrations.AddField(
            model_name="retaileroffer",
            name="failure_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="retaileroffer",
            name="gone_checks",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="retaileroffer",
            name="missing_since",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="retaileroffer",
            name="refresh_url",
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddIndex(
            model_name="retaileroffer",
            index=models.Index(
                fields=["retailer", "next_refresh_at"], name="offer_refresh_due_idx"
            ),
        ),
    ]
