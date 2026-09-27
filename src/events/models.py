# src/events/models.py
"""Event, membership, track, team — the T1 core graph.

Roles are EVENT-SCOPED (EventMembership), never global flags on the user: the same
person can organize one event and judge another. `ext_id` mirrors the fixture's string
ids (evt_01, trk_01, tm_01, jdg_01) so the importer can wire FKs and stay idempotent.
"""
from django.conf import settings
from django.db import models


class Event(models.Model):
    SETUP, OPEN, CLOSED = "setup", "open", "closed"
    STATES = [(SETUP, "setup"), (OPEN, "open"), (CLOSED, "closed")]

    ext_id = models.CharField(max_length=64, unique=True)
    name = models.CharField(max_length=200)
    state = models.CharField(max_length=16, choices=STATES, default=SETUP)
    submissions_close = models.DateTimeField()
    results_published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "event"

    def __str__(self):
        return self.name

    def accepting_submissions(self, now):
        """One canonical deadline rule, evaluated on server time."""
        return self.state == self.OPEN and now < self.submissions_close


class EventMembership(models.Model):
    ORGANIZER, JUDGE, PARTICIPANT = "organizer", "judge", "participant"
    ROLES = [(ORGANIZER, "organizer"), (JUDGE, "judge"), (PARTICIPANT, "participant")]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="event_memberships")
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=ROLES)
    ext_id = models.CharField(max_length=64, blank=True, default="")  # judge id e.g. jdg_01

    class Meta:
        db_table = "event_membership"
        constraints = [
            models.UniqueConstraint(fields=["user", "event", "role"],
                                    name="uniq_user_event_role"),
        ]

    def __str__(self):
        return "%s@%s:%s" % (self.user_id, self.event_id, self.role)


class Track(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="tracks")
    name = models.CharField(max_length=200)

    class Meta:
        db_table = "track"

    def __str__(self):
        return self.name


class Team(models.Model):
    ext_id = models.CharField(max_length=64, unique=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="teams")
    name = models.CharField(max_length=200)

    class Meta:
        db_table = "team"

    def __str__(self):
        return self.name


class TeamMember(models.Model):
    team = models.ForeignKey(Team, on_delete=models.CASCADE, related_name="members")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="team_memberships")

    class Meta:
        db_table = "team_member"
        constraints = [
            models.UniqueConstraint(fields=["team", "user"], name="uniq_team_user"),
        ]

    def __str__(self):
        return "%s in %s" % (self.user_id, self.team_id)


class BootstrapState(models.Model):
    """Seed marker. `--if-empty` is the real idempotency guard (§4g); this row is the
    human-readable receipt that a seed of a given version ran."""
    key = models.CharField(max_length=64, unique=True)
    version = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "bootstrap_state"

    def __str__(self):
        return "%s=%s" % (self.key, self.version)


class Invite(models.Model):
    """A signed, single-use invitation to join an event in a role (§20).

    An organizer mints one; the redeem link carries an Ed25519 signature over the invite's
    canonical fields (events/invite_signing.py, tag dogfood.invite.v1), signed with the SAME
    /state key as the audit spine, so anyone with the deployment's public key can verify it and
    only the key-holder can forge one. Single use is enforced in the DB, not the signature:
    `redeemed_at` flips from NULL under `select_for_update` inside the redeem transaction, so two
    concurrent redemptions cannot both create a membership. Roles are limited to JUDGE/PARTICIPANT
    -- organizer is only ever granted by create_event to the creator, so a shared link can never
    escalate to organizer. The row is never deleted on redeem; it is the audit-linked receipt that
    a specific person consumed it.
    """
    JUDGE, PARTICIPANT = EventMembership.JUDGE, EventMembership.PARTICIPANT
    ROLES = [(JUDGE, "judge"), (PARTICIPANT, "participant")]

    ext_id = models.CharField(max_length=64, unique=True)
    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="invites")
    role = models.CharField(max_length=16, choices=ROLES)
    signature = models.CharField(max_length=128)  # hex Ed25519 sig (64 raw bytes -> 128 hex)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                   related_name="invites_created")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    redeemed_at = models.DateTimeField(null=True, blank=True)
    redeemed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name="invites_redeemed")

    class Meta:
        db_table = "invite"

    def __str__(self):
        return "%s:%s" % (self.ext_id, self.role)

    @property
    def is_redeemed(self):
        return self.redeemed_at is not None

    def is_expired(self, now):
        return self.expires_at is not None and now >= self.expires_at
