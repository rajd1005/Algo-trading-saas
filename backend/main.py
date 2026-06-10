"""
FastAPI application: the web server + REST API + serves the dashboard.

Run it with:   uvicorn main:app --host 0.0.0.0 --port 8000
(or just:      python main.py )
"""
import os
import json
import time
import asyncio
import threading
import datetime as dt

import requests

# Force ALL outbound connections to use IPv4. Dhan whitelists an IPv4 address;
# if the VPS prefers IPv6, orders are rejected with DH-905 "Invalid IP".
import socket as _socket
_orig_getaddrinfo = _socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, *args, **kwargs):
    results = _orig_getaddrinfo(host, *args, **kwargs)
    ipv4 = [r for r in results if r[0] == _socket.AF_INET]
    return ipv4 or results
_socket.getaddrinfo = _ipv4_only_getaddrinfo

from fastapi import FastAPI, Depends, HTTPException, Request
from fastapi.responses import (FileResponse, JSONResponse, RedirectResponse,
                               StreamingResponse, PlainTextResponse)
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import dhan_auth
import auth
import emailer
import angel
import zerodha
import aliceblue
import feeds
import baskets
import margins

import config
from database import init_db, get_db, SessionLocal
from models import (Trade, LogEntry, Setting, Account, SymbolPreset, Watchlist,
                    User, Plan, EmailTemplate, UserSetting, Basket, BasketLeg)
from schemas import (TradeCreate, TradeOut, BrokerConfigIn, SettingsIn, ModifyIn,
                     SymbolPresetIn, WatchlistIn)
from engine import engine, level_price
from instruments import store as instruments
from market_data import DhanMarketData, demo_market
from brokers import verify_dhan_credentials, DhanBroker
from usettings import uget, uset, unum

app = FastAPI(title="Algo Trading SaaS (India)")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

# Paths reachable without a logged-in session.
_OPEN_PREFIXES = ("/static", "/api/auth/", "/api/webhook/")
_OPEN_PATHS = {"/login", "/register", "/favicon.ico"}


def _is_open(path: str) -> bool:
    return path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)


def _plan_expired(u: User) -> bool:
    return bool(u.plan_expiry) and u.plan_expiry < dt.datetime.utcnow()


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    """Resolve the tenant from the session cookie and enforce 1-device login,
    blocked accounts, and plan expiry. Stashes the user on request.state."""
    path = request.url.path
    if path == "/" or path.startswith("/api/") or path in ("/login", "/register"):
        cookie = request.cookies.get("session", "")
        uid = auth.cookie_user_id(cookie)
        user = None
        if uid is not None:
            db = SessionLocal()
            try:
                u = db.get(User, uid)
                if u and auth.cookie_valid(cookie, u):
                    user = u
                    request.state.user = u
                    request.state.uid = u.id
            finally:
                db.close()
        # Open (public) routes don't need a session.
        if _is_open(path):
            return await call_next(request)
        # Auth pages are reachable while logged out; if logged in, send to app.
        if path in ("/login", "/register"):
            return RedirectResponse(url="/") if user else await call_next(request)
        # Everything else requires a valid session.
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Login required"}, status_code=401)
            return RedirectResponse(url="/login")
        if user.status == "BLOCKED":
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Your account has been blocked by the administrator."}, status_code=403)
            return RedirectResponse(url="/login?blocked=1")
        # Plan expiry: admins never expire; a few endpoints stay reachable so the
        # user can see the expired screen / log out.
        if user.role != "SUPER_ADMIN" and _plan_expired(user):
            allowed = path in ("/", "/api/me", "/api/auth/logout") or path.startswith("/static")
            if not allowed:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "PLAN_EXPIRED"}, status_code=402)
        return await call_next(request)
    return await call_next(request)


def current_user(request: Request) -> User:
    u = getattr(request.state, "user", None)
    if u is None:
        raise HTTPException(401, "Login required")
    return u


def require_admin(request: Request) -> User:
    u = current_user(request)
    if u.role != "SUPER_ADMIN":
        raise HTTPException(403, "Admin only")
    return u


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


@app.get("/register")
def register_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


# ---------- authentication / onboarding ----------
def _set_session(user: User, db) -> JSONResponse:
    user.session_token = auth.new_session_token()   # rotate -> kicks other devices
    db.commit()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("session", auth.make_cookie(user), httponly=True, samesite="lax",
                    max_age=30 * 86400)
    return resp


@app.post("/api/auth/register/start")
def register_start(payload: dict, db: Session = Depends(get_db)):
    if get_setting(db, "registration_open", "yes") != "yes":
        raise HTTPException(403, "Public registration is currently closed.")
    email = str(payload.get("email", "")).strip().lower()
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email address.")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "An account with this email already exists. Please log in.")
    code = auth.issue_otp(db, email, "REGISTER")
    ok, _ = emailer.send_email(db, email, "otp_register", code=code, email=email)
    # If SMTP isn't set up yet, surface the code so onboarding still works.
    out = {"ok": True, "emailed": ok}
    if not ok:
        out["dev_code"] = code
    return out


@app.post("/api/auth/register/verify")
def register_verify(payload: dict, db: Session = Depends(get_db)):
    email = str(payload.get("email", "")).strip().lower()
    code = str(payload.get("code", "")).strip()
    pw = str(payload.get("password", ""))
    if len(pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if not auth.verify_otp(db, email, code, "REGISTER"):
        raise HTTPException(400, "Invalid or expired OTP.")
    trial_days = int(get_setting(db, "trial_days", str(config.DEFAULT_TRIAL_DAYS)) or config.DEFAULT_TRIAL_DAYS)
    is_admin = bool(config.SUPER_ADMIN_EMAIL) and email == config.SUPER_ADMIN_EMAIL
    u = User(email=email, password_hash=auth.hash_password(pw),
             role="SUPER_ADMIN" if is_admin else "USER",
             plan_name="Admin" if is_admin else "Trial",
             plan_expiry=(dt.datetime.utcnow() + dt.timedelta(days=3650 if is_admin else trial_days)))
    db.add(u)
    db.commit()
    db.refresh(u)
    if is_admin:
        _claim_legacy_data(db, u.id)   # inherit pre-SaaS trades / accounts / settings
    emailer.send_email(db, email, "welcome", email=email, plan=u.plan_name,
                       expiry=u.plan_expiry.strftime("%d %b %Y"))
    return _set_session(u, db)


@app.post("/api/auth/login")
def login(payload: dict, db: Session = Depends(get_db)):
    email = str(payload.get("email", "")).strip().lower()
    pw = str(payload.get("password", ""))
    u = db.query(User).filter(User.email == email).first()
    if not u or not auth.verify_password(pw, u.password_hash):
        raise HTTPException(401, "Wrong email or password.")
    if u.status == "BLOCKED":
        raise HTTPException(403, "Your account has been blocked by the administrator.")
    return _set_session(u, db)        # rotating the token logs out the old device


@app.post("/api/auth/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    u = getattr(request.state, "user", None)
    if u:
        u.session_token = ""          # invalidate the cookie everywhere
        db.commit()
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


@app.post("/api/auth/forgot/start")
def forgot_start(payload: dict, db: Session = Depends(get_db)):
    email = str(payload.get("email", "")).strip().lower()
    u = db.query(User).filter(User.email == email).first()
    if not u:
        return {"ok": True}           # don't reveal whether the email exists
    code = auth.issue_otp(db, email, "RESET")
    ok, _ = emailer.send_email(db, email, "otp_reset", code=code, email=email)
    out = {"ok": True, "emailed": ok}
    if not ok:
        out["dev_code"] = code
    return out


@app.post("/api/auth/forgot/verify")
def forgot_verify(payload: dict, db: Session = Depends(get_db)):
    email = str(payload.get("email", "")).strip().lower()
    code = str(payload.get("code", "")).strip()
    pw = str(payload.get("password", ""))
    if len(pw) < 6:
        raise HTTPException(400, "Password must be at least 6 characters.")
    if not auth.verify_otp(db, email, code, "RESET"):
        raise HTTPException(400, "Invalid or expired OTP.")
    u = db.query(User).filter(User.email == email).first()
    if not u:
        raise HTTPException(400, "Account not found.")
    u.password_hash = auth.hash_password(pw)
    u.session_token = ""              # force re-login everywhere
    db.commit()
    return {"ok": True}


@app.get("/api/auth/config")
def auth_config(db: Session = Depends(get_db)):
    """Public: what the login/register page needs to render."""
    return {"registration_open": get_setting(db, "registration_open", "yes") == "yes"}


def _broker_connected(db, uid) -> bool:
    return db.query(Account).filter(Account.user_id == uid, Account.connected == 1).count() > 0


ALL_BROKERS = ["DHAN", "ANGEL", "ZERODHA", "ALICE"]


def _enabled_brokers(db):
    """Brokers the admin has made available to users (default: all)."""
    raw = get_setting(db, "enabled_brokers", "")
    if not raw:
        return list(ALL_BROKERS)
    out = [b for b in raw.split(",") if b in ALL_BROKERS]
    return out or list(ALL_BROKERS)


@app.get("/api/me")
def me(request: Request, db: Session = Depends(get_db)):
    u = current_user(request)
    expired = u.role != "SUPER_ADMIN" and _plan_expired(u)
    is_admin = u.role == "SUPER_ADMIN"
    return {
        "email": u.email, "role": u.role, "uuid": u.uuid,
        "is_admin": is_admin,
        "demo_allowed": is_admin,
        "enabled_brokers": ALL_BROKERS if is_admin else _enabled_brokers(db),
        "plan_name": u.plan_name,
        "plan_expiry": u.plan_expiry.isoformat() if u.plan_expiry else None,
        "plan_expired": expired,
        "broker_connected": _broker_connected(db, u.id),
        "blocked": u.status == "BLOCKED",
    }


@app.exception_handler(Exception)
async def log_unhandled_errors(request: Request, exc: Exception):
    """Record every unexpected server error in the activity log."""
    try:
        db = SessionLocal()
        db.add(LogEntry(message=f"Server error on {request.method} {request.url.path}: {exc}",
                        level="ERROR"))
        db.commit()
        db.close()
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": str(exc)})


# ---------- settings helpers ----------
def set_setting(db: Session, key: str, value: str):
    row = db.get(Setting, key)
    if row:
        row.value = value
    else:
        db.add(Setting(key=key, value=value))
    db.commit()


def get_setting(db: Session, key: str, default: str = "") -> str:
    row = db.get(Setting, key)
    return row.value if row else default


def _ist_today() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).strftime("%Y-%m-%d")


def _ist_day_bounds(date_str):
    """UTC [start, end) datetimes for the given IST date 'YYYY-MM-DD'. None if bad."""
    try:
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        base = dt.datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=ist)
        start = base.astimezone(dt.timezone.utc).replace(tzinfo=None)
        end = (base + dt.timedelta(days=1)).astimezone(dt.timezone.utc).replace(tzinfo=None)
        return start, end
    except Exception:
        return None


def _daily_halted(db, uid) -> bool:
    """True when the user's daily target/drawdown halt has tripped for today."""
    return (uget(db, uid, "daily_halt", "off") == "on"
            and uget(db, uid, "daily_halt_date", "") == _ist_today())


def get_trade_creds(db):
    """Credentials used for ORDER execution + account info (balance/positions)."""
    return (get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID),
            get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN))


def get_data_creds(db):
    """Credentials used for MARKET DATA (LTP / feed). Same as trading unless the
    user configured a separate data account."""
    if get_setting(db, "use_same_account", "yes") == "yes":
        return get_trade_creds(db)
    return (get_setting(db, "data_client_id", ""),
            get_setting(db, "data_access_token", ""))


# ---------- multi-account broker logins ----------
def _label(broker, client_id):
    name = {"DHAN": "Dhan", "ANGEL": "Angel One", "ZERODHA": "Zerodha",
            "ALICE": "Alice Blue"}.get(broker, broker)
    return f"{name} · {client_id or '—'}"


def _acc_creds(a):
    try:
        return json.loads(a.creds_json or "{}")
    except Exception:
        return {}


def _set_acc_creds(a, **kw):
    creds = _acc_creds(a)
    for k, v in kw.items():
        if v:
            creds[k] = v
    a.creds_json = json.dumps(creds)


def _account_dict(a):
    hours_left = None
    if a.token_time:
        try:
            age = (dt.datetime.utcnow() - a.token_time).total_seconds() / 3600
            hours_left = round(max(0, 24 - age), 1)
        except Exception:
            pass
    creds = _acc_creds(a)
    return {
        "id": a.id, "broker": a.broker, "client_id": a.client_id,
        "label": a.label or _label(a.broker, a.client_id),
        "connected": bool(a.connected), "token_hours_left": hours_left,
        "has_secret": bool(creds.get("app_secret") or creds.get("api_key")),
    }


def _account_for_provider(db, provider_value, uid=None):
    """provider_value is 'DEMO' or an account id (string). Returns Account or None.
    When uid is given, the account must belong to that tenant."""
    if not provider_value or provider_value == "DEMO":
        return None
    try:
        a = db.get(Account, int(provider_value))
    except Exception:
        return None
    if a is not None and uid is not None and a.user_id != uid:
        return None
    return a


def _auto_select_provider(db, uid, account_id):
    """When a broker is connected, point this user's Data + Trading providers at
    it — unless they're already on another live (connected) account. This makes a
    freshly-connected broker handle the data immediately (option chain, LTP),
    so each broker works independently without Demo/Dhan."""
    for key in ("data_provider", "trade_provider"):
        cur = _account_for_provider(db, uget(db, uid, key, ""), uid)
        if cur is None or not cur.connected:        # demo / empty / disconnected
            uset(db, uid, key, str(account_id))
    for k in ("broker_balance", "broker_balance_provider", "broker_health", "md_status"):
        uset(db, uid, k, "")


def _account_id_for_broker(db, broker, uid):
    """Pick this user's account id to attribute an external order to."""
    tp = _account_for_provider(db, uget(db, uid, "trade_provider", "DEMO"), uid)
    if tp and tp.broker == broker:
        return tp.id
    a = db.query(Account).filter(Account.broker == broker, Account.user_id == uid).first()
    return a.id if a else 0


def _broker_from_account(a):
    if a is None:
        return None
    creds = _acc_creds(a)
    if a.broker == "DHAN" and creds.get("access_token"):
        return DhanBroker(a.client_id, creds["access_token"])
    if a.broker == "ANGEL" and creds.get("jwt"):
        return angel.AngelBroker(a.client_id, creds.get("api_key", ""), creds["jwt"])
    if a.broker == "ZERODHA" and creds.get("access_token"):
        return zerodha.ZerodhaBroker(creds.get("api_key", ""), creds["access_token"])
    if a.broker == "ALICE" and creds.get("session_id"):
        return aliceblue.AliceBroker(a.client_id, creds["session_id"])
    return None


def _broker_in_use_elsewhere(db, broker, client_id, uid):
    """Anti-abuse: the same broker Client ID may not be linked to another tenant."""
    if not client_id:
        return False
    return (db.query(Account)
            .filter(Account.broker == broker, Account.client_id == client_id,
                    Account.user_id != uid).first()) is not None


@app.get("/api/accounts")
def list_accounts(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    return [_account_dict(a) for a in
            db.query(Account).filter(Account.user_id == me.id).order_by(Account.id).all()]


@app.post("/api/accounts")
def save_account(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Add or update a broker account (scoped to the tenant)."""
    me = current_user(request)
    broker = str(payload.get("broker", "")).upper()
    if broker not in ALL_BROKERS:
        broker = "DHAN"
    if me.role != "SUPER_ADMIN" and broker not in _enabled_brokers(db):
        raise HTTPException(403, f"{_label(broker, '').split(' ·')[0]} is not available. "
                                 "Please choose one of the enabled brokers.")
    aid = payload.get("id")
    a = db.get(Account, int(aid)) if aid else None
    if a is not None and a.user_id != me.id:
        raise HTTPException(403, "Not your account.")
    client_id = str(payload.get("client_id", "")).strip()
    if client_id and _broker_in_use_elsewhere(db, broker, client_id, me.id):
        raise HTTPException(400, "This broker Client ID is already linked to another RD Algo "
                                 "account. Each broker account can be used by one user only.")
    if a is None:
        a = Account(broker=broker, user_id=me.id)
        db.add(a)
    if client_id:
        a.client_id = client_id
    a.broker = broker
    _set_acc_creds(a, app_id=payload.get("app_id"), app_secret=payload.get("app_secret"),
                   api_key=payload.get("api_key"), api_secret=payload.get("api_secret"),
                   totp_secret=payload.get("totp_secret"), pin=payload.get("pin"))
    a.label = _label(a.broker, a.client_id)
    db.commit()
    db.refresh(a)
    return _account_dict(a)


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    a = db.get(Account, account_id)
    if a and a.user_id == me.id:
        for key in ("data_provider", "trade_provider"):
            if uget(db, me.id, key, "DEMO") == str(account_id):
                uset(db, me.id, key, "DEMO")
        db.delete(a)
        db.commit()
    return {"ok": True}


# ---------- startup ----------
DEFAULT_PLANS = [("15 Days", 15), ("30 Days", 30), ("90 Days", 90)]


def _seed_globals(db):
    if db.get(Setting, "registration_open") is None:
        set_setting(db, "registration_open", "yes")
    if db.get(Setting, "trial_days") is None:
        set_setting(db, "trial_days", str(config.DEFAULT_TRIAL_DAYS))
    if db.query(Plan).count() == 0:
        for name, days in DEFAULT_PLANS:
            db.add(Plan(name=name, days=days))
        db.commit()


def _claim_legacy_data(db, uid):
    """Assign pre-SaaS (single-user) rows + settings to the first admin."""
    from sqlalchemy import text
    for tbl in ("trades", "accounts", "logs", "symbol_presets", "watchlist"):
        try:
            db.execute(text(f"UPDATE {tbl} SET user_id = :u WHERE user_id = 0 OR user_id IS NULL"), {"u": uid})
        except Exception:
            pass
    # Copy known per-user settings from the old global table into UserSetting.
    for key in ("kill_switch", "default_mode", "data_provider", "trade_provider",
                "broker_mode", "daily_max_profit", "daily_max_loss",
                "global_lock_step", "global_lock_amount", "use_same_account"):
        v = get_setting(db, key, "")
        if v and not uget(db, uid, key, ""):
            uset(db, uid, key, v)
    db.commit()


@app.on_event("startup")
def _startup():
    init_db()
    db = SessionLocal()
    _seed_globals(db)
    db.close()
    instruments.load_async()   # download Dhan's symbol list in the background
    angel.mapper.load_async()  # download Angel One master + build the symbol map
    zerodha.mapper.load_async()  # download Zerodha (Kite) master + symbol map
    aliceblue.mapper.load_async()  # download Alice Blue contract masters + symbol map
    engine.start()
    baskets.start()            # basket scheduler + leg/MTM monitor threads
    threading.Thread(target=_auto_renew_loop, daemon=True).start()
    threading.Thread(target=_broker_monitor_loop, daemon=True).start()
    threading.Thread(target=_fetch_static_ip, daemon=True).start()
    threading.Thread(target=_purge_loop, daemon=True).start()


def _migrate_accounts(db):
    """One-time: turn legacy single-account settings into Account rows."""
    if db.query(Account).count() > 0:
        return
    dhan_id = angel_id = None
    if any(get_setting(db, k, "") for k in ("dhan_app_id", "dhan_client_id", "dhan_access_token")):
        a = Account(broker="DHAN", client_id=get_setting(db, "dhan_client_id", ""))
        a.creds_json = json.dumps({"app_id": get_setting(db, "dhan_app_id", ""),
                                   "app_secret": get_setting(db, "dhan_app_secret", ""),
                                   "access_token": get_setting(db, "dhan_access_token", "")})
        a.connected = 1 if get_setting(db, "dhan_connected", "no") == "yes" else 0
        a.label = _label("DHAN", a.client_id)
        db.add(a); db.flush(); dhan_id = a.id
    if any(get_setting(db, k, "") for k in ("angel_client_id", "angel_api_key")):
        a = Account(broker="ANGEL", client_id=get_setting(db, "angel_client_id", ""))
        a.creds_json = json.dumps({"api_key": get_setting(db, "angel_api_key", ""),
                                   "totp_secret": get_setting(db, "angel_totp_secret", ""),
                                   "pin": get_setting(db, "angel_pin", ""),
                                   "jwt": get_setting(db, "angel_jwt", ""),
                                   "refresh": get_setting(db, "angel_refresh", "")})
        a.connected = 1 if get_setting(db, "angel_jwt", "") else 0
        a.label = _label("ANGEL", a.client_id)
        db.add(a); db.flush(); angel_id = a.id
    for key in ("data_provider", "trade_provider"):
        v = get_setting(db, key, "DEMO")
        if v == "DHAN":
            set_setting(db, key, str(dhan_id) if dhan_id else "DEMO")
        elif v == "ANGEL":
            set_setting(db, key, str(angel_id) if angel_id else "DEMO")
        elif not (v == "DEMO" or v.isdigit()):
            set_setting(db, key, "DEMO")
    db.commit()


def _fetch_static_ip():
    """Discover the server's outbound IPv4 (the one to whitelist with brokers)."""
    try:
        ip = requests.get("https://api.ipify.org", timeout=10).text.strip()
        db = SessionLocal()
        set_setting(db, "static_ip", ip)
        db.commit()
        db.close()
    except Exception:
        pass


def _broker_monitor_loop():
    """Per-user live broker: refresh balance + health and sync external orders for
    each tenant's selected trading account."""
    while True:
        time.sleep(12)
        try:
            db = SessionLocal()
            for u in db.query(User).filter(User.status == "ACTIVE").all():
                if u.role != "SUPER_ADMIN" and _plan_expired(u):
                    continue
                acc = _account_for_provider(db, uget(db, u.id, "trade_provider", "DEMO"), u.id)
                b = _broker_from_account(acc)
                if b is None:
                    continue
                ok, bal = b.fund_limit()
                if ok:
                    uset(db, u.id, "broker_balance", str(bal))
                    uset(db, u.id, "broker_balance_provider", str(acc.id))
                    uset(db, u.id, "broker_health", "ok")
                else:
                    uset(db, u.id, "broker_health", "error")
                try:
                    _sync_external_orders(db, b.get_orders(), acc.broker, acc.id, u.id)
                except Exception:
                    pass
            db.commit()
            db.close()
        except Exception:
            pass


def _purge_loop():
    """Daily: permanently strip broker keys/tokens, custom symbols and webhooks of
    accounts that have been expired for PURGE_AFTER_DAYS or more."""
    while True:
        time.sleep(6 * 3600)        # check 4x/day
        try:
            db = SessionLocal()
            cutoff = dt.datetime.utcnow() - dt.timedelta(days=config.PURGE_AFTER_DAYS)
            for u in db.query(User).filter(User.role != "SUPER_ADMIN").all():
                if not u.plan_expiry or u.plan_expiry > cutoff:
                    continue
                if uget(db, u.id, "purged", "") == "yes":
                    continue
                db.query(Account).filter(Account.user_id == u.id).delete()
                db.query(SymbolPreset).filter(SymbolPreset.user_id == u.id).delete()
                db.query(Watchlist).filter(Watchlist.user_id == u.id).delete()
                u.uuid = __import__("uuid").uuid4().hex     # rotate webhook URL
                u.session_token = ""
                uset(db, u.id, "purged", "yes")
                db.add(LogEntry(message=f"Auto-purged broker data for expired user {u.email}",
                                level="WARN", user_id=u.id))
            db.commit()
            db.close()
        except Exception:
            pass


# Dhan order status -> our trade status.
_EXT_STATUS_MAP = {
    "TRADED": "OPEN", "FILLED": "OPEN",
    "PENDING": "PENDING", "TRANSIT": "PENDING", "MODIFIED": "PENDING", "OPEN": "PENDING",
    "REJECTED": "REJECTED", "CANCELLED": "CANCELLED", "EXPIRED": "CANCELLED",
}


def _sync_external_orders(db, orders, broker="DHAN", account_id=0, user_id=0):
    """Mirror the broker's order book into this tenant's system."""
    for o in orders or []:
        oid = str(o.get("orderId", ""))
        if not oid:
            continue
        raw = str(o.get("orderStatus", "")).upper()
        mapped = _EXT_STATUS_MAP.get(raw)
        if not mapped:
            continue
        avg = float(o.get("averageTradedPrice") or 0)
        price = float(o.get("price") or 0)
        reason = o.get("omsErrorDescription") or o.get("text") or ""

        existing = db.query(Trade).filter(Trade.broker_order_id == oid,
                                          Trade.user_id == user_id).first()
        if existing:
            if existing.source == "EXTERNAL" and existing.status not in ("CLOSED",) \
                    and existing.status != mapped:
                old = existing.status
                existing.status = mapped
                if mapped == "OPEN" and not existing.entry_fill_price:
                    existing.entry_fill_price = avg or price
                if mapped == "REJECTED":
                    existing.exit_reason = "REJECTED"
                db.add(LogEntry(message=f"External order {existing.symbol} status: {old} -> {mapped}"
                                        + (f". Reason: {reason}" if reason else ""), user_id=user_id,
                                level="ERROR" if mapped == "REJECTED" else "INFO"))
            continue

        sec = str(o.get("securityId", ""))
        sym = o.get("tradingSymbol") or sec
        side = (o.get("transactionType") or "BUY").upper()
        try:
            qty = int(float(o.get("quantity") or 0))
        except Exception:
            qty = 0
        seg = o.get("exchangeSegment", "")
        opt = (o.get("drvOptionType") or "")
        itype = "OPTION" if opt in ("CALL", "PUT", "CE", "PE") else ("FUTURES" if "FNO" in seg else "EQUITY")
        t = Trade(symbol=sym, name=sym, security_id=sec, exchange_segment=seg, instrument_type=itype,
                  side=side, quantity=qty, lot_size=1, mode="LIVE", status=mapped,
                  entry_fill_price=(avg or 0) if mapped == "OPEN" else 0, entry_price=price,
                  broker=broker, account_id=account_id, user_id=user_id, source="EXTERNAL",
                  broker_order_id=oid, exit_reason="REJECTED" if mapped == "REJECTED" else "")
        db.add(t)
        db.add(LogEntry(message=f"Synced external order: {sym} {side} x{qty} [{raw}]"
                                + (f". Reason: {reason}" if reason else ""), user_id=user_id,
                        level="ERROR" if mapped == "REJECTED" else "INFO"))


def _auto_renew_loop():
    """Renew each connected account's token ~4h before it expires."""
    while True:
        time.sleep(1800)   # every 30 minutes
        try:
            db = SessionLocal()
            for a in db.query(Account).all():
                if not a.connected or not a.token_time:
                    continue
                age = (dt.datetime.utcnow() - a.token_time).total_seconds() / 3600
                if age < 20:
                    continue
                creds = _acc_creds(a)
                if a.broker == "DHAN" and creds.get("access_token"):
                    new = dhan_auth.renew_token(creds["access_token"], a.client_id)
                    if new:
                        _set_acc_creds(a, access_token=new)
                        a.token_time = dt.datetime.utcnow()
                        db.add(LogEntry(message=f"{a.label} token auto-renewed.", level="INFO"))
                    else:
                        db.add(LogEntry(message=f"{a.label} renew failed — log in again.", level="WARN"))
                elif a.broker == "ANGEL" and creds.get("jwt") and creds.get("refresh"):
                    ok, new = angel.renew(creds.get("api_key", ""), creds["jwt"], creds["refresh"])
                    if ok:
                        _set_acc_creds(a, jwt=new)
                        a.token_time = dt.datetime.utcnow()
                        db.add(LogEntry(message=f"{a.label} token auto-renewed.", level="INFO"))
                    else:
                        db.add(LogEntry(message=f"{a.label} renew failed — log in again.", level="WARN"))
                elif a.broker == "ALICE" and creds.get("api_key"):
                    # Alice Blue is key-based: re-login programmatically (no TOTP/redirect).
                    ok, res = aliceblue.login(a.client_id, creds["api_key"])
                    if ok:
                        _set_acc_creds(a, session_id=res)
                        a.token_time = dt.datetime.utcnow()
                        db.add(LogEntry(message=f"{a.label} session auto-renewed.", level="INFO"))
                    else:
                        db.add(LogEntry(message=f"{a.label} renew failed — log in again.", level="WARN"))
            db.commit()
            db.close()
        except Exception:
            pass


# ---------- trades ----------
def _owned_trade(db, trade_id, uid):
    t = db.get(Trade, trade_id)
    if not t or t.user_id != uid:
        raise HTTPException(404, "Trade not found")
    return t


@app.get("/api/trades", response_model=list[TradeOut])
def list_trades(request: Request, date: str = "", db: Session = Depends(get_db)):
    me = current_user(request)
    q = db.query(Trade).filter(Trade.user_id == me.id)
    if date:
        b = _ist_day_bounds(date)
        if b:
            q = q.filter(Trade.created_at >= b[0], Trade.created_at < b[1])
    return q.order_by(Trade.id.desc()).all()


@app.post("/api/trades", response_model=TradeOut)
def create_trade(payload: TradeCreate, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    # Trial users with no broker connected can't trade.
    if me.role != "SUPER_ADMIN" and not _broker_connected(db, me.id):
        raise HTTPException(400, "Please connect and authenticate your broker to start trading.")
    # Safety: block creating LIVE trades while the kill switch is on.
    if payload.mode == "LIVE" and uget(db, me.id, "kill_switch", "off") == "on":
        raise HTTPException(400, "Kill switch is ON. Turn it off to place LIVE trades.")
    # Block new orders once the daily limit halt has tripped for today.
    if _daily_halted(db, me.id):
        raise HTTPException(400, "Daily limit hit — trading is halted for today. "
                                 + (uget(db, me.id, "daily_halt_reason", "") or ""))
    # A real symbol must be picked (we need its Security ID to fetch the LTP).
    if not payload.security_id:
        raise HTTPException(400, "Please search and select a symbol from the list "
                                 "(so we know its Security ID for live prices).")
    data = payload.model_dump()
    raw_targets = data.pop("targets", [])
    targets = [{"points": float(x["points"]), "qty": int(x["qty"]), "hit": False}
               for x in raw_targets if float(x.get("points", 0)) > 0 and int(x.get("qty", 0)) > 0]
    t = Trade(**data)
    t.user_id = me.id
    t.name = data.get("name") or data["symbol"]
    t.targets_json = json.dumps(targets) if targets else ""
    # Provisional SL/target prices for display before entry (final ones computed at fill).
    ref = payload.entry_price if payload.entry_type == "LIMIT" else 0.0
    if ref > 0:
        t.stop_loss = level_price(t.side, ref, t.sl_points, False)
        t.target = level_price(t.side, ref, targets[0]["points"] if targets else t.target_points, True)
    db.add(t)
    db.commit()
    db.refresh(t)
    # Detailed creation log with the target broker / account + entry style.
    tp = _account_for_provider(db, uget(db, me.id, "trade_provider", "DEMO"), me.id)
    broker_txt = "Demo/Paper" if (t.mode == "TEST" or tp is None) else (tp.label or _label(tp.broker, tp.client_id))
    entry_txt = {"MARKET": "market", "LIMIT": f"limit @{t.entry_price}",
                 "SCHEDULED": f"scheduled {t.scheduled_time} IST",
                 "TRIGGER": f"trigger {t.trigger_dir or 'auto'} @{t.trigger_price}"}.get(t.entry_type, t.entry_type)
    db.add(LogEntry(message=f"Trade #{t.id} created: {t.side} {t.symbol} x{t.quantity} "
                            f"[{t.mode}] entry={entry_txt} via {broker_txt}", trade_id=t.id, user_id=me.id))
    db.commit()
    return t


@app.post("/api/trades/{trade_id}/modify", response_model=TradeOut)
def modify_trade(trade_id: int, payload: ModifyIn, request: Request, db: Session = Depends(get_db)):
    """Change stop-loss / targets / risk on an OPEN or PENDING trade."""
    me = current_user(request)
    t = _owned_trade(db, trade_id, me.id)
    if t.status not in ("OPEN", "PENDING"):
        raise HTTPException(400, "Only open or pending trades can be modified.")

    old_sl, old_trail = t.stop_loss, t.trail_sl
    old_targets = t.targets_json or (str(t.target) if t.target else "")
    changes = []

    # Trade-level monetary risk (edit / remove while running).
    if payload.max_profit_amt is not None:
        t.max_profit_amt = max(0.0, float(payload.max_profit_amt))
        changes.append(f"Max profit: {'₹%.0f' % t.max_profit_amt if t.max_profit_amt else 'off'}")
    if payload.max_loss_amt is not None:
        t.max_loss_amt = max(0.0, float(payload.max_loss_amt))
        changes.append(f"Max loss: {'₹%.0f' % t.max_loss_amt if t.max_loss_amt else 'off'}")
    if payload.lock_step is not None or payload.lock_amount is not None:
        if payload.lock_step is not None:
            t.lock_step = max(0.0, float(payload.lock_step))
        if payload.lock_amount is not None:
            t.lock_amount = max(0.0, float(payload.lock_amount))
        t.lock_floor = 0.0       # re-arm against the new rule
        changes.append(f"Profit-lock: every ₹{t.lock_step:.0f} secure ₹{t.lock_amount:.0f}"
                       if (t.lock_step and t.lock_amount) else "Profit-lock removed")

    # Pending-only edits: scheduled time / algo trigger price.
    if t.status == "PENDING":
        if payload.scheduled_time is not None:
            t.scheduled_time = payload.scheduled_time.strip()
            changes.append(f"Scheduled time: {t.scheduled_time or 'cleared'}")
        if payload.trigger_price is not None:
            t.trigger_price = max(0.0, float(payload.trigger_price))
            changes.append(f"Trigger price: {t.trigger_price or 'cleared'}")
        if payload.trigger_dir is not None:
            t.trigger_dir = str(payload.trigger_dir).upper()

    if payload.trail_mode is not None:
        t.trail_mode = "ENTRY" if str(payload.trail_mode).upper() == "ENTRY" else "CONTINUE"

    # Stop-loss and trailing now work TOGETHER: the SL is where the trail starts.
    if payload.stop_loss is not None:
        t.stop_loss = float(payload.stop_loss)
        if t.entry_fill_price > 0 and t.stop_loss > 0:
            t.sl_points = round(abs(t.entry_fill_price - t.stop_loss), 2)
        if old_sl != t.stop_loss:
            changes.append(f"SL: Old {old_sl or '-'} -> New {t.stop_loss}")

    if payload.trail_sl is not None:
        t.trail_sl = float(payload.trail_sl)
        if t.trail_sl > 0:
            t.hwm = t.last_price or t.entry_fill_price   # re-arm the trailing step reference
        if old_trail != t.trail_sl:
            changes.append(f"Trailing SL: Old {old_trail or 0}pt -> New {t.trail_sl}pt")

    if payload.targets is not None:
        clean = [{"price": float(x.price), "qty": int(x.qty), "hit": False}
                 for x in payload.targets if float(x.price) > 0 and int(x.qty) > 0]
        if len(clean) <= 1:
            # single target -> store as the plain target price, clear scale-out
            t.targets_json = ""
            t.target = clean[0]["price"] if clean else 0.0
            t.target_points = 0.0
            new_targets = str(t.target) if t.target else ""
        else:
            t.targets_json = json.dumps(clean)
            t.target = clean[0]["price"]
            t.target_points = 0.0
            new_targets = ", ".join(str(c["price"]) for c in clean)
        if old_targets != (t.targets_json or (str(t.target) if t.target else "")):
            changes.append(f"Targets: now {new_targets or '-'}")

    msg = f"Trade #{t.id} modified: " + ("; ".join(changes) if changes else "no change")
    db.add(LogEntry(message=msg, level="INFO", trade_id=t.id, user_id=me.id))
    db.commit()
    db.refresh(t)
    return t


@app.post("/api/trades/{trade_id}/close", response_model=TradeOut)
def close_trade(trade_id: int, request: Request, db: Session = Depends(get_db)):
    """Manually exit an OPEN trade at the current price."""
    me = current_user(request)
    t = _owned_trade(db, trade_id, me.id)
    if t.status != "OPEN":
        raise HTTPException(400, "Only OPEN trades can be closed.")
    price = t.last_price or t.entry_fill_price
    direction = 1 if t.side == "BUY" else -1
    remaining = t.quantity - (t.exited_qty or 0)
    t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * remaining
    t.exited_qty = t.quantity
    t.exit_fill_price = price
    t.status = "CLOSED"
    t.exit_reason = "MANUAL"
    t.pnl = round(t.realized_pnl, 2)
    db.add(LogEntry(message=f"Manual close {t.symbol} x{remaining} @ {price} P&L={t.pnl}", trade_id=t.id, user_id=me.id))
    db.commit()
    db.refresh(t)
    return t


@app.post("/api/trades/{trade_id}/cancel", response_model=TradeOut)
def cancel_trade(trade_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    t = _owned_trade(db, trade_id, me.id)
    if t.status != "PENDING":
        raise HTTPException(400, "Only PENDING trades can be cancelled.")
    t.status = "CANCELLED"
    t.exit_reason = "MANUAL"
    db.add(LogEntry(message=f"Trade #{t.id} cancelled (was pending): {t.symbol}", trade_id=t.id, user_id=me.id))
    db.commit()
    db.refresh(t)
    return t


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    t = _owned_trade(db, trade_id, me.id)
    db.delete(t)
    db.commit()
    return {"ok": True}


# ---------- instruments (broker-style picker) ----------
@app.get("/api/underlyings/search")
def underlyings_search(q: str = "", kind: str = "OPTION", limit: int = 25):
    """Search underlyings that have options (kind=OPTION) or futures (kind=FUTURES)."""
    return instruments.search_underlyings(q, kind.upper(), limit=limit)


@app.get("/api/equities/search")
def equities_search(q: str = "", limit: int = 25):
    """Search stocks / indices for equity trading (returns Security IDs directly)."""
    return instruments.search_equities(q, limit=limit)


@app.get("/api/expiries")
def expiries(underlying: str, kind: str = "OPTION"):
    return instruments.expiries(underlying, kind.upper())


@app.get("/api/optionchain")
def option_chain(underlying: str, expiry: str = ""):
    exps = instruments.expiries(underlying, "OPTION")
    if not expiry and exps:
        expiry = exps[0]
    return instruments.option_chain(underlying, expiry)


@app.get("/api/futures")
def futures(underlying: str):
    return instruments.futures(underlying)


@app.post("/api/ltp")
def ltp(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Fetch live LTP for a list of instruments (used to fill the option chain)."""
    me = current_user(request)
    items = payload.get("items", [])
    by_seg = {}
    for it in items:
        seg = it.get("exchange_segment")
        sid = str(it.get("security_id"))
        if seg and sid:
            by_seg.setdefault(seg, []).append(sid)

    acc = _account_for_provider(db, uget(db, me.id, "data_provider", "DEMO"), me.id)
    if acc is None:     # DEMO — admin only; normal users must connect a broker
        if me.role != "SUPER_ADMIN":
            return {"connected": False, "prices": {}, "need_broker": True}
        res = demo_market.get_ltp_batch(by_seg)
        return {"connected": True, "prices": {sid: px for (seg, sid), px in res.items()}}

    # Real-time first: read whatever the broker's WebSocket already streams, and
    # REST-fetch only the rest (the socket keeps prices snappy; REST is the net).
    instruments = [(seg, sid) for seg, ids in by_seg.items() for sid in ids]
    ws = {}
    try:
        ws = feeds.manager.snapshot(acc, instruments)
    except Exception:
        ws = {}
    missing = {}
    for (seg, sid) in instruments:
        if (seg, sid) not in ws:
            missing.setdefault(seg, []).append(sid)

    creds = _acc_creds(acc)
    if acc.broker == "ANGEL":
        cid, key, jwt = acc.client_id, creds.get("api_key", ""), creds.get("jwt", "")
        if not (cid and key and jwt) or not by_seg:
            return {"connected": bool(cid and key and jwt), "prices": {}}
        md = angel.AngelMarketData(cid, key, jwt)
    elif acc.broker == "ZERODHA":
        key, tok = creds.get("api_key", ""), creds.get("access_token", "")
        if not (key and tok) or not by_seg:
            return {"connected": bool(key and tok), "prices": {}}
        md = zerodha.ZerodhaMarketData(key, tok)
    elif acc.broker == "ALICE":
        cid, sid = acc.client_id, creds.get("session_id", "")
        if not (cid and sid) or not by_seg:
            return {"connected": bool(cid and sid), "prices": {}}
        md = aliceblue.AliceMarketData(cid, sid)
    else:               # DHAN
        cid, tok = acc.client_id, creds.get("access_token", "")
        if not cid or not tok or not by_seg:
            return {"connected": bool(cid and tok), "prices": {}}
        md = DhanMarketData(cid, tok)
    res = md.get_ltp_batch(missing) if missing else {}
    prices = {sid: px for (seg, sid), px in res.items()}
    prices.update({sid: px for (seg, sid), px in ws.items()})   # live ticks win
    return {"connected": True, "prices": prices,
            "ws": bool(ws) and feeds.manager.status(acc.id).get("streaming", False),
            "error": md.last_error if not prices else ""}


# ---------- real-time tick stream (Server-Sent Events) ----------
# The browser registers the instruments it's looking at (option chain / selected
# contract) and then opens an EventSource; the server pushes price changes as the
# broker's WebSocket delivers them. The 5-second /api/ltp poll stays as a safety
# net, so prices keep flowing even if the stream/socket drops.
_watch = {}                     # uid -> {"acc_id": int, "demo": bool, "items": [(seg, sid)]}
_watch_lock = threading.Lock()


@app.post("/api/stream/watch")
def stream_watch(payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    items = []
    for it in payload.get("items", []):
        seg = it.get("exchange_segment")
        sid = str(it.get("security_id"))
        if seg and sid:
            items.append((seg, sid))
    acc = _account_for_provider(db, uget(db, me.id, "data_provider", "DEMO"), me.id)
    demo = acc is None and me.role == "SUPER_ADMIN"
    with _watch_lock:
        _watch[me.id] = {"acc_id": acc.id if acc else 0, "demo": demo, "items": items}
    # Prime the feed now (creates / subscribes with the account's fresh creds) so
    # ticks begin flowing before the next engine pass.
    if acc is not None and items:
        try:
            feeds.manager.snapshot(acc, items)
        except Exception:
            pass
    ws_on = feeds.manager.available and acc is not None
    return {"ok": True, "ws": ws_on}


@app.get("/api/stream")
async def stream(request: Request):
    """Server-Sent Events stream of live price ticks for this user's watched
    instruments. Sends only changed prices; heartbeats keep the line open."""
    me = current_user(request)
    uid = me.id

    async def gen():
        last = {}
        t0 = time.time()
        hb = t0
        yield "retry: 3000\n\n"
        while True:
            if await request.is_disconnected():
                break
            if time.time() - t0 > 600:          # recycle hourly-ish; client reconnects
                break
            with _watch_lock:
                w = _watch.get(uid)
            prices = {}
            if w and w["items"]:
                if w["demo"]:
                    by_seg = {}
                    for seg, sid in w["items"]:
                        by_seg.setdefault(seg, []).append(sid)
                    res = demo_market.get_ltp_batch(by_seg)
                    prices = {sid: px for (seg, sid), px in res.items()}
                elif w["acc_id"]:
                    snap = feeds.manager.snapshot_cached(w["acc_id"], w["items"])
                    prices = {sid: px for (seg, sid), px in snap.items()}
            delta = {k: v for k, v in prices.items() if last.get(k) != v}
            if delta:
                last.update(delta)
                yield "data: " + json.dumps(delta) + "\n\n"
                hb = time.time()
            elif time.time() - hb > 15:
                yield ": hb\n\n"
                hb = time.time()
            await asyncio.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


# ---------- demo controls (push price up/down, reset) ----------
@app.post("/api/demo/direction")
def demo_direction(payload: dict):
    d = str(payload.get("direction", "FLAT")).upper()
    demo_market.set_direction(1 if d == "UP" else (-1 if d == "DOWN" else 0))
    return {"direction": demo_market.direction}


@app.post("/api/demo/reset")
def demo_reset(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    demo_market.reset()
    db.add(LogEntry(message="Demo prices reset", level="INFO", user_id=me.id))
    db.commit()
    return {"ok": True}


@app.get("/api/instruments/status")
def instruments_status():
    return instruments.status()


@app.post("/api/instruments/refresh")
def instruments_refresh():
    instruments.refresh()        # Dhan master (the universal picker base)
    angel.mapper.load_async()    # Angel master (for translation)
    zerodha.mapper.load_async()  # Zerodha master (for translation)
    aliceblue.mapper.load_async()  # Alice Blue masters (for translation)
    return {"ok": True, "message": "Refreshing symbol lists in the background…"}


# ---------- summary ----------
# ---------- summary / P&L ----------
def _trade_env(t):
    """Which environment a trade belongs to: PAPER (demo) / DHAN / ANGEL."""
    if t.broker:
        return t.broker
    return "PAPER" if t.mode == "TEST" else "DHAN"


def _metrics(trades):
    """Broker-style P&L: Booked (realized) + Active (unrealized MTM) = Total.

    - Booked  = realized P&L of fully-closed trades + partially-booked legs of
      still-open trades (mirrors a broker terminal's "realized" / booked figure).
    - Active  = unrealized MTM on the quantity still open.
    - Total   = Booked + Active (the live MTM the broker shows).
    """
    closed = [t for t in trades if t.status == "CLOSED"]
    open_trades = [t for t in trades if t.status == "OPEN"]
    booked = sum(t.pnl for t in closed) + sum(t.realized_pnl or 0 for t in open_trades)
    active = sum((t.pnl or 0) - (t.realized_pnl or 0) for t in open_trades)
    total = booked + active
    return {
        "booked": round(booked, 2),
        "active": round(active, 2),
        "total": round(total, 2),
        # legacy aliases (kept so older callers don't break)
        "net": round(total, 2),
        "gross": round(total, 2),
        "open_pnl": round(active, 2),
        "closed_pnl": round(booked, 2),
        "open": len(open_trades),
        "pending": sum(1 for t in trades if t.status == "PENDING"),
        "closed": len(closed),
    }


def _build_summary(db, uid, broker="ALL", date=""):
    q = db.query(Trade).filter(Trade.user_id == uid)
    if date:
        b = _ist_day_bounds(date)
        if b:
            q = q.filter(Trade.created_at >= b[0], Trade.created_at < b[1])
    trades = q.all()
    groups = {}
    for t in trades:
        groups.setdefault(t.account_id or 0, []).append(t)
    flt = (broker or "ALL")
    if flt == "ALL":
        selected = trades
    else:
        try:
            selected = groups.get(int(flt), [])
        except Exception:
            selected = trades
    pnl = _metrics(selected)
    breakdown = [{"key": "0", "label": "Demo / Paper", "net": _metrics(groups.get(0, []))["net"]}]
    for a in db.query(Account).filter(Account.user_id == uid).order_by(Account.id).all():
        breakdown.append({"key": str(a.id), "label": a.label or _label(a.broker, a.client_id),
                          "net": _metrics(groups.get(a.id, []))["net"]})
    active = db.query(Trade).filter(Trade.user_id == uid,
                                    Trade.status.in_(["OPEN", "PENDING"])).count()
    data_provider = uget(db, uid, "data_provider", "DEMO")
    trade_provider = uget(db, uid, "trade_provider", "DEMO")
    data_acc = _account_for_provider(db, data_provider, uid)
    trade_acc = _account_for_provider(db, trade_provider, uid)
    data_name = "Demo" if data_acc is None else (data_acc.label or _label(data_acc.broker, data_acc.client_id))
    broker_name = "Demo" if trade_acc is None else (trade_acc.label or _label(trade_acc.broker, trade_acc.client_id))
    trade_connected = trade_acc is not None and bool(trade_acc.connected)
    balance = None
    if trade_acc is not None and trade_connected \
            and uget(db, uid, "broker_balance_provider", "") == str(trade_acc.id):
        bal = uget(db, uid, "broker_balance", "")
        balance = float(bal) if bal else None
    # Keep the price-feed banner honest: only call it "real-time (WebSocket)"
    # if the data account's socket is actually delivering fresh ticks right now.
    md_status = uget(db, uid, "md_status", "")
    if md_status == "ok:ws":
        streaming = False
        if data_acc is not None:
            try:
                streaming = feeds.manager.status(data_acc.id).get("streaming", False)
            except Exception:
                streaming = False
        if not streaming:
            md_status = "ok:rest" if data_acc is not None else "ok:demo"

    broker_alert, alert_msg = False, ""
    if trade_acc is not None and active > 0:
        if not trade_connected:
            broker_alert = True
            alert_msg = f"{broker_name} is NOT connected but trades are running — reconnect on the Broker tab."
        elif uget(db, uid, "broker_health", "") == "error":
            broker_alert = True
            alert_msg = f"Lost connection to {broker_name} — exits may not fire. Check the Broker tab."
    return {
        "total_trades": len(trades),
        "pending": pnl["pending"], "open": pnl["open"], "closed": pnl["closed"],
        "open_pnl": pnl["open_pnl"], "closed_pnl": pnl["closed_pnl"], "total_pnl": pnl["net"],
        "pnl": pnl, "pnl_filter": flt, "pnl_breakdown": breakdown, "date": date,
        "kill_switch": uget(db, uid, "kill_switch", "off"),
        "md_status": md_status,
        "instruments": instruments.status(),
        "angel_map": angel.mapper.status(),
        "zerodha_map": zerodha.mapper.status(),
        "alice_map": aliceblue.mapper.status(),
        "demo_direction": demo_market.direction,
        "data_provider": data_provider, "trade_provider": trade_provider,
        "data_name": data_name, "broker_name": broker_name, "balance": balance,
        "broker_alert": broker_alert, "alert_msg": alert_msg,
        "active_locked": active > 0,
        "daily_halt": _daily_halted(db, uid),
        "daily_halt_reason": uget(db, uid, "daily_halt_reason", ""),
    }


@app.get("/api/summary")
def summary(request: Request, broker: str = "ALL", date: str = "", db: Session = Depends(get_db)):
    me = current_user(request)
    return _build_summary(db, me.id, broker, date)


# ---------- logs ----------
@app.get("/api/logs")
def list_logs(request: Request, date: str = "", level: str = "", page: int = 1, per_page: int = 25,
              db: Session = Depends(get_db)):
    import math
    me = current_user(request)
    q = db.query(LogEntry).filter(LogEntry.user_id == me.id)
    if date:
        q = q.filter(LogEntry.day == date)
    if level:
        q = q.filter(LogEntry.level == level)
    total = q.count()
    page = max(1, page)
    per_page = min(max(per_page, 1), 200)
    rows = (q.order_by(LogEntry.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    days = [d[0] for d in db.query(LogEntry.day).filter(LogEntry.user_id == me.id).distinct()
            .order_by(LogEntry.day.desc()).all() if d[0]]
    return {
        "logs": [{"id": r.id, "time": r.created_at.isoformat(), "level": r.level,
                  "trade_id": r.trade_id, "message": r.message, "day": r.day} for r in rows],
        "page": page, "per_page": per_page, "total": total,
        "pages": max(1, math.ceil(total / per_page)), "days": days,
    }


# ---------- settings (kill switch etc.) — per user ----------
@app.get("/api/settings")
def get_settings(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    return {
        "kill_switch": uget(db, me.id, "kill_switch", "off"),
        "default_mode": uget(db, me.id, "default_mode", "TEST"),
        "daily_max_profit": unum(db, me.id, "daily_max_profit"),
        "daily_max_loss": unum(db, me.id, "daily_max_loss"),
        "global_lock_step": unum(db, me.id, "global_lock_step"),
        "global_lock_amount": unum(db, me.id, "global_lock_amount"),
        "daily_halt": _daily_halted(db, me.id),
        "daily_halt_reason": uget(db, me.id, "daily_halt_reason", ""),
    }


@app.post("/api/settings")
def update_settings(payload: SettingsIn, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    changed = []
    if payload.kill_switch is not None:
        uset(db, me.id, "kill_switch", "on" if payload.kill_switch else "off")
        db.add(LogEntry(message=f"Kill switch set to "
                                f"{'ON' if payload.kill_switch else 'OFF'}", level="WARN", user_id=me.id))
    if payload.default_mode in ("TEST", "LIVE"):
        uset(db, me.id, "default_mode", payload.default_mode)
    if payload.daily_max_profit is not None:
        uset(db, me.id, "daily_max_profit", str(max(0.0, float(payload.daily_max_profit))))
        changed.append(f"daily max profit ₹{max(0.0, float(payload.daily_max_profit)):.0f}")
    if payload.daily_max_loss is not None:
        uset(db, me.id, "daily_max_loss", str(max(0.0, float(payload.daily_max_loss))))
        changed.append(f"daily max loss ₹{max(0.0, float(payload.daily_max_loss)):.0f}")
    if payload.global_lock_step is not None:
        uset(db, me.id, "global_lock_step", str(max(0.0, float(payload.global_lock_step))))
    if payload.global_lock_amount is not None:
        uset(db, me.id, "global_lock_amount", str(max(0.0, float(payload.global_lock_amount))))
    if payload.global_lock_step is not None or payload.global_lock_amount is not None:
        changed.append(f"account profit-lock every ₹{unum(db, me.id, 'global_lock_step'):.0f} "
                       f"secure ₹{unum(db, me.id, 'global_lock_amount'):.0f}")
    if changed:
        db.add(LogEntry(message="Settings updated: " + ", ".join(changed), level="INFO", user_id=me.id))
    db.commit()
    return get_settings(request, db)


@app.post("/api/settings/reset_halt")
def reset_daily_halt(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    uset(db, me.id, "daily_halt", "off")
    uset(db, me.id, "daily_halt_date", "")
    uset(db, me.id, "daily_halt_reason", "")
    uset(db, me.id, "global_lock_floor", "0")
    db.add(LogEntry(message="Daily limit halt manually cleared.", level="WARN", user_id=me.id))
    db.commit()
    return get_settings(request, db)


# ---------- symbol presets ----------
def _preset_dict(p):
    try:
        targets = json.loads(p.targets_json or "[]")
    except Exception:
        targets = []
    return {
        "id": p.id, "symbol": p.symbol, "kind": p.kind or "OPTION", "lots": p.lots or 0,
        "sl_points": p.sl_points, "trail_sl": p.trail_sl,
        "trail_mode": p.trail_mode, "target_points": p.target_points, "targets": targets,
        "max_profit_amt": p.max_profit_amt, "max_loss_amt": p.max_loss_amt,
        "lock_step": p.lock_step or 0.0, "lock_amount": p.lock_amount or 0.0,
    }


@app.get("/api/presets")
def list_presets(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    return [_preset_dict(p) for p in db.query(SymbolPreset)
            .filter(SymbolPreset.user_id == me.id).order_by(SymbolPreset.symbol).all()]


@app.post("/api/presets")
def save_preset(payload: SymbolPresetIn, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    sym = (payload.symbol or "").strip().upper()
    kind = str(payload.kind or "OPTION").upper()
    if kind not in ("OPTION", "FUTURES", "EQUITY"):
        kind = "OPTION"
    if not sym:
        raise HTTPException(400, "Enter a symbol (underlying) for the preset.")
    # One preset per (symbol, type) per user — saving the same one updates it.
    p = (db.query(SymbolPreset)
         .filter(SymbolPreset.user_id == me.id, SymbolPreset.symbol == sym,
                 SymbolPreset.kind == kind).first())
    is_new = p is None
    if p is None:
        p = SymbolPreset(symbol=sym, kind=kind, user_id=me.id)
        db.add(p)
    p.kind = kind
    p.lots = max(0, int(payload.lots or 0))
    p.sl_points = max(0.0, float(payload.sl_points))
    p.trail_sl = max(0.0, float(payload.trail_sl))
    p.trail_mode = "ENTRY" if str(payload.trail_mode).upper() == "ENTRY" else "CONTINUE"
    p.target_points = max(0.0, float(payload.target_points))
    p.targets_json = json.dumps([float(x) for x in payload.targets if float(x) > 0]) \
        if payload.targets else ""
    p.max_profit_amt = max(0.0, float(payload.max_profit_amt))
    p.max_loss_amt = max(0.0, float(payload.max_loss_amt))
    p.lock_step = max(0.0, float(payload.lock_step))
    p.lock_amount = max(0.0, float(payload.lock_amount))
    db.commit()
    db.refresh(p)
    db.add(LogEntry(message=f"Symbol preset {'created' if is_new else 'updated'}: {sym} [{kind}]", level="INFO", user_id=me.id))
    db.commit()
    return _preset_dict(p)


@app.delete("/api/presets/{preset_id}")
def delete_preset(preset_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    p = db.get(SymbolPreset, preset_id)
    if p and p.user_id == me.id:
        sym, kind = p.symbol, p.kind
        db.delete(p)
        db.add(LogEntry(message=f"Symbol preset deleted: {sym} [{kind}]", level="INFO", user_id=me.id))
        db.commit()
    return {"ok": True}


# ---------- watchlist ----------
def _watch_dict(w):
    return {"id": w.id, "symbol": w.symbol, "security_id": w.security_id,
            "exchange_segment": w.exchange_segment, "instrument_type": w.instrument_type,
            "underlying": w.underlying, "lot_size": w.lot_size or 1}


@app.get("/api/watchlist")
def list_watchlist(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    return [_watch_dict(w) for w in db.query(Watchlist)
            .filter(Watchlist.user_id == me.id).order_by(Watchlist.id).all()]


@app.post("/api/watchlist")
def add_watchlist(payload: WatchlistIn, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    if not payload.security_id and not (payload.underlying or payload.symbol):
        raise HTTPException(400, "Select a symbol first, then add it to the watchlist.")
    if payload.security_id:
        exists = (db.query(Watchlist)
                  .filter(Watchlist.user_id == me.id, Watchlist.security_id == payload.security_id,
                          Watchlist.exchange_segment == payload.exchange_segment).first())
    else:
        exists = (db.query(Watchlist)
                  .filter(Watchlist.user_id == me.id, Watchlist.security_id == "",
                          Watchlist.underlying == (payload.underlying or payload.symbol),
                          Watchlist.instrument_type == payload.instrument_type).first())
    if exists:
        return _watch_dict(exists)
    w = Watchlist(user_id=me.id, symbol=payload.symbol, security_id=payload.security_id,
                  exchange_segment=payload.exchange_segment,
                  instrument_type=payload.instrument_type,
                  underlying=(payload.underlying or payload.symbol),
                  lot_size=max(1, int(payload.lot_size or 1)))
    db.add(w)
    db.commit()
    db.refresh(w)
    return _watch_dict(w)


@app.delete("/api/watchlist/{item_id}")
def delete_watchlist(item_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    w = db.get(Watchlist, item_id)
    if w and w.user_id == me.id:
        db.delete(w)
        db.commit()
    return {"ok": True}


# ---------- baskets (multi-leg orders: build / execute / schedule) ----------
def _owned_basket(db, basket_id, uid):
    b = db.get(Basket, basket_id)
    if b is None or b.user_id != uid:
        raise HTTPException(404, "Basket not found.")
    return b


def _ist_basket_time(date_str, time_str):
    """IST date (YYYY-MM-DD, default today) + time (HH:MM:SS) -> naive UTC datetime.
    With no date given, a time already past today rolls to tomorrow."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    now_ist = dt.datetime.now(ist)
    try:
        parts = [int(x) for x in str(time_str).split(":")]
        hh, mm, ss = (parts + [0, 0])[:3]
    except Exception:
        raise HTTPException(400, "Enter the time as HH:MM:SS.")
    if date_str:
        try:
            d = dt.datetime.strptime(date_str, "%Y-%m-%d").date()
        except Exception:
            raise HTTPException(400, "Enter the date as YYYY-MM-DD.")
    else:
        d = now_ist.date()
    fire = dt.datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=ist)
    if not date_str and fire <= now_ist:
        fire += dt.timedelta(days=1)
    return fire.astimezone(dt.timezone.utc).replace(tzinfo=None)


def _basket_dict(db, b):
    legs = (db.query(BasketLeg).filter(BasketLeg.basket_id == b.id)
            .order_by(BasketLeg.seq, BasketLeg.id).all())
    trades = db.query(Trade).filter(Trade.basket_id == b.id).all()
    by_leg = {}
    for t in trades:
        by_leg.setdefault(t.leg_id, t)
    active_mtm = round(sum((t.pnl or 0) for t in trades if t.status == "OPEN"), 2)
    booked = round(sum((t.pnl or 0) for t in trades if t.status == "CLOSED")
                   + sum((t.realized_pnl or 0) for t in trades if t.status == "OPEN"), 2)
    sched_ist = ""
    if b.scheduled_at:
        ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
        sched_ist = (b.scheduled_at.replace(tzinfo=dt.timezone.utc)
                     .astimezone(ist).strftime("%Y-%m-%d %H:%M:%S"))
    return {
        "id": b.id, "name": b.name, "mode": b.mode, "is_active": bool(b.is_active),
        "is_scheduled": bool(b.is_scheduled), "schedule_status": b.schedule_status,
        "scheduled_at_ist": sched_ist, "exec_status": b.exec_status,
        "lock_step": b.lock_step, "lock_amount": b.lock_amount, "lock_floor": b.lock_floor,
        "active_mtm": active_mtm, "booked": booked, "mtm": round(active_mtm + booked, 2),
        "legs": [{
            "id": l.id, "seq": l.seq, "symbol": l.symbol, "security_id": l.security_id,
            "exchange_segment": l.exchange_segment, "instrument_type": l.instrument_type,
            "underlying": l.underlying, "lot_size": l.lot_size,
            "transaction_type": l.transaction_type, "order_type": l.order_type,
            "entry_type": l.entry_type, "quantity": l.quantity, "price": l.price,
            "trigger_price": l.trigger_price, "trigger_dir": l.trigger_dir,
            "scheduled_time": l.scheduled_time, "sl_points": l.sl_points,
            "target_points": l.target_points, "trail_sl": l.trail_sl, "trail_mode": l.trail_mode,
            "targets_json": l.targets_json, "max_profit_amt": l.max_profit_amt,
            "max_loss_amt": l.max_loss_amt, "lock_step": l.lock_step, "lock_amount": l.lock_amount,
            "status": l.status, "broker_order_id": l.broker_order_id,
            "fill_price": l.fill_price, "error": l.error,
            "ltp": (by_leg[l.id].last_price if l.id in by_leg else 0),
            "pnl": (by_leg[l.id].pnl if l.id in by_leg else 0),
        } for l in legs],
    }


def _leg_fields(d):
    """Normalise a leg payload (from the New Trade form or CSV) into column values."""
    et = str(d.get("entry_type", "") or "").upper()
    if et not in ("MARKET", "LIMIT", "SCHEDULED", "TRIGGER"):
        # CSV / legacy callers send order_type (MARKET/LIMIT/SL) instead.
        et = {"LIMIT": "LIMIT", "SL": "TRIGGER"}.get(str(d.get("order_type", "MARKET")).upper(), "MARKET")
    raw_targets = d.get("targets") or []
    targets = [{"points": float(x.get("points", 0)), "qty": int(x.get("qty", 0)), "hit": False}
               for x in raw_targets if float(x.get("points", 0) or 0) > 0 and int(x.get("qty", 0) or 0) > 0]
    order_type = {"MARKET": "MARKET", "LIMIT": "LIMIT", "SCHEDULED": "TIME", "TRIGGER": "SL"}[et]
    return {
        "symbol": str(d.get("symbol", "")).strip(),
        "security_id": str(d.get("security_id", "") or ""),
        "exchange_segment": str(d.get("exchange_segment", "") or ""),
        "instrument_type": str(d.get("instrument_type", "OPTION") or "OPTION"),
        "underlying": str(d.get("underlying", "") or ""),
        "lot_size": int(float(d.get("lot_size", 1) or 1)) or 1,
        "transaction_type": "SELL" if str(d.get("transaction_type", "BUY")).upper() == "SELL" else "BUY",
        "order_type": order_type, "entry_type": et,
        "quantity": int(float(d.get("quantity", 1) or 1)) or 1,
        "price": float(d.get("price", d.get("entry_price", 0)) or 0),
        "trigger_price": float(d.get("trigger_price", 0) or 0),
        "trigger_dir": str(d.get("trigger_dir", "") or "").upper(),
        "scheduled_time": str(d.get("scheduled_time", "") or "").strip(),
        "sl_points": float(d.get("sl_points", 0) or 0),
        "target_points": float(d.get("target_points", 0) or 0),
        "trail_sl": float(d.get("trail_sl", 0) or 0),
        "trail_mode": "ENTRY" if str(d.get("trail_mode", "CONTINUE")).upper() == "ENTRY" else "CONTINUE",
        "targets_json": json.dumps(targets) if targets else "",
        "max_profit_amt": float(d.get("max_profit_amt", 0) or 0),
        "max_loss_amt": float(d.get("max_loss_amt", 0) or 0),
        "lock_step": float(d.get("lock_step", 0) or 0),
        "lock_amount": float(d.get("lock_amount", 0) or 0),
    }


def _add_leg(db, b, d, seq):
    leg = BasketLeg(basket_id=b.id, user_id=b.user_id, seq=seq, **_leg_fields(d))
    db.add(leg)
    return leg


@app.get("/api/baskets")
def basket_list(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    bs = (db.query(Basket).filter(Basket.user_id == me.id, Basket.is_active == 1)
          .order_by(Basket.id.desc()).all())
    return [_basket_dict(db, b) for b in bs]


@app.post("/api/baskets")
def basket_create(payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    name = (str(payload.get("name", "")).strip() or "Basket")[:80]
    mode = "LIVE" if str(payload.get("mode", "TEST")).upper() == "LIVE" else "TEST"
    b = Basket(user_id=me.id, name=name, mode=mode)
    db.add(b)
    db.commit()
    for i, d in enumerate(payload.get("legs", []) or []):
        if str(d.get("symbol", "")).strip():
            _add_leg(db, b, d, i)
    db.commit()
    return _basket_dict(db, b)


@app.post("/api/baskets/{basket_id}")
def basket_update(basket_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    if "name" in payload:
        b.name = (str(payload["name"]).strip()[:80]) or b.name
    if "mode" in payload:
        b.mode = "LIVE" if str(payload["mode"]).upper() == "LIVE" else "TEST"
    if "is_active" in payload:
        b.is_active = 1 if payload["is_active"] else 0
    if "lock_step" in payload:
        b.lock_step = max(0.0, float(payload.get("lock_step") or 0))
    if "lock_amount" in payload:
        b.lock_amount = max(0.0, float(payload.get("lock_amount") or 0))
    if "lock_step" in payload or "lock_amount" in payload:
        b.lock_floor = 0.0
    db.commit()
    return _basket_dict(db, b)


@app.delete("/api/baskets/{basket_id}")
def basket_delete(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    db.query(BasketLeg).filter(BasketLeg.basket_id == b.id).delete()
    db.delete(b)
    db.commit()
    return {"ok": True}


@app.post("/api/baskets/{basket_id}/legs")
def basket_set_legs(basket_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    """Save the builder's legs. replace=true wipes & re-adds (also used by CSV import);
    replace=false appends (used to add a single leg)."""
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    replace = bool(payload.get("replace", True))
    if replace:
        db.query(BasketLeg).filter(BasketLeg.basket_id == b.id).delete()
        base = 0
    else:
        base = db.query(BasketLeg).filter(BasketLeg.basket_id == b.id).count()
    for i, d in enumerate(payload.get("legs", []) or []):
        if str(d.get("symbol", "")).strip():
            _add_leg(db, b, d, base + i)
    db.commit()
    return _basket_dict(db, b)


@app.post("/api/baskets/{basket_id}/legs/{leg_id}")
def basket_update_leg(basket_id: int, leg_id: int, payload: dict, request: Request,
                      db: Session = Depends(get_db)):
    """Replace a single leg's full config (used when editing a leg in the form)."""
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    l = db.get(BasketLeg, leg_id)
    if not l or l.basket_id != b.id:
        raise HTTPException(404, "Leg not found.")
    if l.status not in ("PENDING",):
        raise HTTPException(400, "Only legs that haven't fired yet can be edited.")
    for k, v in _leg_fields(payload).items():
        setattr(l, k, v)
    db.commit()
    return _basket_dict(db, b)


@app.delete("/api/baskets/{basket_id}/legs/{leg_id}")
def basket_del_leg(basket_id: int, leg_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    l = db.get(BasketLeg, leg_id)
    if l and l.basket_id == b.id:
        db.delete(l)
        db.commit()
    return _basket_dict(db, b)


@app.post("/api/baskets/{basket_id}/clear")
def basket_clear(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    db.query(BasketLeg).filter(BasketLeg.basket_id == b.id).delete()
    b.exec_status = ""
    db.commit()
    return _basket_dict(db, b)


@app.post("/api/baskets/{basket_id}/invert")
def basket_invert(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    for l in db.query(BasketLeg).filter(BasketLeg.basket_id == b.id).all():
        l.transaction_type = "SELL" if l.transaction_type == "BUY" else "BUY"
    db.commit()
    return _basket_dict(db, b)


@app.post("/api/baskets/{basket_id}/clone")
def basket_clone(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    nb = Basket(user_id=me.id, name=(b.name + " (copy)")[:80], mode=b.mode,
                lock_step=b.lock_step, lock_amount=b.lock_amount)
    db.add(nb)
    db.flush()
    legs = (db.query(BasketLeg).filter(BasketLeg.basket_id == b.id)
            .order_by(BasketLeg.seq, BasketLeg.id).all())
    for i, l in enumerate(legs):
        db.add(BasketLeg(basket_id=nb.id, user_id=me.id, seq=i, symbol=l.symbol,
                         security_id=l.security_id, exchange_segment=l.exchange_segment,
                         instrument_type=l.instrument_type, underlying=l.underlying,
                         lot_size=l.lot_size, transaction_type=l.transaction_type,
                         order_type=l.order_type, quantity=l.quantity, price=l.price,
                         trigger_price=l.trigger_price))
    db.commit()
    return _basket_dict(db, nb)


@app.get("/api/baskets/{basket_id}/export")
def basket_export(basket_id: int, request: Request, db: Session = Depends(get_db)):
    import io
    import csv as _csv
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    legs = (db.query(BasketLeg).filter(BasketLeg.basket_id == b.id)
            .order_by(BasketLeg.seq, BasketLeg.id).all())
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["symbol", "security_id", "exchange_segment", "instrument_type", "underlying",
                "transaction_type", "order_type", "quantity", "price", "trigger_price", "lot_size"])
    for l in legs:
        w.writerow([l.symbol, l.security_id, l.exchange_segment, l.instrument_type, l.underlying,
                    l.transaction_type, l.order_type, l.quantity, l.price, l.trigger_price, l.lot_size])
    fn = (b.name or "basket").replace(" ", "_") + ".csv"
    return PlainTextResponse(buf.getvalue(), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})


@app.post("/api/baskets/{basket_id}/margin")
def basket_margin_check(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    legs = (db.query(BasketLeg).filter(BasketLeg.basket_id == b.id)
            .order_by(BasketLeg.seq, BasketLeg.id).all())
    if not legs:
        raise HTTPException(400, "Add at least one leg first.")
    acc = _account_for_provider(db, uget(db, me.id, "trade_provider", "DEMO"), me.id)
    if acc is None:
        return {"required": margins._estimate(legs), "available": 0, "ok": True,
                "verified": False, "detail": "No live broker selected — showing a local estimate."}
    return margins.basket_margin(db, acc, legs)


@app.post("/api/baskets/{basket_id}/execute")
def basket_execute(basket_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    force = bool(payload.get("force"))
    pending_legs = (db.query(BasketLeg)
                    .filter(BasketLeg.basket_id == b.id,
                            BasketLeg.status.in_(["PENDING", "FAILED"])).count())
    if pending_legs == 0:
        raise HTTPException(400, "This basket has no legs left to execute.")
    # Conflict: a schedule is pending — let the UI confirm cancel-and-fire-now.
    if b.is_scheduled and b.schedule_status == "PENDING" and not force:
        return {"conflict": True, "scheduled_at_ist": _basket_dict(db, b)["scheduled_at_ist"]}
    # Immediate guardrails (the dispatch re-checks too).
    if b.mode == "LIVE":
        if me.role != "SUPER_ADMIN" and not _broker_connected(db, me.id):
            raise HTTPException(400, "Connect your broker before firing a LIVE basket.")
        if uget(db, me.id, "kill_switch", "off") == "on":
            raise HTTPException(400, "Kill switch is ON. Turn it off to fire a LIVE basket.")
    if _daily_halted(db, me.id):
        raise HTTPException(400, "Daily limit hit — trading is halted for today.")
    if force and b.is_scheduled:
        baskets.cancel_schedule(db, b.id)
        db.add(LogEntry(message=f"Basket '{b.name}': schedule cancelled — firing manually now.",
                        user_id=me.id))
        db.commit()
    baskets.execute_now(b.id)
    return {"dispatching": True}


@app.post("/api/baskets/{basket_id}/schedule")
def basket_schedule(basket_id: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    if not payload.get("enabled", True):
        baskets.cancel_schedule(db, b.id)
        db.refresh(b)
        return _basket_dict(db, b)
    if db.query(BasketLeg).filter(BasketLeg.basket_id == b.id).count() == 0:
        raise HTTPException(400, "Add at least one leg before scheduling.")
    b.scheduled_at = _ist_basket_time(payload.get("date", ""), payload.get("time", ""))
    b.is_scheduled = 1
    b.schedule_status = "PENDING"
    b.timezone = "IST"
    b.exec_status = ""
    db.commit()
    d = _basket_dict(db, b)
    db.add(LogEntry(message=f"Basket '{b.name}' scheduled for {d['scheduled_at_ist']} IST.",
                    user_id=me.id))
    db.commit()
    return d


@app.post("/api/baskets/{basket_id}/cancel_schedule")
def basket_cancel_schedule(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    baskets.cancel_schedule(db, b.id)
    db.refresh(b)
    return _basket_dict(db, b)


@app.post("/api/baskets/{basket_id}/squareoff")
def basket_squareoff(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    baskets.square_off(b.id)
    return {"ok": True}


@app.post("/api/baskets/{basket_id}/retry")
def basket_retry(basket_id: int, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    b = _owned_basket(db, basket_id, me.id)
    if _daily_halted(db, me.id):
        raise HTTPException(400, "Daily limit hit — trading is halted for today.")
    baskets.retry_failed(b.id)
    return {"dispatching": True}


# ---------- broker info ----------
@app.get("/api/broker")
def get_broker(request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    is_admin = me.role == "SUPER_ADMIN"
    data_provider = uget(db, me.id, "data_provider", "DEMO")
    trade_provider = uget(db, me.id, "trade_provider", "DEMO")
    # Demo is admin-only — never surface it to a normal user (show "no provider").
    if not is_admin:
        if data_provider == "DEMO":
            data_provider = ""
        if trade_provider == "DEMO":
            trade_provider = ""
    data_acc = _account_for_provider(db, data_provider, me.id)
    trade_acc = _account_for_provider(db, trade_provider, me.id)
    data_conn = bool(data_acc) and bool(data_acc.connected) if not is_admin else (data_acc is None or bool(data_acc.connected))
    trade_conn = bool(trade_acc) and bool(trade_acc.connected) if not is_admin else (trade_acc is None or bool(trade_acc.connected))
    base = str(request.base_url).rstrip("/")
    hook = f"{base}/api/webhook/{me.uuid}"
    return {
        "data_provider": data_provider,
        "trade_provider": trade_provider,
        "accounts": [_account_dict(a) for a in db.query(Account)
                     .filter(Account.user_id == me.id).order_by(Account.id).all()],
        "data_connected": data_conn,
        "trade_connected": trade_conn,
        "connected": data_conn and trade_conn,
        "demo_allowed": is_admin,
        "enabled_brokers": ALL_BROKERS if is_admin else _enabled_brokers(db),
        "redirect_url": base + "/api/dhan/callback",
        "postback_url": f"{hook}/dhan",
        "angel_redirect_url": base + "/api/angel/callback",
        "angel_postback_url": f"{hook}/angel",
        "zerodha_redirect_url": base + "/api/zerodha/callback",
        "zerodha_postback_url": f"{hook}/zerodha",
        "aliceblue_postback_url": f"{hook}/aliceblue",
        "static_ip": get_setting(db, "static_ip", ""),
    }


# ---------- per-user webhooks (broker order postbacks; no session needed) ----------
@app.post("/api/webhook/{user_uuid}/{broker}")
async def user_webhook(user_uuid: str, broker: str, request: Request, db: Session = Depends(get_db)):
    u = db.query(User).filter(User.uuid == user_uuid).first()
    if not u:
        return {"ok": False}
    broker = broker.upper()
    bmap = {"DHAN": "DHAN", "ANGEL": "ANGEL", "ZERODHA": "ZERODHA", "ALICEBLUE": "ALICE", "ALICE": "ALICE"}
    bk = bmap.get(broker)
    if not bk:
        return {"ok": False}
    try:
        payload = await request.json()
        orders = payload if isinstance(payload, list) else [payload]
        norm = {"ANGEL": angel.normalize_order, "ZERODHA": zerodha.normalize_order,
                "ALICE": aliceblue.normalize_order}.get(bk)
        rows = [norm(o) for o in orders] if norm else orders
        _sync_external_orders(db, rows, bk, _account_id_for_broker(db, bk, u.id), u.id)
        db.commit()
    except Exception as e:
        db.add(LogEntry(message=f"{bk} webhook error: {e}", level="ERROR", user_id=u.id))
        db.commit()
    return {"ok": True}


def _my_account(db, account_id, uid, broker=None):
    a = db.get(Account, int(account_id or 0))
    if a is None or a.user_id != uid or (broker and a.broker != broker):
        raise HTTPException(400, "Account not found.")
    return a


def _pending_account(db, me):
    """The account a broker OAuth callback is completing — the caller's own, or
    any account when the caller is the Super Admin (admin-assisted setup)."""
    pid = uget(db, me.id, "pending_login_account", "")
    a = db.get(Account, int(pid)) if str(pid).isdigit() else None
    if a and (a.user_id == me.id or me.role == "SUPER_ADMIN"):
        return a
    return None


# ---------- Zerodha (Kite Connect) login ----------
@app.get("/api/zerodha/login")
def zerodha_login(request: Request, account_id: int = 0, db: Session = Depends(get_db)):
    me = current_user(request)
    a = _my_account(db, account_id, me.id, "ZERODHA")
    api_key = _acc_creds(a).get("api_key", "")
    if not api_key:
        raise HTTPException(400, "Enter the Zerodha API Key & Secret first.")
    uset(db, me.id, "pending_login_account", str(a.id))
    db.commit()
    return {"login_url": zerodha.login_url(api_key)}


@app.get("/api/zerodha/callback")
def zerodha_callback(request: Request, request_token: str = "", db: Session = Depends(get_db)):
    me = current_user(request)
    a = _pending_account(db, me)
    if a is None or a.broker != "ZERODHA" or not request_token:
        return RedirectResponse(url="/?login=failed")
    creds = _acc_creds(a)
    if a.client_id and _broker_in_use_elsewhere(db, "ZERODHA", a.client_id, a.user_id):
        db.add(LogEntry(message="Zerodha login blocked: account already linked to another user.",
                        level="ERROR", user_id=a.user_id))
        db.commit()
        return RedirectResponse(url="/?login=failed")
    ok, res = zerodha.exchange_request_token(creds.get("api_key", ""), creds.get("api_secret", ""), request_token)
    if ok:
        _set_acc_creds(a, access_token=res)
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("ZERODHA", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO", user_id=a.user_id))
        _auto_select_provider(db, a.user_id, a.id)
        db.commit()
        return RedirectResponse(url="/?login=ok")
    db.add(LogEntry(message=f"Zerodha login failed: {res}", level="ERROR", user_id=a.user_id))
    db.commit()
    return RedirectResponse(url="/?login=failed")


# ---------- "Login with Dhan" (app consent), per account ----------
@app.get("/api/dhan/login")
def dhan_login(request: Request, account_id: int = 0, db: Session = Depends(get_db)):
    me = current_user(request)
    a = _my_account(db, account_id, me.id, "DHAN")
    creds = _acc_creds(a)
    app_id, app_secret = creds.get("app_id", ""), creds.get("app_secret", "")
    if not app_id or not app_secret or not a.client_id:
        raise HTTPException(400, "Enter App ID, App Secret and Client ID first.")
    try:
        consent = dhan_auth.generate_consent(app_id, app_secret, a.client_id)
        if not consent:
            raise HTTPException(400, "Dhan did not return a consent id. Check your App ID/Secret.")
        uset(db, me.id, "pending_login_account", str(a.id))
        db.commit()
        return {"login_url": dhan_auth.login_url(consent)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not start Dhan login: {e}")


@app.get("/api/dhan/callback")
def dhan_callback(request: Request, tokenId: str = "", db: Session = Depends(get_db)):
    me = current_user(request)
    a = _pending_account(db, me)
    if a is None or not tokenId:
        return RedirectResponse(url="/?login=failed")
    creds = _acc_creds(a)
    try:
        token, client_id, _ = dhan_auth.consume_consent(creds.get("app_id", ""), creds.get("app_secret", ""), tokenId)
        if not token:
            raise RuntimeError("no access token returned")
        if client_id and _broker_in_use_elsewhere(db, "DHAN", client_id, a.user_id):
            raise RuntimeError("this Dhan account is already linked to another user")
        _set_acc_creds(a, access_token=token)
        if client_id:
            a.client_id = client_id
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("DHAN", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO", user_id=a.user_id))
        _auto_select_provider(db, a.user_id, a.id)
        db.commit()
        return RedirectResponse(url="/?login=ok")
    except Exception as e:
        db.add(LogEntry(message=f"Dhan login callback failed: {e}", level="ERROR", user_id=a.user_id))
        db.commit()
        return RedirectResponse(url="/?login=failed")


@app.post("/api/providers")
def set_providers(payload: dict, request: Request, db: Session = Depends(get_db)):
    """Set this user's Data and Trading providers — each 'DEMO' or an account id."""
    me = current_user(request)
    cur_data = uget(db, me.id, "data_provider", "DEMO")
    cur_trade = uget(db, me.id, "trade_provider", "DEMO")
    new_data = str(payload.get("data_provider", cur_data))
    new_trade = str(payload.get("trade_provider", cur_trade))

    def _valid(v):
        if v == "DEMO":
            return True
        a = db.get(Account, int(v)) if v.isdigit() else None
        return a is not None and a.user_id == me.id
    if not _valid(new_data):
        new_data = cur_data
    if not _valid(new_trade):
        new_trade = cur_trade
    # Demo broker is restricted to the Super Admin (anti-abuse / free-trial).
    if me.role != "SUPER_ADMIN" and "DEMO" in (new_data, new_trade):
        raise HTTPException(403, "Demo trading is available to administrators only. "
                                 "Please connect a real broker.")
    if (new_data == "DEMO") != (new_trade == "DEMO"):
        raise HTTPException(400, "Demo can't be mixed with a live broker.")
    active = db.query(Trade).filter(Trade.user_id == me.id,
                                    Trade.status.in_(["OPEN", "PENDING"])).count()
    if active > 0 and (new_data != cur_data or new_trade != cur_trade):
        raise HTTPException(400, f"{active} trade(s) are running — close them before switching providers.")
    changed = (new_data != cur_data or new_trade != cur_trade)
    uset(db, me.id, "data_provider", new_data)
    uset(db, me.id, "trade_provider", new_trade)
    for k in ("broker_balance", "broker_balance_provider", "broker_health", "md_status"):
        uset(db, me.id, k, "")
    if changed:
        da = _account_for_provider(db, new_data, me.id)
        ta = _account_for_provider(db, new_trade, me.id)
        dn = "Demo" if da is None else (da.label or _label(da.broker, da.client_id))
        tn = "Demo" if ta is None else (ta.label or _label(ta.broker, ta.client_id))
        db.add(LogEntry(message=f"Providers switched — Data: {dn}, Trading: {tn}", level="INFO", user_id=me.id))
    db.commit()
    return {"data_provider": new_data, "trade_provider": new_trade}


# ---------- Angel One (per account) ----------
@app.post("/api/angel/login")
def angel_login(payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    a = _my_account(db, payload.get("account_id", 0), me.id, "ANGEL")
    creds = _acc_creds(a)
    cid, pin = a.client_id, creds.get("pin", "")
    key, totp = creds.get("api_key", ""), creds.get("totp_secret", "")
    if not all([cid, pin, key, totp]):
        raise HTTPException(400, "Enter Angel Client ID, PIN, API Key and TOTP secret first.")
    if _broker_in_use_elsewhere(db, "ANGEL", cid, me.id):
        raise HTTPException(400, "This Angel account is already linked to another user.")
    ok, data = angel.login(cid, pin, key, totp)
    if ok:
        _set_acc_creds(a, jwt=data["jwt"], refresh=data.get("refresh", ""), feed=data.get("feed", ""))
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("ANGEL", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO", user_id=me.id))
        _auto_select_provider(db, me.id, a.id)
        db.commit()
        return {"connected": True}
    db.add(LogEntry(message=f"Angel One login failed: {data}", level="ERROR", user_id=me.id))
    db.commit()
    raise HTTPException(400, f"Angel login failed: {data}")


# ---------- Alice Blue (ANT API, per account) ----------
@app.post("/api/aliceblue/login")
def aliceblue_login(payload: dict, request: Request, db: Session = Depends(get_db)):
    me = current_user(request)
    a = _my_account(db, payload.get("account_id", 0), me.id, "ALICE")
    creds = _acc_creds(a)
    cid, key = a.client_id, creds.get("api_key", "")
    if not cid or not key:
        raise HTTPException(400, "Enter Alice Blue User ID and API Key first.")
    if _broker_in_use_elsewhere(db, "ALICE", cid, me.id):
        raise HTTPException(400, "This Alice Blue account is already linked to another user.")
    ok, res = aliceblue.login(cid, key)
    if ok:
        _set_acc_creds(a, session_id=res)
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("ALICE", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO", user_id=me.id))
        _auto_select_provider(db, me.id, a.id)
        db.commit()
        return {"connected": True}
    db.add(LogEntry(message=f"Alice Blue login failed: {res}", level="ERROR", user_id=me.id))
    db.commit()
    raise HTTPException(400, f"Alice Blue login failed: {res}")


# ---------- how-to doc (admin-editable rich HTML, shown to all users) ----------
DEFAULT_HOWTO = (
    "<h3>Dhan</h3><p>Create an app at <b>web.dhan.co</b>, set the Redirect &amp; Postback "
    "URLs shown on the Broker tab, add the account (Client ID, App ID, App Secret), then "
    "click <b>Login</b>. Token lasts 24h and auto-renews.</p>"
    "<h3>Angel One</h3><p>Create a <b>SmartAPI</b> app, enable TOTP, add the account "
    "(Client ID, API Key, PIN, TOTP secret), then click <b>Login</b> (no redirect).</p>"
    "<h3>Zerodha</h3><p>Create a <b>Kite Connect</b> app, set the Redirect URL, add the "
    "account (Client ID, API Key, API Secret), then click <b>Login</b>.</p>"
    "<h3>Alice Blue</h3><p>Enable the <b>ANT API</b>, add the account (User ID, API Key), "
    "then click <b>Login</b> — connects instantly, no redirect.</p>"
)


@app.get("/api/howto")
def get_howto(db: Session = Depends(get_db)):
    html = get_setting(db, "howto_md", "") or DEFAULT_HOWTO
    return {"html": html, "markdown": html}


# ---------- Super Admin control panel ----------
def _user_dict(db, u):
    return {
        "id": u.id, "email": u.email, "role": u.role, "status": u.status, "uuid": u.uuid,
        "plan_name": u.plan_name,
        "plan_expiry": u.plan_expiry.isoformat() if u.plan_expiry else None,
        "expired": u.role != "SUPER_ADMIN" and _plan_expired(u),
        "accounts": db.query(Account).filter(Account.user_id == u.id, Account.connected == 1).count(),
        "created_at": u.created_at.isoformat() if u.created_at else None,
        "online": bool(u.session_token),
    }


@app.get("/api/admin/users")
def admin_users(request: Request, q: str = "", db: Session = Depends(get_db)):
    require_admin(request)
    query = db.query(User)
    if q:
        query = query.filter(User.email.ilike(f"%{q.strip()}%"))
    return [_user_dict(db, u) for u in query.order_by(User.id).all()]


@app.post("/api/admin/users")
def admin_create_user(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    email = str(payload.get("email", "")).strip().lower()
    days = int(payload.get("days", 30) or 30)
    if "@" not in email:
        raise HTTPException(400, "Enter a valid email.")
    if db.query(User).filter(User.email == email).first():
        raise HTTPException(400, "User already exists.")
    u = User(email=email, password_hash="", role="USER", plan_name=f"{days} Days",
             plan_expiry=dt.datetime.utcnow() + dt.timedelta(days=days))
    db.add(u); db.commit(); db.refresh(u)
    # Welcome email + a set-password (reset) code so the user can activate.
    emailer.send_email(db, email, "welcome", email=email, plan=u.plan_name,
                       expiry=u.plan_expiry.strftime("%d %b %Y"))
    code = auth.issue_otp(db, email, "RESET")
    ok, _ = emailer.send_email(db, email, "otp_reset", code=code, email=email)
    return {"ok": True, "id": u.id, "set_password_code": None if ok else code}


@app.get("/api/admin/logs")
def admin_all_logs(request: Request, date: str = "", q: str = "", level: str = "",
                   page: int = 1, db: Session = Depends(get_db)):
    """Day-wise logs across ALL users (with the owner's email)."""
    require_admin(request)
    import math
    per = 50
    query = db.query(LogEntry)
    if date:
        query = query.filter(LogEntry.day == date)
    if level:
        query = query.filter(LogEntry.level == level)
    uid_filter = None
    if q:
        ids = [u.id for u in db.query(User).filter(User.email.ilike(f"%{q.strip()}%")).all()]
        query = query.filter(LogEntry.user_id.in_(ids or [-1]))
    total = query.count()
    rows = query.order_by(LogEntry.id.desc()).offset((max(1, page) - 1) * per).limit(per).all()
    emap = {u.id: u.email for u in db.query(User).all()}
    days = [d[0] for d in db.query(LogEntry.day).distinct().order_by(LogEntry.day.desc()).all() if d[0]]
    return {"logs": [{"time": r.created_at.isoformat(), "level": r.level, "day": r.day,
                      "email": emap.get(r.user_id, "system"), "message": r.message} for r in rows],
            "page": page, "pages": max(1, math.ceil(total / per)), "days": days}


@app.post("/api/admin/users/{uid}/status")
def admin_set_status(uid: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    block = bool(payload.get("blocked"))
    u.status = "BLOCKED" if block else "ACTIVE"
    if block:
        u.session_token = ""        # kill their session immediately
        uset(db, u.id, "kill_switch", "on")   # halt their algos / flatten positions
    db.commit()
    return _user_dict(db, u)


@app.post("/api/admin/users/{uid}/plan")
def admin_set_plan(uid: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    days = int(payload.get("days", 0) or 0)
    name = str(payload.get("plan_name", "") or f"{days} Days")
    base = dt.datetime.utcnow()
    if bool(payload.get("extend")) and u.plan_expiry and u.plan_expiry > base:
        base = u.plan_expiry           # extend from current expiry
    u.plan_expiry = base + dt.timedelta(days=days)
    u.plan_name = name
    db.commit()
    return _user_dict(db, u)


@app.delete("/api/admin/users/{uid}")
def admin_delete_user(uid: int, request: Request, db: Session = Depends(get_db)):
    me = require_admin(request)
    if uid == me.id:
        raise HTTPException(400, "You can't delete your own admin account.")
    u = db.get(User, uid)
    if u:
        for M in (Trade, Account, SymbolPreset, Watchlist, LogEntry, UserSetting):
            db.query(M).filter(M.user_id == uid).delete()
        db.delete(u)
        db.commit()
    return {"ok": True}


@app.post("/api/admin/users/{uid}/kill")
def admin_kill(uid: int, request: Request, db: Session = Depends(get_db)):
    """Remote kill switch: halt the user's algos + flatten open positions."""
    require_admin(request)
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    uset(db, u.id, "kill_switch", "on")
    db.add(LogEntry(message="ADMIN remote kill switch activated.", level="WARN", user_id=u.id))
    db.commit()
    return {"ok": True}


@app.get("/api/admin/users/{uid}/summary")
def admin_user_summary(uid: int, request: Request, broker: str = "ALL", date: str = "",
                       db: Session = Depends(get_db)):
    require_admin(request)
    return _build_summary(db, uid, broker, date)


@app.get("/api/admin/users/{uid}/logs")
def admin_user_logs(uid: int, request: Request, page: int = 1, db: Session = Depends(get_db)):
    require_admin(request)
    import math
    per = 30
    q = db.query(LogEntry).filter(LogEntry.user_id == uid)
    total = q.count()
    rows = q.order_by(LogEntry.id.desc()).offset((max(1, page) - 1) * per).limit(per).all()
    return {"logs": [{"id": r.id, "time": r.created_at.isoformat(), "level": r.level,
                      "message": r.message, "day": r.day} for r in rows],
            "page": page, "pages": max(1, math.ceil(total / per))}


@app.get("/api/admin/users/{uid}/trades")
def admin_user_trades(uid: int, request: Request, date: str = "", db: Session = Depends(get_db)):
    """Full trade history / P&L breakdown of a user (admin audit)."""
    require_admin(request)
    q = db.query(Trade).filter(Trade.user_id == uid)
    if date:
        b = _ist_day_bounds(date)
        if b:
            q = q.filter(Trade.created_at >= b[0], Trade.created_at < b[1])
    rows = q.order_by(Trade.id.desc()).limit(300).all()
    return [{"id": t.id, "symbol": t.symbol, "side": t.side, "qty": t.quantity,
             "mode": t.mode, "broker": t.broker, "status": t.status,
             "entry": t.entry_fill_price or t.entry_price, "ltp": t.last_price,
             "pnl": t.pnl, "exit_reason": t.exit_reason,
             "time": t.created_at.isoformat() if t.created_at else None} for t in rows]


@app.get("/api/admin/users/{uid}/accounts")
def admin_user_accounts(uid: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return [_account_dict(a) for a in db.query(Account).filter(Account.user_id == uid).all()]


@app.post("/api/admin/users/{uid}/accounts")
def admin_add_account(uid: int, payload: dict, request: Request, db: Session = Depends(get_db)):
    """Broker override: admin adds OR edits a broker account in a user's profile,
    with the full credential set (same as the user's own Broker tab)."""
    require_admin(request)
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    broker = str(payload.get("broker", "DHAN")).upper()
    if broker not in ("DHAN", "ANGEL", "ZERODHA", "ALICE"):
        broker = "DHAN"
    client_id = str(payload.get("client_id", "")).strip()
    if client_id and _broker_in_use_elsewhere(db, broker, client_id, uid):
        raise HTTPException(400, "That broker Client ID is already linked to another user.")
    aid = payload.get("id")
    a = db.get(Account, int(aid)) if aid else None
    if a is not None and a.user_id != uid:
        raise HTTPException(403, "Not this user's account.")
    is_new = a is None
    if a is None:
        a = Account(user_id=uid, broker=broker)
        db.add(a)
    a.broker = broker
    if client_id:
        a.client_id = client_id
    _set_acc_creds(a, app_id=payload.get("app_id"), app_secret=payload.get("app_secret"),
                   api_key=payload.get("api_key"), api_secret=payload.get("api_secret"),
                   totp_secret=payload.get("totp_secret"), pin=payload.get("pin"))
    a.label = _label(broker, a.client_id)
    db.add(LogEntry(message=f"ADMIN {'added' if is_new else 'updated'} a {broker} broker account.",
                    level="WARN", user_id=uid))
    db.commit(); db.refresh(a)
    return _account_dict(a)


@app.get("/api/admin/users/{uid}/accounts/{aid}/login")
def admin_account_login(uid: int, aid: int, request: Request, db: Session = Depends(get_db)):
    """Authenticate a user's broker ON THEIR BEHALF. Angel/Alice log in
    programmatically; Dhan/Zerodha return an OAuth URL the admin opens & approves."""
    me = require_admin(request)
    a = db.get(Account, aid)
    if not a or a.user_id != uid:
        raise HTTPException(404, "Account not found")
    creds = _acc_creds(a)
    if a.client_id and _broker_in_use_elsewhere(db, a.broker, a.client_id, uid):
        raise HTTPException(400, "This broker Client ID is already linked to another user.")
    if a.broker == "ANGEL":
        if not all([a.client_id, creds.get("pin"), creds.get("api_key"), creds.get("totp_secret")]):
            raise HTTPException(400, "Fill Client ID, PIN, API Key and TOTP secret first.")
        ok, data = angel.login(a.client_id, creds["pin"], creds["api_key"], creds["totp_secret"])
        if not ok:
            raise HTTPException(400, f"Angel login failed: {data}")
        _set_acc_creds(a, jwt=data["jwt"], refresh=data.get("refresh", ""), feed=data.get("feed", ""))
    elif a.broker == "ALICE":
        if not a.client_id or not creds.get("api_key"):
            raise HTTPException(400, "Fill User ID and API Key first.")
        ok, res = aliceblue.login(a.client_id, creds["api_key"])
        if not ok:
            raise HTTPException(400, f"Alice Blue login failed: {res}")
        _set_acc_creds(a, session_id=res)
    elif a.broker == "ZERODHA":
        if not creds.get("api_key"):
            raise HTTPException(400, "Enter the Zerodha API Key & Secret first.")
        uset(db, me.id, "pending_login_account", str(a.id)); db.commit()
        return {"login_url": zerodha.login_url(creds["api_key"])}
    elif a.broker == "DHAN":
        if not all([creds.get("app_id"), creds.get("app_secret"), a.client_id]):
            raise HTTPException(400, "Enter App ID, App Secret and Client ID first.")
        try:
            consent = dhan_auth.generate_consent(creds["app_id"], creds["app_secret"], a.client_id)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(400, f"Could not start Dhan login: {e}")
        if not consent:
            raise HTTPException(400, "Dhan did not return a consent id. Check App ID/Secret.")
        uset(db, me.id, "pending_login_account", str(a.id)); db.commit()
        return {"login_url": dhan_auth.login_url(consent)}
    a.connected = 1
    a.token_time = dt.datetime.utcnow()
    a.label = _label(a.broker, a.client_id)
    db.add(LogEntry(message=f"ADMIN logged in {a.label} on the user's behalf.", level="WARN", user_id=uid))
    _auto_select_provider(db, uid, a.id)
    db.commit()
    return {"connected": True}


@app.delete("/api/admin/users/{uid}/accounts/{aid}")
def admin_remove_account(uid: int, aid: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    a = db.get(Account, aid)
    if a and a.user_id == uid:
        db.add(LogEntry(message=f"ADMIN removed a {a.broker} broker account.", level="WARN", user_id=uid))
        db.delete(a); db.commit()
    return {"ok": True}


# ---- global SaaS settings + plans + email ----
@app.get("/api/admin/settings")
def admin_get_settings(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return {
        "registration_open": get_setting(db, "registration_open", "yes") == "yes",
        "trial_days": int(get_setting(db, "trial_days", str(config.DEFAULT_TRIAL_DAYS)) or 7),
        "enabled_brokers": _enabled_brokers(db),
        "all_brokers": ALL_BROKERS,
        "smtp_host": get_setting(db, "smtp_host", config.SMTP_HOST),
        "smtp_port": int(get_setting(db, "smtp_port", str(config.SMTP_PORT)) or 587),
        "smtp_user": get_setting(db, "smtp_user", config.SMTP_USER),
        "smtp_pass": "********" if get_setting(db, "smtp_pass", config.SMTP_PASS) else "",
        "smtp_sender": get_setting(db, "smtp_sender", config.SMTP_SENDER),
        "smtp_from": get_setting(db, "smtp_from", config.SMTP_FROM),
        "smtp_bcc": get_setting(db, "smtp_bcc", config.SMTP_BCC),
        "howto_md": get_setting(db, "howto_md", "") or DEFAULT_HOWTO,
    }


@app.post("/api/admin/settings")
def admin_set_settings(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    if "registration_open" in payload:
        set_setting(db, "registration_open", "yes" if payload["registration_open"] else "no")
    if "trial_days" in payload:
        set_setting(db, "trial_days", str(int(payload["trial_days"])))
    if "enabled_brokers" in payload:
        sel = [b.upper() for b in (payload["enabled_brokers"] or []) if b.upper() in ALL_BROKERS]
        set_setting(db, "enabled_brokers", ",".join(sel))   # empty -> all (default)
    for k in ("smtp_host", "smtp_user", "smtp_sender", "smtp_from", "smtp_bcc", "howto_md"):
        if k in payload:
            set_setting(db, k, str(payload[k]))
    if "smtp_port" in payload:
        set_setting(db, "smtp_port", str(int(payload["smtp_port"] or 587)))
    if payload.get("smtp_pass") and payload["smtp_pass"] != "********":
        set_setting(db, "smtp_pass", str(payload["smtp_pass"]))
    return admin_get_settings(request, db)


@app.post("/api/admin/test_email")
def admin_test_email(payload: dict, request: Request, db: Session = Depends(get_db)):
    me = require_admin(request)
    to = str(payload.get("to", "") or me.email)
    ok, msg = emailer.send_email(db, to, "welcome", email=to, plan="Test", expiry="—")
    return {"ok": ok, "message": msg}


@app.get("/api/admin/plans")
def admin_plans(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return [{"id": p.id, "name": p.name, "days": p.days, "price": p.price, "active": bool(p.active)}
            for p in db.query(Plan).order_by(Plan.days).all()]


@app.post("/api/admin/plans")
def admin_save_plan(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    pid = payload.get("id")
    p = db.get(Plan, int(pid)) if pid else None
    if p is None:
        p = Plan()
        db.add(p)
    p.name = str(payload.get("name", "") or f"{payload.get('days', 30)} Days")
    p.days = int(payload.get("days", 30) or 30)
    p.price = float(payload.get("price", 0) or 0)
    p.active = 1 if payload.get("active", True) else 0
    db.commit(); db.refresh(p)
    return {"id": p.id}


@app.delete("/api/admin/plans/{pid}")
def admin_delete_plan(pid: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    p = db.get(Plan, pid)
    if p:
        db.delete(p); db.commit()
    return {"ok": True}


@app.get("/api/plans")
def public_plans(db: Session = Depends(get_db)):
    return [{"name": p.name, "days": p.days, "price": p.price}
            for p in db.query(Plan).filter(Plan.active == 1).order_by(Plan.days).all()]


@app.get("/api/admin/templates")
def admin_templates(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    out = {}
    for key, (subj, body) in emailer.DEFAULT_TEMPLATES.items():
        row = db.get(EmailTemplate, key)
        out[key] = {"subject": (row.subject if row and row.subject else subj),
                    "body_html": (row.body_html if row and row.body_html else body)}
    return out


@app.post("/api/admin/templates")
def admin_save_template(payload: dict, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    key = str(payload.get("key", ""))
    if key not in emailer.DEFAULT_TEMPLATES:
        raise HTTPException(400, "Unknown template")
    row = db.get(EmailTemplate, key)
    if row is None:
        row = EmailTemplate(key=key)
        db.add(row)
    row.subject = str(payload.get("subject", ""))
    row.body_html = str(payload.get("body_html", ""))
    db.commit()
    return {"ok": True}


# ---------- frontend ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Serve the rest of the frontend (app.js, styles.css).
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)
