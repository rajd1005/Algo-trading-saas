"""
FastAPI application: the web server + REST API + serves the dashboard.

Run it with:   uvicorn main:app --host 0.0.0.0 --port 8000
(or just:      python main.py )
"""
import os
import json
import time
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
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import dhan_auth
import auth
import angel
import zerodha
import aliceblue

import config
from database import init_db, get_db, SessionLocal
from models import Trade, LogEntry, Setting, Account, SymbolPreset, Watchlist
from schemas import (TradeCreate, TradeOut, BrokerConfigIn, SettingsIn, ModifyIn,
                     SymbolPresetIn, WatchlistIn)
from engine import engine, level_price
from instruments import store as instruments
from market_data import DhanMarketData, demo_market
from brokers import verify_dhan_credentials, DhanBroker

app = FastAPI(title="Algo Trading SaaS (India)")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


_OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico",
               "/api/dhan/postback", "/api/dhan/callback", "/api/angel/postback",
               "/api/zerodha/callback", "/api/zerodha/postback",
               "/api/aliceblue/postback"}


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Gate the whole site behind the dashboard password (if one is set)."""
    if not auth.auth_required():
        return await call_next(request)
    path = request.url.path
    if path in _OPEN_PATHS or path.startswith("/static"):
        return await call_next(request)
    if auth.token_ok(request.cookies.get("session", "")):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "Login required"}, status_code=401)
    return RedirectResponse(url="/login")


@app.get("/login")
def login_page():
    return FileResponse(os.path.join(FRONTEND_DIR, "login.html"))


@app.post("/api/login")
def login(payload: dict):
    if auth.password_ok(payload.get("password", "")):
        resp = JSONResponse({"ok": True})
        resp.set_cookie("session", auth.make_token(), httponly=True, samesite="lax",
                        max_age=7 * 86400)
        return resp
    raise HTTPException(401, "Wrong password")


@app.post("/api/logout")
def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("session")
    return resp


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


def _daily_halted(db) -> bool:
    """True when the daily target/drawdown halt has tripped for today."""
    return (get_setting(db, "daily_halt", "off") == "on"
            and get_setting(db, "daily_halt_date", "") == _ist_today())


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


def _account_for_provider(db, provider_value):
    """provider_value is 'DEMO' or an account id (string). Returns Account or None."""
    if not provider_value or provider_value == "DEMO":
        return None
    try:
        return db.get(Account, int(provider_value))
    except Exception:
        return None


def _account_id_for_broker(db, broker):
    """Pick the account id to attribute an external order to (prefer the active
    trading account of that broker, else the first such account)."""
    tp = _account_for_provider(db, get_setting(db, "trade_provider", "DEMO"))
    if tp and tp.broker == broker:
        return tp.id
    a = db.query(Account).filter(Account.broker == broker).first()
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


@app.get("/api/accounts")
def list_accounts(db: Session = Depends(get_db)):
    return [_account_dict(a) for a in db.query(Account).order_by(Account.id).all()]


@app.post("/api/accounts")
def save_account(payload: dict, db: Session = Depends(get_db)):
    """Add or update a broker account."""
    broker = str(payload.get("broker", "")).upper()
    if broker not in ("DHAN", "ANGEL", "ZERODHA", "ALICE"):
        broker = "DHAN"
    aid = payload.get("id")
    a = db.get(Account, int(aid)) if aid else None
    if a is None:
        a = Account(broker=broker)
        db.add(a)
    if payload.get("client_id"):
        a.client_id = str(payload["client_id"]).strip()
    a.broker = broker
    # store secrets (only overwrite when provided)
    _set_acc_creds(a, app_id=payload.get("app_id"), app_secret=payload.get("app_secret"),
                   api_key=payload.get("api_key"), api_secret=payload.get("api_secret"),
                   totp_secret=payload.get("totp_secret"), pin=payload.get("pin"))
    a.label = _label(a.broker, a.client_id)
    db.commit()
    db.refresh(a)
    return _account_dict(a)


@app.delete("/api/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    a = db.get(Account, account_id)
    if a:
        # if a provider points at it, fall back to Demo
        for key in ("data_provider", "trade_provider"):
            if get_setting(db, key, "DEMO") == str(account_id):
                set_setting(db, key, "DEMO")
        db.delete(a)
        db.commit()
    return {"ok": True}


# ---------- startup ----------
@app.on_event("startup")
def _startup():
    init_db()
    # sensible defaults on first run
    db = SessionLocal()
    if db.get(Setting, "kill_switch") is None:
        set_setting(db, "kill_switch", "off")
    if db.get(Setting, "default_mode") is None:
        set_setting(db, "default_mode", "TEST")
    if db.get(Setting, "broker_mode") is None:
        set_setting(db, "broker_mode", "DEMO")   # legacy; kept for back-compat
    # Data/Trading providers (Demo / Dhan / Angel). Migrate from broker_mode.
    if db.get(Setting, "data_provider") is None or db.get(Setting, "trade_provider") is None:
        legacy = get_setting(db, "broker_mode", "DEMO")
        prov = "DHAN" if legacy == "DHAN" else "DEMO"
        set_setting(db, "data_provider", prov)
        set_setting(db, "trade_provider", prov)
    _migrate_accounts(db)
    db.close()
    instruments.load_async()   # download Dhan's symbol list in the background
    angel.mapper.load_async()  # download Angel One master + build the symbol map
    zerodha.mapper.load_async()  # download Zerodha (Kite) master + symbol map
    aliceblue.mapper.load_async()  # download Alice Blue contract masters + symbol map
    engine.start()
    threading.Thread(target=_auto_renew_loop, daemon=True).start()
    threading.Thread(target=_broker_monitor_loop, daemon=True).start()
    threading.Thread(target=_fetch_static_ip, daemon=True).start()


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
    """Live broker: refresh balance + connection health, and sync any positions
    placed externally on the broker terminal into our 'Live Trade' list."""
    while True:
        time.sleep(12)
        try:
            db = SessionLocal()
            acc = _account_for_provider(db, get_setting(db, "trade_provider", "DEMO"))
            b = _broker_from_account(acc)
            if b is not None:
                ok, bal = b.fund_limit()
                if ok:
                    set_setting(db, "broker_balance", str(bal))
                    set_setting(db, "broker_balance_provider", str(acc.id))
                    set_setting(db, "broker_health", "ok")
                    set_setting(db, "broker_health_time", dt.datetime.utcnow().isoformat())
                else:
                    set_setting(db, "broker_health", "error")
                try:
                    _sync_external_orders(db, b.get_orders(), acc.broker, acc.id)
                except Exception:
                    pass
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


def _sync_external_orders(db, orders, broker="DHAN", account_id=0):
    """Mirror the broker's ENTIRE order book into our system — every order placed
    on the broker terminal (filled, pending, rejected, cancelled) shows up here,
    and its status is kept up to date."""
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

        existing = db.query(Trade).filter(Trade.broker_order_id == oid).first()
        if existing:
            # Keep externally-synced orders in step with the broker.
            if existing.source == "EXTERNAL" and existing.status not in ("CLOSED",) \
                    and existing.status != mapped:
                old = existing.status
                existing.status = mapped
                if mapped == "OPEN" and not existing.entry_fill_price:
                    existing.entry_fill_price = avg or price
                if mapped == "REJECTED":
                    existing.exit_reason = "REJECTED"
                db.add(LogEntry(message=f"External order {existing.symbol} status: {old} -> {mapped}"
                                        + (f". Reason: {reason}" if reason else ""),
                                level="ERROR" if mapped == "REJECTED" else "INFO"))
            continue

        # New external order we've not seen before.
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
                  broker=broker, account_id=account_id, source="EXTERNAL", broker_order_id=oid,
                  exit_reason="REJECTED" if mapped == "REJECTED" else "")
        db.add(t)
        db.add(LogEntry(message=f"Synced external order: {sym} {side} x{qty} [{raw}]"
                                + (f". Reason: {reason}" if reason else ""),
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
@app.get("/api/trades", response_model=list[TradeOut])
def list_trades(db: Session = Depends(get_db)):
    return db.query(Trade).order_by(Trade.id.desc()).all()


@app.post("/api/trades", response_model=TradeOut)
def create_trade(payload: TradeCreate, db: Session = Depends(get_db)):
    # Safety: block creating LIVE trades while the kill switch is on.
    if payload.mode == "LIVE" and get_setting(db, "kill_switch", "off") == "on":
        raise HTTPException(400, "Kill switch is ON. Turn it off to place LIVE trades.")
    # Block new orders once the daily limit halt has tripped for today.
    if _daily_halted(db):
        raise HTTPException(400, "Daily limit hit — trading is halted for today. "
                                 + (get_setting(db, "daily_halt_reason", "") or ""))
    # A real symbol must be picked (we need its Security ID to fetch the LTP).
    if not payload.security_id:
        raise HTTPException(400, "Please search and select a symbol from the list "
                                 "(so we know its Security ID for live prices).")
    data = payload.model_dump()
    raw_targets = data.pop("targets", [])
    targets = [{"points": float(x["points"]), "qty": int(x["qty"]), "hit": False}
               for x in raw_targets if float(x.get("points", 0)) > 0 and int(x.get("qty", 0)) > 0]
    t = Trade(**data)
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
    tp = _account_for_provider(db, get_setting(db, "trade_provider", "DEMO"))
    broker_txt = "Demo/Paper" if (t.mode == "TEST" or tp is None) else (tp.label or _label(tp.broker, tp.client_id))
    entry_txt = {"MARKET": "market", "LIMIT": f"limit @{t.entry_price}",
                 "SCHEDULED": f"scheduled {t.scheduled_time} IST",
                 "TRIGGER": f"trigger {t.trigger_dir or 'auto'} @{t.trigger_price}"}.get(t.entry_type, t.entry_type)
    db.add(LogEntry(message=f"Trade #{t.id} created: {t.side} {t.symbol} x{t.quantity} "
                            f"[{t.mode}] entry={entry_txt} via {broker_txt}", trade_id=t.id))
    db.commit()
    return t


@app.post("/api/trades/{trade_id}/modify", response_model=TradeOut)
def modify_trade(trade_id: int, payload: ModifyIn, db: Session = Depends(get_db)):
    """Change stop-loss / targets on an OPEN trade, using DIRECT prices.
    Supports editing or adding multiple (scale-out) targets here too."""
    t = db.get(Trade, trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
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
    db.add(LogEntry(message=msg, level="INFO", trade_id=t.id))
    db.commit()
    db.refresh(t)
    return t


@app.post("/api/trades/{trade_id}/close", response_model=TradeOut)
def close_trade(trade_id: int, db: Session = Depends(get_db)):
    """Manually exit an OPEN trade at the current price."""
    t = db.get(Trade, trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
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
    db.add(LogEntry(message=f"Manual close {t.symbol} x{remaining} @ {price} P&L={t.pnl}", trade_id=t.id))
    db.commit()
    db.refresh(t)
    return t


@app.post("/api/trades/{trade_id}/cancel", response_model=TradeOut)
def cancel_trade(trade_id: int, db: Session = Depends(get_db)):
    t = db.get(Trade, trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
    if t.status != "PENDING":
        raise HTTPException(400, "Only PENDING trades can be cancelled.")
    t.status = "CANCELLED"
    t.exit_reason = "MANUAL"
    db.add(LogEntry(message=f"Trade #{t.id} cancelled (was pending): {t.symbol}", trade_id=t.id))
    db.commit()
    db.refresh(t)
    return t


@app.delete("/api/trades/{trade_id}")
def delete_trade(trade_id: int, db: Session = Depends(get_db)):
    t = db.get(Trade, trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
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
def ltp(payload: dict, db: Session = Depends(get_db)):
    """Fetch live LTP for a list of instruments (used to fill the option chain)."""
    items = payload.get("items", [])
    by_seg = {}
    for it in items:
        seg = it.get("exchange_segment")
        sid = str(it.get("security_id"))
        if seg and sid:
            by_seg.setdefault(seg, []).append(sid)

    acc = _account_for_provider(db, get_setting(db, "data_provider", "DEMO"))
    if acc is None:     # DEMO
        res = demo_market.get_ltp_batch(by_seg)
        return {"connected": True, "prices": {sid: px for (seg, sid), px in res.items()}}
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
    res = md.get_ltp_batch(by_seg)
    return {"connected": True, "prices": {sid: px for (seg, sid), px in res.items()},
            "error": md.last_error}


# ---------- demo controls (push price up/down, reset) ----------
@app.post("/api/demo/direction")
def demo_direction(payload: dict):
    d = str(payload.get("direction", "FLAT")).upper()
    demo_market.set_direction(1 if d == "UP" else (-1 if d == "DOWN" else 0))
    return {"direction": demo_market.direction}


@app.post("/api/demo/reset")
def demo_reset(db: Session = Depends(get_db)):
    demo_market.reset()
    db.add(LogEntry(message="Demo prices reset", level="INFO"))
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


@app.get("/api/summary")
def summary(broker: str = "ALL", db: Session = Depends(get_db)):
    trades = db.query(Trade).all()
    # Group P&L per ACCOUNT (0 = Demo/paper) — accounts are not merged by broker.
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
    for a in db.query(Account).order_by(Account.id).all():
        breakdown.append({"key": str(a.id), "label": a.label or _label(a.broker, a.client_id),
                          "net": _metrics(groups.get(a.id, []))["net"]})
    active = sum(1 for t in trades if t.status in ("OPEN", "PENDING"))

    data_provider = get_setting(db, "data_provider", "DEMO")
    trade_provider = get_setting(db, "trade_provider", "DEMO")
    data_acc = _account_for_provider(db, data_provider)
    trade_acc = _account_for_provider(db, trade_provider)
    data_name = "Demo" if data_acc is None else (data_acc.label or _label(data_acc.broker, data_acc.client_id))
    broker_name = "Demo" if trade_acc is None else (trade_acc.label or _label(trade_acc.broker, trade_acc.client_id))
    trade_connected = trade_acc is not None and bool(trade_acc.connected)
    # Show the balance ONLY for the connected, currently-selected trading account.
    balance = None
    if trade_acc is not None and trade_connected \
            and get_setting(db, "broker_balance_provider", "") == str(trade_acc.id):
        bal = get_setting(db, "broker_balance", "")
        balance = float(bal) if bal else None

    # Connection-lost warning while trades are running.
    broker_alert, alert_msg = False, ""
    if trade_acc is not None and active > 0:
        if not trade_connected:
            broker_alert = True
            alert_msg = f"{broker_name} is NOT connected but trades are running — reconnect on the Broker tab."
        elif get_setting(db, "broker_health", "") == "error":
            broker_alert = True
            alert_msg = f"Lost connection to {broker_name} — exits may not fire. Check the Broker tab."

    return {
        "total_trades": len(trades),
        "pending": pnl["pending"], "open": pnl["open"], "closed": pnl["closed"],
        "open_pnl": pnl["open_pnl"], "closed_pnl": pnl["closed_pnl"], "total_pnl": pnl["net"],
        "pnl": pnl, "pnl_filter": flt, "pnl_breakdown": breakdown,
        "kill_switch": get_setting(db, "kill_switch", "off"),
        "md_status": get_setting(db, "md_status", ""),
        "instruments": instruments.status(),
        "angel_map": angel.mapper.status(),
        "zerodha_map": zerodha.mapper.status(),
        "alice_map": aliceblue.mapper.status(),
        "demo_direction": demo_market.direction,
        "data_provider": data_provider, "trade_provider": trade_provider,
        "data_name": data_name, "broker_name": broker_name, "balance": balance,
        "broker_alert": broker_alert, "alert_msg": alert_msg,
        "active_locked": active > 0,     # broker switch is locked while trades run
        "daily_halt": _daily_halted(db),
        "daily_halt_reason": get_setting(db, "daily_halt_reason", ""),
    }


# ---------- logs ----------
@app.get("/api/logs")
def list_logs(date: str = "", level: str = "", page: int = 1, per_page: int = 25,
              db: Session = Depends(get_db)):
    import math
    q = db.query(LogEntry)
    if date:
        q = q.filter(LogEntry.day == date)
    if level:
        q = q.filter(LogEntry.level == level)
    total = q.count()
    page = max(1, page)
    per_page = min(max(per_page, 1), 200)
    rows = (q.order_by(LogEntry.id.desc())
            .offset((page - 1) * per_page).limit(per_page).all())
    days = [d[0] for d in db.query(LogEntry.day).distinct()
            .order_by(LogEntry.day.desc()).all() if d[0]]
    return {
        "logs": [{"id": r.id, "time": r.created_at.isoformat(), "level": r.level,
                  "trade_id": r.trade_id, "message": r.message, "day": r.day} for r in rows],
        "page": page, "per_page": per_page, "total": total,
        "pages": max(1, math.ceil(total / per_page)), "days": days,
    }


# ---------- settings (kill switch etc.) ----------
def _num_setting(db, key):
    try:
        v = get_setting(db, key, "")
        return float(v) if v not in ("", None) else 0.0
    except Exception:
        return 0.0


@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    return {
        "kill_switch": get_setting(db, "kill_switch", "off"),
        "default_mode": get_setting(db, "default_mode", "TEST"),
        "daily_max_profit": _num_setting(db, "daily_max_profit"),
        "daily_max_loss": _num_setting(db, "daily_max_loss"),
        "global_lock_step": _num_setting(db, "global_lock_step"),
        "global_lock_amount": _num_setting(db, "global_lock_amount"),
        "daily_halt": _daily_halted(db),
        "daily_halt_reason": get_setting(db, "daily_halt_reason", ""),
    }


@app.post("/api/settings")
def update_settings(payload: SettingsIn, db: Session = Depends(get_db)):
    changed = []
    if payload.kill_switch is not None:
        set_setting(db, "kill_switch", "on" if payload.kill_switch else "off")
        db.add(LogEntry(message=f"Kill switch set to "
                                f"{'ON' if payload.kill_switch else 'OFF'}", level="WARN"))
        db.commit()
    if payload.default_mode in ("TEST", "LIVE"):
        set_setting(db, "default_mode", payload.default_mode)
    if payload.daily_max_profit is not None:
        set_setting(db, "daily_max_profit", str(max(0.0, float(payload.daily_max_profit))))
        changed.append(f"daily max profit ₹{max(0.0, float(payload.daily_max_profit)):.0f}")
    if payload.daily_max_loss is not None:
        set_setting(db, "daily_max_loss", str(max(0.0, float(payload.daily_max_loss))))
        changed.append(f"daily max loss ₹{max(0.0, float(payload.daily_max_loss)):.0f}")
    if payload.global_lock_step is not None:
        set_setting(db, "global_lock_step", str(max(0.0, float(payload.global_lock_step))))
    if payload.global_lock_amount is not None:
        set_setting(db, "global_lock_amount", str(max(0.0, float(payload.global_lock_amount))))
    if payload.global_lock_step is not None or payload.global_lock_amount is not None:
        changed.append(f"account profit-lock every ₹{_num_setting(db, 'global_lock_step'):.0f} "
                       f"secure ₹{_num_setting(db, 'global_lock_amount'):.0f}")
    if changed:
        db.add(LogEntry(message="Settings updated: " + ", ".join(changed), level="INFO"))
        db.commit()
    return get_settings(db)


@app.post("/api/settings/reset_halt")
def reset_daily_halt(db: Session = Depends(get_db)):
    """Manually clear the daily limit halt (e.g. to resume trading deliberately)."""
    set_setting(db, "daily_halt", "off")
    set_setting(db, "daily_halt_date", "")
    set_setting(db, "daily_halt_reason", "")
    set_setting(db, "global_lock_floor", "0")
    db.add(LogEntry(message="Daily limit halt manually cleared.", level="WARN"))
    db.commit()
    return get_settings(db)


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
def list_presets(db: Session = Depends(get_db)):
    return [_preset_dict(p) for p in db.query(SymbolPreset).order_by(SymbolPreset.symbol).all()]


@app.post("/api/presets")
def save_preset(payload: SymbolPresetIn, db: Session = Depends(get_db)):
    sym = (payload.symbol or "").strip().upper()
    kind = str(payload.kind or "OPTION").upper()
    if kind not in ("OPTION", "FUTURES", "EQUITY"):
        kind = "OPTION"
    if not sym:
        raise HTTPException(400, "Enter a symbol (underlying) for the preset.")
    # One preset per (symbol, type) — saving for the same symbol+type updates it.
    p = (db.query(SymbolPreset)
         .filter(SymbolPreset.symbol == sym, SymbolPreset.kind == kind).first())
    is_new = p is None
    if p is None:
        p = SymbolPreset(symbol=sym, kind=kind)
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
    db.add(LogEntry(message=f"Symbol preset {'created' if is_new else 'updated'}: {sym} [{kind}]", level="INFO"))
    db.commit()
    return _preset_dict(p)


@app.delete("/api/presets/{preset_id}")
def delete_preset(preset_id: int, db: Session = Depends(get_db)):
    p = db.get(SymbolPreset, preset_id)
    if p:
        sym, kind = p.symbol, p.kind
        db.delete(p)
        db.add(LogEntry(message=f"Symbol preset deleted: {sym} [{kind}]", level="INFO"))
        db.commit()
    return {"ok": True}


# ---------- watchlist ----------
def _watch_dict(w):
    return {"id": w.id, "symbol": w.symbol, "security_id": w.security_id,
            "exchange_segment": w.exchange_segment, "instrument_type": w.instrument_type,
            "underlying": w.underlying, "lot_size": w.lot_size or 1}


@app.get("/api/watchlist")
def list_watchlist(db: Session = Depends(get_db)):
    return [_watch_dict(w) for w in db.query(Watchlist).order_by(Watchlist.id).all()]


@app.post("/api/watchlist")
def add_watchlist(payload: WatchlistIn, db: Session = Depends(get_db)):
    if not payload.security_id:
        raise HTTPException(400, "Select a symbol first, then add it to the watchlist.")
    # de-dupe on the same instrument
    exists = (db.query(Watchlist)
              .filter(Watchlist.security_id == payload.security_id,
                      Watchlist.exchange_segment == payload.exchange_segment).first())
    if exists:
        return _watch_dict(exists)
    w = Watchlist(symbol=payload.symbol, security_id=payload.security_id,
                  exchange_segment=payload.exchange_segment,
                  instrument_type=payload.instrument_type, underlying=payload.underlying,
                  lot_size=max(1, int(payload.lot_size or 1)))
    db.add(w)
    db.commit()
    db.refresh(w)
    return _watch_dict(w)


@app.delete("/api/watchlist/{item_id}")
def delete_watchlist(item_id: int, db: Session = Depends(get_db)):
    w = db.get(Watchlist, item_id)
    if w:
        db.delete(w)
        db.commit()
    return {"ok": True}


# ---------- broker info ----------
@app.get("/api/broker")
def get_broker(request: Request, db: Session = Depends(get_db)):
    data_provider = get_setting(db, "data_provider", "DEMO")
    trade_provider = get_setting(db, "trade_provider", "DEMO")
    data_acc = _account_for_provider(db, data_provider)
    trade_acc = _account_for_provider(db, trade_provider)
    data_conn = data_acc is None or bool(data_acc.connected)
    trade_conn = trade_acc is None or bool(trade_acc.connected)
    base = str(request.base_url).rstrip("/")
    return {
        "data_provider": data_provider,
        "trade_provider": trade_provider,
        "accounts": [_account_dict(a) for a in db.query(Account).order_by(Account.id).all()],
        "data_connected": data_conn,
        "trade_connected": trade_conn,
        "connected": data_conn and trade_conn,
        "redirect_url": base + "/api/dhan/callback",
        "postback_url": base + "/api/dhan/postback",
        "angel_redirect_url": base + "/api/angel/callback",
        "angel_postback_url": base + "/api/angel/postback",
        "zerodha_redirect_url": base + "/api/zerodha/callback",
        "zerodha_postback_url": base + "/api/zerodha/postback",
        "aliceblue_postback_url": base + "/api/aliceblue/postback",
        "static_ip": get_setting(db, "static_ip", ""),
    }


# ---------- Zerodha (Kite Connect) login ----------
@app.get("/api/zerodha/login")
def zerodha_login(account_id: int = 0, db: Session = Depends(get_db)):
    a = db.get(Account, account_id)
    if a is None or a.broker != "ZERODHA":
        raise HTTPException(400, "Zerodha account not found.")
    api_key = _acc_creds(a).get("api_key", "")
    if not api_key:
        raise HTTPException(400, "Enter the Zerodha API Key & Secret first.")
    set_setting(db, "pending_login_account", str(a.id))
    return {"login_url": zerodha.login_url(api_key)}


@app.get("/api/zerodha/callback")
def zerodha_callback(request_token: str = "", db: Session = Depends(get_db)):
    a = _account_for_provider(db, get_setting(db, "pending_login_account", ""))
    if a is None or a.broker != "ZERODHA" or not request_token:
        return RedirectResponse(url="/?login=failed")
    creds = _acc_creds(a)
    ok, res = zerodha.exchange_request_token(creds.get("api_key", ""), creds.get("api_secret", ""), request_token)
    if ok:
        _set_acc_creds(a, access_token=res)
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("ZERODHA", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO"))
        db.commit()
        return RedirectResponse(url="/?login=ok")
    db.add(LogEntry(message=f"Zerodha login failed: {res}", level="ERROR"))
    db.commit()
    return RedirectResponse(url="/?login=failed")


@app.post("/api/zerodha/postback")
async def zerodha_postback(request: Request, db: Session = Depends(get_db)):
    try:
        payload = await request.json()
        orders = payload if isinstance(payload, list) else [payload]
        _sync_external_orders(db, [zerodha.normalize_order(o) for o in orders],
                              "ZERODHA", _account_id_for_broker(db, "ZERODHA"))
        db.commit()
    except Exception as e:
        db.add(LogEntry(message=f"Zerodha postback error: {e}", level="ERROR"))
        db.commit()
    return {"ok": True}


# ---------- "Login with Dhan" (app consent), per account ----------
@app.get("/api/dhan/login")
def dhan_login(account_id: int = 0, db: Session = Depends(get_db)):
    """Start the Dhan login for an account; returns the URL to send the user to."""
    a = db.get(Account, account_id)
    if a is None or a.broker != "DHAN":
        raise HTTPException(400, "Dhan account not found.")
    creds = _acc_creds(a)
    app_id, app_secret = creds.get("app_id", ""), creds.get("app_secret", "")
    if not app_id or not app_secret or not a.client_id:
        raise HTTPException(400, "Enter App ID, App Secret and Client ID first.")
    try:
        consent = dhan_auth.generate_consent(app_id, app_secret, a.client_id)
        if not consent:
            raise HTTPException(400, "Dhan did not return a consent id. Check your App ID/Secret.")
        set_setting(db, "pending_login_account", str(a.id))
        return {"login_url": dhan_auth.login_url(consent)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(400, f"Could not start Dhan login: {e}")


@app.get("/api/dhan/callback")
def dhan_callback(tokenId: str = "", db: Session = Depends(get_db)):
    a = _account_for_provider(db, get_setting(db, "pending_login_account", ""))
    if a is None or not tokenId:
        return RedirectResponse(url="/?login=failed")
    creds = _acc_creds(a)
    try:
        token, client_id, _ = dhan_auth.consume_consent(creds.get("app_id", ""), creds.get("app_secret", ""), tokenId)
        if not token:
            raise RuntimeError("no access token returned")
        _set_acc_creds(a, access_token=token)
        if client_id:
            a.client_id = client_id
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("DHAN", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO"))
        db.commit()
        return RedirectResponse(url="/?login=ok")
    except Exception as e:
        db.add(LogEntry(message=f"Dhan login callback failed: {e}", level="ERROR"))
        db.commit()
        return RedirectResponse(url="/?login=failed")


@app.post("/api/dhan/postback")
async def dhan_postback(request: Request, db: Session = Depends(get_db)):
    """Order-update webhook: Dhan POSTs order status here for instant sync."""
    try:
        payload = await request.json()
        orders = payload if isinstance(payload, list) else [payload]
        _sync_external_orders(db, orders, "DHAN", _account_id_for_broker(db, "DHAN"))
        db.commit()
    except Exception as e:
        db.add(LogEntry(message=f"Postback error: {e}", level="ERROR"))
        db.commit()
    return {"ok": True}


@app.post("/api/providers")
def set_providers(payload: dict, db: Session = Depends(get_db)):
    """Set the Data and Trading providers — each is 'DEMO' or an account id."""
    cur_data = get_setting(db, "data_provider", "DEMO")
    cur_trade = get_setting(db, "trade_provider", "DEMO")
    new_data = str(payload.get("data_provider", cur_data))
    new_trade = str(payload.get("trade_provider", cur_trade))

    def _valid(v):
        return v == "DEMO" or db.get(Account, int(v)) if v.isdigit() else (v == "DEMO")
    if not _valid(new_data):
        new_data = cur_data
    if not _valid(new_trade):
        new_trade = cur_trade
    # Demo is all-or-nothing.
    if (new_data == "DEMO") != (new_trade == "DEMO"):
        raise HTTPException(400, "Demo can't be mixed with a live broker — set both to Demo, "
                                 "or pick a live account for both.")
    active = db.query(Trade).filter(Trade.status.in_(["OPEN", "PENDING"])).count()
    if active > 0 and (new_data != cur_data or new_trade != cur_trade):
        raise HTTPException(400, f"{active} trade(s) are running — close them before switching providers.")
    changed = (new_data != cur_data or new_trade != cur_trade)
    set_setting(db, "data_provider", new_data)
    set_setting(db, "trade_provider", new_trade)
    set_setting(db, "broker_mode", "DEMO" if new_trade == "DEMO" else "DHAN")
    for k in ("broker_balance", "broker_balance_provider", "broker_health", "md_status"):
        set_setting(db, k, "")
    if changed:
        da = _account_for_provider(db, new_data)
        ta = _account_for_provider(db, new_trade)
        dn = "Demo" if da is None else (da.label or _label(da.broker, da.client_id))
        tn = "Demo" if ta is None else (ta.label or _label(ta.broker, ta.client_id))
        db.add(LogEntry(message=f"Providers switched — Data: {dn}, Trading: {tn}", level="INFO"))
    db.commit()
    return {"data_provider": new_data, "trade_provider": new_trade}


# legacy endpoint kept for safety
@app.post("/api/broker/mode")
def set_broker_mode(payload: dict, db: Session = Depends(get_db)):
    mode = "DEMO" if str(payload.get("mode", "")).upper() == "DEMO" else "DHAN"
    active = db.query(Trade).filter(Trade.status.in_(["OPEN", "PENDING"])).count()
    if active > 0 and mode != get_setting(db, "broker_mode", "DHAN"):
        raise HTTPException(400, f"{active} trade(s) are running — close them before switching.")
    set_setting(db, "broker_mode", mode)
    set_setting(db, "data_provider", mode)
    set_setting(db, "trade_provider", mode)
    db.commit()
    return {"mode": mode}


# ---------- Angel One (per account) ----------
@app.post("/api/angel/login")
def angel_login(payload: dict, db: Session = Depends(get_db)):
    """Log in to Angel One (password + TOTP) for an account and store the token."""
    a = db.get(Account, int(payload.get("account_id", 0)))
    if a is None or a.broker != "ANGEL":
        raise HTTPException(400, "Angel account not found.")
    creds = _acc_creds(a)
    cid, pin = a.client_id, creds.get("pin", "")
    key, totp = creds.get("api_key", ""), creds.get("totp_secret", "")
    if not all([cid, pin, key, totp]):
        raise HTTPException(400, "Enter Angel Client ID, PIN, API Key and TOTP secret first.")
    ok, data = angel.login(cid, pin, key, totp)
    if ok:
        _set_acc_creds(a, jwt=data["jwt"], refresh=data.get("refresh", ""), feed=data.get("feed", ""))
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("ANGEL", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO"))
        db.commit()
        return {"connected": True}
    db.add(LogEntry(message=f"Angel One login failed: {data}", level="ERROR"))
    db.commit()
    raise HTTPException(400, f"Angel login failed: {data}")


@app.post("/api/angel/postback")
async def angel_postback(request: Request, db: Session = Depends(get_db)):
    """Angel order-update webhook: instant sync of external Angel orders."""
    try:
        payload = await request.json()
        orders = payload if isinstance(payload, list) else [payload]
        _sync_external_orders(db, [angel.normalize_order(o) for o in orders],
                              "ANGEL", _account_id_for_broker(db, "ANGEL"))
        db.commit()
    except Exception as e:
        db.add(LogEntry(message=f"Angel postback error: {e}", level="ERROR"))
        db.commit()
    return {"ok": True}


# ---------- Alice Blue (ANT API, per account) ----------
@app.post("/api/aliceblue/login")
def aliceblue_login(payload: dict, db: Session = Depends(get_db)):
    """Log in to Alice Blue (User ID + API Key -> session) and store the session."""
    a = db.get(Account, int(payload.get("account_id", 0)))
    if a is None or a.broker != "ALICE":
        raise HTTPException(400, "Alice Blue account not found.")
    creds = _acc_creds(a)
    cid, key = a.client_id, creds.get("api_key", "")
    if not cid or not key:
        raise HTTPException(400, "Enter Alice Blue User ID and API Key first.")
    ok, res = aliceblue.login(cid, key)
    if ok:
        _set_acc_creds(a, session_id=res)
        a.connected = 1
        a.token_time = dt.datetime.utcnow()
        a.label = _label("ALICE", a.client_id)
        db.add(LogEntry(message=f"Logged in to {a.label}.", level="INFO"))
        db.commit()
        return {"connected": True}
    db.add(LogEntry(message=f"Alice Blue login failed: {res}", level="ERROR"))
    db.commit()
    raise HTTPException(400, f"Alice Blue login failed: {res}")


@app.post("/api/aliceblue/postback")
async def aliceblue_postback(request: Request, db: Session = Depends(get_db)):
    """Alice Blue order-update webhook: instant sync of external Alice orders."""
    try:
        payload = await request.json()
        orders = payload if isinstance(payload, list) else [payload]
        _sync_external_orders(db, [aliceblue.normalize_order(o) for o in orders],
                              "ALICE", _account_id_for_broker(db, "ALICE"))
        db.commit()
    except Exception as e:
        db.add(LogEntry(message=f"Alice Blue postback error: {e}", level="ERROR"))
        db.commit()
    return {"ok": True}


@app.post("/api/broker")
def save_broker(payload: BrokerConfigIn, db: Session = Depends(get_db)):
    if payload.dhan_client_id:
        set_setting(db, "dhan_client_id", payload.dhan_client_id)
    if payload.dhan_access_token:
        set_setting(db, "dhan_access_token", payload.dhan_access_token)
    set_setting(db, "dhan_connected", "no")  # re-verify after any change
    return {"ok": True}


@app.post("/api/broker/connect")
def connect_broker(db: Session = Depends(get_db)):
    """Authenticate with Dhan using the saved client id + access token."""
    if get_setting(db, "broker_mode", "DHAN") == "DEMO":
        return {"connected": True, "message": "Demo mode — simulated broker connected."}
    cid = get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
    tok = get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
    if not cid or not tok:
        raise HTTPException(400, "Enter your Dhan Client ID and Access Token first.")
    ok, msg = verify_dhan_credentials(cid, tok)
    set_setting(db, "dhan_connected", "yes" if ok else "no")
    db.add(LogEntry(message=f"Dhan connect: {msg}", level="INFO" if ok else "ERROR"))
    db.commit()
    return {"connected": ok, "message": msg}


# ---------- frontend ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


# Serve the rest of the frontend (app.js, styles.css).
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=config.HOST, port=config.PORT, reload=False)
