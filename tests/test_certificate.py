# tests/test_certificate.py
"""DB-free tests for the results certificate builder + renderers (run by `pytest tests/`).

No database and no Django models: build_certificate is exercised on lightweight stand-ins, so these
pin the pure attestation/rendering layer and the refuse-a-non-signed-run rule. A real Ed25519 key
(deterministic per RFC 8032, via the shared normalize.signing code path) signs a run tuple so the
certificate's own signature block is proven to verify offline -- and to FAIL when a signed field is
tampered. They also lock the offline/no-PII properties of the HTML and text renderings.

Django is configured by the container env (DJANGO_SETTINGS_MODULE=portal.settings, PYTHONPATH=
/app/src) and pytest-django runs django.setup() before collection, but nothing here touches the ORM
-- normalize.certificate keeps its builder/renderers Django-free and imports models only lazily.
"""
import types

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from normalize import certificate, signing

_SEED = bytes(range(32))
_RESULT = {"rows": [
    {"rank": 1, "submission": "prj_a", "title": "Alpha", "q": 4.0, "raw_mean": 4.0,
     "delta": 0.0, "n_ballots": 3, "component": 0},
    {"rank": 2, "submission": "prj_b", "title": "Beta", "q": 3.0, "raw_mean": 3.0,
     "delta": 0.0, "n_ballots": 3, "component": 0},
], "n_ballots": 6, "n_submissions": 2, "lambda": 1.0}


def _key():
    return Ed25519PrivateKey.from_private_bytes(_SEED)


def _signed_run(key):
    """A NormalizationRun stand-in whose signature is REAL (over signing.RUN_FIELDS)."""
    pub = key.public_key()
    fields = dict(engine_version="ridge-additive-v1", instance_id="inst_cert_0001",
                  event_ext_id="evt_demo", run_ext_id="nrun_00112233445566",
                  inputs_hash="a" * 64, result_hash="b" * 64,
                  created_at="2026-09-28T00:00:00+00:00")
    sig = signing.sign_run(key, **fields)
    return types.SimpleNamespace(
        fingerprint=signing.public_fingerprint(pub), signature=sig,
        audit_seq=7, lambda_value=1.0, n_boot=120, seed=0, result=_RESULT, **fields)


def _publication(run, *, version=1, status=certificate.FINAL):
    return types.SimpleNamespace(version=version, status=status, run_ext_id=run.run_ext_id,
                                 event_ext_id=run.event_ext_id,
                                 recorded_at="2026-09-28T00:00:00+00:00")


def _cert():
    key = _key()
    run = _signed_run(key)
    pem = signing.public_key_to_pem(key.public_key()).decode("ascii")
    return certificate.build_certificate(_publication(run), run,
                                         event_name="Demo Event", public_key_pem=pem)


def test_build_includes_required_non_pii_fields():
    cert = _cert()
    assert cert["kind"] == certificate.CERTIFICATE_KIND
    assert cert["event"] == {"ext_id": "evt_demo", "name": "Demo Event"}
    assert cert["publication"]["version"] == 1
    assert cert["publication"]["status"] == certificate.FINAL
    assert cert["engine_version"] == "ridge-additive-v1"
    assert cert["result_hash"] == "b" * 64
    assert cert["signer_fingerprint"] == signing.public_fingerprint(_key().public_key())
    assert cert["signature"]["algorithm"] == "ed25519"
    # signed_material is EXACTLY the fields the signature covers, and result_hash agrees with the
    # headline field -- so a reader checks the signature over the same bytes it displays.
    assert tuple(cert["signed_material"]) == signing.RUN_FIELDS
    assert cert["signed_material"]["result_hash"] == cert["result_hash"]
    assert [r["submission"] for r in cert["ranking"]] == ["prj_a", "prj_b"]
    for row in cert["ranking"]:
        assert set(row) == {"rank", "submission", "title", "q"}   # no scores/ballots leak in


def test_signature_block_verifies_and_tamper_fails():
    cert = _cert()
    assert certificate.verify_certificate_signature(cert) is True
    tampered = dict(cert, result_hash="d" * 64)
    tampered["signed_material"] = dict(cert["signed_material"], result_hash="d" * 64)
    assert certificate.verify_certificate_signature(tampered) is False
    no_pem = dict(cert, signature=dict(cert["signature"], public_key_pem=None))
    assert certificate.verify_certificate_signature(no_pem) is False


def test_refuses_unpublished_or_unsigned():
    key = _key()
    run = _signed_run(key)
    with pytest.raises(certificate.CertificateError):
        certificate.build_certificate(None, run)                      # never published
    with pytest.raises(certificate.CertificateError):
        certificate.build_certificate(_publication(run), {"rows": _RESULT["rows"]})  # live dict
    unsigned = _signed_run(key)
    unsigned.signature = ""
    with pytest.raises(certificate.CertificateError):
        certificate.build_certificate(_publication(unsigned), unsigned)  # not signed
    bad_status = _publication(run, status="draft")
    with pytest.raises(certificate.CertificateError):
        certificate.build_certificate(bad_status, run)                # not a publication status


def test_html_and_text_are_offline_and_pii_free():
    cert = _cert()
    html_doc = certificate.render_html(cert)
    text_doc = certificate.render_text(cert)
    assert html_doc.startswith("<!DOCTYPE html>")
    assert "<script" not in html_doc.lower()                          # CSP-safe, no JS
    assert "http://" not in html_doc and "https://" not in html_doc   # no external assets
    assert "url(" not in html_doc
    assert cert["result_hash"] in html_doc and cert["signer_fingerprint"] in html_doc
    assert "Alpha" in html_doc and "Alpha" in text_doc                # ranking rendered
    for tok in ("functionality", "quality", "innovation", "display_name", "@"):
        assert tok not in html_doc and tok not in text_doc


def test_render_dispatch_json_roundtrips():
    import json
    cert = _cert()
    assert json.loads(certificate.render(cert, "json")) == cert
    assert certificate.render(cert, "html") == certificate.render_html(cert)
    assert certificate.render(cert, "txt") == certificate.render_text(cert)
    with pytest.raises(ValueError):
        certificate.render(cert, "pdf")
