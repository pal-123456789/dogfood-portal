# src/events/migrations/0002_invite.py
"""Add the Invite model (§20): signed, single-use, DB-enforced invitations to join an event.

Hand-authored to match events/models.py::Invite field-for-field; the docker build's
`makemigrations --check --dry-run` is the authority and fails the image if this migration and
the model disagree. This is a pure CreateModel (new table `invite`) -- it adds one table and
three foreign keys, touches no existing table, and depends on 0001_initial (for events.event)
plus the swappable AUTH_USER_MODEL (created_by / redeemed_by).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("events", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Invite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("role", models.CharField(choices=[("judge", "judge"), ("participant", "participant")], max_length=16)),
                ("signature", models.CharField(max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(blank=True, null=True)),
                ("redeemed_at", models.DateTimeField(blank=True, null=True)),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invites_created", to=settings.AUTH_USER_MODEL)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invites", to="events.event")),
                ("redeemed_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="invites_redeemed", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "invite"},
        ),
    ]
