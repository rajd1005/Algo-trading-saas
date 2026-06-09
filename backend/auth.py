"""
Multi-tenant authentication: password hashing, signed session cookies with
strict 1-device locking, and email OTP codes.

Session cookie = "uid.sig" where sig signs "uid:session_token". The user's
current session_token is stored in the DB; logging in elsewhere rotates it,
which instantly invalidates every other device's cookie.
"""
import datetime as dt
import hashlib
import hmac
import os
import secrets

import config
from models import User, OtpCode

_ALGO = "sha256"
_ITER = 120_000


def _secret() -> bytes:
    return (config.SECRET_KEY or "rdalgo").encode()


# ---------- passwords ----------
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(_ALGO, (pw or "").encode(), salt.encode(), _ITER).hex()
    return f"{salt}${dk}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        salt, dk = (stored or "").split("$", 1)
        calc = hashlib.pbkdf2_hmac(_ALGO, (pw or "").encode(), salt.encode(), _ITER).hex()
        return hmac.compare_digest(calc, dk)
    except Exception:
        return False


# ---------- session cookie (1-device) ----------
def new_session_token() -> str:
    return secrets.token_hex(24)


def make_cookie(user: User) -> str:
    msg = f"{user.id}:{user.session_token}"
    sig = hmac.new(_secret(), msg.encode(), hashlib.sha256).hexdigest()
    return f"{user.id}.{sig}"


def cookie_user_id(cookie: str):
    """Return the user id embedded in a cookie (unverified) or None."""
    try:
        uid, _sig = (cookie or "").split(".", 1)
        return int(uid)
    except Exception:
        return None


def cookie_valid(cookie: str, user: User) -> bool:
    """Valid only if the signature matches the user's CURRENT session token."""
    if not user or not user.session_token:
        return False
    try:
        uid, sig = (cookie or "").split(".", 1)
        if int(uid) != user.id:
            return False
        good = hmac.new(_secret(), f"{user.id}:{user.session_token}".encode(),
                        hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good)
    except Exception:
        return False


# ---------- OTP ----------
def _otp_hash(code: str) -> str:
    return hmac.new(_secret(), code.encode(), hashlib.sha256).hexdigest()


def issue_otp(db, email: str, purpose: str = "REGISTER", ttl_min: int = 10) -> str:
    email = (email or "").strip().lower()
    db.query(OtpCode).filter(OtpCode.email == email, OtpCode.purpose == purpose).delete()
    code = f"{secrets.randbelow(1_000_000):06d}"
    db.add(OtpCode(email=email, code_hash=_otp_hash(code), purpose=purpose,
                   expires_at=dt.datetime.utcnow() + dt.timedelta(minutes=ttl_min)))
    db.commit()
    return code


def verify_otp(db, email: str, code: str, purpose: str = "REGISTER") -> bool:
    email = (email or "").strip().lower()
    row = (db.query(OtpCode)
           .filter(OtpCode.email == email, OtpCode.purpose == purpose)
           .order_by(OtpCode.id.desc()).first())
    if not row or not row.expires_at or row.expires_at < dt.datetime.utcnow():
        return False
    if (row.attempts or 0) > 8:
        return False
    row.attempts = (row.attempts or 0) + 1
    db.commit()
    if hmac.compare_digest(row.code_hash, _otp_hash(code or "")):
        db.delete(row)
        db.commit()
        return True
    return False
