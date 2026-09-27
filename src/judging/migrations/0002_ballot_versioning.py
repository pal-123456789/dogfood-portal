# src/judging/migrations/0002_ballot_versioning.py
"""Append-only ballot versioning + the deferred 1..5 DB bound.

Adds BallotRevision (immutable, write-once per (ballot, version)) and, at last, the DB
CheckConstraint the 0001 docstring deferred — now on BOTH Ballot and BallotRevision, so a
score outside 1..5 cannot be persisted even by a writer that bypasses record_ballot. Then
backfills a v1 revision for every pre-existing Ballot so the "every ballot has >=1 revision"
invariant holds on an upgraded DB (a freshly-seeded DB gets its v1 rows from dogfood_import).
Hand-authored to match judging/models.py; non-destructive (only adds a table + constraints).
"""
from django.db import migrations, models
import django.db.models.deletion


def backfill_v1(apps, schema_editor):
    Ballot = apps.get_model("judging", "Ballot")
    BallotRevision = apps.get_model("judging", "BallotRevision")
    rows = [
        BallotRevision(ballot_id=b.id, version=1, functionality=b.functionality,
                       quality=b.quality, innovation=b.innovation, comment=b.comment)
        for b in Ballot.objects.all().iterator()
    ]
    if rows:
        BallotRevision.objects.bulk_create(rows, batch_size=500)


class Migration(migrations.Migration):

    dependencies = [
        ("judging", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="BallotRevision",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("version", models.PositiveIntegerField()),
                ("functionality", models.PositiveSmallIntegerField()),
                ("quality", models.PositiveSmallIntegerField()),
                ("innovation", models.PositiveSmallIntegerField()),
                ("comment", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("ballot", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="revisions", to="judging.ballot")),
            ],
            options={"db_table": "ballot_revision"},
        ),
        migrations.AddConstraint(
            model_name="ballotrevision",
            constraint=models.UniqueConstraint(fields=("ballot", "version"), name="uniq_ballot_version"),
        ),
        migrations.AddConstraint(
            model_name="ballotrevision",
            constraint=models.CheckConstraint(
                condition=(models.Q(functionality__range=(1, 5))
                           & models.Q(quality__range=(1, 5))
                           & models.Q(innovation__range=(1, 5))),
                name="ck_ballotrevision_scores_1_5"),
        ),
        migrations.AddConstraint(
            model_name="ballot",
            constraint=models.CheckConstraint(
                condition=(models.Q(functionality__range=(1, 5))
                           & models.Q(quality__range=(1, 5))
                           & models.Q(innovation__range=(1, 5))),
                name="ck_ballot_scores_1_5"),
        ),
        migrations.RunPython(backfill_v1, migrations.RunPython.noop),
    ]
