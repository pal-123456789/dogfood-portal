# src/voting/migrations/0001_initial.py
"""Initial community-voting schema, hand-authored to match voting/models.py field-for-field.

Repo convention: migrations are written by hand and the docker build's
`makemigrations --check --dry-run` is the authoritative agreement test between this file and the
models (it needs no database). Operation order creates each FK target before it is referenced
(VotingCampaign first; Voter and VoteToken before the rows that point at them). Depends on
events/submissions 0001 (Event, Submission) and the swappable AUTH_USER_MODEL (Voter.user).
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("events", "0001_initial"),
        ("submissions", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="VotingCampaign",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("mode", models.CharField(choices=[("authenticated", "authenticated"), ("email_link", "email link"), ("email_gated", "email gated")], default="authenticated", max_length=16)),
                ("credit_budget", models.PositiveIntegerField(default=25)),
                ("opens_at", models.DateTimeField()),
                ("closes_at", models.DateTimeField()),
                ("results_published", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("event", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name="voting_campaign", to="events.event")),
            ],
            options={"db_table": "voting_campaign"},
        ),
        migrations.CreateModel(
            name="EligibleVoter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.EmailField(max_length=254)),
                ("email_normalized", models.CharField(max_length=254)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="eligible_voters", to="voting.votingcampaign")),
            ],
            options={"db_table": "voting_eligible_voter"},
        ),
        # CHUNK_MIGRATION_1
        migrations.CreateModel(
            name="VoteToken",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token", models.CharField(max_length=64, unique=True)),
                ("email", models.EmailField(max_length=254)),
                ("email_normalized", models.CharField(max_length=254)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("confirmed_at", models.DateTimeField(blank=True, null=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tokens", to="voting.votingcampaign")),
            ],
            options={"db_table": "voting_token"},
        ),
        migrations.CreateModel(
            name="Voter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("email", models.EmailField(blank=True, default="", max_length=254)),
                ("email_normalized", models.CharField(blank=True, default="", max_length=254)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="voters", to="voting.votingcampaign")),
                ("user", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name="voting_identities", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "voting_voter"},
        ),
        migrations.CreateModel(
            name="VoteAllocation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("votes", models.PositiveIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("submission", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="vote_allocations", to="submissions.submission")),
                ("voter", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="allocations", to="voting.voter")),
            ],
            options={"db_table": "voting_allocation"},
        ),
        migrations.CreateModel(
            name="OutboundEmail",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("to", models.EmailField(max_length=254)),
                ("subject", models.CharField(max_length=200)),
                ("body", models.TextField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("campaign", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="emails", to="voting.votingcampaign")),
                ("token", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="emails", to="voting.votetoken")),
            ],
            options={"db_table": "voting_outbound_email", "ordering": ["-id"]},
        ),
        migrations.AddConstraint(
            model_name="eligiblevoter",
            constraint=models.UniqueConstraint(fields=("campaign", "email_normalized"), name="uniq_eligible_campaign_email"),
        ),
        migrations.AddConstraint(
            model_name="voter",
            constraint=models.UniqueConstraint(condition=models.Q(("user__isnull", False)), fields=("campaign", "user"), name="uniq_voter_campaign_user"),
        ),
        migrations.AddConstraint(
            model_name="voter",
            constraint=models.UniqueConstraint(condition=models.Q(("email_normalized", ""), _negated=True), fields=("campaign", "email_normalized"), name="uniq_voter_campaign_email"),
        ),
        migrations.AddConstraint(
            model_name="voteallocation",
            constraint=models.UniqueConstraint(fields=("voter", "submission"), name="uniq_allocation_voter_submission"),
        ),
    ]
