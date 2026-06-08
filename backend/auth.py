"""
Simple dashboard authentication (single shared password).

If DASHBOARD_PASSWORD is set (in .env), the whole site requires login. A
signed, expiring cookie keeps you logged in. If no password is set, the site
stays open (handy for local/demo use) — so set a password before going live.
"""
import hashlib
import hmac
import time

import config


def auth_required() -> bool:
    return bool(config.DASHBOARD_PASSWORD)


def _secret() -> bytes:
    if config.SECRET_KEY:
        return config.SECRET_KEY.encode()
    # Stable across restarts; changes (logs everyone out) if the password changes.
    return hashlib.sha256(("rdalgo-secret::" + config.DASHBOARD_PASSWORD).encode()).digest()


def password_ok(pw: str) -> bool:
    return bool(config.DASHBOARD_PASSWORD) and hmac.compare_digest(pw or "", config.DASHBOARD_PASSWORD)


def make_token(days: int = 7) -> str:
    exp = str(int(time.time()) + days * 86400)
    sig = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def token_ok(token: str) -> bool:
    try:
        exp, sig = (token or "").split(".", 1)
        good = hmac.new(_secret(), exp.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, good) and int(exp) > time.time()
    except Exception:
        return False
