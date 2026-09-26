# src/judging/migrations/0001_initial.py
"""Initial judging tables: JudgeAssignment, Ballot, RubricWeight.

Score fields are plain PositiveSmallIntegerField here; the 1..5 bound is enforced in the
record_ballot service and added as a DB CheckConstraint in a later, non-destructive
migration (keeps this initial migration autodetector-clean). Hand-authored to match
judging/models.py.
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
            name="JudgeAssignment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("judge", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignments", to="events.eventmembership")),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="assignments", to="submissions.submission")),
            ],
            options={"db_table": "judge_assignment"},
        ),
        migrations.CreateModel(
            name="Ballot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("functionality", models.PositiveSmallIntegerField()),
                ("quality", models.PositiveSmallIntegerField()),
                ("innovation", models.PositiveSmallIntegerField()),
                ("comment", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("assignment", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="ballot", to="judging.judgeassignment")),
            ],
            options={"db_table": "ballot"},
        ),
        migrations.CreateModel(
            name="RubricWeight",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("criterion", models.CharField(max_length=32)),
                ("weight", models.FloatField(default=1.0)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="rubric_weights", to="events.event")),
            ],
            options={"db_table": "rubric_weight"},
        ),
        migrations.AddConstraint(
            model_name="judgeassignment",
            constraint=models.UniqueConstraint(fields=("judge", "submission"), name="uniq_judge_submission"),
        ),
        migrations.AddConstraint(
            model_name="rubricweight",
            constraint=models.UniqueConstraint(fields=("event", "criterion"), name="uniq_event_criterion"),
        ),
    ]
