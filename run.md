# Running the Dogfood Portal

The Dogfood Portal is a self-hostable platform for running a hackathon end to end — collecting
project submissions, judging them, and publishing a **signed, reproducible** final leaderboard
that anyone can verify offline.

This guide brings the whole stack up on your machine, publishes the results, and then walks
through the same short demo shown in the video. Every command below is copy-paste ready and has
been run start to finish.

## What you need

- **Docker** and **Docker Compose** (on Windows, run them inside **WSL**).
- A terminal opened in the project root: `/mnt/d/dogfood/portal`.

Everything runs in two containers — `db` (PostgreSQL) and `web` (the Django app). No other
services, no cloud account, nothing else to configure.

---

## 1 · Start the stack and publish the results

Run this once before the demo. It boots the containers and publishes the signed results so the
public pages have something to show (a fresh stack starts with nothing published yet).

```bash
cd /mnt/d/dogfood/portal
docker compose up -d --wait
curl -fsS http://localhost:8000/healthz && echo "  <- healthz OK"
docker compose exec -T web python src/manage.py publish_results --status final --note "Final results after judge review."
```

**What each line does**

- `docker compose up -d --wait` — starts `db` and `web` in the background and waits until both
  report **healthy**. On the very first boot this also runs the database migrations and seeds a
  demo event (an organizer, judges, participants, and their scored submissions).
  *First run, or after any code change, add `--build` so the image is rebuilt.*
- `curl -fsS http://localhost:8000/healthz …` — pings the app's health endpoint to confirm `web`
  is actually serving. You should see `ok  <- healthz OK`.
- `publish_results --status final …` — computes the normalized leaderboard, **signs it with the
  operator's Ed25519 key**, and records it as an append-only published result while writing a
  matching `normalization.published` entry onto the tamper-evident audit chain. This is what makes
  the signed results page go live. You'll see a short confirmation that the results were published.

---

## 2 · The 60-second demo

Four beats — two in the browser, two in the terminal.

### Beat 1 — the gallery  (~10s)

Open **http://localhost:8000/projects**

The public project gallery: every submitted project, visible to anyone with no login required.

### Beat 2 — the signed leaderboard  (~10s)

Open **http://localhost:8000/normalize/results**

The final results after normalization — the same numbers you just published, shown with their
result hash and signature. (This page is empty until `publish_results` has run, which is why it
is part of the setup step above.)

### Beat 3 — verify it offline  (~28s, the money shot)

```bash
docker compose exec -T web python src/manage.py release_bundle /state/release
docker compose exec -T -w /app/src web python -m normalize.release /state/release
```

- `release_bundle /state/release` — exports the complete evidence bundle into `/state/release`
  inside the container: the ranking CSV, the signed run (its pinned inputs and result), the audit
  checkpoint plus the chain prefix, and the operator's public key. It self-verifies before it
  finishes.
- `python -m normalize.release /state/release` — the **offline verifier**. With no access to the
  database, it re-runs the estimator from the pinned ballots, re-checks every signature and hash,
  and cross-links the signed run to the audit chain. It prints a series of `[PASS]` lines ending in
  **`RESULT: VERIFIED`** — hold on that frame. That is the whole point: anyone can re-verify the
  result independently, from the bundle alone.

### Beat 4 — reproducibility  (~12s)

```bash
python3 tools/replay.py
```

Replays the same seven acceptance checks a grader runs — driven entirely by `.dogfood.toml`, the
same contract file the grader reads — against the fixed routes, and is deliberately stricter in one
way: it does not follow redirects, so a route that only reaches 200 after an append-slash bounce is
a visible failure. It ends on the bright green **7/7 checks green** summary — hold on that, then
let the backing track carry the last few seconds.

---

## 3 · Stop the stack (optional)

```bash
docker compose down
```

Stops and removes the containers but keeps the database volume, so the next `up` starts with the
same data. To wipe the data and reseed the demo from scratch, use `docker compose down -v` instead.
