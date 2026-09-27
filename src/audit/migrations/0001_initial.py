# src/audit/migrations/0001_initial.py
"""Initial audit tables: AuditHead (singleton chain tip) + AuditEvent (immutable rows).

Hand-authored to match audit/models.py (the project seeds migrations by hand and the build
gate runs `makemigrations --check --dry-run`, so model and migration must agree exactly).
The final RunPython seeds the single AuditHead row with a fresh per-deployment instance_id,
so the append service only ever LOCKS the head, never races to create it.
"""
import secrets

from django.db import migrations, models


def _seed_head(apps, schema_editor):
    AuditHead = apps.get_model("audit", "AuditHead")
    db = schema_editor.connection.alias
    if not AuditHead.objects.using(db).exists():
        AuditHead.objects.using(db).create(
            singleton=True, instance_id="inst_" + secrets.token_hex(8),
            seq=0, row_hash="0" * 64)


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="AuditHead",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("singleton", models.BooleanField(default=True, editable=False, unique=True)),
                ("instance_id", models.CharField(max_length=64)),
                ("seq", models.BigIntegerField(default=0)),
                ("row_hash", models.CharField(default="0000000000000000000000000000000000000000000000000000000000000000", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"db_table": "audit_head"},
        ),
        migrations.CreateModel(
            name="AuditEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("schema_version", models.IntegerField(default=1)),
                ("instance_id", models.CharField(max_length=64)),
                ("seq", models.BigIntegerField()),
                ("event_type", models.CharField(max_length=64)),
                ("object_type", models.CharField(max_length=64)),
                ("object_id", models.CharField(max_length=128)),
                ("actor_user_id", models.CharField(blank=True, default="", max_length=64)),
                ("actor_membership_id", models.CharField(blank=True, default="", max_length=64)),
                ("occurred_at", models.CharField(max_length=40)),
                ("payload", models.JSONField(default=dict)),
                ("payload_hash", models.CharField(max_length=64)),
                ("prev_hash", models.CharField(max_length=64)),
                ("row_hash", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "audit_event", "ordering": ["seq"]},
        ),
        migrations.AddConstraint(
            model_name="auditevent",
            constraint=models.UniqueConstraint(fields=("instance_id", "seq"), name="uniq_audit_instance_seq"),
        ),
        migrations.RunPython(_seed_head, migrations.RunPython.noop),
    ]
