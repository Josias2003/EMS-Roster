"""
Password hashing and JWT helpers for the 4-role auth model
(admin | call_center_tl | drivers_manager | phtl).
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from dotenv import load_dotenv

load_dotenv()

# ENVIRONMENT defaults to "development" so local/test runs are unaffected —
# but anything else (e.g. ENVIRONMENT=production on a real deploy) with no
# real JWT_SECRET set fails loudly at startup instead of silently signing
# every login token with a publicly-known placeholder secret.
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
JWT_SECRET = os.environ.get("JWT_SECRET")
if not JWT_SECRET:
    if ENVIRONMENT != "development":
        raise RuntimeError(
            "JWT_SECRET must be set when ENVIRONMENT is not 'development' — refusing to start with "
            "no secret configured. Generate one with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\" and set it in the environment."
        )
    JWT_SECRET = "dev-secret-change-me"
JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_MINUTES = 60 * 12  # 12 hours


_COMMON_WEAK_PASSWORDS = {
    "password", "password1", "12345678", "123456789", "qwerty123",
    "letmein", "admin123", "changeme", "welcome1",
}


def password_strength_error(password: str) -> Optional[str]:
    """None if the password is acceptable; otherwise a user-facing reason
    it was rejected. Deliberately simple (length + not-purely-numeric + not
    a known-weak password) rather than a full complexity policy — the goal
    is catching obviously weak passwords, not frustrating real users."""
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if password.isdigit():
        return "Password can't be all numbers."
    if password.lower() in _COMMON_WEAK_PASSWORDS:
        return "That password is too common — pick something harder to guess."
    return None


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def create_access_token(user_id: int, username: str, role: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_MINUTES),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
