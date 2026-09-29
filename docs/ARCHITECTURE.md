# Architecture

DOGFOOD is a single Django 5.2 project served by gunicorn, backed by one PostgreSQL 16 database.
There is no message broker, no Redis, and no separate worker process: caching and rate-limit
counters live in a database table (`DatabaseCache`), and static files are served in-process by
WhiteNoise (`src/portal/settings.py`, `docker-compose.yml`).

## Request lifecycle

```
client
  -> gunicorn (2 workers by default; DOGFOOD_WORKERS)      docker/entrypoint.sh
  -> Django WSGI                                           src/portal/wsgi.py
  -> MIDDLEWARE stack (src/portal/settings.py), incl. two project-specific classes
       DemoAuthMiddleware              src/portal/middleware.py
       ContentSecurityPolicyMiddleware src/portal/middleware.py
  -> URL routing                                          src/portal/urls.py
  -> view                                                 src/<app>/views.py
  -> service layer (transaction.atomic; appends an audit event)
                                                          src/<app>/services.py
  -> response (Content-Security-Policy: script-src 'self' set via setdefault)
```

Two project middlewares are registered (`src/portal/middleware.py`):

- **`DemoAuthMiddleware`** — gated on the `DOGFOOD_DEMO` setting. When enabled, a
  `Cookie: session=<token>` is resolved against the `DemoSession` table; on a hit it sets
  `request.user` to the seeded user, sets `request._dont_enforce_csrf_checks = True`, and marks
  `request.demo_shim = True`. A no-op when the flag is off or the cookie is absent/unknown.
- **`ContentSecurityPolicyMiddleware`** — sets `Content-Security-Policy: script-src 'self'` on every
  response (via `setdefault`, so a view may override it). The one view that does is the Swagger docs
  page (`/api/v1/docs/`), which sets `script-src 'self' 'unsafe-inline'` **scoped to that page** for
  the UI's inline bootstrap `<script>`; the site-wide policy is unchanged (`src/api/views.py`
  `CspSwaggerView`).

Writes go through a **service layer**, not the views. `submissions.services.create_submission` runs
auth → participant → deadline → validation and writes the `Submission` plus an audit event in one
`transaction.atomic()`. The single writer for scores is `judging.services.record_ballot`, which
updates the `Ballot`, appends an immutable `BallotRevision`, and chains a `ballot.recorded` audit
event — all atomically, so none can exist without the others.

## The reachable HTTP surface

The complete routing table is `src/portal/urls.py`. The five flat, un-prefixed routes below are the
acceptance-checker contract and are kept byte-stable. Eleven **prefixed includes** contribute the
rest of the surface — `/accounts/`, `/events/`, `/submissions/`, `/judging/`, `/normalize/`,
`/api/v1/`, `/voting/`, `/comments/`, `/records/`, `/bundles/`, `/webhooks/`. The app-level
`urls.py` for `gallery` is currently empty (`app_name` + `urlpatterns = []`); its views are wired as
flat routes in `portal/urls.py`, as are `embed`'s two. Three further paths are wired **top-level by
contract**: `/.well-known/dogfood-signing-key` is a well-known location (served by
`records.views.signing_key`, which is also reachable at `/records/signing-key`), and `/embed.js` /
`/embed/<ext_id>` are the entry points a third-party page loads. Finally, `awards` is included at the
**root** (`path("", include("awards.urls"))`) and is declared **last**, so its
`events/<event_ext_id>/awards…` patterns are reached only after every earlier pattern has failed to
match — it can shadow nothing. Every include and top-level path beyond the five flat routes is
declared *after* them, each needs a distinct leading segment, and none is linked from `base.html`,
so `tools/replay.py` stays 7/7.

The `/submissions/` routes are participant self-service (edit / withdraw / "mine");
they are reachable by URL but intentionally not linked from `base.html`, so the gallery and nav the
checker renders stay byte-identical. The `/events/invite/<ext_id>` redeem route is the one `events`
route addressed to an invitee rather than the organizer; like `/submissions/`, it is reachable by
URL but not linked from `base.html`. Path placeholders below are the parameter names the URLconf
actually declares.

| Method(s)  | Path                          | View                        | Access |
|------------|-------------------------------|-----------------------------|--------|
| GET        | `/healthz`                    | inline lambda -> `ok`       | public |
| GET        | `/projects`                   | `gallery.views.projects`    | public (checks 1–2) |
| GET, POST  | `/projects/new`               | `submissions.views.submit`  | GET public form; POST needs an authenticated participant (check 3) |
| GET        | `/api/judge/scores`           | `judging.views.judge_scores`| judge only; own rows (`?judge=` mismatch -> 403; unauth -> 401) (checks 4–6) |
| GET        | `/api/export.csv`             | `judging.views.export_csv`  | organizer only (check 7) |
| GET        | `/debug/whoami`               | `gallery.views.whoami`      | DEMO-auth proof |
| —          | `/admin/`                     | Django admin                | staff |
| GET, POST  | `/accounts/login/`            | `accounts.views.login_view` | public; real email + password login, IP-throttled |
| POST       | `/accounts/logout/`           | `accounts.views.logout_view`| authenticated; POST-only by decorator |
| GET        | `/events/`                    | `events.views.dashboard`    | authenticated; lists the events the caller organizes + the create form |
| POST       | `/events/new`                 | `events.views.create_event` | authenticated; creating an event makes the caller its organizer |
| GET, POST  | `/events/invite/<ext_id>`     | `events.views.redeem`       | authenticated invitee; GET confirms, POST redeems (Ed25519-verified, single-use, rate-limited) -> joins event |
| GET        | `/events/<ext_id>`            | `events.views.detail`       | organizer of that event; tracks, teams, invites, state |
| POST       | `/events/<ext_id>/tracks/new`, `/teams/new` | `events.views.create_track` / `create_team` | organizer of that event |
| POST       | `/events/<ext_id>/state`      | `events.views.set_state`    | organizer of that event; moves the event lifecycle state |
| POST       | `/events/<ext_id>/invites/new`| `events.views.create_invite`| organizer of that event; mints a signed, single-use judge/participant invite (atomic + audited) |
| GET, POST  | `/judging/score`              | `judging.views.score`       | judge only; scores own assigned queue (append-only + audited); **not** a checker route |
| GET        | `/judging/<ext_id>/progress`  | `judging.views.progress`    | organizer of that event; read-only coverage per judge + submission |
| GET        | `/judging/<ext_id>/assignments`| `judging.views.assignments`| organizer of that event; assignment console |
| POST       | `/judging/<ext_id>/assignments/add`, `/remove` | `judging.views.assign` / `unassign` | organizer of that event; add, or remove an **unscored** assignment (atomic + audited) |
| GET, POST  | `/judging/<ext_id>/auto-assign`| `judging.views.auto_assign`| organizer of that event; GET previews a connectivity-aware assignment plan, POST applies it (rate-limited, atomic + audited) |
| GET, POST  | `/judging/<ext_id>/rubric`    | `judging.views.rubric`      | organizer of that event; per-criterion weights (preview + next signed run) |
| GET        | `/submissions/mine`           | `submissions.views.mine`    | authenticated participant; lists own submissions (all states) |
| GET, POST  | `/submissions/<ext_id>/edit`  | `submissions.views.edit`    | owning team only; revise while accepting (else read-only); atomic + audited |
| POST       | `/submissions/<ext_id>/withdraw` | `submissions.views.withdraw` | owning team only; soft-withdraw while accepting (state -> withdrawn, never a delete); atomic + audited |
| GET        | `/normalize/`, `/normalize/leaderboard.json` | `normalize.views` | organizer (embargoed) |
| GET        | `/normalize/results`, `/normalize/results.json` | `normalize.views` | **public** (frozen signed result) |
| GET        | `/normalize/results/explain/<ext_id>` | `normalize.views.explain_rank` | authenticated **owning team or organizer**; a read-only plain-language reading of that one row of the **published, signed** result (never a recompute); every other case is a uniform 404; `private, no-store` + `noindex` |
| GET, POST  | `/normalize/results/publish`  | `normalize.views.results_publish` | organizer |
| GET        | `/normalize/diagnostics`, `/normalize/diagnostics.json` | `normalize.views` | organizer; review diagnostics (leave-one-ballot-out residuals, **leave-one-judge-out** decision influence, coverage, duplicate-title clusters) — explicitly *not* fraud detection |
| GET        | `/normalize/pairwise`         | `normalize.views.pairwise`  | organizer; **live** pairwise-sensitivity recompute (never signed/published) |
| GET        | `/api/v1/events/`, `/api/v1/events/<ext_id>/` | `api.views`         | **public**, read-only; event metadata only (no memberships / PII) |
| GET        | `/api/v1/events/<ext_id>/{tracks,teams,submissions,results}/` | `api.views` | **public**, read-only; submissions are **SUBMITTED-only**; results are the frozen signed run or `{"published": false}` |
| GET        | `/api/v1/events/<ext_id>/certificate/` | `api.views`        | **public**, read-only; verifiable certificate over the frozen signed run, or `404` when unpublished |
| GET        | `/api/v1/schema/`, `/api/v1/docs/` | `drf_spectacular` / `api.views.CspSwaggerView` | **public**; OpenAPI 3 schema + self-hosted Swagger UI |
| GET        | `/api/v1/me/`                 | `api.views.MeView`          | **authenticated** (personal Bearer token); returns only the caller's own token metadata + event memberships (no PII) |
| GET, POST  | `/voting/<event_ext_id>/ballot` | `voting.views.ballot`     | depends on campaign mode: in `authenticated` mode anonymous -> 401 and a **judge of that event** -> 403; in the email modes the caller must first confirm an emailed link. POST casts/replaces a budgeted ballot (rate-limited; demo shim exempt) |
| POST       | `/voting/<event_ext_id>/join` | `voting.views.join`         | public; email modes only — submits an address to be sent a confirm link |
| GET, POST  | `/voting/confirm/<token>`     | `voting.views.confirm`      | public; holder of the emailed token. GET confirms, POST creates the voter identity |
| GET        | `/voting/<event_ext_id>/results` | `voting.views.results`   | **public**, but only once an organizer has published **and** the window has closed; otherwise 404 (never 403), so an in-progress campaign discloses nothing |
| GET, POST  | `/voting/<event_ext_id>/manage` | `voting.views.manage`     | organizer of that event; campaign console |
| POST       | `/voting/<event_ext_id>/publish` | `voting.views.publish`   | organizer of that event; 409 while the window is still open (atomic + audited) |
| GET, POST  | `/comments/projects/<ext_id>` | `comments.views.project_comments` | GET **public** JSON list of a project's visible comments (author is the display name, never the email); POST requires authentication (401 otherwise) and is rate-limited |
| POST       | `/comments/<ext_id>/moderate` | `comments.views.moderate`   | organizer of the comment's event; soft-hides one comment (401 anonymous, 403 non-organizer) |
| GET        | `/records/judge?event=<ext_id>` | `records.views.judge_record` | authenticated **judge** of that event; own record only (`?judge=` mismatch -> 403) |
| GET        | `/records/participant?event=<ext_id>` | `records.views.participant_record` | authenticated **participant** of that event; own record only |
| POST       | `/records/verify`             | `records.views.verify`      | **public**; stateless `{"valid": bool}` check of an issued record against the deployment public key (CSRF-exempt; writes nothing) |
| GET        | `/records/signing-key`, `/.well-known/dogfood-signing-key` | `records.views.signing_key` | **public**; the Ed25519 **public** key as PEM + `X-Signing-Key-Fingerprint` |
| GET        | `/embed.js`                   | `embed.views.embed_js`      | **public**; the dependency-free loader script |
| GET        | `/embed/<ext_id>`             | `embed.views.embed_gallery` | **public**, frame-able (`xframe_options_exempt`); one event's **SUBMITTED-only** projects |
| GET        | `/bundles/<event_ext_id>/export.json`, `/export.csv` | `bundles.views.export_json` / `export_csv` | **site administrator** (`is_superuser`); 401 anonymous, 403 for an authenticated non-superuser — an event organizer role is *not* sufficient |
| POST       | `/bundles/import`             | `bundles.views.import_bundle` | **site administrator**; takes the target event from the signed body (CSRF-exempt; the `is_superuser` gate is the authority) |
| GET, POST  | `/webhooks/<event_ext_id>/endpoints` | `webhooks.views.endpoints` | organizer of that event; GET lists, POST registers an endpoint (SSRF-screened, rate-limited) |
| POST       | `/webhooks/<event_ext_id>/endpoints/<endpoint_ext_id>/delete` | `webhooks.views.delete_endpoint` | organizer of that event |
| POST       | `/webhooks/<event_ext_id>/deliver` | `webhooks.views.deliver` | organizer of that event; signed outbound delivery, recorded |
| POST       | `/webhooks/<event_ext_id>/deliveries/<delivery_ext_id>/retry` | `webhooks.views.retry` | organizer of that event |
| GET        | `/events/<event_ext_id>/awards` | `awards.views.podium`     | **public** podium, derived entirely from the **frozen, signed** normalization result (never a live recompute); before publication it renders a neutral "not yet published" state with no ranking. No per-judge score, judge identity, or PII |
| GET, POST  | `/events/<event_ext_id>/awards/manage` | `awards.views.manage` | organizer of that event; lists prizes, POST creates one (atomic + audited) |
| POST       | `/events/<event_ext_id>/awards/prizes/<prize_ext_id>/assign`, `/clear`, `/remove` | `awards.views.assign` / `clear` / `remove` | organizer of that event (atomic + audited) |
| GET        | `/events/<event_ext_id>/awards/topup` | `awards.views.topup`  | organizer of that event; a **pre-finalization review-planning aid** over the **live, unsigned** standings — see below. `Cache-Control: private, no-store`, `Vary: Cookie` |

Access control is **event-scoped**: roles come from the `EventMembership` table
(`organizer` / `judge` / `participant`), never from global user flags. `judge_scores`,
`export_csv`, and the `score` page all check the caller's `EventMembership` role; the normalize
views gate on an organizer membership. The one deliberate exception is `bundles`, whose three
whole-graph export/import endpoints gate on Django's `is_superuser` **site-administrator** flag — an
event organizer role is explicitly *not* sufficient there (`bundles.views._site_admin_or_response`).

Event resolution is **not uniform**, and the difference matters. The checker-facing, single-event
surfaces — `gallery`, `submissions`, `judging`'s `score` / `judge_scores` / `export_csv`, and every
`normalize` view — resolve the current event as the single oldest row,
`Event.objects.order_by("id").first()` (`_current_event` in `gallery`, `judging`, `submissions`;
`normalize.services.current_event`). Every newer event-scoped surface instead takes the event's
`ext_id` from the URL path: the events organizer UI, the judging **control room**
(`/judging/<ext_id>/...`), `voting`, `webhooks`, `bundles`, `embed`, `awards`, and the nested
`/api/v1/events/<ext_id>/…` collections. `records` takes it from an `?event=<ext_id>` query
parameter, and `comments` derives it from the addressed project or comment. The path-scoped
organizer views share one guard shape (`_organizer_event_or_response` / `_organizer_or_response`:
anonymous → login, unknown event → 404, non-organizer → 403), so the control room, the events UI,
voting, webhooks, and awards all gate identically.

**Awards.** The public podium at `/events/<event_ext_id>/awards` is derived entirely from the frozen,
signed normalization result — never a live recompute — and before publication it renders a neutral
"not yet published" state with no ranking. It shows only published-safe values (title, team name,
place, public `q`): no per-judge score, no judge identity, no PII.
`/events/<event_ext_id>/awards/topup` is the one awards route that reads the **live, unsigned**
standings (`normalize.services.leaderboard`). For each podium prize it takes the rank cutoff that
prize implies and lists the contenders sitting at or straddling it, with the reason each was
flagged: fewer recorded reviews than the coverage target, a bootstrap rank interval that spans the
cutoff, or a `q`-gap across the cutoff below a threshold. It is a **pre-finalization
review-planning aid** — it ranks nothing, assigns no score, is not the final ranking, and is **not
fraud detection**; awarding a prize stays a separate, explicit organizer action and the official
podium is always derived from the signed result. Its response carries
`Cache-Control: private, no-store` and `Vary: Cookie`. None of the awards routes is one of the
acceptance checker's five flat routes, none is linked from `base.html`, and the root include is
declared **last**, so none of them can shadow an earlier route.

## The seventeen code units

Sixteen first-party apps plus the `portal` config package. `INSTALLED_APPS` also lists the
`django.contrib.*` apps and three third-party entries (`rest_framework`, `drf_spectacular`,
`drf_spectacular_sidecar`); those are dependencies, not code units.

| App           | Responsibility | Models? | HTTP routes? |
|---------------|----------------|---------|--------------|
| `portal`      | settings, root URLconf, WSGI, the two middlewares, the rate-limit helper, bootstrap CLI | no | root URLconf |
| `accounts`    | custom user (`AppUser`), demo-session shim, login/logout | yes | `/accounts/` |
| `audit`       | append-only hash-chained log + Ed25519 checkpoints + offline verifier | yes | none |
| `events`      | event / track / team / membership core graph + signed single-use invitations | yes | `/events/` (organizer UI + invitee redeem) |
| `submissions` | submission create endpoint + service, plus participant self-service (edit / withdraw / "mine") | yes | `submit` (flat route) + `/submissions/` |
| `judging`     | assignments, recusals, ballots, revisions, rubric weights, the connectivity-aware assignment planner, scores read + CSV export + in-app scoring | yes | `judge_scores`, `export_csv` (flat), `score` + organizer control room (`progress` / `assignments` / `auto-assign` / `rubric`, all `/judging/`) |
| `normalize`   | score-normalization engine, signed runs, publication, review diagnostics, duplicate-title clustering, pairwise sensitivity, explain-my-rank | yes | `/normalize/` |
| `gallery`     | public project listing + `whoami` | no | flat routes (its own `urls.py` is empty) |
| `api`         | read-only public REST API (`/api/v1/`) + OpenAPI 3 schema / Swagger docs + the single authenticated `me/` endpoint | no (no models, no migrations) | `/api/v1/` |
| `apitokens`   | personal Bearer tokens (sha256-hashed at rest, soft revoke) + the DRF authentication class | yes | none (it authenticates `/api/v1/me/`) |
| `voting`      | community-voting campaigns: eligible voters, email-link confirmation, budgeted ballots, tallies, publication | yes | `/voting/` |
| `comments`    | per-project comments with organizer soft-hide moderation | yes | `/comments/` |
| `records`     | signed participation/judging records + a public verifier and public-key endpoint; computed on the fly | no (model-free) | `/records/` + `/.well-known/dogfood-signing-key` |
| `embed`       | dependency-free third-party embed: loader script + frame-able SUBMITTED-only gallery | no (model-free) | `/embed.js`, `/embed/<ext_id>` (wired top-level) |
| `bundles`     | signed whole-event export (JSON + CSV) and signed import, site-administrator only; computed on the fly | no (model-free) | `/bundles/` |
| `webhooks`    | organizer-registered outbound endpoints (SSRF-screened), signed deliveries, recorded attempts + retry | yes | `/webhooks/` |
| `awards`      | prizes and the public podium derived from the frozen signed result, plus the organizer review-top-up planner | yes | `events/<event_ext_id>/awards…` (root include, declared last) |

## Auth: DEMO shim vs real login

**Real login** (`src/accounts/views.py`) authenticates `AppUser` by email + password, is throttled
per client IP by `DOGFOOD_RATE_LIMITS['login']` (default `10/m`), and returns `429` + `Retry-After`
when exceeded. The throttle key is `REMOTE_ADDR` only — `X-Forwarded-For` is deliberately not
trusted (spoofable). `LOGIN_URL` is `/accounts/login/`; `?next` is open-redirect-guarded with
`url_has_allowed_host_and_scheme`.

**DEMO shim** (`DemoAuthMiddleware`) is the path the acceptance checker uses: it never logs in, it
attaches `Cookie: session=<token>`. That path is **CSRF-exempt** and **rate-limit-exempt** (write
views skip throttling when `request.demo_shim` is set), so the checker's responses stay byte-stable.
It is gated entirely on `DOGFOOD_DEMO` (independent of `DEBUG`) and **must be off in production**.

## Integrity spine

Three independent, offline-verifiable mechanisms. All hashing is SHA-256; all signatures are
Ed25519 (`cryptography`).

1. **Tamper-evident audit log.** `AuditHead` is a single-row chain tip; `AuditEvent` rows form an
   append-only hash chain (`prev_hash` -> `row_hash`) with a contiguous per-instance `seq`. The
   append service locks the head row `FOR UPDATE` so sequence numbers never race
   (`src/audit/service.py`); the pure chain logic and `verify_chain` live in `src/audit/hashchain.py`.
   The head is seeded by a migration, not the request path. Beyond `record_ballot` and
   `create_submission`, the participant self-service writes (`update_submission`,
   `withdraw_submission`), the organizer control-room writes (`assign_judge`, `unassign_judge`,
   `set_rubric_weights`), and the invitation writes (`create_invite`, `redeem_invite`) co-commit
   their own chained events — `submission.revised` / `submission.withdrawn` / `judge.assigned` /
   `judge.unassigned` / `rubric.reweighted` / `invite.created` / `invite.redeemed` — in the same
   `transaction.atomic()` as the write, so a revision or withdrawal, judging *configuration*, and
   invitation mint/redeem all ride the same tamper-evident trail as scores (a property of those
   service paths, not a repo-wide guarantee). The later feature services follow the same contract —
   `voting` (`campaign.configured`, `vote.cast`, `vote.duplicate_refused`, `results.publish`),
   `comments` (`comment.posted`, `comment.hidden`), `webhooks` (`webhook.registered` / `.deleted` /
   `.delivered` / `.delivery_failed` / `.delivery_retried`), `bundles` (`bundle.exported`,
   `bundle.imported`) and `awards` (`prize.created`, `prize.winner_assigned`,
   `prize.winner_cleared`, `prize.removed`) each co-commit their chained event inside the same
   `transaction.atomic()` as their write.
2. **Ed25519 signed checkpoints.** A checkpoint signs the chain tip under a per-deployment key at
   `/state/audit_ed25519_key.pem` (`O_EXCL`, `0600`) (`src/audit/keys.py`, `receipts.py`). Verify
   offline with `python -m audit.verify <bundle_dir>` (`src/audit/verify.py`).
3. **Signed, reproducible normalization run.** A published run commits to `inputs_hash` and
   `result_hash` and is signed (`src/normalize/signing.py`, `runs.py`). `python -m normalize.verify`
   pins the public key by fingerprint, checks the signature, and **re-runs the estimator from the
   pinned inputs**, confirming the ranking canonicalizes to the same `result_hash`
   (`src/normalize/verify.py`). The combined `python -m normalize.release` runs both sub-verifiers
   and adds four cross-links binding the checkpoint, the run, its audit event, and the published
   ranking CSV (`src/normalize/release.py`).

Three **further** signed artifacts reuse the **same** `/state` key and the same single Ed25519
implementation (`audit.receipts`) under distinct domain tags: the single-use **invitation**
(`dogfood.invite.v1`, `src/events/invite_signing.py`), re-checkable with
`manage.py invite_verify <ext_id>`; the **participation / judging record** (`dogfood.record.v1`,
`src/records/signing.py`), re-checkable by anyone at `POST /records/verify`; and the **portable
event bundle** (`dogfood.bundle.v1`, `src/bundles/signing.py`). None of the three serves results
integrity — the invitation carries authorization (who may join in which role), the record attests
participation facts, the bundle binds exported bytes to the operator's key — so all three sit
outside the three-mechanism spine above; per-tag domain separation is what stops any of these
signatures from verifying in another's space. Outbound **webhook** deliveries are the one signature
that does *not* use this key: each endpoint is issued its own random secret at registration and
deliveries are HMAC-signed with it (`src/webhooks/services.py`), so a receiver verifies with a
shared secret rather than the pinned public key.

Honest scope: because the operator holds the private key, a PASS is decisive only if an independent
party pinned the public key + fingerprint **before** judging. This is stated in the code and in
[Threat model](THREAT-MODEL.md).

## Score normalization

Pure numpy, no scikit-learn (`src/normalize/engine.py`). Ballot composites are modelled as
`y = q + b + e` (per-submission quality `q`, per-judge severity `b`, noise `e`); ridge penalizes
only `b`; each connected component of the judge–submission graph is gauged to `mean(b) = 0`; the
ridge `λ` is chosen by cross-validation; rank uncertainty comes from a parametric bootstrap.
`canonical_result` freezes the output so hashes are stable, and `ENGINE_VERSION = "ridge-additive-v1"`
pins the schema. Every figure in [Judging](JUDGING.md) is emitted by `manage.py normalize_report`,
so the write-up cannot drift from the code.

## Rate limiting

A fixed-window limiter over the shared `DatabaseCache` (`src/portal/ratelimit.py`); it is
**fail-open** (a cache outage never becomes an availability outage). `settings.DOGFOOD_RATE_LIMITS`
holds **seven** policies, and there are **nine** live `ratelimit.hit(` call sites across seven view
modules — `submission_write` is read from two places in `submissions.views`, and `judging.views`
carries both the ballot throttle and an eighth policy name that is *not* a settings key (below).
Every write throttle except `login` is skipped when `request.demo_shim` is set, so the acceptance
checker's responses stay byte-stable; `login` is keyed on `REMOTE_ADDR` and is never exempted.

| Policy             | Default | Enforced today? | Where |
|--------------------|---------|-----------------|-------|
| `login`            | `10/m`  | **yes**         | `src/accounts/views.py` (`login_view`), keyed on client IP |
| `invite_redeem`    | `20/h`  | **yes** (DEMO shim exempt) | `src/events/views.py` (`redeem`), keyed on user |
| `submission_write` | `60/h`  | **yes** (DEMO shim exempt) | `src/submissions/views.py` — `submit`, and `_write_throttled` for edit / withdraw; keyed on user |
| `ballot_write`     | `120/h` | **yes** (DEMO shim exempt) | `src/judging/views.py` (`score`), keyed on user |
| `vote_write`       | `20/m`  | **yes** (DEMO shim exempt) | `src/voting/views.py` (`ballot` POST), keyed on campaign + voter |
| `comment_write`    | `60/h`  | **yes** (DEMO shim exempt) | `src/comments/views.py` (`project_comments` POST), keyed on user |
| `webhook_write`    | `60/h`  | **yes** (DEMO shim exempt) | `src/webhooks/views.py` (`endpoints` POST), keyed on event + user |

One further throttle has **no settings key**: `judging.views.auto_assign` reads
`DOGFOOD_RATE_LIMITS.get("assignment_write", "60/h")`, so the auto-assign POST is limited at the
inline default of `60/h` (keyed on event + user, DEMO shim exempt) unless an
`assignment_write` key is added. The three call sites that use `.get(..., default)` — `vote_write`,
`webhook_write`, `assignment_write` — therefore run even if the key is absent; the other five read
the key directly.

## Read-only public API (`/api/v1/`)

A versioned, **read-only** JSON API over the data that is already public, served by Django REST
Framework and documented by an OpenAPI 3 schema. It is `GET`-only **by construction** — the views are
`ListAPIView` / `RetrieveAPIView` / `APIView.get`, so there is no write path. Every endpoint is
`AllowAny` and unauthenticated **except** `me/` (below), which requires a personal Bearer token and
returns only the caller's own identity. The single authentication class
(`apitokens.authentication.BearerTokenAuthentication`) is a no-op when no `Authorization: Bearer`
header is present, so the public endpoints stay anonymous and byte-identical
(`src/portal/settings.py` `REST_FRAMEWORK`). What it serves:

- `events/` and `events/<ext_id>/` — public event metadata only (memberships are a reverse relation
  and are never serialized);
- `events/<ext_id>/tracks/`, `/teams/` — track and team **names**, event-scoped (team *members* and
  their emails are never serialized);
- `events/<ext_id>/submissions/` — **SUBMITTED submissions only**; drafts and withdrawn projects are
  filtered out in the queryset;
- `events/<ext_id>/results/` — the official published ranking read **verbatim from the frozen, signed
  normalization run** (never a live recompute), or `{"published": false}` before an organizer
  publishes;
- `events/<ext_id>/certificate/` — a self-contained **verifiable certificate** over that same frozen,
  signed run (result hash, signer fingerprint, public key, Ed25519 signature, the exact signed fields,
  and the public rank/title/q), or `404` when the event has no published results. It introduces no new
  key or signature scheme — `normalize.certificate` restates and re-checks the run that
  `normalize.signing` already signed — and carries the same published-safe, no-PII fields as the
  results endpoint;
- `schema/` and `docs/` — the OpenAPI 3 schema and a self-hosted (no-CDN) Swagger UI.
- `me/` — the **only authenticated** endpoint. With a personal Bearer token it returns the calling
  token's metadata (name, display prefix, last-used time) and the caller's own event memberships
  (operational `ext_id`s and roles) — never an email, display name, another user's data, a ballot, or
  a per-judge score. Tokens are minted with `manage.py mint_api_token`; only a token's sha256 hash is
  stored (the raw string is shown once and never persisted) and revocation is a soft flag.

Each serializer declares its fields as an explicit allowlist (never `fields = "__all__"`), so no
per-judge score, ballot, judge identity, invitation, audit row, or user PII (email / display name) can
appear — a property `tests/test_api_contract.py` asserts without a database. Event-scoped collections
are **nested** under `events/<ext_id>/`, so scoping is structural rather than a queryset convention.
Every list is `PageNumberPagination`-bounded (`PAGE_SIZE = 50`) and the API is anon-throttled by a
**fail-open** throttle (`src/api/throttling.py`; default `240/min`, `DOGFOOD_RATE_API`), so it never
becomes an unbounded amplifier or a hard availability dependency. Mounted after the five flat checker
routes and unlinked from `base.html`, so `tools/replay.py` stays 7/7 (`src/api/*`,
`src/portal/urls.py`).

## Deployment topology

`docker-compose.yml`:

- **`db`** — `postgres:16-alpine`, **no published port** (reachable only on the compose network),
  `pg_isready` healthcheck.
- **`web`** — built from `Dockerfile` (python:3.12-slim, `psycopg[binary]`, runs as uid 10001,
  collects static at build), depends on `db` healthy, publishes `${DOGFOOD_PORT:-8000}:8000`, mounts
  a `dogfood_state` volume at `/state` (secret key + audit key), healthchecks `GET /healthz`, and
  defaults `DOGFOOD_DEMO=1`.

The image build is itself a gate: the `Dockerfile` runs `manage.py check --fail-level WARNING` and
`manage.py makemigrations --check` at build time, so a model that disagrees with its (hand-authored)
migration, or an unresolved import, fails the build. CI (`.github/workflows/ci.yml`) builds the
image, runs the containerized `pytest tests/` suite against a real PostgreSQL service, and then
restarts the `web` container to exercise a second, idempotent boot. That suite includes a
network-free release-bundle regression (`tests/test_verifier_golden.py`), and it is **active**: the
golden bytes are committed at `tests/goldens/golden_bundle.json` (generated on a dev machine by
`tools/make_golden_bundle.py`, never regenerated in CI). Its four tests import only
`normalize.verify` — no Django DB, no sockets — unpack a fresh copy of the bundle into a temp dir,
and assert that the committed signed bytes pass every check, and that each of three one-byte
tampers is rejected with the failure isolated to the right check: a flipped input score breaks
`inputs.json matches inputs_hash`, a nudged published `q` breaks `result.json matches result_hash`,
and a corrupted hex char in the Ed25519 signature breaks `run signature valid`. The module-level
skip is a fixture-missing guard only; with the golden committed it does not fire.

## Implemented vs planned

The line every reviewer should be able to trust. "Shipped" means reachable and tested; "planned"
means the model/service may exist but no endpoint wires it yet.

| Capability | Status | Evidence |
|------------|--------|----------|
| Public project gallery (`GET /projects`) | **Shipped** | `src/gallery/views.py` |
| Submission **create** (`POST /projects/new`) | **Shipped** | `src/submissions/{views,services}.py` |
| Judge reads own scores (`GET /api/judge/scores`) | **Shipped** | `src/judging/views.py` |
| Organizer CSV export (`GET /api/export.csv`) | **Shipped** | `src/judging/{views,services}.py` |
| Public frozen results + organizer leaderboard/diagnostics | **Shipped** | `src/normalize/{views,urls}.py` |
| Real login/logout + login throttle | **Shipped** | `src/accounts/views.py` |
| DEMO auth shim (gated on `DOGFOOD_DEMO`) | **Shipped** | `src/portal/middleware.py` |
| Hash-chained audit log + Ed25519 checkpoints + offline verifier | **Shipped** | `src/audit/*` |
| Append-only ballot revisions (DB `CHECK 1..5`) | **Shipped** | `src/judging/models.py`, `migrations/0002` |
| Signed reproducible normalization run + release bundle + verifiers | **Shipped** | `src/normalize/{signing,verify,release}.py` |
| Submission-write / login rate limiting | **Shipped** | `src/portal/ratelimit.py` |
| **In-app judge scoring** (submit a ballot over HTTP) | **Shipped** | `src/judging/{views,services}.py`, `GET/POST /judging/score` |
| **Judging-progress dashboard** | **Shipped** — read-only assigned / scored / pending per judge and per submission | `src/judging/{views,services}.py`, `templates/judging/progress.html` |
| **Judge-assignment management UI** | **Shipped** — organizer console; add, or remove an **unscored** assignment (atomic + audited; a scored one is refused) | `src/judging/{views,services,urls}.py`, `templates/judging/assignments.html` |
| **Event / team creation UI** | **Shipped** — organizer creates events, tracks, and teams in-app (the `dogfood_import` seed still works) | `src/events/{views,services,urls}.py`, `templates/events/{dashboard,detail}.html` |
| **Rubric-weight editing** | **Shipped** — organizer sets per-criterion weights (live preview + next signed run; never rewrites a published result) | `src/judging/{views,services}.py`, `templates/judging/rubric.html` |
| **Submission edit / withdraw** | **Shipped** — a team revises or **soft-withdraws** its own submission while the event is accepting writes (owner-gated, deadline-gated, atomic + audited); withdrawal flips state to `withdrawn` and hides it from the gallery but never deletes the row, so ballot/audit history survives | `src/submissions/{views,services,urls}.py`, `migrations/0002`, `templates/submissions/{mine,edit}.html`, `GET /submissions/mine`, `GET/POST /submissions/<id>/edit`, `POST /submissions/<id>/withdraw` |
| **Single-use invitations** | **Shipped** — an organizer mints a signed, single-use invite; redeeming it is Ed25519-verified, single-use (DB-enforced under `select_for_update`), rate-limited, and atomically creates an `EventMembership` + `invite.redeemed` audit event. Roles are limited to judge/participant, so a link can never escalate to organizer | `src/events/{models,invite_signing,services,views,urls}.py`, `migrations/0002_invite`, `templates/events/{detail,redeem}.html`, `manage.py invite_verify`; `GET`/`POST` `/events/invite/<ext_id>` |
| **App models in Django admin** | **Shipped** — all 30 models registered across 11 of the 12 `admin.py` modules (`gallery` has no models to register); append-only/signed tables (audit, ballots, revisions, runs, publications) are inspect-only, the signed `Invite` table is likewise inspect-only with adding disabled (delete-to-revoke an un-redeemed link is still allowed), and any row whose cascade would reach a **scored** assignment (the assignment itself, or a parent `Event`/`EventMembership`/`Submission`/`Team`/`AppUser`) refuses deletion so ballot history can't be destroyed through the admin UI (raw-DB access is the A8 operator boundary) | `src/*/admin.py`, `src/portal/admin_mixins.py` |
| **Read-only public API** (`GET /api/v1/*`) + OpenAPI 3 / Swagger | **Shipped** — public + `GET`-only (the one authenticated endpoint, `/api/v1/me/`, is a separate row below); events, tracks, teams, **SUBMITTED** submissions, and the **frozen signed** results (or `{"published": false}`); explicit-allowlist serializers (no ballots / per-judge scores / judge identity / invites / audit chain / PII), page-bounded + fail-open throttled | `src/api/*`, `src/portal/urls.py`, `tests/test_api_contract.py` |
| **Verifiable results certificate** (`GET /api/v1/events/<id>/certificate/` + `manage.py certificate`) | **Shipped** — a self-contained attestation over the **frozen, signed** run (result hash, signer fingerprint, public key, Ed25519 signature, the signed fields, and the public rank/title/q); adds **no new key or signature scheme** (reuses `normalize.signing`) and no model/migration; refuses any non-published run; exposes no ballots / per-judge scores / judge identity / PII; the CLI self-verifies via the offline bundle verifier before it emits | `src/normalize/certificate.py`, `src/normalize/management/commands/certificate.py`, `src/api/*`, `tests/test_certificate.py`, `src/api/test_certificate.py` |
| **Personal API tokens + authenticated `/api/v1/me/`** | **Shipped** — a user mints a personal Bearer token (`manage.py mint_api_token`); only its sha256 hash is stored (raw shown once), and revocation is a soft flag. `BearerTokenAuthentication` authenticates **only** `/api/v1/me/` (set on the view, not globally), which returns just the caller's own token metadata + event memberships — no email / display name / other users / ballots / scores. With no `Authorization: Bearer` header the public endpoints stay anonymous and byte-identical | `src/apitokens/*`, `src/apitokens/migrations/0001_initial.py`, `src/api/{views,urls}.py`, `src/portal/settings.py`, `src/api/tests.py`, `tests/test_apitokens.py` |
| **Pairwise-sensitivity view** (`GET /normalize/pairwise`) | **Shipped** — organizer-only; the model-based P(one project outranks another) from the **same** bootstrap as the leaderboard's rank intervals, flagging every adjacent pair inside the pre-registered unresolved band; a **live recompute**, prominently labelled *not signed, not published, not a merit score, not fraud detection*; behind the same organizer gate as the leaderboard, and never rendered on the public results page | `src/normalize/{services,views,urls}.py`, `templates/normalize/pairwise.html`, `src/normalize/tests.py` |
| **Community voting** (`/voting/`) | **Shipped** — organizer-configured campaign (`manage`) with three identity modes (`authenticated`, `email_link`, `email_gated`), a budgeted per-voter ballot, `join` / `confirm/<token>` for the email modes, rate-limited casts, and tallies that become **public only after** an organizer publishes **and** the window closes (hidden tallies answer 404, not 403, so an in-progress campaign discloses nothing). In authenticated mode a **judge of the event may not vote** (403). No `normalize` module reads `voting`, so community votes are a separate tally that never feeds the signed run | `src/voting/*`, `src/voting/migrations/0001_initial.py`, `src/voting/{tests,test_pure}.py` |
| **Project comments** (`/comments/`) | **Shipped** — `GET /comments/projects/<ext_id>` is a public JSON list of a project's visible comments (author is the display/short name, never the email); POST requires authentication and is rate-limited; `POST /comments/<ext_id>/moderate` lets an organizer of that event **soft-hide** a comment (a visibility flag, not a delete) | `src/comments/*`, `src/comments/migrations/0001_initial.py`, `src/comments/tests.py` |
| **Signed participation / judging records** (`/records/`) | **Shipped** — a judge or participant fetches **their own** signed record for `?event=<ext_id>` (anonymous 401, wrong role 403, another judge's record 403); `POST /records/verify` is a public, stateless `{"valid": bool}` re-check that writes nothing; the deployment's Ed25519 **public** key is served as PEM at `/records/signing-key` and `/.well-known/dogfood-signing-key` with the fingerprint in `X-Signing-Key-Fingerprint`. Model-free — records are computed from the live event graph | `src/records/*`, `src/records/{tests,test_signing}.py` |
| **Embeddable gallery widget** (`/embed.js`, `/embed/<ext_id>`) | **Shipped** — a dependency-free loader script plus a frame-able (`xframe_options_exempt`) standalone page listing one event's **SUBMITTED-only** projects (withdrawn and draft rows excluded); unknown event → 404. Model-free | `src/embed/*`, `src/embed/tests.py`, `src/portal/urls.py` |
| **Event bundle export + import** (`/bundles/`) | **Shipped** — `export.json` is a **signed** portable whole-event bundle and `import` reconstructs an event graph from one (400 on a bad signature or body, 409 if the event `ext_id` already exists, 201 on success); `export.csv` is an unsigned all-stages submissions CSV served `private, no-store`. All three gate on the **site-administrator** (`is_superuser`) flag: anonymous 401, authenticated non-superuser 403, and an event organizer role is explicitly not enough. Model-free — the export is computed on the fly from the live event graph | `src/bundles/*`, `src/bundles/{tests,test_signing}.py` |
| **Outbound signed webhooks** (`/webhooks/`) | **Shipped** — organizer-of-that-event registers an endpoint (URL screened against SSRF targets before it is stored), triggers a signed delivery, and retries a recorded one; registration POSTs are rate-limited. Deliveries and attempts are persisted rather than fired and forgotten | `src/webhooks/*`, `src/webhooks/migrations/0001_initial.py`, `src/webhooks/{tests,test_ssrf}.py` |
| **Judge recusal / conflict of interest** | **Shipped** — a `JudgeRecusal` (judge, team) row is declared in the Django admin and expanded to that team's SUBMITTED submissions, then fed to the assignment planner as that judge's recusal set, so a recused judge is never planned onto that team's projects. It has **no bespoke HTTP route** of its own; the admin page is the UI | `src/judging/{models,recusal,services,admin}.py`, `src/judging/migrations/`, `tests/test_recusal.py` |
| **Auto-assignment planner** (`GET/POST /judging/<ext_id>/auto-assign`) | **Shipped** — organizer-only; GET previews a connectivity-aware plan for a target reviews-per-project `k`, POST applies it (rate-limited, atomic + audited). The planner itself is pure and DB-free, so it unit-tests without a database | `src/judging/{assignment,services,views,urls}.py`, `src/templates/judging/auto_assign.html`, `tests/test_assignment.py` |
| **Explain-my-rank** (`GET /normalize/results/explain/<ext_id>`) | **Shipped** — a read-only, plain-language reading of **one row of the published, signed result**; it never recomputes, so it cannot move a `result_hash`. Access is the owning team or an organizer; every other case is a uniform 404, and the response is `private, no-store` + `noindex` | `src/normalize/{explain,views,urls}.py`, `src/normalize/templates/normalize/explain.html`, `tests/test_explain.py` |
| **Duplicate-title diagnostic** | **Shipped** — submissions in the **same track** that share the same normalized title (internal whitespace collapsed, stripped, casefolded) are grouped and surfaced on the organizer diagnostics panel as something for a human to look at. Display-only: it flags nothing as fraud, changes no score, and is never fed back into the leaderboard, the signed result, or any hash | `src/normalize/{duplicates,services,views}.py`, `src/normalize/templates/normalize/diagnostics.html`, `tests/test_duplicates.py` |
| **Decisive-judge fragility** | **Shipped** — leave-one-**judge**-out refits (drop all of one judge's ballots, refit, measure how far the decision moves) sit alongside the leave-one-ballot-out residuals in the same organizer diagnostics report. It measures how fragile the ranking is to any one judge — explicitly *not* whether a judge did anything wrong | `src/normalize/diagnostics.py`, `src/normalize/services.py` (`diagnostics_report`), `src/normalize/templates/normalize/diagnostics.html`, `src/normalize/tests.py` |
| **Prizes + public podium** (`GET /events/<event_ext_id>/awards`) | **Shipped** — an organizer creates prizes and may assign a winner explicitly (`.../awards/manage`, `.../awards/prizes/<prize_ext_id>/assign|clear|remove`, all organizer-only, atomic + audited). The **public** podium derives every place from the **frozen, signed** normalization result (never a live recompute) and renders a neutral "not yet published" state with no ranking until results are published; it exposes only title, team name, place and public `q` — no per-judge score, judge identity, or PII. Mounted as a root include declared **last**, off the checker's five flat routes and unlinked from `base.html` | `src/awards/*`, `src/awards/migrations/0001_initial.py`, `src/awards/tests.py`, `tests/test_podium.py` |
| **Review top-up planner** (`GET /events/<event_ext_id>/awards/topup`) | **Shipped** — organizer-only; the one awards route that reads the **live, unsigned** standings. Per podium prize it lists contenders at or straddling that prize's rank cutoff with a reason each (fewer recorded reviews than the coverage target, a bootstrap rank interval spanning the cutoff, or a `q`-gap across the cutoff below a threshold). A pre-finalization review-planning aid: it ranks nothing, assigns no score, is not the final ranking, and is **not fraud detection**. Response is `Cache-Control: private, no-store` + `Vary: Cookie`. The planner is pure stdlib — no Django, no numpy — so it unit-tests without a database | `src/awards/{topup,services,views}.py`, `src/awards/templates/awards/topup.html`, `tests/test_topup.py` |
| **Offline release-bundle regression test** | **Shipped** — the golden bytes are committed at `tests/goldens/golden_bundle.json`, so the test runs in CI: it re-verifies the committed signed bundle and asserts that each of three one-byte tampers (an input score, a published `q`, a signature hex char) is rejected with the failure isolated to the matching check | `tests/test_verifier_golden.py`, `tests/goldens/golden_bundle.json`, `tools/make_golden_bundle.py` |
| **Multi-event support** | *Partial by design* — the checker-facing surfaces (`gallery`, `submissions`, `judging`'s flat routes, all of `normalize`) still resolve one current event as the oldest row; the event-scoped surfaces (events UI, judging control room, voting, comments, records, embed, bundles, webhooks, awards, `/api/v1/events/<ext_id>/…`) address the event explicitly | `src/{gallery,judging,submissions}/views.py` (`_current_event`), `src/normalize/services.py` (`current_event`) |



