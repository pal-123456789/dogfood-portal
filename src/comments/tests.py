# src/comments/tests.py
"""DB-backed tests for the project-comments app (run via `manage.py test`).

Drive the real service + view + URL stack under /comments/: public JSON read, the authenticated
POST (atomic + audited), body validation, the no-PII author rendering, organizer soft-moderation
(hide -> gone from the public read), the non-organizer/anonymous moderation gates, and the
per-user `comment_write` throttle. None of the five flat checker routes or base.html are exercised.

CACHES is overridden to LocMemCache only where the throttle is under test (the test DB has no cache
table); elsewhere the limiter fails open, so a bare POST is allowed. DOGFOOD_RATE_LIMITS is
overridden so `comment_write` is present regardless of how the deployment wires it.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import caches
from django.test import TestCase, override_settings
from django.utils import timezone

from audit.models import AuditEvent
from comments.models import Comment
from events.models import Event, EventMembership, Team, Track
from submissions.models import Submission

User = get_user_model()

_LOCMEM = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                       "LOCATION": "comments-tests"}}
_LIMITS = {"login": "10/m", "invite_redeem": "20/h", "submission_write": "60/h",
           "ballot_write": "120/h", "comment_write": "60/h"}


@override_settings(DOGFOOD_RATE_LIMITS=_LIMITS)
class ProjectCommentsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_c", name="C", state=Event.OPEN,
            submissions_close=timezone.now() + timedelta(days=1))
        cls.track = Track.objects.create(ext_id="trk_c", event=cls.event, name="Main")
        cls.team = Team.objects.create(ext_id="tm_c", event=cls.event, name="T")
        cls.sub = Submission.objects.create(
            ext_id="prj_c", event=cls.event, team=cls.team, track=cls.track,
            title="Alpha", state=Submission.SUBMITTED)
        cls.author = User.objects.create_user(
            email="author@t.demo", password="pw", display_name="Ada")
        cls.organizer = User.objects.create_user(
            email="org@t.demo", password="pw", display_name="Org")
        EventMembership.objects.create(user=cls.organizer, event=cls.event,
                                       role=EventMembership.ORGANIZER, ext_id="org_c")
        cls.outsider = User.objects.create_user(
            email="out@t.demo", password="pw", display_name="Out")

    def _url(self, ext="prj_c"):
        return "/comments/projects/%s" % ext

    def _moderate_url(self, ext):
        return "/comments/%s/moderate" % ext

    # -- GET (public, anonymous allowed) -------------------------------------
    def test_get_is_public_and_returns_empty_list(self):
        resp = self.client.get(self._url())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"comments": []})

    def test_unknown_project_is_404_on_get_and_post(self):
        self.assertEqual(self.client.get(self._url("prj_nope")).status_code, 404)
        self.client.force_login(self.author)
        self.assertEqual(self.client.post(self._url("prj_nope"), {"body": "x"}).status_code, 404)

    # -- POST auth + validation ----------------------------------------------
    def test_anonymous_post_is_401_and_writes_nothing(self):
        resp = self.client.post(self._url(), {"body": "hello"})
        self.assertEqual(resp.status_code, 401)
        self.assertFalse(Comment.objects.exists())

    def test_authenticated_post_creates_row_and_audits_without_pii(self):
        self.client.force_login(self.author)
        before = AuditEvent.objects.count()
        resp = self.client.post(self._url(), {"body": "  great project  "})
        self.assertEqual(resp.status_code, 201)
        data = resp.json()
        self.assertTrue(data["ext_id"].startswith("cmt_"))
        self.assertEqual(data["body"], "great project")        # stripped
        self.assertEqual(data["author"], "Ada")                # display name...
        self.assertNotIn("@", data["author"])                  # ...never the email (no PII)
        c = Comment.objects.get(ext_id=data["ext_id"])
        self.assertEqual((c.submission_id, c.author_id, c.hidden), (self.sub.id, self.author.id, False))
        self.assertTrue(AuditEvent.objects.filter(
            event_type="comment.posted", object_id=c.ext_id).exists())
        self.assertEqual(AuditEvent.objects.count(), before + 1)

    def test_empty_body_is_400_and_writes_nothing(self):
        self.client.force_login(self.author)
        resp = self.client.post(self._url(), {"body": "   "})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(Comment.objects.exists())

    # -- GET excludes hidden --------------------------------------------------
    def test_public_get_excludes_hidden_comments(self):
        Comment.objects.create(
            ext_id="cmt_vis", submission=self.sub, author=self.author, body="shown")
        Comment.objects.create(
            ext_id="cmt_hid", submission=self.sub, author=self.author, body="secret", hidden=True)
        rows = self.client.get(self._url()).json()["comments"]
        self.assertEqual([r["ext_id"] for r in rows], ["cmt_vis"])
        self.assertNotIn("secret", [r["body"] for r in rows])

    # -- moderation -----------------------------------------------------------
    def test_organizer_hide_succeeds_and_drops_from_public_get(self):
        self.client.force_login(self.author)
        ext = self.client.post(self._url(), {"body": "to be hidden"}).json()["ext_id"]
        self.assertIn("to be hidden",
                      [r["body"] for r in self.client.get(self._url()).json()["comments"]])
        self.client.force_login(self.organizer)
        before = AuditEvent.objects.count()
        resp = self.client.post(self._moderate_url(ext), {"action": "hide"})
        self.assertEqual(resp.status_code, 200)
        c = Comment.objects.get(ext_id=ext)
        self.assertTrue(c.hidden)                              # soft state; the row survives
        self.assertTrue(AuditEvent.objects.filter(
            event_type="comment.hidden", object_id=ext).exists())
        self.assertEqual(AuditEvent.objects.count(), before + 1)
        self.assertNotIn("to be hidden",
                         [r["body"] for r in self.client.get(self._url()).json()["comments"]])

    def test_non_organizer_moderate_is_403_and_stays_visible(self):
        self.client.force_login(self.author)
        ext = self.client.post(self._url(), {"body": "keep"}).json()["ext_id"]
        self.client.force_login(self.outsider)
        self.assertEqual(self.client.post(self._moderate_url(ext), {"action": "hide"}).status_code, 403)
        self.assertFalse(Comment.objects.get(ext_id=ext).hidden)

    def test_anonymous_moderate_is_401(self):
        Comment.objects.create(ext_id="cmt_a", submission=self.sub, author=self.author, body="x")
        self.assertEqual(self.client.post(self._moderate_url("cmt_a"), {"action": "hide"}).status_code, 401)
        self.assertFalse(Comment.objects.get(ext_id="cmt_a").hidden)


@override_settings(CACHES=_LOCMEM, DOGFOOD_RATE_LIMITS=dict(_LIMITS, comment_write="2/h"))
class CommentThrottleTests(TestCase):
    """A real logged-in user over `comment_write` gets 429 + Retry-After (the checker path, marked
    by request.demo_shim, is exempt -- covered by the submit()/redeem() throttle tests)."""

    @classmethod
    def setUpTestData(cls):
        cls.event = Event.objects.create(
            ext_id="evt_rl", name="RL", state=Event.OPEN,
            submissions_close=timezone.now() + timedelta(days=1))
        cls.track = Track.objects.create(ext_id="trk_rl", event=cls.event, name="M")
        cls.team = Team.objects.create(ext_id="tm_rl", event=cls.event, name="T")
        cls.sub = Submission.objects.create(
            ext_id="prj_rl", event=cls.event, team=cls.team, track=cls.track,
            title="RL", state=Submission.SUBMITTED)
        cls.user = User.objects.create_user(email="rl@t.demo", password="pw", display_name="RL")

    def setUp(self):
        caches["default"].clear()

    def test_real_user_is_throttled_after_the_limit(self):
        self.client.force_login(self.user)
        url = "/comments/projects/%s" % self.sub.ext_id
        codes = [self.client.post(url, {"body": "c%d" % i}).status_code for i in range(3)]
        self.assertEqual(codes[:2], [201, 201])                # 2/h allows two
        self.assertEqual(codes[2], 429)                        # the third is refused
        self.assertIn("Retry-After", self.client.post(url, {"body": "again"}))
