"""Password hashing for per-admin web panel logins (PBKDF2-HMAC-SHA256, stdlib only)."""
from __future__ import annotations

import hashlib
import secrets

_ITERATIONS = 100_000


def hash_password(password: str) -> tuple[str, str]:
    """Return (password_hash_hex, salt_hex) for storage."""
    salt = secrets.token_hex(16)
    return _derive(password, salt), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    return secrets.compare_digest(_derive(password, salt), password_hash)


def _derive(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS).hex()
