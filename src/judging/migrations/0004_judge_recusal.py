# src/judging/migrations/0004_judge_recusal.py
"""Judge<->team recusal (conflict of interest) table.

Hand-authored to match judging/models.py.JudgeRecusal. A recusal records that a judge will not
review a given team's submissions; the auto-assignment planner (judging/assignment.py, via the
expansion in judging/recusal.py) skips that team's projects for that judge. This is ADDITIVE to the
automatic own-team exclusion the planner already derives from TeamMember -- it captures a COI with a
team the judge is not a member of.

Non-destructive: it only CREATES a new table (no change to any existing table, no data migration,
and nothing on the acceptance checker's five flat routes), so it cannot move replay 7/7 or any
signed normalization hash. UNIQUE(judge, team) makes a recusal idempotent -- filing the same pair
twice is refused at the DB layer.
"""
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0001_initial"),
        ("judging", "0003_rubric_weight_nonneg"),
    ]

    operations = [
        migrations.CreateModel(
            name="JudgeRecusal",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reason", models.CharField(blank=True, default="", max_length=200)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="judge_recusals", to="events.event")),
                ("judge", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="recusals", to="events.eventmembership")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="judge_recusals", to="events.team")),
            ],
            options={"db_table": "judge_recusal"},
        ),
        migrations.AddConstraint(
            model_name="judgerecusal",
            constraint=models.UniqueConstraint(fields=("judge", "team"), name="uniq_judge_recusal"),
        ),
    ]
