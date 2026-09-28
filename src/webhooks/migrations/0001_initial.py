# src/webhooks/migrations/0001_initial.py
"""Initial outbound-webhooks schema, hand-authored to match webhooks/models.py field-for-field.

Repo convention: migrations are written by hand and the docker build's
`makemigrations --check --dry-run` is the authoritative agreement test between this file and the
models (no database needed). WebhookEndpoint is created before WebhookDelivery, which FKs it.
Depends on events 0001 (Event) and the swappable AUTH_USER_MODEL (WebhookEndpoint.created_by).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("events", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="WebhookEndpoint",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("url", models.URLField(max_length=500)),
                ("secret", models.CharField(max_length=64)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="webhook_endpoints", to="events.event")),
                ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="webhook_endpoints_created", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "webhook_endpoint"},
        ),
        migrations.CreateModel(
            name="WebhookDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("event_type", models.CharField(max_length=64)),
                ("payload", models.TextField()),
                ("status", models.CharField(choices=[("pending", "pending"), ("success", "success"), ("failed", "failed")], default="pending", max_length=16)),
                ("attempts", models.PositiveIntegerField(default=0)),
                ("response_code", models.IntegerField(blank=True, null=True)),
                ("error", models.TextField(blank=True, default="")),
                ("signature", models.CharField(blank=True, default="", max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_attempt_at", models.DateTimeField(blank=True, null=True)),
                ("endpoint", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="deliveries", to="webhooks.webhookendpoint")),
            ],
            options={"db_table": "webhook_delivery", "ordering": ["-id"]},
        ),
        migrations.AddConstraint(
            model_name="webhookendpoint",
            constraint=models.UniqueConstraint(fields=("event", "url"), name="uniq_webhook_event_url"),
        ),
    ]
