# src/judging/services.py
"""Judging read/write helpers.

Two invariants live here, not in the views: the score bound (integers 1..5) is enforced in
record_ballot, and "who may read a ballot" is decided by ASSIGNMENT OWNERSHIP, not by any
URL parameter. Checks 4/5/6 are the same endpoint differing only by caller identity, so the
ownership rule must be a property of the data layer.
"""
import math

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Max

from events.models import EventMembership, TeamMember, Track

from audit import service as audit_service
from submissions.models import Submission

from . import assignment
from .models import Ballot, BallotRevision, JudgeAssignment, JudgeRecusal, RubricWeight
from .recusal import expand_recusals

SCORE_MIN, SCORE_MAX = 1, 5
# Rubric criteria in engine.CRITERIA order. Declared here (not imported from normalize) so the
# dependency arrow stays judging -> (events, submissions, audit); normalize depends on judging.
CRITERIA = ("functionality", "quality", "innovation")


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


def assigned_submissions(membership):
    """The judge's assigned submissions with their current Ballot (or None), for the scoring
    queue. A judge scores only what they are assigned to, so this queryset is also the set of
    submissions the score endpoint will accept a write for."""
    qs = (JudgeAssignment.objects
          .filter(judge=membership)
          .select_related("submission", "submission__track", "ballot")
          .order_by("submission__ext_id"))
    rows = []
    for a in qs:
        try:
            ballot = a.ballot                      # reverse OneToOne; may not exist yet
        except Ballot.DoesNotExist:
            ballot = None
        rows.append({"submission": a.submission, "ballot": ballot})
    return rows


def assignment_for(membership, submission_ext_id):
    """The judge's assignment for this submission ext_id, or None if it is not assigned to them.
    Ownership is a property of the data (an existing JudgeAssignment row), never of the URL."""
    if not submission_ext_id:
        return None
    return (JudgeAssignment.objects
            .filter(judge=membership, submission__ext_id=submission_ext_id)
            .select_related("submission").first())


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

    A ballot must also never span two events: the submission has to belong to the judge
    membership's own event. assign_judge already refuses a cross-event pairing, and the score
    view only ever reaches an already-assigned submission, but enforcing the invariant here --
    at the single score writer -- means no caller can create a cross-event JudgeAssignment that
    the membership-scoped scores_for_judge read would then trust.
    """
    if submission.event_id != membership.event_id:
        raise ValidationError("submission does not belong to this judge's event")
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


# --- Organizer control room -------------------------------------------------------------------
# Organizer-scoped, event-scoped helpers behind /judging/<event>/... : judging-progress coverage
# (read-only), judge-assignment management, and rubric-weight configuration. None sit on the
# acceptance checker's five flat routes, and none add a model or field, so they need no migration
# and cannot move replay 7/7. Every WRITE is atomic and co-commits an audit event, exactly like
# record_ballot and events.services -- extending the tamper-evident trail to judging configuration.


def event_judges(event):
    """JUDGE memberships for this event, ext_id-ordered (stable, legible order for the UI)."""
    return list(EventMembership.objects.filter(event=event, role=EventMembership.JUDGE)
                .select_related("user").order_by("ext_id", "id"))


def event_submissions(event):
    """Submissions for this event, oldest-first (prj_01, prj_02, ...)."""
    return list(Submission.objects.filter(event=event)
                .select_related("track", "team").order_by("id"))


def _scored_assignment_ids(event):
    """The set of JudgeAssignment ids that already carry a Ballot (a recorded score)."""
    return set(Ballot.objects
               .filter(assignment__submission__event=event)
               .values_list("assignment_id", flat=True))
# SENTINEL_CONTROL_ROOM


def judging_progress(event):
    """Read-only coverage snapshot for the organizer: per-judge and per-submission
    assigned/scored/pending counts, each submission's assigned judges (with a scored flag), and
    event totals. Pure ORM aggregation over existing rows -- it writes nothing and touches no
    checker route."""
    judges = event_judges(event)
    submissions = event_submissions(event)
    assignments = list(JudgeAssignment.objects
                       .filter(submission__event=event)
                       .select_related("judge", "submission"))
    scored_ids = _scored_assignment_ids(event)

    by_judge = {m.id: {"judge": m, "assigned": 0, "scored": 0} for m in judges}
    by_sub = {s.id: {"submission": s, "assigned": 0, "scored": 0,
                     "judges": [], "pending_judges": []} for s in submissions}
    for a in assignments:
        scored = a.id in scored_ids
        jext = a.judge.ext_id or str(a.judge_id)
        jrow = by_judge.get(a.judge_id)
        if jrow is not None:
            jrow["assigned"] += 1
            jrow["scored"] += 1 if scored else 0
        srow = by_sub.get(a.submission_id)
        if srow is not None:
            srow["assigned"] += 1
            srow["judges"].append({"ext_id": jext, "scored": scored})
            if scored:
                srow["scored"] += 1
            else:
                srow["pending_judges"].append(jext)

    judge_rows = []
    for m in judges:
        d = by_judge[m.id]
        d["pending"] = d["assigned"] - d["scored"]
        judge_rows.append(d)
    sub_rows = []
    for s in submissions:
        d = by_sub[s.id]
        d["pending"] = d["assigned"] - d["scored"]
        d["status"] = ("uncovered" if d["assigned"] == 0
                       else "complete" if d["scored"] >= d["assigned"] else "partial")
        sub_rows.append(d)

    n_assign = len(assignments)
    n_scored = len(scored_ids)
    return {
        "event": event, "judge_rows": judge_rows, "sub_rows": sub_rows,
        "totals": {
            "judges": len(judges), "submissions": len(submissions),
            "assignments": n_assign, "scored": n_scored, "pending": n_assign - n_scored,
            "uncovered": sum(1 for d in sub_rows if d["status"] == "uncovered"),
            "complete": sum(1 for d in sub_rows if d["status"] == "complete"),
            "pct_scored": round(100.0 * n_scored / n_assign, 1) if n_assign else 0.0,
        },
    }
# SENTINEL_CONTROL_ROOM_2


def _judge_membership_in_event(event, judge_ext_id):
    """A JUDGE membership of THIS event by ext_id, or None. Scoping is a DB fact, never trusted
    from the form: a judge ext_id from another event simply does not match this filter."""
    if not judge_ext_id:
        return None
    return (EventMembership.objects
            .filter(event=event, role=EventMembership.JUDGE, ext_id=judge_ext_id)
            .select_related("user").first())


def assign_judge(actor, event, *, judge_ext_id, submission_ext_id):
    """Assign a judge (by membership ext_id) to a submission, both scoped to `event`. Atomic +
    audited ('judge.assigned'). Idempotent via get_or_create: re-assigning an existing pair writes
    nothing and emits no second audit event, so a double-submit never trips uniq_judge_submission."""
    membership = _judge_membership_in_event(event, judge_ext_id)
    if membership is None:
        raise ValidationError("Select a judge of this event.")
    submission = Submission.objects.filter(event=event, ext_id=submission_ext_id or "").first()
    if submission is None:
        raise ValidationError("Select a submission of this event.")
    with transaction.atomic():
        assignment, created = JudgeAssignment.objects.get_or_create(
            judge=membership, submission=submission)
        if created:
            audit_service.record_event(
                event_type="judge.assigned", object_type="judge_assignment",
                object_id="%s:%s" % (membership.ext_id or membership.pk, submission.ext_id),
                actor_user_id=getattr(actor, "pk", ""),
                payload={"event": event.ext_id, "judge": membership.ext_id,
                         "submission": submission.ext_id})
    return assignment


def unassign_judge(actor, event, *, judge_ext_id, submission_ext_id):
    """Remove an assignment ONLY if no score exists for it. A scored assignment is permanent: its
    Ballot and append-only BallotRevision history (and the audit events referencing them) must
    never be cascade-deleted, so we refuse rather than destroy history. Atomic + audited
    ('judge.unassigned').

    The has-ballot check and the delete are ONE serialized step: we lock the assignment row
    FOR UPDATE (of=self) before checking. A concurrent first-ever record_ballot inserts a Ballot
    that takes an FK KEY SHARE lock on this same row, which conflicts with our FOR UPDATE -- so
    either it waits and we then see its ballot and refuse, or we delete first and its insert fails
    the foreign key. A score can never slip in between our check and our delete and be cascaded away.
    """
    membership = _judge_membership_in_event(event, judge_ext_id)
    if membership is None:
        raise ValidationError("Unknown judge for this event.")
    with transaction.atomic():
        assignment = (JudgeAssignment.objects
                      .select_for_update(of=("self",))
                      .filter(judge=membership, submission__event=event,
                              submission__ext_id=submission_ext_id or "")
                      .select_related("submission").first())
        if assignment is None:
            raise ValidationError("That assignment does not exist.")
        if Ballot.objects.filter(assignment=assignment).exists():
            raise ValidationError(
                "This judge has already scored that submission; a scored assignment cannot be "
                "removed because its score history is append-only.")
        sub_ext = assignment.submission.ext_id
        assignment.delete()
        audit_service.record_event(
            event_type="judge.unassigned", object_type="judge_assignment",
            object_id="%s:%s" % (membership.ext_id or membership.pk, sub_ext),
            actor_user_id=getattr(actor, "pk", ""),
            payload={"event": event.ext_id, "judge": membership.ext_id, "submission": sub_ext})
# SENTINEL_CONTROL_ROOM_3


def current_weights(event):
    """criterion -> weight from the DB (missing => 1.0), in CRITERIA order. Mirrors
    normalize.services.rubric_weights so the editor and the engine agree on the equal-weight
    default; read-only."""
    rows = {rw.criterion: float(rw.weight) for rw in RubricWeight.objects.filter(event=event)}
    return {c: rows.get(c, 1.0) for c in CRITERIA}


def set_rubric_weights(actor, event, *, weights):
    """Set the event's per-criterion rubric weights (organizer-only). Each must be a finite number
    >= 0 and their sum must be > 0 (the engine's composite divides by that sum). Atomic + audited
    ('rubric.reweighted', old -> new).

    Scope of effect (honest): weights are read LIVE only by the organizer leaderboard PREVIEW (the
    in-waiting ranking) and by the NEXT signed run you publish. Official published results are a
    frozen, signed run that pins the exact weights and ballots it consumed; the public results view
    serves that frozen result verbatim and the offline verifier recomputes from the pinned weights,
    not the live ones. So re-weighting can never invalidate a result you have already published.
    """
    old = current_weights(event)
    clean = {}
    for c in CRITERIA:
        try:
            val = float(weights.get(c, ""))
        except (TypeError, ValueError):
            raise ValidationError("%s weight must be a number." % c)
        if not math.isfinite(val) or val < 0:
            raise ValidationError("%s weight must be zero or a positive number." % c)
        clean[c] = val
    if sum(clean.values()) <= 0:
        raise ValidationError("At least one weight must be greater than zero.")
    with transaction.atomic():
        for c in CRITERIA:
            RubricWeight.objects.update_or_create(
                event=event, criterion=c, defaults={"weight": clean[c]})
        audit_service.record_event(
            event_type="rubric.reweighted", object_type="event", object_id=event.ext_id,
            actor_user_id=getattr(actor, "pk", ""),
            payload={"event": event.ext_id, "old": old, "new": clean})
    return clean


# --- Connectivity-aware auto-assignment (organizer-only) --------------------------------------
# A preview/apply layer over the pure planner in judging/assignment.py. It reads the event's judges
# and SUBMITTED projects into the plain dicts the planner consumes, and APPLIES a plan by calling
# assign_judge once per edge -- so every planned assignment is created and audited on the exact same
# path as a manual one (idempotent get_or_create + a single 'judge.assigned' event), never a new
# write path. Model-free: the planner is pure and the writes reuse assign_judge, so no field or
# migration is added and none of the five flat checker routes is touched.


def _assignment_inputs(event):
    """(judges, projects) as the plain dicts judging.assignment.plan_assignments consumes.

    Only the eligibility this schema can express is derived here; the rest the pure planner supports
    generally and is unit-tested for:
      * tracks -- there is no per-judge track table in this schema, so every judge is offered every
        track in the event (the planner still enforces track-matching, tested with mixed tracks);
      * teams  -- a judge's conflict of interest: the teams in THIS event whose members include the
        judge's user, so the planner never assigns a judge to their own team's project;
      * recused -- organizer-declared conflicts of interest: each JudgeRecusal (judge, team) is
        expanded to that team's SUBMITTED submissions (judging/recusal.py) and fed as the judge's
        recusal set, so the planner never assigns a recused judge to that team's projects. This is
        ADDITIVE to the own-team `teams` exclusion above (it covers a COI with a team the judge is
        not a member of).
    Only SUBMITTED projects are planned; drafts and withdrawn projects are not judged."""
    judges = event_judges(event)
    submissions = [s for s in event_submissions(event) if s.state == Submission.SUBMITTED]
    track_ids = list(Track.objects.filter(event=event).values_list("ext_id", flat=True))
    sub_ids = [s.id for s in submissions]
    assigns = (JudgeAssignment.objects
               .filter(submission_id__in=sub_ids)
               .select_related("judge", "submission"))
    held = {}          # judge membership id -> {submission ext_id}
    reviewers = {}     # submission id -> {judge ext_id}
    for a in assigns:
        held.setdefault(a.judge_id, set()).add(a.submission.ext_id)
        reviewers.setdefault(a.submission_id, set()).add(a.judge.ext_id or str(a.judge_id))
    coi_teams = {}     # user id -> {team ext_id} in this event
    for tm in TeamMember.objects.filter(team__event=event).select_related("team"):
        coi_teams.setdefault(tm.user_id, set()).add(tm.team.ext_id)
    # Organizer-declared conflicts of interest (#135): each JudgeRecusal names a (judge, team) the
    # judge must not review. A recusal is filed at TEAM granularity (the COI is with the people, so
    # it must cover that team's future submissions too), while the planner works in submission
    # ext_ids -- so expand each recusal to that team's currently-planned (SUBMITTED) submissions.
    # ADDITIVE to the own-team exclusion in `teams` above: this covers a COI with a team the judge
    # is NOT a member of. The expansion itself is pure/DB-free in judging/recusal.py.
    team_subs = {}     # team ext_id -> [submission ext_id] among the submissions being planned
    for s in submissions:
        team_subs.setdefault(s.team.ext_id, []).append(s.ext_id)
    recused_pairs = (JudgeRecusal.objects.filter(event=event)
                     .values_list("judge__ext_id", "team__ext_id"))
    recused_map = expand_recusals(recused_pairs, team_subs)   # {judge ext_id: {submission ext_id}}
    judges_in = []
    for m in judges:
        if not m.ext_id:
            continue          # assign_judge resolves judges by ext_id; skip a blank-ext_id judge
        judges_in.append({
            "ext_id": m.ext_id,
            "tracks": track_ids,
            "teams": coi_teams.get(m.user_id, set()),
            "assigned": held.get(m.id, set()),
            "recused": recused_map.get(m.ext_id, set()),
            "load": len(held.get(m.id, set())),
        })
    projects_in = [{
        "ext_id": s.ext_id,
        "track": s.track.ext_id,
        "team": s.team.ext_id,
        "reviews": len(reviewers.get(s.id, set())),
        "assigned_judges": reviewers.get(s.id, set()),
    } for s in submissions]
    return judges_in, projects_in


def preview_assignment_plan(event, *, k, seed=0):
    """The plan plus a DB-free summary (per-project coverage, per-judge load, component count) for
    the organizer preview. Reads the event's judges/projects/assignments only; writes nothing."""
    judges, projects = _assignment_inputs(event)
    plan = assignment.plan_assignments(judges, projects, k, seed=seed)
    summary = assignment.summarize_plan(judges, projects, plan, k, seed=seed)
    return plan, summary


def apply_assignment_plan(actor, event, *, k, seed=0):
    """Compute the plan and APPLY it by calling assign_judge once per edge -- each its own atomic,
    audited 'judge.assigned' write, identical to a manual assignment. Idempotent end to end: a
    planned pair that already exists is a no-op (assign_judge's get_or_create), so re-running adds
    nothing new. Returns (created, planned) counts."""
    plan, _summary = preview_assignment_plan(event, k=k, seed=seed)
    created = 0
    for jext, pext in plan:
        existed = JudgeAssignment.objects.filter(
            judge__event=event, judge__ext_id=jext, submission__ext_id=pext).exists()
        assign_judge(actor, event, judge_ext_id=jext, submission_ext_id=pext)
        if not existed:
            created += 1
    return created, len(plan)
