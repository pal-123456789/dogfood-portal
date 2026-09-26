# src/events/migrations/0001_initial.py
"""Initial T1 core graph: Event, EventMembership, Track, Team, TeamMember, BootstrapState.

Hand-authored to match events/models.py byte-for-byte; the docker build's
`makemigrations --check --dry-run` is the authority and will fail the image if this
migration and the model disagree. Operation order creates each FK target before it is
referenced (Event first; Team before TeamMember).
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
            name="Event",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=200)),
                ("state", models.CharField(choices=[("setup", "setup"), ("open", "open"), ("closed", "closed")], default="setup", max_length=16)),
                ("submissions_close", models.DateTimeField()),
                ("results_published", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "event"},
        ),
        migrations.CreateModel(
            name="Track",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=200)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="tracks", to="events.event")),
            ],
            options={"db_table": "track"},
        ),
        migrations.CreateModel(
            name="Team",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("ext_id", models.CharField(max_length=64, unique=True)),
                ("name", models.CharField(max_length=200)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="teams", to="events.event")),
            ],
            options={"db_table": "team"},
        ),
        migrations.CreateModel(
            name="BootstrapState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=64, unique=True)),
                ("version", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={"db_table": "bootstrap_state"},
        ),
        migrations.CreateModel(
            name="EventMembership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("organizer", "organizer"), ("judge", "judge"), ("participant", "participant")], max_length=16)),
                ("ext_id", models.CharField(blank=True, default="", max_length=64)),
                ("event", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="memberships", to="events.event")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="event_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "event_membership"},
        ),
        migrations.CreateModel(
            name="TeamMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("team", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="members", to="events.team")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="team_memberships", to=settings.AUTH_USER_MODEL)),
            ],
            options={"db_table": "team_member"},
        ),
        migrations.AddConstraint(
            model_name="eventmembership",
            constraint=models.UniqueConstraint(fields=("user", "event", "role"), name="uniq_user_event_role"),
        ),
        migrations.AddConstraint(
            model_name="teammember",
            constraint=models.UniqueConstraint(fields=("team", "user"), name="uniq_team_user"),
        ),
    ]
