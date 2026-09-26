#!/usr/bin/env python3
# tools/replay.py
"""Local mirror of the DOGFOOD acceptance checker — my private green ladder (runbook §6).

Runs the same seven checks the graders' run.py performs, against a running demo stack, so I
can watch checks flip green as I build. It is driven entirely by .dogfood.toml (the same
contract file the grader reads), so there is one source of truth for URLs and cookies.

Deliberately STRICTER than run.py in exactly one way: it does NOT follow redirects. run.py
follows them, so a route that only reaches 200 after a 301/append-slash bounce would pass
there while hiding a routing bug. Here a redirect is a visible RED. stdlib-only; exits
non-zero if any check fails, so it doubles as a CI/pre-push gate.

Usage:
    python tools/replay.py                 # reads ./.dogfood.toml, base_url from it
    python tools/replay.py --config path   # explicit contract file
    python tools/replay.py --base URL      # override base_url (e.g. another host/port)
"""
import argparse
import http.client
import json
import sys
import urllib.parse

try:
    import tomllib
except ModuleNotFoundError:          # Python < 3.11
    tomllib = None

SEEDED_TITLE = "Glass Signal"   # prj_01: first fixture project, always on the un-paginated page


def _token(header_value):
    """'Cookie: session=org_7f2a' -> 'org_7f2a'. Tolerates a bare token too."""
    v = header_value.split("session=", 1)[-1].strip()
    return v.split(";", 1)[0].strip()


def _load_toml_fallback(path):
    """Parse the tiny, flat .dogfood.toml when tomllib is absent (Python < 3.11).

    Only the shapes this harness reads: [section] headers and `key = "value"` string
    pairs. Splits on the FIRST '=' so cookie/route values that themselves contain '='
    survive; skips arrays, inline tables, and comments (none are read here).
    """
    cfg, section = {}, None
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
                cfg.setdefault(section, {})
                continue
            if section is None or "=" not in line:
                continue
            key, val = (s.strip() for s in line.split("=", 1))
            if val[:1] in ("[", "{"):
                continue
            if len(val) >= 2 and val[0] in "\"'" and val[-1] == val[0]:
                val = val[1:-1]
            cfg[section][key] = val
    return cfg


def load_config(path):
    if tomllib is not None:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    return _load_toml_fallback(path)


def request(base, method, path, token=None, body=None):
    """One request, NO redirect following. Returns (status, body_text). status 0 == no connect."""
    u = urllib.parse.urlsplit(base)
    conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=10)
    headers = {}
    if token:
        headers["Cookie"] = "session=" + token
    data = None
    if body is not None:
        data = urllib.parse.urlencode(body).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        text = resp.read().decode("utf-8", "replace")
        return resp.status, text
    except (ConnectionRefusedError, OSError) as exc:
        return 0, "%s: %s" % (type(exc).__name__, exc)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="DOGFOOD acceptance replay (7 checks, no redirects).")
    ap.add_argument("--config", default=".dogfood.toml")
    ap.add_argument("--base", default=None, help="override base_url from the contract file")
    ap.add_argument("--title", default=SEEDED_TITLE, help="seeded title to look for (check 2)")
    args = ap.parse_args()

    cfg = load_config(args.config)
    base = args.base or cfg["portal"]["base_url"]
    auth = cfg["auth"]
    routes = cfg["routes"]
    org = _token(auth["organizer"])
    ja = _token(auth["judge_a"])
    jb = _token(auth["judge_b"])
    pa = _token(auth["participant"])

    results = []

    def check(name, ok, detail):
        results.append((ok, name, detail))

    # 1 & 2: public gallery, one GET serves both checks.
    st, body = request(base, "GET", routes["gallery"])
    check("1 GET %s no-auth -> 200" % routes["gallery"], st == 200, "status=%s" % st)
    check("2 gallery body contains a seeded title (%r)" % args.title,
          st == 200 and args.title in body,
          "status=%s title_present=%s" % (st, args.title in body))

    # 3: participant POST after the deadline -> 4xx (closed, not CSRF).
    st, _ = request(base, "POST", routes["submit"], token=pa,
                    body={"title": "Replay Probe", "summary": "x",
                          "track": "trk_01", "repo_url": ""})
    check("3 POST %s participant -> 4xx" % routes["submit"],
          400 <= st < 500, "status=%s" % st)

    # 4: judge_a sees own scores.
    st, body = request(base, "GET", routes["judge_scores"], token=ja)
    rows = None
    if st == 200:
        try:
            rows = json.loads(body).get("scores")
        except ValueError:
            rows = None
    check("4 GET %s judge_a -> 200 + own rows" % routes["judge_scores"],
          st == 200 and isinstance(rows, list) and len(rows) > 0,
          "status=%s rows=%s" % (st, None if rows is None else len(rows)))

    # 5: judge_b asking for judge_a's rows -> denied.
    st, _ = request(base, "GET", routes["peer_scores"], token=jb)
    check("5 GET %s judge_b -> 401/403" % routes["peer_scores"],
          st in (401, 403), "status=%s" % st)

    # 6: participant hitting the judge API -> denied.
    st, _ = request(base, "GET", routes["judge_scores"], token=pa)
    check("6 GET %s participant -> 401/403" % routes["judge_scores"],
          st in (401, 403), "status=%s" % st)

    # 7: organizer CSV export, header row has a comma.
    st, body = request(base, "GET", routes["csv_export"], token=org)
    first = body.splitlines()[0] if body else ""
    check("7 GET %s organizer -> 200 + comma in line 1" % routes["csv_export"],
          st == 200 and "," in first, "status=%s line1=%r" % (st, first[:60]))

    width = max(len(n) for _, n, _ in results)
    passed = 0
    for ok, name, detail in results:
        print("  %s  %-*s  %s" % ("GREEN" if ok else " RED ", width, name, "" if ok else detail))
        passed += ok
    print("\n%d/%d checks green  (base_url=%s)" % (passed, len(results), base))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
