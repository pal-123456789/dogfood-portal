# src/records/test_signing.py
"""DB-free tests for records.signing (run: python src/manage.py test records).

SimpleTestCase (no database): the pure Ed25519 record layer round-trips, rejects any tampered field,
rejects a wrong key and a malformed signature, and keeps the frozen tag/field set that separates a
record signature from every other domain in the repo.
"""
from django.test import SimpleTestCase

from records import signing


def _body():
    """A representative record body carrying exactly RECORD_FIELDS (scalars + structured values)."""
    return {
        "kind": "dogfood.participation-record.v1",
        "role": "judge",
        "event_ext_id": "evt_01",
        "event_name": "Spring Hack",
        "subject": {"membership_ext_id": "jdg_01"},
        "items": [{"ext_id": "prj_01", "title": "Alpha"},
                  {"ext_id": "prj_02", "title": "Beta"}],
        "issued_at": "2026-09-28T12:00:00+00:00",
        "attestation": "Attests only that the operator's key signed these participation facts.",
    }


class RecordSigningTests(SimpleTestCase):
    def test_frozen_tag_and_fields(self):
        # The domain tag + field set are frozen for record schema v1.
        self.assertEqual(signing._RECORD_TAG, b"dogfood.record.v1")
        self.assertEqual(signing.RECORD_FIELDS,
                         ("kind", "role", "event_ext_id", "event_name",
                          "subject", "items", "issued_at", "attestation"))
        self.assertTrue(signing.record_material(**_body()).startswith(b"dogfood.record.v1\x1e"))

    def test_sign_verify_round_trip(self):
        key = signing.generate_private_key()
        pub = key.public_key()
        body = _body()
        sig = signing.sign_record(key, **body)
        self.assertEqual(len(bytes.fromhex(sig)), 64)          # Ed25519 sig is 64 raw bytes
        self.assertTrue(signing.verify_record(pub, signature=sig, **body))

    def test_tamper_any_field_fails(self):
        key = signing.generate_private_key()
        pub = key.public_key()
        body = _body()
        sig = signing.sign_record(key, **body)
        mutations = {
            "kind": "dogfood.other.v1",
            "role": "participant",
            "event_ext_id": "evt_99",
            "event_name": "Different Event",
            "subject": {"membership_ext_id": "jdg_99"},
            "items": [{"ext_id": "prj_01", "title": "Alpha"},
                      {"ext_id": "prj_02", "title": "Beta EDITED"}],
            "issued_at": "2026-09-28T12:00:01+00:00",
            "attestation": "This proves merit.",          # doctored framing must fail verification
        }
        for field, bad in mutations.items():
            tampered = dict(body)
            tampered[field] = bad
            self.assertFalse(signing.verify_record(pub, signature=sig, **tampered),
                             "tampering %r should fail verification" % field)

    def test_wrong_key_fails(self):
        body = _body()
        sig = signing.sign_record(signing.generate_private_key(), **body)
        other_pub = signing.generate_private_key().public_key()
        self.assertFalse(signing.verify_record(other_pub, signature=sig, **body))

    def test_malformed_signature_fails(self):
        pub = signing.generate_private_key().public_key()
        for bad in ("not-hex", "", "00" * 64):        # non-hex / empty / valid-shape-wrong-value
            self.assertFalse(signing.verify_record(pub, signature=bad, **_body()))
