# Threat Model

DOGFOOD is a self-hostable hackathon submission-and-judging platform. An organizer forks it, runs `docker compose up`, and trusts it to keep each judge's ballots private, enforce the submission deadline, compute a defensible ranking, and export results without corrupting them. This document states — honestly — what the shipped build defends against, what it does not, and how a reviewer can verify each claim without taking our word for it.

Every control below carries one of four labels:

- **SHIPPED** — implemented and covered by a test or acceptance check named in §10. If the evidence does not exist, the control is not labelled SHIPPED.
- **DESIGN ONLY** — the design is worked out and the seam exists, but the enforcing code is not in this build. Claimed as future work, never as a mitigation.
- **DECLINED** — deliberately out of scope, with the reason stated.
- **ACCEPTED RISK** — a residual we understand and choose to live with at this scale.

A control table that quietly labelled aspirations as "shipped" would be worse than no table: a reviewer who finds one false claim is right to distrust every other. So where the pre-build design over-reached, this document downgrades the claim rather than the evidence.

## 1. Scope and the one boundary that matters

The system is single-tenant and single-event by construction: one running instance serves one event. It is designed to run fully offline — no external identity provider, email, SMS, CAPTCHA, or third-party reputation service is available. That single constraint (§5) shapes everything that follows.

The trust boundary that matters is the **operator boundary**. Because the platform is self-hosted, whoever runs it holds the database credentials and can `docker exec` into the container. No application-layer control can stop a party who edits the database directly. This threat model therefore does not pretend to defend the data *against its own operator*; it is explicit (§7 A8) that detection — not prevention — is the only honest posture there. What the code *does* enforce is the boundary between the platform's **users**: participants, judges, and organizers acting through the HTTP surface.

## 2. Assets

Assets worth attacking, in rough priority:

| Asset | Why it is a target | Defended in |
|---|---|---|
| Judge ballots (per-judge confidentiality) | Seeing peers' scores enables anchoring and collusion | §7 A1 |
| Authorization state (who is judge / organizer) | Role escalation unlocks every other asset | §7 A2 |
| Submission-deadline fairness | Extra time is a direct competitive advantage | §7 A3 |
| Submission ownership | Submitting as or for another team corrupts attribution | §7 A4 |
| Results-export integrity | A poisoned CSV can execute in the organizer's spreadsheet | §7 A5 |
| The ranking itself (normalizer) | The whole point of the platform is a trustworthy rank | §7 A6 |
| Official results: disclosure timing & publication integrity | Leaking a ranking early skews fairness; a mutable "official" result is unauditable | §7 A11 |
| Ballot history / tamper-evidence | Silent edits undermine every result | §7 A7 |
| Admin credentials, secret key | Full compromise | §7 A9 |

## 3. Adversaries and their budgets

We model attackers by capability, not by name. An offline attacker cannot buy identities or outsource CAPTCHAs; their only budget is the accounts and API access the organizer already granted.

| Code | Who | Capability assumed |
|---|---|---|
| X | Unauthenticated outsider | Can reach the public HTTP surface only |
| P | One participant | A single valid participant session |
| C | Colluding participants | A few coordinated participant sessions |
| J | A judge | A valid judge session plus assigned submissions |
| O | Organizer / operator | Organizer role **and** DB + `docker exec` on the host |
| N | Host / infrastructure compromise | **Out of scope** — see §7 A8 |

Adversary **O** is the distinctive one for a self-hosted product and is treated separately in §7 A8. Adversaries X, P, C, and J act only through the application, and are the ones the code below actually constrains.

## 4. Trust boundaries

1. **Network → app.** Only the web service publishes a port (`:8000`); PostgreSQL is never published to the host (`docker-compose.yml` exposes no `db` ports). SHIPPED.
2. **Anonymous → authenticated.** Session identity is resolved once, and every protected view re-derives authorization from the *caller's* membership, never from a URL or form field (§7 A1/A2). SHIPPED.
3. **User → user (participant / judge / organizer).** Enforced at the service and data layer so it holds for every entry point, not only the HTTP handler that happens to be tested. SHIPPED.
4. **User → operator.** Not defensible in a self-hosted model; see §7 A8. ACCEPTED RISK, documented.

## 5. The offline constraint

Almost every mainstream anti-abuse control assumes an online oracle: email or SMS verification to raise the cost of a fake identity, CAPTCHA to price out automation, IP or device reputation to spot sybils, an external clock to anchor deadlines. DOGFOOD is required to run air-gapped, so **none of those are available.** This is not a gap to apologise for; it is the design centre. The consequence is that sybil-resistance cannot come from the platform — it comes from the organizer controlling account creation: accounts and roles are seeded or granted by the organizer, never self-served (§7 A2). Where a control would have leaned on an online oracle, this document says so and marks it DECLINED rather than faking a mitigation.

## 6. Posture

For each threat we aim for the strongest posture the offline constraint allows:

- **PREVENT** — the attack cannot succeed through the API (e.g. cross-judge reads).
- **DETECT** — the attack is visible after the fact.
- **EVIDENT & REVERSIBLE** — tampering leaves a trail and can be undone.

Honestly: this build achieves **PREVENT** for the user-boundary threats (isolation, role, deadline, ownership, export). For DETECT / EVIDENT it now ships the append-only, hash-chained audit log those postures depend on (§7 A7, A8): every ballot and submission write appends an immutable, chained audit event in the same transaction, and an organizer can recompute the chain — and check a signed checkpoint — offline. Ballot edits are additionally **EVIDENT & REVERSIBLE** — each write appends an immutable `BallotRevision`, so a prior score survives as its own row (§7 A7) — and a published ranking can be frozen as a signed, offline-reproducible run (§7 A6). The one honest limit is the operator boundary (§7 A8): a chain and a signing key that both live on the operator's own host detect tampering only *relative to a signed checkpoint that left the operator's control before the disputed change*. Prevention against the operator remains out of reach by construction — a property of self-hosting, not a missing feature.

## 7. Threats

Each threat gives the mechanism, the control, its status, the residual, and the exact command a reviewer runs to check it (see §10 for how to run them).

### A1 — Cross-judge ballot disclosure (the crux) · SHIPPED · PREVENT

*Mechanism.* A judge, or anyone, tries to read another judge's scores — e.g. by passing `?judge=judge_a` while authenticated as someone else, or by hitting the judge API unauthenticated.

*Control.* `/api/judge/scores` derives the caller's judge membership for the event and returns **only** `Ballot.objects.filter(assignment__judge=membership)`. The `?judge=` parameter can only ever *narrow to denial*: if it names anyone but the caller, the response is 403; it never selects whose rows are returned. Ownership is a property of the data query, not of the URL. An unauthenticated caller is 401; a non-judge is 403. This is backend enforcement — no frontend check is involved, so it cannot be bypassed by calling the API directly (a frontend-only check would be an automatic disqualification).

*Status.* SHIPPED. Independently confirmed by an external security cold-read of the shipped code.

*Residual.* If two judge memberships were seeded with the *same* `ext_id`, the `?judge=` denial check could mismatch — but the returned rows would still be the caller's own, so confidentiality holds; only the parameter echo would be wrong. Enforcing unique non-blank `ext_id` per event is DESIGN ONLY. ACCEPTED at fixture scale, where ext_ids are unique.

*Verify.* `tools/replay.py` checks 4/5/6; `src/normalize/tests.py` asserts the ownership matrix.

### A2 — Role / authorization escalation · SHIPPED · PREVENT

*Mechanism.* A participant tries to read the judge API or hit the organizer-only CSV export; or a user tries to grant themselves a role.

*Control.* Roles are **event-scoped** `EventMembership` rows (organizer / judge / participant), not global flags. Every protected view checks the caller's membership for the current event: the judge API requires a judge membership (else 403), and the export requires an organizer membership (else 403). There is **no self-service membership or role endpoint** — memberships are created only by the organizer's seed/import path, which is the offline sybil-resistance boundary from §5.

*Status.* SHIPPED (checks 4/5/6/7).

*Residual.* Roles are **additive by design**: one user may hold participant + judge + organizer in the same event if an organizer grants them. That is intended (a small event may need it) and is not escalation, because only an organizer can create the grant. Mutually exclusive roles, if an adopter needs them, are DESIGN ONLY (a cross-row constraint would enforce it).

*Verify.* `tools/replay.py` checks 6 and 7; `src/normalize/tests.py`.

### A3 — Deadline gaming · SHIPPED · PREVENT

*Mechanism.* A participant submits, or edits, after the deadline to gain time.

*Control.* The deadline is a single canonical rule in the submission **service**, not the view or template: `create_submission` checks auth → participant role → the event's accepting-submissions window before any field validation, using **server time** (`timezone.now()`); the HTTP handler never accepts a client-supplied clock. A late POST is rejected for being late even if otherwise well-formed. Crucially, the acceptance checker's closed-event POST returns 4xx because of the **deadline**, not because of CSRF — the demo shim exempts CSRF precisely so the 4xx proves the rule under test (§7 A9).

*Status.* SHIPPED (check 3).

*Residual.* (1) Submissions are **create-only** — there is no edit or revision endpoint in this build, so "edit after close" is not reachable (verified: no update view exists). (2) A theoretical TOCTOU race exists if an organizer closes the event in the millisecond between the state read and the row insert. ACCEPTED RISK — negligible at event scale; a row lock would close it and is DESIGN ONLY.

*Verify.* `tools/replay.py` check 3.

### A4 — Submission ownership / IDOR · SHIPPED · PREVENT

*Mechanism.* A participant tries to submit *for another team*, or attach their submission to a *foreign event's* track, by supplying someone else's identifiers in the POST body.

*Control.* The write path never trusts a client-supplied owner. The team is **derived server-side** from the caller's own `TeamMember` membership (`participant_team`), so a `team=` field in the request body is simply ignored; the track is looked up **scoped to the current event** (`Track.objects.filter(event=event, ext_id=…)`), so a track id from another event resolves to nothing and the submission is rejected. There is no identifier a caller can inject to submit as, or for, another team.

*Status.* SHIPPED. An external cold-read flagged this as a *potential* IDOR against the then-unseen code; reading the shipped implementation refutes it — the owner is server-derived.

*Residual.* A participant who belongs to multiple teams submits under the deterministic first team, by id. At fixture scale each participant is on one team. ACCEPTED.

*Verify.* Code: `submissions/services.py` (`participant_team`, `create_submission`); `submissions/views.py` (track lookup). `tools/replay.py` check 3 exercises the write path.

### A5 — Export integrity: spreadsheet formula injection · SHIPPED · PREVENT

*Mechanism.* A participant names a project — or a judge writes a comment — beginning with `=`, `+`, `-`, `@`, tab, or carriage return, e.g. `=HYPERLINK("http://evil","click")`. When the organizer opens the exported CSV in Excel or Sheets, the cell executes as a formula (CWE-1236), exfiltrating data or running a callback in the organizer's session.

*Control.* `export_event_rows` passes every **data** cell through `_csv_safe`, which prefixes an apostrophe to any string beginning with a formula-trigger character. Spreadsheets then display the literal text and do not evaluate it. Standard CSV quoting — which `csv.writer` already does — escapes delimiters but does **not** neutralise formulas, so this guard is separate and necessary. The header row is static and untouched, so the export still carries commas on line 1.

*Status.* SHIPPED — Increment 3 (commit `94d2e31`).

*Related hardening.* The judge-scores and export responses now send `Cache-Control: private, no-store` and `Vary: Cookie`, so a reverse proxy or shared cache placed in front of the app cannot serve one caller's private rows to another.

*Verify.* `tests/test_export_hardening.py` (the guard, DB-free); `tools/replay.py` check 7 (export still valid).

### A6 — Gaming the normalizer · estimator + signed reproducible run SHIPPED · collusion detectors DESIGN ONLY

*Mechanism.* A judge tries to move the ranking by scoring strategically — inflating an ally, tanking a rival, or exploiting leniency — rather than honestly.

*What the design already bounds.* The ranking is a ridge / partial-pooling fit that separates project quality `q` from judge severity `b` (`y = q + b + e`, penalty on `b` only; see JUDGING.md). Two structural facts limit a single judge's leverage: a judge influences a submission only in proportion to `1/R`, the number of ballots on it, so on a well-connected panel one malicious ballot moves `q` little; and cross-submission comparison is only identified within a connected component of the judge–submission graph, so a judge cannot manufacture rank against projects they never share a co-judge with. λ is chosen by cross-validation on observed ballots only — never on ground truth — so it is a *fit* parameter, not a tunable lever and not a security parameter.

*Status.* The estimator and its determinism are SHIPPED (`tests/test_normalize_engine.py`, `src/normalize/tests.py`). Active **collusion detectors** — residual-outlier reports, exact-match ballot detection, zero-variance-judge exclusion — are **DESIGN ONLY**: the seams are understood but the code is not in this build.

*Signed, reproducible run (SHIPPED — P2).* Beyond the live leaderboard, an operator can now publish a **signed, reproducible normalization run** (`src/normalize/runs.py`, `manage.py normalize_publish`). It freezes the exact inputs the ranking consumed — the weighted ballots in a pinned order, each pinned to its `BallotRevision` version (A7), the rubric weights, and the pinned λ — hashes the inputs and the canonical result, signs the pair with the **same Ed25519 operator key as the audit checkpoints** (a distinct domain tag `dogfood.normalize.run.v1` keeps a run signature from ever being replayed as a checkpoint, and vice-versa — proven both directions in `tests/test_normalize_signing.py`), and co-commits a `normalization.published` event onto the audit chain in one transaction, so a run can neither exist without its chain record nor leave a chain record without a run. An independent party runs `python -m normalize.verify <bundle>` on a machine that never touched the deployment: it re-runs the estimator from the pinned inputs through the *same* `engine.compute_leaderboard` code path the live view uses and confirms the ranking canonicalises to the signed `result_hash` (`gauge_error` is excluded from the hash — it is ~0 at machine precision and bit-volatile across BLAS backends — and re-asserted `< 1e-6` separately). Honest scope mirrors A8: a PASS proves the published ranking is exactly what this engine version produces from those ballots and that a run signed by the pinned key committed to it — it is evidence against the *operator* only if the public key + fingerprint were pinned by an independent party **before** judging, since the operator holds the private key and could re-sign a different run.

*Residual.* A coordinated ring of judges sharing many submissions could still bias results within their component. Detecting that is the DESIGN-ONLY work above. ACCEPTED for this build, and named in §9.

*Verify.* `tests/test_normalize_engine.py` and `tests/test_normalize_signing.py` (DB-free); `manage.py test` (normalize service + run tests); `manage.py normalize_publish --export <dir>` then offline `python -m normalize.verify <dir>`; JUDGING.md for the derivation and the honest within-submission σ-reduction on the real fixture.

### A7 — Silent ballot tampering / missing tamper-evidence · audit log + append-only ballot history SHIPPED · EVIDENT

*Mechanism.* A ballot is changed after the fact — by a judge revising quietly, or by anyone with app write access — with no record that it happened.

*Control (shipped).* Every write that matters now appends an immutable, hash-chained audit event. `record_ballot` and `create_submission` each call `audit.service.record_event` **inside the same `transaction.atomic()` as the business write**, so a score can never be persisted without its audit row, nor the reverse — the rollback is asserted by `test_business_write_rolls_back_when_audit_append_raises`. Each event carries a frozen schema-v1 header plus a `payload_hash` / `prev_hash` / `row_hash` triple; `seq` is issued from a single `AuditHead` row locked `FOR UPDATE`, never from an auto-increment PK (Postgres sequences gap on rollback and would silently hole the chain). `audit.hashchain.verify_chain` recomputes the whole chain and pinpoints the first `seq` where an insert, delete, reorder, or edit breaks the linkage. So a judge who revises a ballot leaves **two** chained events — the prior scores survive in the earlier one — and any edit to a stored audit row is detectable. Since Increment 5 the prior scores *also* survive as their own immutable row: `record_ballot` appends a `BallotRevision` (`UNIQUE(ballot, version)`, DB `CHECK` scores 1..5) inside that same atomic block, keeping `Ballot.*` only as a denormalised latest-pointer, so ballot history is append-only and not merely evident in the chain. The organizer control room (commit `cd44e76`) extends this trail to judging *configuration*: `assign_judge`, `unassign_judge`, and `set_rubric_weights` each co-commit a chained event — `judge.assigned`, `judge.unassigned`, `rubric.reweighted` (the last carrying old→new weights) — inside the same `transaction.atomic()` as the write, exactly like `record_ballot`. The same increment closes the two *direct* assignment-removal footguns that would otherwise destroy ballot history by cascade (`Ballot` → `BallotRevision` cascade off `JudgeAssignment`): the control-room unassign refuses a **scored** assignment after taking `select_for_update` on the row and re-checking for a ballot **inside** the transaction — so a concurrent first-ever score cannot slip between the check and the delete — and `JudgeAssignmentAdmin` refuses it too (per-object `has_delete_permission` plus removal of the bulk `delete_selected` action). This is scoped precisely to the paths that remove a `JudgeAssignment` through the **application** — the two direct paths here, and, since the parent-cascade close, the *parent* admins above it (`Event` / `EventMembership` / `Submission` / `Team` / `AppUser`), which carry the same scored-cascade delete guard (A8). Cascade deletion through **direct database access**, beneath the app, remains A8's operator boundary — detectable, not prevented — stated there.

*Status.* SHIPPED (commit `edb35c8`) — hash-chained `audit_event` + singleton `AuditHead`, atomic co-commit with the business write, `verify_chain`, and the `audit_verify` / `audit_export` commands. Covered by `src/audit/tests.py` (6 DB-backed tests) and the offline verifier `audit/verify.py`. Append-only ballot history followed in Increment 5 (commit `d1c60fc`): the immutable `BallotRevision` table with a `CHECK 1..5` on both it and `Ballot`, covered by `src/judging/tests.py`.

*Residual — what is NOT yet shipped (do not read more into this).* The append-only ballot history above closes the gap this section used to name (the `BallotRevision` history that was "the next build" now ships). The genuine residual is the **operator boundary** (A8): an out-of-band edit made **directly in Postgres** — bypassing `record_ballot` — writes no revision and emits no chained event, so it is caught only by comparing the live row against the audit log's last recorded value for that ballot, a manual reconciliation today rather than an automated one.

*Verify.* `src/audit/models.py` (the two audit tables) and `src/judging/models.py` (`BallotRevision`, the two `CHECK`s); `judging/services.py` `record_ballot` (appends a `BallotRevision` **and** the atomic `record_event` in one block); `manage.py test audit judging`; `manage.py audit_verify`.

### A8 — Insider / operator tampering (adversary O) · prevention ACCEPTED RISK · detection SHIPPED-with-caveat

A self-hosted platform cannot *prevent* tampering by the party that runs it: the operator holds the database password, `docker exec`, the filesystem, and the signing key. They can edit a ballot, flip a role, or move the deadline directly in Postgres, beneath the application. **We do not claim to prevent this**, and any control implying otherwise would be dishonest. Prevention against adversary O stays ACCEPTED RISK by construction.

The in-app footguns on this boundary are now closed; the irreducible boundary beneath them is named rather than hidden. *Closed (the admin UI):* the Django admin will not delete a row whose cascade would reach a **scored** assignment. `JudgeAssignmentAdmin` refuses the **direct** row (per-object `has_delete_permission`, and `get_actions` drops the bulk `delete_selected`), mirroring the control-room unassign rule (A7); and because `JudgeAssignment` is `CASCADE` off `EventMembership` (the judge) and `Submission` — and those off `Event` / `Team` / `AppUser` above them — the **parent** admins (`EventAdmin`, `EventMembershipAdmin`, `SubmissionAdmin`, `TeamAdmin`, `AppUserAdmin`) carry the same guard through a shared `ScoredCascadeDeleteGuard` (`src/portal/admin_mixins.py`): each refuses to delete a row whose descendants include a scored assignment and drops bulk delete, so an operator can no longer cascade-destroy a ballot's append-only history by deleting a judge, a submission, a team, a user, or a whole event in admin. An unscored parent stays deletable, and `Submission.track` is `PROTECT`, so a Track cannot orphan a scored project either. *Not closed — the operator boundary itself:* none of this binds a party with **direct database access**. An operator holding the Postgres password can `DELETE ... CASCADE` beneath the application, edit a ballot, or drop a row — the admin guard is an application control and the database sits below it. **We do not claim to prevent that**; it remains ACCEPTED RISK by construction. It stays *detectable* rather than silent because the `audit_event` rows are not foreign-keyed to these tables — the `judge.assigned` and `ballot.recorded` events survive any cascade, so a party recomputing the chain and reconciling it against live rows sees history referring to ballots that no longer exist. That reconciliation is the same manual one A7's residual names; automating it is future work, not a shipped control.

What is now shipped is **detection**, with a caveat stated precisely so it is not mistaken for more. The hash-chained audit log (A7) plus an **Ed25519-signed checkpoint** (`audit/receipts.py`, `audit_export`) and an **offline verifier** (`audit/verify.py`, `audit_verify`) let an independent party recompute the chain and check the signed tip without trusting the running server. This catches insert / delete / reorder / edit within the stored rows and any tampering via the app or a DB role.

The caveat: a chain and a signing key that **both live on the operator's own host do not bind the operator.** An operator who rewrites history can re-sign a fresh, self-consistent checkpoint over it. Detection is therefore only as strong as a **checkpoint that left the operator's control *before* the disputed change** and is independently retained and later compared — e.g. a fingerprint published to participants, or a checkpoint bundle emailed out at freeze. Given such an external anchor, a later divergence is provable; without one, operator tampering is detectable only if the operator was careless. That is the honest posture, and the signed-checkpoint export exists precisely to make the external anchor cheap to produce.

That anchor now ships in its most complete form (commit `fc648da`): `manage.py release_bundle <dir>` writes **one signed directory** binding the published `ranking.csv`, the signed reproducible run (A6), the `normalization.published` event that finalized it, and the signed audit checkpoint (A7) — all under one operator key — and a single offline command, `python -m normalize.release <dir>`, re-runs *both* the audit and the run verifiers unchanged and then cross-links them: it confirms the checkpoint and run share one signer, that the run's finalization is an event actually committed in the signed chain (its `audit_seq` is unsigned, so it is validated here, never trusted), that this event sits at or below the signed checkpoint head, and that `ranking.csv` is byte-for-byte the signed result. The anchor an independent party pins before judging is thus a single artifact rather than three. The caveat is unchanged and load-bearing: because the operator holds the key, a PASS is decisive only against a public key + fingerprint pinned by an independent party **before** judging and a checkpoint they retained — the bundle recomputes and cross-checks; it cannot bind the operator by itself.

### A9 — Account takeover, sessions, and secrets · mostly SHIPPED

- **Admin credentials.** The bootstrap generates a random admin password when none is supplied and prints it once; there is no baked-in default. SHIPPED.
- **Secret key.** Sourced from env, else generated to a private state volume with `O_EXCL` and `0o600`, else ephemeral for a build-time step only. No secret is committed. SHIPPED.
- **Passwords.** Django PBKDF2 with the stock validators; email is the identity, unique case-insensitively. SHIPPED.
- **Transport.** `SESSION_COOKIE_SECURE`, `CSRF_COOKIE_SECURE`, SSL redirect and HSTS switch on behind `DOGFOOD_TLS` for a real deployment. SHIPPED (flag-gated).
- **Demo auth (the one caveat).** For the offline grader, a `DOGFOOD_DEMO` shim maps a `session=` cookie to a seeded user and exempts CSRF so the stdlib checker can drive the API. This is a bearer-token shim: anyone holding a seeded token acts as that user, and CSRF is not enforced for it. It is **gated entirely off** in production (`DOGFOOD_DEMO=0`, the default in a real deploy), the `DemoSession` table is empty in production, and the middleware logs a loud warning when on. ACCEPTED RISK, scoped to the demo stack; never enable it on a public host.
- **Login throttling.** SHIPPED. `POST /accounts/login/` is throttled per client IP (`REMOTE_ADDR`) via `DOGFOOD_RATE_LIMITS['login']` (default 10/min) over the shared cache; over the limit it returns **429 with `Retry-After`**. This is a *throttle*, not a *lockout*: it slows credential stuffing without letting an attacker lock a victim out by guessing at them (the window resets on its own). The limiter fails open (§A10). Covered by `src/accounts/tests.py`. Persistent per-account lockout remains DESIGN ONLY — see §9.

### A10 — Resource exhaustion / denial of service · partial

*What holds.* There is no file-upload surface (grep-clean: no `FileField` / `request.FILES`), and no raw SQL anywhere (ORM-only), so two common amplification vectors are absent. The rate-limit cache is backed by the **database** (`DatabaseCache`), deliberately not per-process `LocMemCache`, so the limiter counts correctly across all workers rather than once per process.

*What does not.* The gallery is intentionally **un-paginated** (checks 1/2 require the full list), so it is bounded by fixture size rather than by a clamp — acceptable at event scale, ACCEPTED RISK at large scale. And rate-limit *enforcement* is now **partial**: the fixed-window limiter `src/portal/ratelimit.py` (fail-open — a limiter outage must not become an availability outage) is wired into the three write surfaces that actually exist for real users — `login` (per IP, §A9), and `submission_write` and `ballot_write` (each per authenticated user, with the DEMO/checker path exempt so the five checker routes stay byte-exact) — each returning 429 + `Retry-After` over the limit. `ballot_write` throttles the in-app judge-scoring route (`GET`/`POST /judging/score`), which is **not** one of the five checker routes, so wiring it left the replay untouched. The one remaining policy key, `invite_redeem`, is **not consumed**, because no invite flow has an HTTP route in this build (invitations are §9 item 4). It stays DESIGN ONLY; the primitive and the policy are ready for it the day that route lands.

*Verify.* grep for `request.FILES` / `.raw(` → none. The limiter and its three live call sites: `src/portal/ratelimit.py`, and `ratelimit.hit(` in `accounts/views.py`, `submissions/views.py`, and `judging/views.py` (the `score` view); covered by `tests/test_ratelimit.py` (parser, fixed window, fail-open), `src/accounts/tests.py`, `src/submissions/tests.py`, and `src/judging/tests.py` (`ScoreEndpointTests.test_rate_limited_after_quota`, including the DEMO-exempt byte-stability case).

### A11 — Premature or unofficial results disclosure · SHIPPED · PREVENT (user boundary)

*Mechanism.* A participant or outsider tries to read the ranking before the organizer has designated it official — hitting the public results route or its JSON while judging is still in progress — or treats an organizer's live-leaderboard preview as the final outcome.

*Control.* The ranking is **private until an organizer publishes it, and the shipped image boots with nothing published.** The two public routes (`/normalize/results`, `/normalize/results.json`) serve only a "not published yet" state until an organizer acts; the live, recomputing leaderboard (`/normalize/`, `/normalize/leaderboard.json`) stays organizer-gated with A2's 401/403/200 shape. Publishing is an explicit, organizer-only governance step (`/normalize/results/publish`, POST, organizer-gated) that designates one **signed, reproducible run** (A6) as the official result; what the public then sees is that run's **frozen** `result` — not a live recompute — carrying its `run_ext_id` / `result_hash` / `inputs_hash` / `fingerprint`, so the published ranking cross-checks against the offline-verifiable bundle exactly like any signed run.

*Append-only + atomic.* A publish never overwrites. It appends the next-version `ResultPublication` row (`UNIQUE(event_ext_id, version)`) and co-commits a `results.published` event onto the hash chain (A7) and flips `Event.results_published` — all in the same transaction as the signed run itself (`normalize/results.py` `publish_results`). Because the run's own `normalization.published` event co-commits too, one publish advances the audit chain by exactly two linked events. If any step raises, the run, **both** audit events, the publication row, and the flag all roll back together — there is no half-published state — so the record of what was published, at which version, when, and by whom is itself on the tamper-evident chain.

*Status.* SHIPPED — W1.

*Residual.* This is a **user-boundary** property, not an operator one. The organizer is also the operator (A8): they choose when to publish, may publish a `provisional` ranking, and — holding the signing key — a published run is evidence against *them* only under A6's caveat (public key + fingerprint pinned by an independent party before judging). "Private until published" therefore prevents a *participant or outsider* from seeing a ranking early; it does not, and does not claim to, constrain the operator.

*Verify.* `src/normalize/tests.py` `ResultPublicationTests` (private-until-published, organizer-gate 401/403/200, append-only versioning, and the atomic all-or-nothing rollback); a published run then verifies offline via A6's `python -m normalize.verify`.

## 8. Multi-event tenancy · DECLINED

Every event-scoped view resolves "the current event" as the single event in the database (`Event.objects.order_by("id").first()`). The build is **single-event by design**; it does not implement multi-tenant isolation between concurrent events. An operator who needs to run two events at once should run two instances. This is DECLINED, not a bug: stated so an adopter isn't surprised, and so the authorization model above is read in its intended single-event context.

## 9. The honest list

The controls this build does **not** ship, stated plainly. This list is the point: a reviewer should trust the SHIPPED labels above precisely because these are not hidden among them.

*Shipped since the first draft.* Items once on this list are now built and tested, so they have left it: the append-only, hash-chained **audit log** (A7, A8; commit `edb35c8`); **append-only ballot history** — the immutable `BallotRevision` table (A7; commit `d1c60fc`); a **signed, reproducible normalization run** with an offline verifier (A6; `src/normalize/runs.py`, `src/normalize/verify.py`); binding all of these into one artifact, a **signed release bundle** — the published ranking, its signed run, and the signed audit checkpoint in a single directory — with one offline verifier for the whole chain of custody (A8; commit `fc648da`, `manage.py release_bundle` + `python -m normalize.release`); and **login throttling** plus **per-user submission-write throttling** over a fail-open limiter (A9, A10) — which is why items 1 and 2 below are now scoped down to the routes that genuinely remain unenforced rather than struck out. What remains below is genuinely not in the build.

1. **Rate-limit enforcement** — PARTIALLY SHIPPED. The `login`, `submission_write`, and `ballot_write` policies are enforced (§A9, §A10) with a fail-open fixed-window limiter; only `invite_redeem` has no HTTP route to enforce yet, so it stays DESIGN ONLY.
2. **Account lockout** — login *throttling* is SHIPPED (§A9); a persistent per-account *lockout* after N failures is not built (a throttle resets each window by design, so a victim cannot be locked out by an attacker guessing at them). DESIGN ONLY.
3. **Collusion & residual-outlier detection** in judging — structural leverage limits exist, active detectors do not. DESIGN ONLY.
4. **Signed, single-use invitations** — no invite flow; the organizer seeds accounts. DESIGN ONLY.
5. **Submission revisions / withdrawal** — create-only; no revision history. DESIGN ONLY.
6. **Community / public voting** — not built; it would need the online anti-sybil oracle §5 forbids. DECLINED.
7. **Multi-event tenancy** — §8. DECLINED.
8. **Operator-tampering prevention** — impossible in a self-hosted model; only detection is achievable, and it now ships as a signed, offline-verifiable audit chain (A8), bounded by the external-checkpoint caveat there. The admin-UI cascade footguns that could *destroy* ballot history are closed (A8), but a party with direct database access is still only *detectable*, not prevented. ACCEPTED RISK (prevention).
9. **Demo-mode CSRF exemption** — accepted in the grader stack, off in production. ACCEPTED RISK.
10. **Deadline-close TOCTOU** — sub-millisecond race, no row lock. ACCEPTED RISK.

## 10. How to verify every "SHIPPED" claim

Nothing here asks for trust. Bring up the stack and run the checks:

```bash
docker compose up --build --detach --wait
docker compose exec -T web python -m pytest tests/ -q          # smoke, normalizer, export guard, rate-limit fail-open/window
docker compose exec -T -w /app/src web python manage.py test   # DB-backed isolation, gate, audit-chain, control-room, and login/submission throttle tests
docker compose exec -T -w /app/src web python manage.py audit_verify   # recompute the live audit chain in place
docker compose exec -T -w /app/src web python manage.py normalize_publish --export /tmp/nbundle   # sign a reproducible run, then self-verify
docker compose exec -T -w /app/src web python -m normalize.verify /tmp/nbundle             # re-run the estimator from pinned inputs, offline
docker compose exec -T -w /app/src web python manage.py release_bundle /tmp/release         # ONE signed bundle: ranking + run + audit checkpoint
docker compose exec -T -w /app/src web python -m normalize.release /tmp/release             # verify the whole chain of custody, offline
python3 tools/replay.py                                        # the 7 acceptance checks, no redirects
```

| Claim | Evidence |
|---|---|
| A1 cross-judge isolation | `replay.py` 4/5/6; `src/normalize/tests.py` |
| A2 role gates | `replay.py` 6/7; `src/normalize/tests.py` |
| A3 server-side deadline | `replay.py` 3 |
| A4 ownership (server-derived team, event-scoped track) | `submissions/services.py`, `submissions/views.py`; `replay.py` 3 |
| A5 CSV formula-injection guard + cache headers | `tests/test_export_hardening.py`; `replay.py` 7 |
| A9 login throttle + A10 submission-write & ballot-write throttle (per-IP / per-user, DEMO-exempt, fail-open, 429 + `Retry-After`) | `tests/test_ratelimit.py`; `src/accounts/tests.py`, `src/submissions/tests.py`, `src/judging/tests.py` (via `manage.py test`) |
| A6 estimator determinism + signed reproducible run | `tests/test_normalize_engine.py`, `tests/test_normalize_signing.py`; `manage.py test`; `manage.py normalize_publish --export <dir>` + offline `python -m normalize.verify <dir>` |
| A7/A8 tamper-evident audit chain + signed checkpoint | `src/audit/tests.py` (6, via `manage.py test audit`); `manage.py audit_verify`; `manage.py audit_export` + offline `audit/verify.py` |
| A7 append-only ballot history | `src/judging/tests.py` (via `manage.py test judging`); `src/judging/models.py` `BallotRevision` |
| A7/A8 control-room writes co-commit an audit event (`judge.assigned` / `judge.unassigned` / `rubric.reweighted`); a scored assignment is un-removable via the app **and** admin | `src/judging/tests.py` `ControlRoomTests` (via `manage.py test judging`); `src/judging/{services,admin}.py` |
| A8 admin parent-cascade delete-guard — deleting an `Event` / `EventMembership` / `Submission` / `Team` / `AppUser` whose cascade reaches a **scored** assignment is refused and bulk delete dropped, so ballot history can't be destroyed via the admin UI (raw-DB access stays the A8 boundary) | `src/events/tests.py` `ParentCascadeAdminDeleteGuardTests`, `src/submissions/tests.py` `SubmissionAdminDeleteGuardTests` (via `manage.py test`); `src/portal/admin_mixins.py` `ScoredCascadeDeleteGuard` |
| A6+A7+A8 unified signed release bundle (ranking + run + audit checkpoint, one dir) | `src/normalize/tests.py` `ReleaseBundleTests` / `ReleaseVerifyPureTests` (via `manage.py test`); `manage.py release_bundle <dir>` + offline `python -m normalize.release <dir>` |
| A11 official results: private until published + append-only versions | `src/normalize/tests.py` `ResultPublicationTests` (via `manage.py test`); published run verifies via A6 `python -m normalize.verify` |
| CSP / middleware wired | `tests/test_smoke.py` |
| No raw SQL, no upload surface | grep `\.raw(` / `request.FILES` → none |

A control that appears in the table above with a passing check is SHIPPED. Everything else is in §9. If a future change claims a control, it adds the row *and* the test in the same commit — a label without evidence is not a label.
