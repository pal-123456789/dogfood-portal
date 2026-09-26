#!/usr/bin/env bash
# tools/smoke.sh -- one-shot acceptance smoke, run entirely inside a SINGLE wsl session.
# Why a script (not inline `wsl -- bash -lc '...'`): (1) the loop/quoting survives the
# PowerShell->wsl->bash hop intact, (2) the whole sequence shares one distro lifetime, so
# the stack can't be torn down between separate `wsl.exe` calls. Run it with:
#   wsl -d Ubuntu -- bash /mnt/d/dogfood/portal/tools/smoke.sh
set -u
cd /mnt/d/dogfood/portal || exit 1
COMPOSE="docker compose"

echo "=== 1. bring the stack up (idempotent; recreates with the restart policy) ==="
if ! $COMPOSE up -d --wait; then
  echo "--- up --wait did NOT report healthy; recent web logs: ---"
  $COMPOSE ps
  $COMPOSE logs --tail=80 web
  exit 1
fi
$COMPOSE ps

echo; echo "=== 2. /healthz ==="
curl -fsS http://localhost:8000/healthz && echo

echo; echo "=== 3. verify_demo (seed coherence) ==="
$COMPOSE exec -T web python src/manage.py verify_demo

echo; echo "=== 4. DEMO-auth proof: each cookie -> its user/role ==="
for t in org_7f2a jdg_a_91bc jdg_b_44de prt_2e88; do
  echo "-- session=$t --"
  curl -fsS --cookie "session=$t" http://localhost:8000/debug/whoami && echo
done

echo; echo "=== 5. replay.py -- the 7-check green ladder ==="
python3 tools/replay.py
echo; echo "=== smoke complete ==="
