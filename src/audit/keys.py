# src/audit/keys.py
"""Where the Ed25519 signing key lives and how it is created -- the thin file/env wiring that
receipts.py (pure) deliberately leaves out.

The PRIVATE key sits on the operator's protected /state volume (same volume as the Django
secret_key), generated once, 0600. The PUBLIC key + its fingerprint are exported and pinned by
an independent party BEFORE judging; only the private-key holder can sign, so a checkpoint is
verifiable by anyone yet forgeable by no one (see receipts.py for why Ed25519, not HMAC).

Django-free on purpose (like portal/bootstrap.py): a management command wires it in. Key
generation uses O_EXCL so two racing boots cannot clobber each other's key -- the same
check-then-act guard bootstrap.ensure_secret uses for the secret_key.
"""
from __future__ import annotations

import os

from . import receipts

DEFAULT_KEY_PATH = "/state/audit_ed25519_key.pem"


def key_path() -> str:
    """Env override, else the /state default. Kept in one place so every command agrees."""
    return os.environ.get("DOGFOOD_AUDIT_KEY", DEFAULT_KEY_PATH)


def ensure_private_key(path: str | None = None):
    """Load the private key at `path`, generating + persisting one (0600) if absent.

    Returns (key, created): created is True only on the boot that actually wrote the file.
    O_EXCL closes the check-then-act window; a loser of the race falls through to a plain read.
    """
    path = path or key_path()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return load_private_key(path), False
    try:
        key = receipts.generate_private_key()
        # newline handling matches bootstrap.ensure_secret: a PEM is text, and a judge who
        # inspects the /state volume on Windows should see the same bytes the container wrote.
        with os.fdopen(fd, "wb") as fh:
            fh.write(receipts.private_key_to_pem(key))
        os.chmod(path, 0o600)
    except BaseException:
        # Don't leave a half-written key file to be trusted on the next boot.
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return key, True


def load_private_key(path: str | None = None):
    with open(path or key_path(), "rb") as fh:
        return receipts.private_key_from_pem(fh.read())
