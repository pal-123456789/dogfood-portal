# src/apitokens/migrations/0001_initial.py
"""Initial personal-API-token schema, hand-authored to match apitokens/models.py field-for-field.

Repo convention: migrations are written by hand and the docker build's
`makemigrations --check --dry-run` is the authoritative agreement test between this file and the
model (no database needed). Depends only on the swappable AUTH_USER_MODEL (ApiToken.user); the FK is
emitted last, mirroring makemigrations' ordering (see webhooks/migrations/0001_initial.py).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ApiToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=120)),
                ("prefix", models.CharField(max_length=12)),
                ("token_hash", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used_at", models.DateTimeField(blank=True, null=True)),
                ("revoked_at", models.DateTimeField(blank=True, null=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="api_tokens", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "api_token", "ordering": ["-created_at"]},
        ),
    ]
