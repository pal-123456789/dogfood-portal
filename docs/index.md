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
- **Normalized results** — organizers see an embargoed leaderboard, per-rank credibility intervals,
  and a **pairwise-sensitivity** view (the model-based probability that one project outranks another,
  flagging every adjacent pair the data cannot resolve); these are clearly labelled *live recomputes*
  and are never part of any signed or published result. The public sees only the frozen, signed result
  once published (`/normalize/…`, `src/normalize/views.py`).
- **Read-only public API** — a versioned, read-only REST API at `/api/v1/` serves public event,
  track, team, and **submitted** project data as JSON, plus the official **frozen, signed** results
  once published, with an OpenAPI 3 schema (`/api/v1/schema/`) and a self-hosted Swagger UI
  (`/api/v1/docs/`). It is `GET`-only by construction — there are no write endpoints. Every endpoint
  is public **except** `/api/v1/me/`, which takes a personal Bearer token (minted with
  `manage.py mint_api_token`, stored only as a sha256 hash) and returns just the caller's own token
  metadata and event memberships; no endpoint ever exposes ballots, per-judge scores, judge
  identities, invitations, the audit chain, another user's data, or user PII
  (`src/api/{views,serializers,urls}.py`, `src/apitokens/*`).
- **Verifiable results certificate** — for a published event, `GET /api/v1/events/<id>/certificate/`
  (and the operator command `manage.py certificate`) returns a one-page, self-contained attestation
  over the *frozen, signed* normalization run: the engine version, result hash, signer fingerprint,
  public key, Ed25519 signature, the exact signed fields, and the public ranking (rank/title/q), plus
  the offline commands to re-verify it. It adds no new key or signature scheme — it restates and
  re-checks the run that `normalize.signing` already signed — carries no ballots, per-judge scores,
  judge identities, or PII, and refuses to certify anything that is not published
  (`src/normalize/certificate.py`, `src/api/views.py`).
- **Awards & public podium** — an organizer defines prizes and may assign winners at
  `GET/POST /events/<event>/awards/manage`; the **public** podium at `GET /events/<event>/awards`
  derives every place from the **frozen, signed** result and stays neutral, with no ranking, until
  results are published. A separate organizer-only **review top-up** view
  (`GET /events/<event>/awards/topup`) reads the *live, unsigned* standings to show where a few more
  reviews would most reduce uncertainty at each prize's cutoff — a planning aid, never a score, never
  the final ranking, and not fraud detection. Assigning a prize never changes the signed result
  (`src/awards/*`).
- **Community voting** — an organizer runs a separate, clearly-labelled popularity vote
  (`/voting/<event>/…`) with an eligibility list, email-confirmed single-use tokens, and
  one-allocation accounting. It is a second, **unsigned** publication surface and feeds nothing into
  the judged, signed result (`src/voting/*`).
- **Project comments** — a signed-in user leaves scoped comments on a submission, rate-limited per
  author (`src/comments/*`).
- **Signed participation records** — a participant obtains an Ed25519-signed participation record;
  the **public verification key** is served at `/.well-known/dogfood-signing-key` so a record can be
  checked offline (`src/records/*`).
- **Embeddable gallery widget** — a third-party page embeds a read-only project gallery via
  `/embed.js` and `/embed/<ext_id>` (`src/embed/*`).
- **Signed event bundle export/import** — an operator exports an event as an Ed25519-signed bundle
  and re-imports it on another instance with signature verification (`/bundles/…`, `src/bundles/*`).
- **Outbound signed webhooks** — an organizer registers endpoints that receive HMAC-signed event
  notifications, with an SSRF guard on the target URL and a `webhook_write` rate limit
  (`/webhooks/…`, `src/webhooks/*`).
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

- **[Architecture](ARCHITECTURE.md)** — request lifecycle, the seventeen code units, the
  DEMO-vs-real auth split, the integrity spine, rate limiting, deployment topology, and an explicit
  implemented-vs-planned table.
- **[Data model](DATA-MODEL.md)** — every shipped model, its fields, constraints, and
  relationships, plus which tables are live-endpoint vs seed-only, with ER diagrams.
- **[Judging & normalization](JUDGING.md)** — the score-normalization method, the reproducibility
  contract, and a pointer to the per-control ablation table.
- **[Ablation](ABLATION.md)** — a per-control table: what each integrity or credibility control
  buys, how the system would behave *without* it, and the file, test, and command a reviewer can
  check independently.
- **[Threat model](THREAT-MODEL.md)** — the security controls that ship, and those that are design
  only, declined, or accepted risk.

## License

See the `LICENSE` file in the repository root.

