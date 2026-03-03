"""
Server-side AES-256 encryption for message content.

MVP note: This is encryption at rest with a server-held key.
It protects against database breaches but the server can technically
decrypt messages. True E2EE via Matrix is planned for Phase 2.
"""
import os
import base64
from cryptography.fernet import Fernet
from app.core.config import settings


def _get_key() -> bytes:
    """Derive a Fernet key from the app's SECRET_KEY."""
    import hashlib
    digest = hashlib.sha256(settings.secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


_fernet = Fernet(_get_key())


def encrypt_message(plaintext: str) -> str:
    """Encrypt a message string. Returns base64-encoded ciphertext."""
    return _fernet.encrypt(plaintext.encode()).decode()


def decrypt_message(ciphertext: str) -> str:
    """Decrypt a message string. Returns plaintext."""
    try:
        return _fernet.decrypt(ciphertext.encode()).decode()
    except Exception:
        return "[message could not be decrypted]"
