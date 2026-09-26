# src/submissions/migrations/0001_initial.py
"""Initial Submission table. No unique(team, track, title) constraint — the planted
within-track duplicate (prj_07 & prj_41) must import cleanly; duplicate control is a
normalizer diagnostic, not a DB guard. Hand-authored to match submissions/models.py.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("events", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Submission",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("title", models.CharField(max_length=200)),
                ("summary", models.TextField(blank=True, default="")),
                ("repo_url", models.URLField(blank=True, default="", max_length=500)),
                ("state", models.CharField(choices=[("draft", "draft"), ("submitted", "submitted")], default="draft", max_length=16)),
                ("submitted_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="submissions", to="events.event")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="submissions", to="events.team")),
                ("track", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="submissions", to="events.track")),
            ],
            options={"db_table": "submission", "ordering": ["id"]},
        ),
    ]
