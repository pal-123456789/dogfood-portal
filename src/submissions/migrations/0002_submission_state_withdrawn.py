# src/submissions/migrations/0002_submission_state_withdrawn.py
"""Add the "withdrawn" choice to Submission.state.

This is a choices-only AlterField: it changes validation/label metadata, not the
column type (still varchar(16)), so it emits no DDL and is a no-op against existing
rows. Withdrawal is modelled as a soft state, never a row delete, so a withdrawn
project keeps its JudgeAssignment -> Ballot -> BallotRevision history and audit trail.
Hand-authored to match submissions/models.py; `manage.py makemigrations --check`
(run at image-build time) proves the model and this migration agree.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("submissions", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="submission",
            name="state",
            field=models.CharField(
                choices=[("draft", "draft"), ("submitted", "submitted"), ("withdrawn", "withdrawn")],
                default="draft",
                max_length=16,
            ),
        ),
    ]
