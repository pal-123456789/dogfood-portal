# src/normalize/certificate.py
"""Verifiable results certificate (T4) -- a rendering + attestation layer over an ALREADY signed,
ALREADY published normalization run. It introduces NO new key, signature scheme, crypto primitive,
model, or migration: every value is copied verbatim from a signed NormalizationRun and the
ResultPublication that names it, and nothing here is ever recomputed.

A certificate restates, in one self-contained page, exactly what a published ResultPublication
points at: the event, the publication version + status, the ENGINE_VERSION, the signed run's
result_hash, the signer fingerprint, the operator's Ed25519 public key, the signature, and the
ranking (rank, submission ext_id, title, normalized q).

WHAT IT IS NOT. Not a new source of truth, not a measure of merit, not fraud detection. It attests
only that the operator's key signed the named run -- the same honest, retrospective scope as
normalize.signing / normalize.verify (docs/THREAT-MODEL.md): decisive only if an independent party
pinned public-key.pem + its fingerprint BEFORE judging. It REFUSES (raises CertificateError) if
asked to certify anything that is not a signed, published run, so a live recompute can never be
dressed up as a certificate.

PII. It excludes every participant identifier: no emails, no judge or display names, no ballots, no
per-judge or per-criterion scores. It carries only run.json-level fields (which contain no PII) plus
the public ranking columns already shown at /normalize/results and in ranking.csv.

Pure + Django-free at import time (only `signing` + stdlib), so the builder/renderers unit-test
without a database; the DB wrappers import the ORM lazily. Crypto is never reimplemented -- the
signature re-check reuses normalize.signing.
"""
from __future__ import annotations

import html
import json

from . import signing

CERTIFICATE_KIND = "dogfood.results-certificate.v1"

# Mirrors normalize.models.ResultPublication.{PROVISIONAL,FINAL}; kept as plain strings so the pure
# builder needs no Django import. Frozen with the model's status choices.
PROVISIONAL, FINAL = "provisional", "final"
_STATUSES = (PROVISIONAL, FINAL)

# Exactly the fields the run signature covers (normalize.signing.RUN_FIELDS order). Reproduced in the
# certificate so the Ed25519 signature is checkable from the document alone; none of them is PII.
_SIGNED_FIELDS = signing.RUN_FIELDS

_ATTESTATION = (
    "Attests that the operator's Ed25519 key signed the published normalization run named below. "
    "It restates a signed, frozen result; it is not a measure of merit and not fraud detection."
)

_UNSET = object()


class CertificateError(Exception):
    """Raised when asked to certify anything that is not a signed, PUBLISHED run."""


def _require(cond, msg):
    if not cond:
        raise CertificateError(msg)


def _published_at(publication) -> str:
    ts = getattr(publication, "recorded_at", None)
    if ts is None:
        return ""
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def build_certificate(publication, run, *, event_name: str = "",
                      public_key_pem: str | None = None) -> dict:
    """Build the machine-readable certificate dict for one PUBLISHED, signed run.

    `publication` exposes version / status / run_ext_id / event_ext_id / recorded_at (a
    ResultPublication); `run` is the signed NormalizationRun it names, exposing the RUN_FIELDS plus
    fingerprint, signature, result, audit_seq, lambda_value, n_boot, seed. `public_key_pem` is the
    operator's Ed25519 public key PEM, or None when unavailable in this context (a reader can still
    obtain public-key.pem from the signed release bundle).

    Raises CertificateError unless this is a real signed, published run -- never a live recompute.
    """
    _require(publication is not None,
             "no published results to certify (event has never been published)")
    version = getattr(publication, "version", None)
    status = getattr(publication, "status", None)
    pub_run_ref = getattr(publication, "run_ext_id", None)
    _require(isinstance(version, int) and version >= 1,
             "not a ResultPublication: missing/invalid version")
    _require(status in _STATUSES, "not a ResultPublication: unknown status %r" % (status,))
    _require(bool(pub_run_ref), "not a ResultPublication: missing run_ext_id")

    _require(run is not None, "published run %r is missing" % (pub_run_ref,))
    for attr in ("run_ext_id", "engine_version", "instance_id", "event_ext_id",
                 "inputs_hash", "result_hash", "created_at", "fingerprint", "signature"):
        _require(bool(getattr(run, attr, None)),
                 "cannot certify: not a signed run (missing %s)" % attr)
    _require(getattr(run, "run_ext_id") == pub_run_ref,
             "publication/run mismatch: %r vs %r" % (pub_run_ref, getattr(run, "run_ext_id")))
    result = getattr(run, "result", None)
    _require(isinstance(result, dict) and isinstance(result.get("rows"), list),
             "cannot certify: run carries no result rows")

    ranking = [{"rank": r.get("rank"), "submission": r.get("submission"),
                "title": r.get("title"), "q": r.get("q")} for r in result["rows"]]
    return {
        "kind": CERTIFICATE_KIND,
        "attestation": _ATTESTATION,
        "event": {"ext_id": run.event_ext_id, "name": event_name or ""},
        "publication": {"version": version, "status": status,
                        "published_at": _published_at(publication)},
        "engine_version": run.engine_version,
        "result_hash": run.result_hash,
        "signer_fingerprint": run.fingerprint,
        "signature": {"algorithm": "ed25519", "value": run.signature,
                      "public_key_pem": public_key_pem},
        "signed_material": {f: getattr(run, f) for f in _SIGNED_FIELDS},
        "run_provenance": {"audit_seq": getattr(run, "audit_seq", None),
                           "lambda_value": getattr(run, "lambda_value", None),
                           "n_boot": getattr(run, "n_boot", None),
                           "seed": getattr(run, "seed", None)},
        "ranking": ranking,
        "verification": _verification_block(),
    }


def _verification_block() -> dict:
    """Precise, honest offline-verification instructions -- string-only, no URLs/assets."""
    return {
        "verifier_command": "python -m normalize.release <bundle_dir>",
        "run_verifier_command": "python -m normalize.verify <bundle_dir>",
        "steps": [
            "This certificate copies its result_hash, signature, fingerprint and ranking verbatim "
            "from a signed, frozen normalization run; nothing here is recomputed.",
            "Check the signature from this document alone: the Ed25519 signature under "
            "signature.value covers the domain-tagged canonical bytes of signed_material (tag "
            "dogfood.normalize.run.v1, fields in normalize.signing.RUN_FIELDS order). Load "
            "signature.public_key_pem, verify, and confirm its fingerprint equals "
            "signer_fingerprint.",
            "Reproduce result_hash and confirm the ranking came from the ballots: obtain the signed "
            "release bundle (manage.py release_bundle <dir>) and run the verifier command on any "
            "machine with Python 3 + numpy + cryptography (no Django, no database). The result_hash "
            "here must equal run.json's, and the ranking rows are a human-readable excerpt of the "
            "signed result.json the verifier recomputes.",
            "Honest scope: the operator holds the private key, so this is decisive only if you "
            "pinned the public key and its fingerprint BEFORE judging. It attests a signature over a "
            "ranking; it is not a measure of merit and not fraud detection.",
        ],
        "scope": "signed, published-run attestation -- not merit, not fraud detection",
    }


def verify_certificate_signature(cert: dict) -> bool:
    """Re-check the certificate's Ed25519 signature using its embedded public key, via
    normalize.signing (no crypto reimplemented). Returns False if the PEM is absent, the fingerprint
    disagrees, or the signature does not verify over signed_material."""
    sig = cert.get("signature") or {}
    pem = sig.get("public_key_pem")
    if not pem:
        return False
    try:
        pub = signing.public_key_from_pem(pem.encode("ascii") if isinstance(pem, str) else pem)
    except Exception:
        return False
    if signing.public_fingerprint(pub) != cert.get("signer_fingerprint"):
        return False
    material = cert.get("signed_material") or {}
    try:
        return signing.verify_run(pub, signature=sig.get("value", ""),
                                  **{f: material.get(f) for f in _SIGNED_FIELDS})
    except Exception:
        return False


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


_NO_PEM = "(not embedded here -- obtain public-key.pem from the signed release bundle)"


def render_text(cert: dict) -> str:
    """Plain-text certificate."""
    ev, pub, sig = cert["event"], cert["publication"], cert["signature"]
    sm, prov = cert["signed_material"], cert["run_provenance"]
    out = ["DOGFOOD verifiable results certificate",
           "=" * 38, "", cert["attestation"], "",
           "Event:        %s (%s)" % (ev.get("name") or "-", ev["ext_id"]),
           "Publication:  v%s  status=%s  published_at=%s"
           % (pub["version"], pub["status"], pub["published_at"] or "-"),
           "Engine:       %s" % cert["engine_version"],
           "result_hash:  %s" % cert["result_hash"],
           "fingerprint:  %s" % cert["signer_fingerprint"],
           "signature:    %s (%s)" % (sig["value"], sig["algorithm"]),
           "", "Signed material (exactly what the signature covers):"]
    out += ["  %-14s %s" % (f + ":", sm.get(f)) for f in _SIGNED_FIELDS]
    out += ["Run provenance (NOT part of the signature):",
            "  audit_seq=%s lambda=%s n_boot=%s seed=%s"
            % (prov.get("audit_seq"), prov.get("lambda_value"),
               prov.get("n_boot"), prov.get("seed")),
            "", "Ranking (rank / submission / normalized q / title):"]
    out += ["  %4s  %-18s  q=%-9s %s"
            % (r.get("rank"), r.get("submission"), r.get("q"), r.get("title") or "")
            for r in cert["ranking"]]
    out += ["", "Public key (PEM):", (sig.get("public_key_pem") or ("  " + _NO_PEM)).rstrip(),
            "", "How to verify:"]
    out += ["  - " + s for s in cert["verification"]["steps"]]
    out += ["", "Offline verifier: " + cert["verification"]["verifier_command"], ""]
    return "\n".join(out)


# Inline stylesheet for render_html. Kept as a raw constant (never %-formatted) so its literal
# braces and `%` units pass through untouched. No url()/@import, so the page needs nothing external.
_CSS = """
    :root { color-scheme: light; }
    * { box-sizing: border-box; }
    body { margin: 0; background: #eef1f5; color: #12161f; padding: 24px;
           font-family: -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
           line-height: 1.5; }
    .cert { max-width: 820px; margin: 0 auto; background: #fff; border: 1px solid #d5dbe6;
            border-radius: 12px; padding: 30px 34px; box-shadow: 0 1px 3px rgba(18,22,31,.08); }
    h1 { font-size: 22px; margin: 0 0 4px; }
    h2 { font-size: 15px; margin: 22px 0 6px; }
    .sub { color: #5b6472; margin: 0 0 18px; font-size: 13px; }
    .grid { display: grid; grid-template-columns: 170px 1fr; gap: 6px 16px; margin: 0 0 6px; }
    .grid dt { color: #5b6472; font-size: 13px; }
    .grid dd { margin: 0; font-size: 13px; word-break: break-all; }
    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .badge { display: inline-block; padding: 2px 10px; border-radius: 999px; font-size: 12px;
             font-weight: 600; text-transform: capitalize; }
    .badge.final { background: #e7f6ec; color: #1a7f37; border: 1px solid #b7e2c4; }
    .badge.provisional { background: #fdf3e2; color: #9a6700; border: 1px solid #f0dcae; }
    table { border-collapse: collapse; width: 100%; margin: 6px 0 8px; font-size: 13px; }
    th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e7ebf2; }
    th { color: #5b6472; font-weight: 600; }
    td.num, th.num { text-align: right; }
    pre { background: #f6f8fb; border: 1px solid #e2e8f2; border-radius: 8px; padding: 12px;
          overflow: auto; font-size: 12px; white-space: pre-wrap; word-break: break-all; }
    ol { padding-left: 20px; } ol li { margin: 6px 0; font-size: 13px; }
    footer { color: #5b6472; font-size: 12px; margin-top: 20px; border-top: 1px solid #e7ebf2;
             padding-top: 12px; }
"""


def render_html(cert: dict) -> str:
    """Self-contained one-page HTML certificate: inline <style> ONLY, no <script>, no external or
    CDN assets, so it renders and is auditable fully offline (CSP script-src 'self' safe)."""
    ev, pub, sig, prov = (cert["event"], cert["publication"], cert["signature"],
                          cert["run_provenance"])
    status = pub.get("status")
    status_class = status if status in _STATUSES else PROVISIONAL
    rank_rows = "".join(
        "<tr><td class=\"num\">%s</td><td class=\"mono\">%s</td><td>%s</td>"
        "<td class=\"num mono\">%s</td></tr>"
        % (_esc(r.get("rank")), _esc(r.get("submission")), _esc(r.get("title")), _esc(r.get("q")))
        for r in cert["ranking"])
    signed_rows = "".join(
        "<tr><th>%s</th><td class=\"mono\">%s</td></tr>"
        % (_esc(f), _esc(cert["signed_material"].get(f))) for f in _SIGNED_FIELDS)
    steps = "".join("<li>%s</li>" % _esc(s) for s in cert["verification"]["steps"])
    parts = [
        "<!DOCTYPE html>", "<html lang=\"en\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
        "<title>DOGFOOD results certificate - %s</title>" % _esc(ev.get("ext_id")),
        "<style>", _CSS, "</style></head><body><main class=\"cert\">",
        "<h1>DOGFOOD verifiable results certificate</h1>",
        "<p class=\"sub\">%s</p>" % _esc(cert["attestation"]),
        "<dl class=\"grid\">",
        "<dt>Event</dt><dd>%s <span class=\"mono\">(%s)</span></dd>"
        % (_esc(ev.get("name") or "-"), _esc(ev.get("ext_id"))),
        "<dt>Publication</dt><dd>v%s &nbsp; <span class=\"badge %s\">%s</span></dd>"
        % (_esc(pub.get("version")), _esc(status_class), _esc(status)),
        "<dt>Published at</dt><dd>%s</dd>" % _esc(pub.get("published_at") or "-"),
        "<dt>Engine</dt><dd class=\"mono\">%s</dd>" % _esc(cert["engine_version"]),
        "<dt>result_hash</dt><dd class=\"mono\">%s</dd>" % _esc(cert["result_hash"]),
        "<dt>Signer fingerprint</dt><dd class=\"mono\">%s</dd>" % _esc(cert["signer_fingerprint"]),
        "<dt>Signature</dt><dd class=\"mono\">%s <small>(%s)</small></dd>"
        % (_esc(sig.get("value")), _esc(sig.get("algorithm"))),
        "<dt>Provenance</dt><dd>audit_seq=%s &middot; lambda=%s &middot; n_boot=%s &middot; "
        "seed=%s <small>(not part of the signature)</small></dd>"
        % (_esc(prov.get("audit_seq")), _esc(prov.get("lambda_value")),
           _esc(prov.get("n_boot")), _esc(prov.get("seed"))),
        "</dl>", "<h2>Ranking</h2>",
        "<table><thead><tr><th class=\"num\">Rank</th><th>Submission</th><th>Title</th>"
        "<th class=\"num\">q</th></tr></thead><tbody>", rank_rows, "</tbody></table>",
        "<h2>Signed material</h2>",
        "<p class=\"sub\">Exactly the fields the Ed25519 signature covers (no PII).</p>",
        "<table><tbody>", signed_rows, "</tbody></table>",
        "<h2>Public key (PEM)</h2><pre>%s</pre>" % _esc(sig.get("public_key_pem") or _NO_PEM),
        "<h2>How to verify offline</h2><ol>", steps, "</ol>",
        "<p class=\"sub mono\">%s</p>" % _esc(cert["verification"]["verifier_command"]),
        "<footer>%s</footer>" % _esc(cert["verification"]["scope"]),
        "</main></body></html>",
    ]
    return "".join(parts)


def render(cert: dict, fmt: str = "json") -> str:
    """Render a certificate as 'json' (canonical, sorted keys), 'html', or 'txt'."""
    if fmt == "html":
        return render_html(cert)
    if fmt == "txt":
        return render_text(cert)
    if fmt == "json":
        return json.dumps(cert, indent=2, sort_keys=True)
    raise ValueError("unknown format %r (want json|html|txt)" % (fmt,))


# --- DB-backed wrappers (import the ORM lazily so the pure layer above stays Django-free) ---------

def operator_public_key_pem(expected_fingerprint: str | None = None) -> str | None:
    """Best-effort PEM of the operator's EXISTING Ed25519 public key (audit.keys / normalize.signing).

    NEVER creates a key (uses load, not ensure) and NEVER raises: returns None if the key file is
    absent/unreadable, or -- when expected_fingerprint is given -- if it does not match. This is the
    same operator key that signed the run; no new key is introduced. The API view relies on this
    degrading to None when the key is not present on the host (e.g. under `manage.py test`).
    """
    try:
        from audit import keys
        pub = keys.load_private_key().public_key()
        if expected_fingerprint and signing.public_fingerprint(pub) != expected_fingerprint:
            return None
        return signing.public_key_to_pem(pub).decode("ascii")
    except Exception:
        return None


def _load_run(run_ext_id):
    from .models import NormalizationRun
    return NormalizationRun.objects.filter(run_ext_id=run_ext_id).first()


def certificate_for_publication(publication, *, event_name: str = "", public_key_pem=_UNSET) -> dict:
    """Build a certificate for a specific ResultPublication, loading its signed run. DB-backed.

    If public_key_pem is left unset it is derived best-effort from the operator key (matching the
    run's fingerprint); pass an explicit PEM (or None) to override.
    """
    run = _load_run(getattr(publication, "run_ext_id", None))
    if public_key_pem is _UNSET:
        public_key_pem = operator_public_key_pem(getattr(run, "fingerprint", None))
    return build_certificate(publication, run, event_name=event_name, public_key_pem=public_key_pem)


def certificate_for_event(event, *, version: int | None = None, public_key_pem=_UNSET):
    """The event's official certificate: highest-version publication, or a specific --version.

    Returns None if the event has no such publication (the caller decides 404 vs error). Never
    recomputes -- it certifies the signed run the publication already froze. DB-backed.
    """
    from .models import ResultPublication
    qs = ResultPublication.objects.filter(event_ext_id=event.ext_id)
    publication = (qs.filter(version=version).first() if version is not None
                   else qs.order_by("-version").first())
    if publication is None:
        return None
    return certificate_for_publication(
        publication, event_name=getattr(event, "name", ""), public_key_pem=public_key_pem)
