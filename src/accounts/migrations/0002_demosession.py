# src/accounts/migrations/0002_demosession.py
"""Adds DemoSession — the token->user map behind the DOGFOOD_DEMO auth shim.

New file on purpose: 0001_initial is generated and must not be hand-edited. This depends
on 0001 (for AppUser) and on the swappable user model. Hand-authored to match the
DemoSession added to accounts/models.py.
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="DemoSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=64, unique=True)),
                ("label", models.CharField(blank=True, default="", max_length=32)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="demo_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "demo_session"},
        ),
    ]
