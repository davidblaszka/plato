"""
Server-side encryption helpers — transitional state during Phase 4 E2EE rollout.

Phase 4 protocol:
  - New messages (is_encrypted=True):  client encrypts before POST, server relays
    ciphertext as-is.  Backend never calls encrypt/decrypt for these.
  - Legacy messages (is_encrypted=False): stored with server-side Fernet encryption.
    Backend still decrypts these on GET so existing conversations remain readable.

TODO (Phase 5): Once all clients are on E2EE builds and legacy messages have been
migrated to local SQLite, remove Fernet entirely and delete this file.
"""

import os
import base64
from cryptography.fernet import Fernet
from app.core.config import settings


def _get_key() -> bytes:
    import hashlib
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_get_key())


def encrypt_message(plaintext: str) -> str:
    """Legacy server-side encrypt — used only for is_encrypted=False messages."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_message(ciphertext: str) -> str:
    """Legacy server-side decrypt — used only for is_encrypted=False messages."""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        return "[message could not be decrypted]"
