# DOGFOOD Portal

A self-hostable hackathon **submission-and-judging** platform: a Django 5.2 + PostgreSQL 16
monolith that boots from a single command with a seeded demo event, needs no external services
(no Redis, no broker, no separate worker), and ships an **offline-verifiable integrity trail** for
its published results.

The HTTP surface is deliberately small, and this documentation tracks the **code, not the
roadmap**: anything not yet wired to an endpoint is labelled *planned* here and in
[Architecture](ARCHITECTURE.md). The project is graded against over-claiming, so the docs are
written to be checkable line-by-line against `src/`.

## What it does today

- **Public project gallery** — anyone can browse submitted projects at `GET /projects`, with an
  optional `?track=` filter (`src/gallery/views.py`).
- **Submission intake, edit & withdraw** — a participant creates a submission at `POST /projects/new`,
  which refuses writes after the event deadline. A team can then revise or **soft-withdraw** its own
  submission while the event is still accepting writes (`GET /submissions/mine`,
  `GET/POST /submissions/<id>/edit`, `POST /submissions/<id>/withdraw`); withdrawal hides the project
  from the gallery without deleting the row, so any scores and audit trail beneath it survive
  (`src/submissions/views.py`, `services.py`).
- **Judge reads their own scores** — a judge reads back only their own ballots at
  `GET /api/judge/scores`; requesting another judge's rows is `403` (`src/judging/views.py`).
- **In-app judge scoring** — a judge scores the submissions assigned to them at
  `GET`/`POST /judging/score`; every write appends an immutable, audited ballot revision, so a
  changed score never overwrites its history (`src/judging/views.py`, `services.py`).
- **Organizer control room** — an organizer manages judge assignments, sets rubric weights, and
  watches judging-progress coverage under `/judging/<event>/…`; assignment and rubric writes are
  atomic and audited (`judge.assigned` / `judge.unassigned` / `rubric.reweighted`), and a **scored**
  assignment cannot be unassigned through the console or admin, because its ballot history is
  append-only (`src/judging/{views,services}.py`).
- **Signed single-use invitations** — an organizer mints a signed link inviting one person to join
  as a judge or participant (`POST /events/<event>/invites/new`); redeeming it
  (`GET/POST /events/invite/<ext_id>`) is Ed25519-verified, single-use (enforced in the database),
  and rate-limited, and atomically grants the membership plus an audit event. A link can only ever
  grant judge or participant — never organizer (`src/events/{invite_signing,services,views}.py`,
  `manage.py invite_verify`).
- **Organizer CSV export** — organizers export scored ballots as CSV with spreadsheet
  formula-injection guarding at `GET /api/export.csv` (`src/judging/{views,services}.py`).
- **Normalized results** — organizers see an embargoed leaderboard and score diagnostics; the
  public sees only the frozen, signed result once published (`/normalize/…`,
  `src/normalize/views.py`).
- **Read-only public API** — a versioned, read-only REST API at `/api/v1/` serves public event,
  track, team, and **submitted** project data as JSON, plus the official **frozen, signed** results
  once published, with an OpenAPI 3 schema (`/api/v1/schema/`) and a self-hosted Swagger UI
  (`/api/v1/docs/`). It is unauthenticated and `GET`-only by construction — there are no write
  endpoints — and never exposes ballots, per-judge scores, judge identities, invitations, the audit
  chain, or user PII (`src/api/{views,serializers,urls}.py`).
- **Real login / logout** with per-IP rate limiting, for humans self-hosting the portal
  (`/accounts/login/`, `/accounts/logout/`, `src/accounts/views.py`).
- **Offline integrity verification** — the audit log, the normalization run, and a combined
  release bundle can each be verified on a clean machine with no access to the deployment (below).

For the complete reachable route list and the exact implemented-vs-planned boundary, read
[Architecture](ARCHITECTURE.md).

## Five-minute self-host

**Prerequisites:** Docker with Compose v2. Nothing else — PostgreSQL runs in a container.

```bash
# from the repo root (the directory holding docker-compose.yml)
docker compose up --build --wait
```

`--wait` returns once the `web` container's healthcheck (`GET /healthz` == `ok`) passes. The
container entrypoint runs eight idempotent steps, then execs gunicorn (`docker/entrypoint.sh`):

1. verify `/state` is writable (fails on line one with a remedy, not a later traceback);
2. `python -m portal.bootstrap wait-for-db` (bounded 30×1s retry);
3. `python -m portal.bootstrap ensure-secret` — writes a Django secret key to `/state` (`O_EXCL`, `0600`);
4. `manage.py migrate`;
5. `manage.py createcachetable` — creates the `DatabaseCache` table;
6. `manage.py dogfood_import --if-empty /app/fixtures.json` — seeds the demo event;
7. `manage.py verify_demo` — asserts the seed matches the acceptance contract (no-op when `DOGFOOD_DEMO=0`);
8. `python -m portal.bootstrap ensure-admin` — if no superuser exists, creates one (email from
   `DOGFOOD_ADMIN_EMAIL`, else `admin@localhost`; password from `DOGFOOD_ADMIN_PASSWORD`, else a
   generated `token_urlsafe(24)`) and **prints the credentials once to the boot log**.

Then open `http://localhost:8000/projects` (change the port with `DOGFOOD_PORT`). The Django admin
is at `/admin/`; log in with the credentials step 8 printed.

> **Security — DEMO mode is ON by default.** Compose sets `DOGFOOD_DEMO=1`. In this mode a
> `Cookie: session=<token>` matching a seeded `DemoSession` is auto-authenticated as that user and
> the request is **CSRF-exempt** (`src/portal/middleware.py`). This exists so the automated
> acceptance checker can drive the app without a login flow; it is **not** an authentication
> mechanism. **Set `DOGFOOD_DEMO=0` for any real deployment** — with the flag off the shim is a
> no-op and the ordinary login flow applies. The middleware logs a loud warning at boot when it is on.

### Verify the integrity trail offline

These run with only Python, numpy, and `cryptography` — no database, no deployment access:

```bash
python -m audit.verify <bundle_dir>       # replay the SHA-256 audit hash-chain + check the signed checkpoint
python -m normalize.verify <bundle_dir>   # re-run the estimator from pinned inputs -> same signed result_hash
python -m normalize.release <bundle_dir>  # both of the above, plus four cross-links, in one command
```

Their honest scope — what a PASS does and does not bind — is documented in
[Threat model](THREAT-MODEL.md).

## Documentation

- **[Architecture](ARCHITECTURE.md)** — request lifecycle, the nine code units, the DEMO-vs-real
  auth split, the integrity spine, rate limiting, deployment topology, and an explicit
  implemented-vs-planned table.
- **[Data model](DATA-MODEL.md)** — every shipped model, its fields, constraints, and
  relationships, plus which tables are live-endpoint vs seed-only, with an ER diagram.
- **[Judging & normalization](JUDGING.md)** — the score-normalization method, the reproducibility
  contract, and the ablation table.
- **[Threat model](THREAT-MODEL.md)** — the security controls that ship, and those that are design
  only, declined, or accepted risk.

## License

See the `LICENSE` file in the repository root.

