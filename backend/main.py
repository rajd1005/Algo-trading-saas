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

import config
from database import init_db, get_db, SessionLocal
from models import Trade, LogEntry, Setting
from schemas import TradeCreate, TradeOut, BrokerConfigIn, SettingsIn, ModifyIn
from engine import engine, level_price
from instruments import store as instruments
from market_data import DhanMarketData, demo_market
from brokers import verify_dhan_credentials, DhanBroker

app = FastAPI(title="Algo Trading SaaS (India)")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


_OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico",
               "/api/dhan/postback", "/api/dhan/callback"}


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
    db.close()
    instruments.load_async()   # download Dhan's symbol list in the background
    angel.mapper.load_async()  # download Angel One master + build the symbol map
    engine.start()
    threading.Thread(target=_auto_renew_loop, daemon=True).start()
    threading.Thread(target=_broker_monitor_loop, daemon=True).start()


def _broker_monitor_loop():
    """Live broker: refresh balance + connection health, and sync any positions
    placed externally on the broker terminal into our 'Live Trade' list."""
    while True:
        time.sleep(12)
        try:
            db = SessionLocal()
            tp = get_setting(db, "trade_provider", "DEMO")
            b = None
            if tp == "DHAN":
                cid, tok = get_trade_creds(db)
                if cid and tok:
                    b = DhanBroker(cid, tok)
            elif tp == "ANGEL":
                cid = get_setting(db, "angel_client_id", "")
                key = get_setting(db, "angel_api_key", "")
                jwt = get_setting(db, "angel_jwt", "")
                if cid and key and jwt:
                    b = angel.AngelBroker(cid, key, jwt)
            if b is not None:
                ok, bal = b.fund_limit()
                if ok:
                    set_setting(db, "broker_balance", str(bal))
                    set_setting(db, "broker_health", "ok")
                    set_setting(db, "broker_health_time", dt.datetime.utcnow().isoformat())
                else:
                    set_setting(db, "broker_health", "error")
                try:
                    _sync_external_orders(db, b.get_orders())
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


def _sync_external_orders(db, orders):
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
                  broker="DHAN", source="EXTERNAL", broker_order_id=oid,
                  exit_reason="REJECTED" if mapped == "REJECTED" else "")
        db.add(t)
        db.add(LogEntry(message=f"Synced external order: {sym} {side} x{qty} [{raw}]"
                                + (f". Reason: {reason}" if reason else ""),
                        level="ERROR" if mapped == "REJECTED" else "INFO"))


def _auto_renew_loop():
    """Keep the Dhan token alive by renewing it ~4h before it expires."""
    while True:
        time.sleep(1800)   # check every 30 minutes
        try:
            db = SessionLocal()
            connected = get_setting(db, "broker_mode", "DHAN") == "DHAN" and \
                get_setting(db, "dhan_connected", "no") == "yes"
            token_time = get_setting(db, "dhan_token_time", "")
            tok = get_setting(db, "dhan_access_token", "")
            cid = get_setting(db, "dhan_client_id", "")
            if connected and token_time and tok and cid:
                age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(token_time)).total_seconds() / 3600
                if age >= 20:
                    new = dhan_auth.renew_token(tok, cid)
                    if new:
                        set_setting(db, "dhan_access_token", new)
                        set_setting(db, "dhan_token_time", dt.datetime.utcnow().isoformat())
                        db.add(LogEntry(message="Dhan token auto-renewed for another 24h.", level="INFO"))
                    else:
                        db.add(LogEntry(message="Dhan token auto-renew failed — please Login with Dhan again.",
                                        level="WARN"))
                    db.commit()
            # Angel One token renewal
            a_jwt = get_setting(db, "angel_jwt", "")
            a_key = get_setting(db, "angel_api_key", "")
            a_ref = get_setting(db, "angel_refresh", "")
            a_time = get_setting(db, "angel_token_time", "")
            if a_jwt and a_key and a_ref and a_time:
                try:
                    age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(a_time)).total_seconds() / 3600
                    if age >= 20:
                        ok, new = angel.renew(a_key, a_jwt, a_ref)
                        if ok:
                            set_setting(db, "angel_jwt", new)
                            set_setting(db, "angel_token_time", dt.datetime.utcnow().isoformat())
                            db.add(LogEntry(message="Angel One token auto-renewed.", level="INFO"))
                        else:
                            db.add(LogEntry(message="Angel token renew failed — log in to Angel again.", level="WARN"))
                        db.commit()
                except Exception:
                    pass
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
    db.add(LogEntry(message=f"Trade created: {t.symbol} [{t.mode}]", trade_id=t.id))
    db.commit()
    return t


@app.post("/api/trades/{trade_id}/modify", response_model=TradeOut)
def modify_trade(trade_id: int, payload: ModifyIn, db: Session = Depends(get_db)):
    """Change stop-loss / targets on an OPEN trade, using DIRECT prices.
    Supports editing or adding multiple (scale-out) targets here too."""
    t = db.get(Trade, trade_id)
    if not t:
        raise HTTPException(404, "Trade not found")
    if t.status != "OPEN":
        raise HTTPException(400, "Only open trades can be modified.")

    old_sl, old_trail = t.stop_loss, t.trail_sl
    old_targets = t.targets_json or (str(t.target) if t.target else "")
    changes = []

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

    dp = get_setting(db, "data_provider", "DEMO")
    if dp == "DEMO":
        res = demo_market.get_ltp_batch(by_seg)
        return {"connected": True, "prices": {sid: px for (seg, sid), px in res.items()}}
    if dp == "ANGEL":
        acid = get_setting(db, "angel_client_id", "")
        akey = get_setting(db, "angel_api_key", "")
        ajwt = get_setting(db, "angel_jwt", "")
        if not (acid and akey and ajwt) or not by_seg:
            return {"connected": bool(acid and akey and ajwt), "prices": {}}
        md = angel.AngelMarketData(acid, akey, ajwt)
        res = md.get_ltp_batch(by_seg)
        return {"connected": True, "prices": {sid: px for (seg, sid), px in res.items()},
                "error": md.last_error}
    # Dhan
    dcid, dtok = get_data_creds(db)
    if not dcid or not dtok or not by_seg:
        return {"connected": bool(dcid and dtok), "prices": {}}
    md = DhanMarketData(dcid, dtok)
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
    instruments.refresh()
    return {"ok": True, "message": "Refreshing symbol list in the background…"}


# ---------- summary ----------
# ---------- summary / P&L ----------
def _trade_env(t):
    """Which environment a trade belongs to: PAPER (demo) / DHAN / ANGEL."""
    if t.broker:
        return t.broker
    return "PAPER" if t.mode == "TEST" else "DHAN"


def _metrics(trades):
    """Compute the P&L metric set for a list of trades."""
    closed = [t for t in trades if t.status == "CLOSED"]
    gross = sum(t.pnl for t in trades if t.status in ("OPEN", "CLOSED"))
    open_pnl = sum(t.pnl for t in trades if t.status == "OPEN")
    closed_pnl = sum(t.pnl for t in closed)
    charges = 0.0
    for t in trades:
        legs = 1 if (t.entry_fill_price or 0) > 0 else 0
        hits = 0
        if t.targets_json:
            try:
                hits = sum(1 for x in json.loads(t.targets_json) if x.get("hit"))
            except Exception:
                hits = 0
        if hits > 0:
            legs += hits
        elif t.status == "CLOSED":
            legs += 1
        charges += legs * config.CHARGE_PER_LEG
    wins = sum(1 for t in closed if t.pnl > 0)
    return {
        "gross": round(gross, 2),
        "charges": round(charges, 2),
        "net": round(gross - charges, 2),
        "open_pnl": round(open_pnl, 2),
        "closed_pnl": round(closed_pnl, 2),
        "win_rate": round(100 * wins / len(closed), 1) if closed else 0.0,
        "wins": wins,
        "closed": len(closed),
        "open": sum(1 for t in trades if t.status == "OPEN"),
        "pending": sum(1 for t in trades if t.status == "PENDING"),
    }


@app.get("/api/summary")
def summary(broker: str = "ALL", db: Session = Depends(get_db)):
    trades = db.query(Trade).all()
    buckets = {"PAPER": [], "DHAN": [], "ANGEL": []}
    for t in trades:
        buckets.setdefault(_trade_env(t), []).append(t)
    flt = (broker or "ALL").upper()
    selected = buckets.get(flt, trades) if flt in buckets else trades

    pnl = _metrics(selected)
    breakdown = {k: _metrics(v)["net"] for k, v in buckets.items()}
    active = sum(1 for t in trades if t.status in ("OPEN", "PENDING"))

    data_provider = get_setting(db, "data_provider", "DEMO")
    trade_provider = get_setting(db, "trade_provider", "DEMO")
    PNAME = {"DEMO": "Demo", "DHAN": "Dhan", "ANGEL": "Angel One"}
    broker_name = PNAME.get(trade_provider, trade_provider)
    if trade_provider == "DEMO":
        balance = None
    else:
        bal = get_setting(db, "broker_balance", "")
        balance = float(bal) if bal else None

    # Connection-lost warning while trades are running.
    broker_alert, alert_msg = False, ""
    if trade_provider != "DEMO" and active > 0:
        connected = (get_setting(db, "dhan_connected", "no") == "yes") if trade_provider == "DHAN" \
            else bool(get_setting(db, "angel_jwt", ""))
        if not connected:
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
        "demo_direction": demo_market.direction,
        "data_provider": data_provider, "trade_provider": trade_provider,
        "broker_name": broker_name, "balance": balance,
        "broker_alert": broker_alert, "alert_msg": alert_msg,
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
@app.get("/api/settings")
def get_settings(db: Session = Depends(get_db)):
    return {
        "kill_switch": get_setting(db, "kill_switch", "off"),
        "default_mode": get_setting(db, "default_mode", "TEST"),
    }


@app.post("/api/settings")
def update_settings(payload: SettingsIn, db: Session = Depends(get_db)):
    if payload.kill_switch is not None:
        set_setting(db, "kill_switch", "on" if payload.kill_switch else "off")
        db.add(LogEntry(message=f"Kill switch set to "
                                f"{'ON' if payload.kill_switch else 'OFF'}", level="WARN"))
        db.commit()
    if payload.default_mode in ("TEST", "LIVE"):
        set_setting(db, "default_mode", payload.default_mode)
    return get_settings(db)


# ---------- broker (Dhan) ----------
def _account_info(db, pre):
    token_time = get_setting(db, pre + "token_time", "")
    hours_left = None
    if token_time:
        try:
            age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(token_time)).total_seconds() / 3600
            hours_left = round(max(0, 24 - age), 1)
        except Exception:
            pass
    return {
        "client_id": get_setting(db, pre + "client_id", ""),
        "app_id": get_setting(db, pre + "app_id", ""),
        "has_app_secret": bool(get_setting(db, pre + "app_secret", "")),
        "connected": get_setting(db, pre + "connected", "no") == "yes",
        "token_hours_left": hours_left,
    }


def _angel_account_info(db):
    tt = get_setting(db, "angel_token_time", "")
    hours_left = None
    if tt:
        try:
            age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(tt)).total_seconds() / 3600
            hours_left = round(max(0, 24 - age), 1)
        except Exception:
            pass
    return {
        "client_id": get_setting(db, "angel_client_id", ""),
        "has_api_key": bool(get_setting(db, "angel_api_key", "")),
        "has_totp": bool(get_setting(db, "angel_totp_secret", "")),
        "connected": bool(get_setting(db, "angel_jwt", "")),
        "token_hours_left": hours_left,
    }


@app.get("/api/broker")
def get_broker(request: Request, db: Session = Depends(get_db)):
    mode = get_setting(db, "broker_mode", "DHAN")
    same = get_setting(db, "use_same_account", "yes") == "yes"
    trade = _account_info(db, "dhan_")
    data = trade if same else _account_info(db, "data_")
    angel_acct = _angel_account_info(db)
    data_provider = get_setting(db, "data_provider", "DEMO")
    trade_provider = get_setting(db, "trade_provider", "DEMO")

    def _conn(p):
        if p == "DEMO":
            return True
        if p == "ANGEL":
            return angel_acct["connected"]
        return trade["connected"]   # DHAN
    connected = _conn(data_provider) and _conn(trade_provider)
    base = str(request.base_url).rstrip("/")
    return {
        "mode": mode,
        "data_provider": data_provider,
        "trade_provider": trade_provider,
        "use_same_account": same,
        "trade": trade,
        "data": data,
        "angel": angel_acct,
        "data_connected": _conn(data_provider),
        "trade_connected": _conn(trade_provider),
        "connected": connected,
        # back-compat fields (trading account)
        "dhan_client_id": trade["client_id"],
        "dhan_app_id": trade["app_id"],
        "has_app": bool(trade["app_id"]),
        "has_app_secret": trade["has_app_secret"],
        "token_hours_left": trade["token_hours_left"],
        # webhook / redirect URLs to configure on the Dhan app(s)
        "redirect_url": base + "/api/dhan/callback",
        "postback_url": base + "/api/dhan/postback",
    }


# ---------- "Login with Dhan" (app consent) ----------
def _acct_prefix(account: str) -> str:
    """Settings prefix for an account: trading uses 'dhan_', data uses 'data_'."""
    return "data_" if str(account).upper() == "DATA" else "dhan_"


@app.post("/api/broker/same")
def set_same_account(payload: dict, db: Session = Depends(get_db)):
    """Toggle 'use the same account for Data and Trading'."""
    same = "yes" if payload.get("same", True) else "no"
    set_setting(db, "use_same_account", same)
    return {"use_same_account": same}


@app.post("/api/dhan/app")
def save_dhan_app(payload: dict, db: Session = Depends(get_db)):
    """Save App ID / App Secret / Client ID for an account (TRADE or DATA)."""
    pre = _acct_prefix(payload.get("account", "TRADE"))
    if payload.get("app_id"):
        set_setting(db, pre + "app_id", payload["app_id"].strip())
    if payload.get("app_secret"):
        set_setting(db, pre + "app_secret", payload["app_secret"].strip())
    if payload.get("client_id"):
        set_setting(db, pre + "client_id", payload["client_id"].strip())
    return {"ok": True}


@app.get("/api/dhan/login")
def dhan_login(account: str = "TRADE", db: Session = Depends(get_db)):
    """Start the Dhan login for an account; returns the URL to send the user to."""
    pre = _acct_prefix(account)
    app_id = get_setting(db, pre + "app_id", "")
    app_secret = get_setting(db, pre + "app_secret", "")
    client_id = get_setting(db, pre + "client_id", "")
    if not app_id or not app_secret or not client_id:
        raise HTTPException(400, "Enter App ID, App Secret and Client ID first.")
    try:
        consent = dhan_auth.generate_consent(app_id, app_secret, client_id)
        if not consent:
            raise HTTPException(400, "Dhan did not return a consent id. Check your App ID/Secret.")
        set_setting(db, "pending_login_acct", "DATA" if pre == "data_" else "TRADE")
        return {"login_url": dhan_auth.login_url(consent)}
    except HTTPException:
        raise
    except Exception as e:
        db.add(LogEntry(message=f"Dhan login start failed: {e}", level="ERROR"))
        db.commit()
        raise HTTPException(400, f"Could not start Dhan login: {e}")


@app.get("/api/dhan/callback")
def dhan_callback(tokenId: str = "", db: Session = Depends(get_db)):
    """Dhan redirects here after login; fetch the access token for the pending account."""
    pre = _acct_prefix(get_setting(db, "pending_login_acct", "TRADE"))
    app_id = get_setting(db, pre + "app_id", "")
    app_secret = get_setting(db, pre + "app_secret", "")
    if not tokenId or not app_id:
        return RedirectResponse(url="/?login=failed")
    try:
        token, client_id, _ = dhan_auth.consume_consent(app_id, app_secret, tokenId)
        if not token:
            raise RuntimeError("no access token returned")
        set_setting(db, pre + "access_token", token)
        if client_id:
            set_setting(db, pre + "client_id", client_id)
        set_setting(db, pre + "connected", "yes")
        set_setting(db, pre + "token_time", dt.datetime.utcnow().isoformat())
        # 'dhan_connected' drives the live engine; keep it in sync with the trade acct.
        if pre == "dhan_":
            set_setting(db, "dhan_connected", "yes")
        label = "Data" if pre == "data_" else "Trading"
        db.add(LogEntry(message=f"Logged in to Dhan ({label} account).", level="INFO"))
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
        _sync_external_orders(db, orders)
        db.commit()
    except Exception as e:
        db.add(LogEntry(message=f"Postback error: {e}", level="ERROR"))
        db.commit()
    return {"ok": True}


_PROVIDERS = {"DEMO", "DHAN", "ANGEL"}


@app.post("/api/providers")
def set_providers(payload: dict, db: Session = Depends(get_db)):
    """Set the Data and Trading providers (DEMO / DHAN / ANGEL)."""
    new_data = str(payload.get("data_provider", "")).upper()
    new_trade = str(payload.get("trade_provider", "")).upper()
    active = db.query(Trade).filter(Trade.status.in_(["OPEN", "PENDING"])).count()
    cur_data = get_setting(db, "data_provider", "DEMO")
    cur_trade = get_setting(db, "trade_provider", "DEMO")
    if active > 0 and ((new_data and new_data != cur_data) or (new_trade and new_trade != cur_trade)):
        raise HTTPException(400, f"{active} trade(s) are running — close them before switching providers.")
    if new_data in _PROVIDERS:
        set_setting(db, "data_provider", new_data)
    if new_trade in _PROVIDERS:
        set_setting(db, "trade_provider", new_trade)
    # keep legacy broker_mode roughly in sync
    tp = get_setting(db, "trade_provider", "DEMO")
    set_setting(db, "broker_mode", "DEMO" if tp == "DEMO" else "DHAN")
    db.commit()
    return {"data_provider": get_setting(db, "data_provider", "DEMO"),
            "trade_provider": get_setting(db, "trade_provider", "DEMO")}


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


# ---------- Angel One ----------
@app.post("/api/angel/app")
def save_angel_app(payload: dict, db: Session = Depends(get_db)):
    """Save Angel One SmartAPI credentials."""
    for k in ("client_id", "api_key", "totp_secret", "pin"):
        if payload.get(k):
            set_setting(db, "angel_" + k, str(payload[k]).strip())
    return {"ok": True}


@app.post("/api/angel/login")
def angel_login(db: Session = Depends(get_db)):
    """Log in to Angel One (password + TOTP) and store the session token."""
    cid = get_setting(db, "angel_client_id", "")
    pin = get_setting(db, "angel_pin", "")
    key = get_setting(db, "angel_api_key", "")
    totp = get_setting(db, "angel_totp_secret", "")
    if not all([cid, pin, key, totp]):
        raise HTTPException(400, "Enter Angel Client ID, PIN, API Key and TOTP secret first.")
    ok, data = angel.login(cid, pin, key, totp)
    if ok:
        set_setting(db, "angel_jwt", data["jwt"])
        set_setting(db, "angel_refresh", data.get("refresh", ""))
        set_setting(db, "angel_feed", data.get("feed", ""))
        set_setting(db, "angel_token_time", dt.datetime.utcnow().isoformat())
        db.add(LogEntry(message="Logged in to Angel One.", level="INFO"))
        db.commit()
        return {"connected": True}
    db.add(LogEntry(message=f"Angel One login failed: {data}", level="ERROR"))
    db.commit()
    raise HTTPException(400, f"Angel login failed: {data}")


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
