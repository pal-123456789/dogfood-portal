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
  response (via `setdefault`, so a view may override it).

Writes go through a **service layer**, not the views. `submissions.services.create_submission` runs
auth → participant → deadline → validation and writes the `Submission` plus an audit event in one
`transaction.atomic()`. The single writer for scores is `judging.services.record_ballot`, which
updates the `Ballot`, appends an immutable `BallotRevision`, and chains a `ballot.recorded` audit
event — all atomically, so none can exist without the others.

## The reachable HTTP surface

The complete routing table is `src/portal/urls.py`. The five flat, un-prefixed routes below are the
acceptance-checker contract and are kept byte-stable; the app-level `urls.py` for `gallery` is
currently empty (`app_name` + `urlpatterns = []`) — its views are wired as flat routes in
`portal/urls.py` — so `accounts`, `events`, `submissions`, `judging`, and `normalize` are the apps
that contribute *included* routes (`/accounts/`, `/events/`, `/submissions/`, `/judging/`,
`/normalize/`). The `/submissions/` routes are participant self-service (edit / withdraw / "mine");
they are reachable by URL but intentionally not linked from `base.html`, so the gallery and nav the
checker renders stay byte-identical.

| Method(s)  | Path                          | View                        | Access |
|------------|-------------------------------|-----------------------------|--------|
| GET        | `/healthz`                    | inline lambda -> `ok`       | public |
| GET        | `/projects`                   | `gallery.views.projects`    | public (checks 1–2) |
| GET, POST  | `/projects/new`               | `submissions.views.submit`  | GET public form; POST needs an authenticated participant (check 3) |
| GET        | `/api/judge/scores`           | `judging.views.judge_scores`| judge only; own rows (`?judge=` mismatch -> 403; unauth -> 401) (checks 4–6) |
| GET        | `/api/export.csv`             | `judging.views.export_csv`  | organizer only (check 7) |
| GET, POST  | `/judging/score`              | `judging.views.score`       | judge only; scores own assigned queue (append-only + audited); **not** a checker route |
| GET        | `/judging/<event>/progress`   | `judging.views.progress`    | organizer of that event; read-only coverage per judge + submission |
| GET        | `/judging/<event>/assignments`| `judging.views.assignments` | organizer of that event; assignment console |
| POST       | `/judging/<event>/assignments/add`, `/remove` | `judging.views.assign` / `unassign` | organizer of that event; add, or remove an **unscored** assignment (atomic + audited) |
| GET, POST  | `/judging/<event>/rubric`     | `judging.views.rubric`      | organizer of that event; per-criterion weights (preview + next signed run) |
| GET        | `/submissions/mine`           | `submissions.views.mine`    | authenticated participant; lists own submissions (all states) |
| GET, POST  | `/submissions/<ext_id>/edit`  | `submissions.views.edit`    | owning team only; revise while accepting (else read-only); atomic + audited |
| POST       | `/submissions/<ext_id>/withdraw` | `submissions.views.withdraw` | owning team only; soft-withdraw while accepting (state -> withdrawn, never a delete); atomic + audited |
| GET        | `/debug/whoami`               | `gallery.views.whoami`      | DEMO-auth proof |
| —          | `/admin/`                     | Django admin                | staff |
| GET/POST   | `/accounts/login/`, `/logout/`| `accounts.views`            | public login / logout |
| GET        | `/normalize/`, `/normalize/leaderboard.json` | `normalize.views` | organizer (embargoed) |
| GET        | `/normalize/results`, `/normalize/results.json` | `normalize.views` | **public** (frozen signed result) |
| POST       | `/normalize/results/publish`  | `normalize.views`           | organizer |
| GET        | `/normalize/diagnostics`, `.json` | `normalize.views`       | organizer |

Access control is **event-scoped**: roles come from the `EventMembership` table
(`organizer` / `judge` / `participant`), never from global user flags. `judge_scores`,
`export_csv`, and the `score` page all check the caller's `EventMembership` role; the normalize
views gate on an organizer membership. The app is effectively single-event: callers resolve the current event with
`Event.objects.order_by("id").first()`. The organizer **control room** (`/judging/<event>/...`) is
the exception to that resolution — it addresses the event by `ext_id` in the path and requires an
**organizer** `EventMembership` in *that* event (`_organizer_event_or_response`: anonymous → login,
unknown event → 404, non-organizer → 403), mirroring the events organizer UI.

## The eight code units

Seven domain apps plus the `portal` config package (`INSTALLED_APPS`):

| App           | Responsibility | Models? | HTTP routes? |
|---------------|----------------|---------|--------------|
| `portal`      | settings, root URLconf, WSGI, the two middlewares, the rate-limit helper, bootstrap CLI | no | root URLconf |
| `accounts`    | custom user (`AppUser`), demo-session shim, login/logout | yes | `/accounts/` |
| `audit`       | append-only hash-chained log + Ed25519 checkpoints + offline verifier | yes | none |
| `events`      | event / track / team / membership core graph | yes | `/events/` (organizer UI) |
| `submissions` | submission create endpoint + service, plus participant self-service (edit / withdraw / "mine") | yes | `submit` (flat route) + `/submissions/` |
| `judging`     | assignments, ballots, revisions, rubric weights, scores read + CSV export + in-app scoring | yes | `judge_scores`, `export_csv` (flat), `score` + organizer control room (`progress` / `assignments` / `rubric`, all `/judging/`) |
| `normalize`   | score-normalization engine, signed runs, publication, diagnostics | yes | `/normalize/` |
| `gallery`     | public project listing + `whoami` | no | flat routes |

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
   `withdraw_submission`) and the organizer control-room writes (`assign_judge`, `unassign_judge`,
   `set_rubric_weights`) co-commit their own chained events — `submission.revised` /
   `submission.withdrawn` / `judge.assigned` / `judge.unassigned` / `rubric.reweighted` — in the same
   `transaction.atomic()` as the write, so a revision or withdrawal (and judging *configuration*)
   rides the same tamper-evident trail as scores (a property of those service paths, not a
   repo-wide guarantee).
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
**fail-open** (a cache outage never becomes an availability outage). Policies live in
`settings.DOGFOOD_RATE_LIMITS`:

| Policy             | Default | Enforced today? | Where |
|--------------------|---------|-----------------|-------|
| `login`            | `10/m`  | **yes**         | `src/accounts/views.py` |
| `submission_write` | `60/h`  | **yes** (DEMO shim exempt) | `src/submissions/views.py` |
| `ballot_write`     | `120/h` | **yes** (DEMO shim exempt) | `src/judging/views.py` (`score`) |
| `invite_redeem`    | `20/h`  | no — *planned*  | no invite flow exists |

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
restarts the `web` container to prove a second boot is idempotent.

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
| **Single-use invitations** | *Not built* — no `Invite` model or flow | — |
| **App models in Django admin** | **Shipped** — all 17 models registered; append-only/signed tables (audit, ballots, revisions, runs, publications) are inspect-only, and any row whose cascade would reach a **scored** assignment (the assignment itself, or a parent `Event`/`EventMembership`/`Submission`/`Team`/`AppUser`) refuses deletion so ballot history can't be destroyed through the admin UI (raw-DB access is the A8 operator boundary) | `src/*/admin.py`, `src/portal/admin_mixins.py` |
| **Multi-event support** | *Limitation by design* — uses the first event | `src/*/views.py` (`_current_event`) |



