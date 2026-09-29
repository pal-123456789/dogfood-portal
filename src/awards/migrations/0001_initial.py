# src/awards/migrations/0001_initial.py
"""Initial prizes/awards schema, hand-authored to match awards/models.py field-for-field.

Repo convention: migrations are written by hand and the docker build's
`makemigrations --check --dry-run` is the authoritative agreement test between this file and the
models (it needs no database). Prize has no user FK, so this depends only on events/submissions
0001 (Event, Track, Submission). Field order mirrors what makemigrations emits: the auto id, then
the concrete fields in declaration order, then the relational fields (event, track,
awarded_submission). There is no cross-event constraint here -- that invariant lives in
awards.services -- so this migration is a single CreateModel with no AddConstraint.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("events", "0001_initial"),
        ("submissions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Prize",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=200)),
                ("description", models.TextField(blank=True, default="")),
                ("position", models.PositiveSmallIntegerField(default=1)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="prizes", to="events.event")),
                ("track", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="prizes", to="events.track")),
                ("awarded_submission", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="prizes", to="submissions.submission")),
            ],
            options={"db_table": "prize", "ordering": ["event_id", "track_id", "position", "id"]},
        ),
    ]
