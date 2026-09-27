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
acceptance-checker contract and are kept byte-stable; the app-level `urls.py` for `gallery`,
`events`, and `submissions` are currently empty (`app_name` + `urlpatterns = []`), so `accounts`,
`judging`, and `normalize` are the apps that contribute *included* routes.

| Method(s)  | Path                          | View                        | Access |
|------------|-------------------------------|-----------------------------|--------|
| GET        | `/healthz`                    | inline lambda -> `ok`       | public |
| GET        | `/projects`                   | `gallery.views.projects`    | public (checks 1–2) |
| GET, POST  | `/projects/new`               | `submissions.views.submit`  | GET public form; POST needs an authenticated participant (check 3) |
| GET        | `/api/judge/scores`           | `judging.views.judge_scores`| judge only; own rows (`?judge=` mismatch -> 403; unauth -> 401) (checks 4–6) |
| GET        | `/api/export.csv`             | `judging.views.export_csv`  | organizer only (check 7) |
| GET, POST  | `/judging/score`              | `judging.views.score`       | judge only; scores own assigned queue (append-only + audited); **not** a checker route |
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
`Event.objects.order_by("id").first()`.

## The eight code units

Seven domain apps plus the `portal` config package (`INSTALLED_APPS`):

| App           | Responsibility | Models? | HTTP routes? |
|---------------|----------------|---------|--------------|
| `portal`      | settings, root URLconf, WSGI, the two middlewares, the rate-limit helper, bootstrap CLI | no | root URLconf |
| `accounts`    | custom user (`AppUser`), demo-session shim, login/logout | yes | `/accounts/` |
| `audit`       | append-only hash-chained log + Ed25519 checkpoints + offline verifier | yes | none |
| `events`      | event / track / team / membership core graph | yes | (included, empty) |
| `submissions` | submission create endpoint + service | yes | `submit` (flat route) |
| `judging`     | assignments, ballots, revisions, rubric weights, scores read + CSV export + in-app scoring | yes | `judge_scores`, `export_csv` (flat), `score` (`/judging/`) |
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
   The head is seeded by a migration, not the request path.
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
| **Judge-assignment management UI** | *Planned* — dedicated UI; assignments are seeded and editable via the Django admin | `src/judging/models.py`, `src/judging/admin.py` |
| **Event / team creation UI** | *Planned* — created by the `dogfood_import` seed; editable via the Django admin | `src/events/management/commands/`, `src/events/admin.py` |
| **Rubric-weight editing** | *Planned* — dedicated UI; weights are seeded and editable via the Django admin | `src/judging/models.py`, `src/judging/admin.py` |
| **Submission edit / withdraw** | *Planned* — the endpoint is create-only | `src/submissions/views.py` |
| **Single-use invitations** | *Not built* — no `Invite` model or flow | — |
| **App models in Django admin** | **Shipped** — all 17 models registered; append-only/signed tables (audit, ballots, revisions, runs, publications) are inspect-only | `src/*/admin.py`, `src/portal/admin_mixins.py` |
| **Multi-event support** | *Limitation by design* — uses the first event | `src/*/views.py` (`_current_event`) |



