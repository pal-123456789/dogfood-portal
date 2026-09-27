# src/judging/services.py
"""Judging read/write helpers.

Two invariants live here, not in the views: the score bound (integers 1..5) is enforced in
record_ballot, and "who may read a ballot" is decided by ASSIGNMENT OWNERSHIP, not by any
URL parameter. Checks 4/5/6 are the same endpoint differing only by caller identity, so the
ownership rule must be a property of the data layer.
"""
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from events.models import EventMembership

from audit import service as audit_service

from .models import Ballot, BallotRevision, JudgeAssignment

SCORE_MIN, SCORE_MAX = 1, 5


def judge_membership(user, event):
    """The caller's judge membership for this event, or None if they are not a judge."""
    if not (user and user.is_authenticated):
        return None
    return EventMembership.objects.filter(
        user=user, event=event, role=EventMembership.JUDGE).first()


def scores_for_judge(membership):
    """The caller's OWN ballots as serializable rows (check 4)."""
    qs = (Ballot.objects
          .filter(assignment__judge=membership)
          .select_related("assignment__submission", "assignment__submission__track")
          .order_by("assignment__submission__ext_id"))
    return [{
        "submission": b.assignment.submission.ext_id,
        "title": b.assignment.submission.title,
        "track": b.assignment.submission.track.ext_id,
        "functionality": b.functionality,
        "quality": b.quality,
        "innovation": b.innovation,
        "comment": b.comment,
    } for b in qs]


def record_ballot(membership, submission, *, functionality, quality, innovation, comment=""):
    """Append the caller's score for a submission as an immutable revision; enforce 1..5.

    Not on the acceptance checker's path (its T2 is read-only), but it is the single writer
    the DB CheckConstraints mirror, so the rule is stated once here. Three things happen in
    ONE transaction so none can exist without the others: the Ballot row is updated to the
    latest score (a denormalized read-pointer, kept byte-stable for existing readers), a
    write-once BallotRevision is appended (UNIQUE(ballot, version) is the append-only
    backstop; the audit-head lock in record_event already serializes the common path), and a
    tamper-evident `ballot.recorded` audit event carrying that version is chained. A score can
    never be persisted without both its history row and its audit row, nor vice versa.
    """
    for name, val in (("functionality", functionality),
                      ("quality", quality), ("innovation", innovation)):
        try:
            ival = int(val)
        except (TypeError, ValueError):
            raise ValidationError("%s must be an integer 1..5" % name)
        if not (SCORE_MIN <= ival <= SCORE_MAX):
            raise ValidationError("%s must be %d..%d" % (name, SCORE_MIN, SCORE_MAX))
    with transaction.atomic():
        assignment, _ = JudgeAssignment.objects.get_or_create(
            judge=membership, submission=submission)
        ballot, _ = Ballot.objects.update_or_create(
            assignment=assignment,
            defaults=dict(functionality=int(functionality), quality=int(quality),
                          innovation=int(innovation), comment=comment))
        version = (ballot.revisions.aggregate(m=Max("version"))["m"] or 0) + 1
        BallotRevision.objects.create(
            ballot=ballot, version=version,
            functionality=int(functionality), quality=int(quality),
            innovation=int(innovation), comment=comment)
        audit_service.record_event(
            event_type="ballot.recorded", object_type="ballot",
            object_id="%s:%s" % (membership.ext_id or membership.pk, submission.ext_id),
            actor_user_id=membership.user_id, actor_membership_id=membership.ext_id,
            payload={"submission": submission.ext_id, "judge": membership.ext_id,
                     "version": version,
                     "functionality": int(functionality), "quality": int(quality),
                     "innovation": int(innovation)})
    return ballot


# CSV formula-injection guard (CWE-1236). A spreadsheet evaluates any cell whose first
# character is one of these, so an attacker-controlled title or comment like
# `=HYPERLINK("http://evil","click")` would run when an organizer opens export.csv. csv.writer
# quotes delimiters but does NOT neutralise formulas, so we prefix such cells with an
# apostrophe -- shown as-is by spreadsheets, but no longer parsed as a formula. Data cells
# only; the header row is static and check 7 only needs commas on line 1, which is unaffected.
_CSV_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value):
    """Neutralise one cell against spreadsheet-formula injection; non-strings pass through."""
    if isinstance(value, str) and value[:1] in _CSV_FORMULA_LEAD:
        return "'" + value
    return value


def export_event_rows(event):
    """Rows for the organizer CSV export; header first so line 1 always has commas (check 7)."""
    rows = [["submission", "title", "team", "track", "judge",
             "functionality", "quality", "innovation", "comment"]]
    qs = (Ballot.objects
          .filter(assignment__submission__event=event)
          .select_related("assignment__submission", "assignment__submission__team",
                          "assignment__submission__track", "assignment__judge")
          .order_by("assignment__submission__ext_id", "assignment__judge__ext_id"))
    for b in qs:
        s = b.assignment.submission
        rows.append([_csv_safe(v) for v in (
            s.ext_id, s.title, s.team.ext_id, s.track.ext_id,
            b.assignment.judge.ext_id,
            b.functionality, b.quality, b.innovation, b.comment)])
    return rows
