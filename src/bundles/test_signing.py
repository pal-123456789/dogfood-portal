# src/bundles/test_signing.py
"""DB-free tests for the bundle signing layer (run: python src/manage.py test bundles).

SimpleTestCase (no database): the pure Ed25519 bundle layer round-trips, rejects any tampered
field, rejects a wrong key and a malformed signature, and keeps the frozen tag that separates a
bundle signature from every other domain in the repo. It also exercises services.verify_signed_bundle
(itself DB-free -- it only reads the /state key), including the wrong-fingerprint-claim rejection.

setUpModule points DOGFOOD_AUDIT_KEY at a throwaway temp file (audit.keys.key_path honours the env
override) so verify_signed_bundle's ensure_private_key never touches a real /state volume.
"""
import copy
import os
import shutil
import tempfile

from django.test import SimpleTestCase

from audit import keys
from bundles import services, signing

_KEYDIR = None
_PREV_KEY = None


def setUpModule():
    global _KEYDIR, _PREV_KEY
    _KEYDIR = tempfile.mkdtemp(prefix="bundles_key_")
    _PREV_KEY = os.environ.get("DOGFOOD_AUDIT_KEY")
    os.environ["DOGFOOD_AUDIT_KEY"] = os.path.join(_KEYDIR, "audit_ed25519_key.pem")


def tearDownModule():
    if _PREV_KEY is None:
        os.environ.pop("DOGFOOD_AUDIT_KEY", None)
    else:
        os.environ["DOGFOOD_AUDIT_KEY"] = _PREV_KEY
    if _KEYDIR:
        shutil.rmtree(_KEYDIR, ignore_errors=True)


def _bundle():
    """A representative bundle dict (scalars + nested lists/dicts + a float weight)."""
    return {
        "kind": "dogfood.event-bundle.v1",
        "event": {"ext_id": "evt_01", "name": "Spring Hack", "state": "closed",
                  "submissions_close": "2026-09-28T12:00:00+00:00", "results_published": False},
        "tracks": [{"ext_id": "trk_01", "name": "AI"}],
        "teams": [{"ext_id": "tm_01", "name": "Falcons",
                   "members": [{"email": "a@example.org", "display_name": "A",
                                "role": "participant"}]}],
        "submissions": [{"ext_id": "prj_01", "title": "Alpha", "summary": "s", "repo_url": "",
                         "state": "submitted", "track_ext_id": "trk_01", "team_ext_id": "tm_01"}],
        "rubric_weights": [{"criterion": "quality", "weight": 1.0}],
        "exported_at": "2026-09-28T12:00:00+00:00",
    }


class BundleSigningTests(SimpleTestCase):
    def test_frozen_tag(self):
        self.assertEqual(signing._BUNDLE_TAG, b"dogfood.bundle.v1")
        self.assertTrue(signing.bundle_material(_bundle()).startswith(b"dogfood.bundle.v1\x1e"))

    def test_sign_verify_round_trip(self):
        key = signing.generate_private_key()
        pub = key.public_key()
        b = _bundle()
        sig = signing.sign_bundle(key, b)
        self.assertEqual(len(bytes.fromhex(sig)), 64)          # Ed25519 sig is 64 raw bytes
        self.assertTrue(signing.verify_bundle_sig(pub, signature=sig, bundle_dict=b))

    def test_tamper_any_field_fails(self):
        key = signing.generate_private_key()
        pub = key.public_key()
        b = _bundle()
        sig = signing.sign_bundle(key, b)
        for path in (("event", "name", "Different"),
                     ("submissions", 0, "title", "Alpha EDITED"),
                     ("tracks", 0, "ext_id", "trk_99")):
            t = copy.deepcopy(b)
            node = t
            for step in path[:-2]:
                node = node[step]
            node[path[-2]] = path[-1]
            self.assertFalse(signing.verify_bundle_sig(pub, signature=sig, bundle_dict=t),
                             "tampering %r should fail verification" % (path,))

    def test_wrong_key_fails(self):
        b = _bundle()
        sig = signing.sign_bundle(signing.generate_private_key(), b)
        other_pub = signing.generate_private_key().public_key()
        self.assertFalse(signing.verify_bundle_sig(other_pub, signature=sig, bundle_dict=b))

    def test_malformed_signature_fails(self):
        pub = signing.generate_private_key().public_key()
        for bad in ("not-hex", "", "00" * 64):        # non-hex / empty / valid-shape-wrong-value
            self.assertFalse(signing.verify_bundle_sig(pub, signature=bad, bundle_dict=_bundle()))

    def test_verify_signed_bundle_and_wrong_fingerprint(self):
        # verify_signed_bundle uses the /state (temp) key; sign with that same key so it verifies.
        key, _ = keys.ensure_private_key()
        pub = key.public_key()
        b = _bundle()
        good = {"bundle": b, "signature": {"algorithm": "ed25519",
                "value": signing.sign_bundle(key, b),
                "signer_fingerprint": signing.public_fingerprint(pub)}}
        self.assertTrue(services.verify_signed_bundle(good))
        wrong_fp = copy.deepcopy(good)
        wrong_fp["signature"]["signer_fingerprint"] = "00" * 32     # claim rejected
        self.assertFalse(services.verify_signed_bundle(wrong_fp))
        tampered = copy.deepcopy(good)
        tampered["bundle"]["event"]["name"] = "Doctored"            # tampered body rejected
        self.assertFalse(services.verify_signed_bundle(tampered))
        for bad in (None, "nope", {}, {"bundle": b}, {"bundle": b, "signature": {}}):
            self.assertFalse(services.verify_signed_bundle(bad))    # never raises
