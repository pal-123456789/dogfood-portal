# DOGFOOD 2026 portal

A self-hostable submission-and-judging platform for hackathons. Django + PostgreSQL, one command to run, network off, seeded with the shared fixtures.

```
docker compose up --build --wait   # portal on http://localhost:8000
```

Acceptance: `python3 run.py .dogfood.toml > acceptance-report.txt`. Licensed under MIT (see LICENSE).
