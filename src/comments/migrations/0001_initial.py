# src/comments/migrations/0001_initial.py
"""Initial Comment table (`project_comment`): project comments + a soft-hide moderation flag.

Hand-authored to match comments/models.py::Comment field-for-field (the docker build's
`makemigrations --check --dry-run` is the authority and fails the image if this migration and the
model disagree). A pure CreateModel -- one new table + two foreign keys, touching no existing
table. Depends on the swappable AUTH_USER_MODEL (author) and submissions.0001_initial (submission).
Field order follows Django's autodetector: non-relational fields in declaration order, then the
relational fields alphabetically (author, submission).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("submissions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="Comment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("body", models.TextField()),
                ("hidden", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("author", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="comments_authored", to=settings.AUTH_USER_MODEL)),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="comments", to="submissions.submission")),
            ],
            options={"db_table": "project_comment", "ordering": ["id"]},
        ),
    ]
