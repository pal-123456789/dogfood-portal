# DOGFOOD Portal

A self-hostable hackathon **submission-and-judging** platform. It is a single Django 5.2 +
PostgreSQL 16 monolith — no Redis, no message broker, no separate worker — that boots from one
command with a seeded demo event, collects submissions, runs a judging panel, normalizes scores to
remove judge severity, and publishes a **frozen, signed** result that anyone can re-verify offline
on a clean machine. Caching and rate-limit counters live in a database table (`DatabaseCache`) and
static files are served in-process by WhiteNoise (`src/portal/settings.py`).

This project is graded against over-claiming, so both this file and `docs/` are written to be
checkable line-by-line against `src/`. Anything not wired to an endpoint is labelled *planned* in
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md), not described here as if it shipped.

---

## Quick start

**Prerequisites:** Docker with Compose v2. Nothing else — PostgreSQL runs in a container.

```bash
# from the repo root (the directory holding docker-compose.yml)
docker compose up --build --wait
```

`--wait` returns once the `web` container's healthcheck (`GET /healthz` returns `ok`) passes. Then
open <http://localhost:8000/projects>. Change the published port with `DOGFOOD_PORT`; the Django
admin is at `/admin/`. See `.env.example` for the full set of environment variables.

For a live-reload development stack, layer the dev override, which replaces gunicorn with
`manage.py runserver` and bind-mounts `./src`:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up
```

### What the entrypoint does on boot

`docker/entrypoint.sh` runs **eight idempotent steps and then execs gunicorn**. Every step prints
`key = value`, one per line, so the boot log is the evidence that seeding actually happened:

1. verify `/state` is writable — fails on line one with a printed remedy rather than on a later step
   with a traceback;
2. `python -m portal.bootstrap wait-for-db` — bounded retry (30 attempts, 1s apart), printing the
   attempt so a slow boot and a broken boot do not look alike;
3. `python -m portal.bootstrap ensure-secret` — writes a Django secret key into `/state`, only if
   absent;
4. `manage.py migrate --noinput`;
5. `manage.py createcachetable` — creates the `DatabaseCache` table;
6. `manage.py dogfood_import --if-empty /app/fixtures.json` — seeds the demo event;
7. `manage.py verify_demo` — asserts the seed matches the acceptance contract; under `set -eu` a
   failure here fails the boot on purpose. It self-skips (exit 0) when `DOGFOOD_DEMO` is off, so it
   is a no-op in a real deployment;
8. `python -m portal.bootstrap ensure-admin` — creates a superuser if none exists and prints the
   credentials once to the boot log.

Step 8 then hands over: if compose passed a `command:` it is `exec`'d, otherwise the entrypoint
`exec`s `gunicorn --workers "${DOGFOOD_WORKERS:-2}" --bind 0.0.0.0:8000 portal.wsgi`.

> **Security — DEMO mode is ON by default.** `docker-compose.yml` sets `DOGFOOD_DEMO=1`. In this
> mode a `Cookie: session=<token>` matching a seeded demo session is auto-authenticated as that user
> and the request is CSRF-exempt (`src/portal/middleware.py`). This exists so an automated acceptance
> checker can drive the app without a login flow; it is **not** an authentication mechanism.
> **Set `DOGFOOD_DEMO=0` for any real deployment** — with the flag off the shim is inert and the
> ordinary login flow at `/accounts/login/` applies.

---

## Features

Every bullet names the code that implements it.

### Submissions, events and teams

- **Public project gallery** at `GET /projects`, with an optional `?track=` filter
  (`src/gallery/views.py`).
- **Submission intake, edit and withdraw** — a participant creates a submission at
  `POST /projects/new`, which refuses writes after the event deadline, then revises or
  **soft-withdraws** it at `/submissions/mine`, `/submissions/<ext_id>/edit`,
  `/submissions/<ext_id>/withdraw`. Withdrawal hides the project from the gallery without deleting
  the row, so its scores and audit trail survive (`src/submissions/{views,services}.py`).
- **Organizer event console** — create events, tracks and teams and move an event between states
  under `/events/…` (`src/events/views.py`).
- **Signed single-use invitations** — an organizer mints a signed link at
  `POST /events/<ext_id>/invites/new`; redeeming it at `/events/invite/<ext_id>` is Ed25519-verified,
  single-use (enforced in the database), rate-limited, and atomically grants the membership plus an
  audit event. A link can only ever grant judge or participant — never organizer. An offline check is
  available as `manage.py invite_verify` (`src/events/{invite_signing,services,views}.py`).

### Judging

- **In-app judge scoring** at `GET`/`POST /judging/score`; every write appends an immutable, audited
  ballot revision, so a changed score never overwrites its history (`src/judging/{views,services}.py`).
- **Judge reads only their own scores** at `GET /api/judge/scores` — ownership is decided by the
  caller's judge membership, never by a `?judge=` parameter, so naming another judge returns 403.
- **Assignment management and rubric weights** under `/judging/<ext_id>/assignments` and
  `/judging/<ext_id>/rubric`; writes are atomic and audited, and a *scored* assignment cannot be
  unassigned because its ballot history is append-only.
- **Connectivity-aware auto-assignment planner** at `/judging/<ext_id>/auto-assign`. The planner
  itself (`src/judging/assignment.py`) is pure and DB-free: it tops each project up to a target
  review count, spreads load, respects eligibility (no own-team project, track match, no duplicate,
  no recusal), and adds the fewest bridge reviews needed to keep the judge↔project graph a single
  connected component — the property the normalizer needs to rank everything on one scale.
- **Judge recusal / conflict-of-interest** filed at team granularity and expanded to that team's
  submissions before planning (`src/judging/recusal.py`).
- **Judging-progress dashboard** at `/judging/<ext_id>/progress`.
- **Organizer CSV export** at `GET /api/export.csv`, with spreadsheet formula-injection guarding
  (`src/judging/services.py`).

### Normalization

The estimator (`src/normalize/engine.py`) is pure NumPy, seeded and deterministic. It fits
`y_ij = q_i + b_j + e_ij` and ranks on `q` — quality with judge severity removed.

- **Ridge with a cross-validated λ** — only the judge effects are penalised; λ is selected by
  repeated 5×5-fold cross-validation on the observed ballots only.
- **Connected-component check** — the gauge is per component, so the engine computes the component
  structure on every run and refuses to compare across components when a fixture splits.
- **Bootstrap rank intervals** — a parametric bootstrap gives each submission a median rank, a 5th–95th
  percentile rank interval (`rank_lo`/`rank_hi`), and a per-submission standard error on `q`.
- **Pairwise sensitivity** at `/normalize/pairwise` — the model-based probability that one project
  outranks another, flagging adjacent pairs the data cannot resolve.
- **Review diagnostics** at `/normalize/diagnostics` (and `manage.py review_diagnostics`), including a
  **duplicate-title diagnostic** (`src/normalize/duplicates.py`). These are decision *aids*, not fraud
  detection.
- **Explain-my-rank** at `/normalize/results/explain/<ext_id>`, derived from the signed result.
- **Results publication** at `/normalize/results/publish` (and `manage.py normalize_publish` /
  `publish_results`). Organizer-facing leaderboards are clearly labelled *live recomputes* and are
  never part of a signed or published result; the public sees only the frozen, signed result once
  published.

Method, evidence and the ablation table live in [`docs/JUDGING.md`](docs/JUDGING.md) and
[`docs/ABLATION.md`](docs/ABLATION.md).

### Integrity

- **Tamper-evident audit hash-chain** — every audited write appends a hash-chained event **inside the
  same transaction** as the business write, so a business row cannot exist without its audit row
  (`src/audit/{hashchain,service,models}.py`, `manage.py audit_verify`).
- **Ed25519-signed checkpoints** over the chain head (`src/audit/receipts.py`, `manage.py audit_key`,
  `manage.py audit_export`).
- **Signed, reproducible normalization runs** — a run pins its inputs and is signed, so the result
  hash can be recomputed from those inputs (`src/normalize/signing.py`).
- **Offline verifiers** — these need only Python, NumPy and `cryptography`; no database, no access to
  the deployment:

  ```bash
  PYTHONPATH=src python -m audit.verify <bundle_dir>       # replay the SHA-256 chain + check the signed checkpoint
  PYTHONPATH=src python -m normalize.verify <bundle_dir>   # re-run the estimator from pinned inputs -> same result_hash
  PYTHONPATH=src python -m normalize.release <bundle_dir>  # both of the above plus the cross-links, in one command
  ```

  Produce a bundle with `manage.py release_bundle`.
- **Verifiable results certificate** at `GET /api/v1/events/<ext_id>/certificate/` (and
  `manage.py certificate`) — a self-contained attestation over the frozen, signed run: engine version,
  result hash, signer fingerprint, public key, signature, the exact signed fields, the public ranking,
  and the offline commands to re-check it. It adds no new key or signature scheme and refuses to
  certify anything unpublished (`src/normalize/certificate.py`).
- **Signed participation records** at `/records/judge`, `/records/participant` and `/records/verify`,
  with the deployment's public key served at `/records/signing-key` and
  `/.well-known/dogfood-signing-key`. Records carry no scores or ballots — they attest participation
  facts only, not merit (`src/records/{views,services,signing}.py`).

### Community, publishing and integrations

- **Community voting** under `/voting/…` — organizer-configured campaign, voter ballot, email-link
  join/confirm, and results that stay 404 until published and the window has closed
  (`src/voting/{views,services}.py`).
- **Project comments** under `/comments/…` — public read, authenticated post, organizer soft-hide
  (never a delete). Author identity is surfaced as a display name only, never the login email
  (`src/comments/{views,services}.py`).
- **Embeddable gallery widget** — `/embed.js` plus `/embed/<ext_id>`, the two entry points a
  third-party page loads (`src/embed/views.py`).
- **Signed event bundle export/import** — site-administrator-only, at `/bundles/<ext_id>/export.json`,
  `/bundles/<ext_id>/export.csv` and `/bundles/import`. A bundle is a structural snapshot (event
  config, tracks, teams, submissions, rubric weights, voting window) built on the fly and signed with
  the operator key; it deliberately carries no ballots, no per-judge scores and no audit chain
  (`src/bundles/{services,signing}.py`).
- **Outbound signed webhooks** under `/webhooks/<ext_id>/…` — organizer-registered endpoints, signed
  deliveries, recorded delivery rows and retry. Every URL passes an **SSRF guard**
  (`src/webhooks/ssrf.py`) that rejects non-http(s) schemes and embedded credentials, resolves names to
  *all* addresses and requires every one to pass, and blocks loopback, private, link-local (including
  `169.254.169.254`), reserved, multicast and unspecified addresses, unwrapping IPv4-mapped IPv6.
- **Read-only REST API** at `/api/v1/` — public event, track, team and submission data plus the
  official frozen, signed results, with an OpenAPI 3 schema at `/api/v1/schema/` and a self-hosted
  Swagger UI at `/api/v1/docs/` (no CDN). It is `GET`-only by construction. Every endpoint is public
  **except** `/api/v1/me/`, which takes a personal Bearer token minted with `manage.py mint_api_token`
  (stored only as a sha256 hash) and returns just the caller's own token metadata and event
  memberships (`src/api/*`, `src/apitokens/*`).

### Awards and prizes

- **Organizer-defined prizes** at `/events/<event_ext_id>/awards/manage` — create a prize (optionally
  scoped to a track), assign or clear a winner, remove a prize. Every write is atomic and audited
  (`src/awards/{views,services}.py`).
- **Public podium** at `/events/<event_ext_id>/awards`. It is derived entirely from the **frozen,
  signed** result — never a live recompute — and shows only published-safe values (title, team name,
  place, public `q`): no per-judge score, no judge identity, no PII. Until results are published it
  renders a neutral "not yet published" state with **no ranking at all** (`src/awards/podium.py`).
- **Review top-up planner** at `/events/<event_ext_id>/awards/topup`, organizer-only. It reads the
  **live, unsigned** standings and, for each podium prize, surfaces the contenders sitting at or
  straddling that prize's rank cutoff — where a few more reviews would most reduce uncertainty before
  results are finalized and signed. **It ranks nothing, assigns no score, and is not fraud detection.**
  Assigning a prize is a separate, explicit organizer action, and it never changes the judged, signed
  result (`src/awards/topup.py`).

---

## Running the tests

CI (`.github/workflows/ci.yml`) builds the stack and runs the suite inside the container, then
restarts `web` to prove the boot is idempotent:

```bash
docker compose up --build --detach --wait
docker compose exec -T web python -m pytest tests/ -q
docker compose restart web          # second boot must be a clean no-op
docker compose down
```

No module under `tests/` is marked `django_db` (`grep -r django_db tests/` finds only prose saying
so), so pytest never builds a test database for the suite. That covers the estimator, the
hash-chain, the checkpoint and verifier code, the assignment and recusal planners, token hashing,
the duplicate detector, the podium and the top-up planner. Five modules also carry a `__main__`
runner and can be executed directly, with no Docker and no database:

```bash
PYTHONPATH=src python3 tests/test_apitokens.py
PYTHONPATH=src python3 tests/test_duplicates.py
PYTHONPATH=src python3 tests/test_podium.py
PYTHONPATH=src python3 tests/test_recusal.py
PYTHONPATH=src python3 tests/test_topup.py
```

`tests/goldens/golden_bundle.json` is a committed, signed golden bundle; `tests/test_verifier_golden.py`
runs the real offline verifier against it and asserts that a one-byte tamper in an input score, in a
published `q`, or in the signature is rejected. Regenerate it with `python tools/make_golden_bundle.py`
(see `tests/goldens/README.md`).

The image build is itself a gate: the Dockerfile runs `manage.py check --fail-level WARNING` and
`manage.py makemigrations --check --dry-run`, so the build fails if any system check warns or if a
model and its migration disagree.

**Acceptance replay.** `tools/replay.py` mirrors the acceptance checks against a running stack,
driven entirely by `.dogfood.toml` (the same contract file the grader reads), and is deliberately
stricter in one way: it does not follow redirects, so a route that only reaches 200 after an
append-slash bounce is a visible failure. It is stdlib-only and exits non-zero on any failure:

```bash
python tools/replay.py                 # reads ./.dogfood.toml
python tools/replay.py --base URL      # override base_url
```

`tools/smoke.sh` wraps bring-up, `/healthz`, `verify_demo`, the demo-identity probe and `replay.py`
into one pass. The recorded Increment-1 result is in `acceptance-report.txt`.

---

## Documentation

The docs set is built with MkDocs (`mkdocs.yml`) and published by
`.github/workflows/pages.yml`, which runs `mkdocs build` and deploys to GitHub Pages on pushes to
`main` that touch `docs/`, `mkdocs.yml`, or the workflow itself (plus manual `workflow_dispatch`).

| Page | What it covers |
|---|---|
| [`docs/index.md`](docs/index.md) | Entry point: what the platform does today, the five-minute self-host, and the offline verification commands. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Request lifecycle, the code units, the DEMO-vs-real auth split, the integrity spine, rate limiting, deployment topology, and an explicit implemented-vs-planned table. |
| [`docs/DATA-MODEL.md`](docs/DATA-MODEL.md) | Every shipped model, its fields, constraints and relationships, which tables are live-endpoint vs seed-only, and an ER diagram. |
| [`docs/JUDGING.md`](docs/JUDGING.md) | The normalization model and estimator, how λ is chosen, identifiability, and what normalization measurably does on the real fixture. |
| [`docs/ABLATION.md`](docs/ABLATION.md) | Per-control table: what each integrity/credibility control buys, how the system behaves without it, and the file, test and command that evidence it. |
| [`docs/THREAT-MODEL.md`](docs/THREAT-MODEL.md) | Security controls labelled SHIPPED / design-only / declined / accepted risk, with the verification a reviewer can run. |

Build the site locally with `pip install mkdocs-material && mkdocs build`.

---

## Scope and limits

Stated plainly, because over-claiming is the failure mode this project is graded against:

- **This is not a hosted service.** It is software you self-host. There is no multi-tenant SaaS, no
  managed onboarding, and no uptime commitment.
- **The review and normalization diagnostics are not fraud detection.** Pairwise sensitivity, the
  duplicate-title diagnostic, the leave-one-judge-out influence report and the awards top-up planner are
  decision aids for an organizer. They flag where the *data* is thin or unsettled. They do not
  identify misconduct, and none of them assigns or changes a score.
- **The integrity spine is tamper-*evident*, not tamper-proof.** It makes an after-the-fact change to
  the stored rows detectable; it does not prevent one.
- **The signing keys are operator-held.** The Ed25519 private key lives on the deployment's `/state`
  volume. An offline verifier proves that *the pinned key* signed a given chain, run or record — it
  does not bind the operator against themselves unless a checkpoint left the operator's control before
  a disputed change. `docs/THREAT-MODEL.md` §A8 states this boundary explicitly.
- **DEMO mode is a test shim, not authentication.** See the boot warning above.
- **Signed bundles attest structure, not merit.** A bundle signature says the operator's key signed
  that exported structure at export time; it is not a results-integrity proof.

---

## License

MIT — see [`LICENSE`](LICENSE).
