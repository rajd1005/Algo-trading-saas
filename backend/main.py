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

import config
from database import init_db, get_db, SessionLocal
from models import Trade, LogEntry, Setting
from schemas import TradeCreate, TradeOut, BrokerConfigIn, SettingsIn, ModifyIn
from engine import engine, level_price
from instruments import store as instruments
from market_data import DhanMarketData, demo_market
from brokers import verify_dhan_credentials

app = FastAPI(title="Algo Trading SaaS (India)")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


_OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/favicon.ico"}


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
        set_setting(db, "broker_mode", "DEMO")   # start in safe Demo mode
    db.close()
    instruments.load_async()   # download Dhan's symbol list in the background
    engine.start()
    threading.Thread(target=_auto_renew_loop, daemon=True).start()


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

    if payload.trail_mode is not None:
        t.trail_mode = "ENTRY" if str(payload.trail_mode).upper() == "ENTRY" else "CONTINUE"

    # Stop-loss and trailing now work TOGETHER: the SL is where the trail starts.
    if payload.stop_loss is not None:
        t.stop_loss = float(payload.stop_loss)
        if t.entry_fill_price > 0 and t.stop_loss > 0:
            t.sl_points = round(abs(t.entry_fill_price - t.stop_loss), 2)

    if payload.trail_sl is not None:
        t.trail_sl = float(payload.trail_sl)
        if t.trail_sl > 0:
            t.hwm = t.last_price or t.entry_fill_price   # re-arm the trailing step reference

    if payload.targets is not None:
        clean = [{"price": float(x.price), "qty": int(x.qty), "hit": False}
                 for x in payload.targets if float(x.price) > 0 and int(x.qty) > 0]
        if len(clean) <= 1:
            # single target -> store as the plain target price, clear scale-out
            t.targets_json = ""
            t.target = clean[0]["price"] if clean else 0.0
            t.target_points = 0.0
        else:
            t.targets_json = json.dumps(clean)
            t.target = clean[0]["price"]
            t.target_points = 0.0

    db.add(LogEntry(message=f"Modified {t.symbol}: SL {t.stop_loss} / targets updated", trade_id=t.id))
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

    if get_setting(db, "broker_mode", "DHAN") == "DEMO":
        res = demo_market.get_ltp_batch(by_seg)
        return {"connected": True, "prices": {sid: px for (seg, sid), px in res.items()}}

    cid = get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
    tok = get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
    if not cid or not tok or not by_seg:
        return {"connected": bool(cid and tok), "prices": {}}
    md = DhanMarketData(cid, tok)
    res = md.get_ltp_batch(by_seg)
    prices = {sid: px for (seg, sid), px in res.items()}
    return {"connected": True, "prices": prices, "error": md.last_error}


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
@app.get("/api/summary")
def summary(db: Session = Depends(get_db)):
    trades = db.query(Trade).all()
    open_pnl = sum(t.pnl for t in trades if t.status == "OPEN")
    closed_pnl = sum(t.pnl for t in trades if t.status == "CLOSED")
    return {
        "total_trades": len(trades),
        "pending": sum(1 for t in trades if t.status == "PENDING"),
        "open": sum(1 for t in trades if t.status == "OPEN"),
        "closed": sum(1 for t in trades if t.status == "CLOSED"),
        "open_pnl": round(open_pnl, 2),
        "closed_pnl": round(closed_pnl, 2),
        "total_pnl": round(open_pnl + closed_pnl, 2),
        "kill_switch": get_setting(db, "kill_switch", "off"),
        "md_status": get_setting(db, "md_status", ""),
        "instruments": instruments.status(),
        "demo_direction": demo_market.direction,
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
@app.get("/api/broker")
def get_broker(db: Session = Depends(get_db)):
    cid = get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
    tok = get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
    mode = get_setting(db, "broker_mode", "DHAN")
    token_time = get_setting(db, "dhan_token_time", "")
    hours_left = None
    if token_time:
        try:
            age = (dt.datetime.utcnow() - dt.datetime.fromisoformat(token_time)).total_seconds() / 3600
            hours_left = round(max(0, 24 - age), 1)
        except Exception:
            pass
    return {
        "dhan_client_id": cid,
        "dhan_app_id": get_setting(db, "dhan_app_id", ""),
        # Never send secrets back; just say if they're set.
        "has_access_token": bool(tok),
        "has_app": bool(get_setting(db, "dhan_app_id", "")),
        "has_app_secret": bool(get_setting(db, "dhan_app_secret", "")),
        "mode": mode,
        "connected": mode == "DEMO" or get_setting(db, "dhan_connected", "no") == "yes",
        "token_hours_left": hours_left,
    }


# ---------- "Login with Dhan" (app consent) ----------
@app.post("/api/dhan/app")
def save_dhan_app(payload: dict, db: Session = Depends(get_db)):
    """Save the one-time App ID / App Secret / Client ID (valid ~12 months)."""
    if payload.get("app_id"):
        set_setting(db, "dhan_app_id", payload["app_id"].strip())
    if payload.get("app_secret"):
        set_setting(db, "dhan_app_secret", payload["app_secret"].strip())
    if payload.get("client_id"):
        set_setting(db, "dhan_client_id", payload["client_id"].strip())
    return {"ok": True}


@app.get("/api/dhan/login")
def dhan_login(db: Session = Depends(get_db)):
    """Start the Dhan login: returns the URL to send the user to."""
    app_id = get_setting(db, "dhan_app_id", "")
    app_secret = get_setting(db, "dhan_app_secret", "")
    client_id = get_setting(db, "dhan_client_id", "")
    if not app_id or not app_secret or not client_id:
        raise HTTPException(400, "Enter your Dhan App ID, App Secret and Client ID first.")
    try:
        consent = dhan_auth.generate_consent(app_id, app_secret, client_id)
        if not consent:
            raise HTTPException(400, "Dhan did not return a consent id. Check your App ID/Secret.")
        return {"login_url": dhan_auth.login_url(consent)}
    except HTTPException:
        raise
    except Exception as e:
        db.add(LogEntry(message=f"Dhan login start failed: {e}", level="ERROR"))
        db.commit()
        raise HTTPException(400, f"Could not start Dhan login: {e}")


@app.get("/api/dhan/callback")
def dhan_callback(tokenId: str = "", db: Session = Depends(get_db)):
    """Dhan redirects here after login with a tokenId; we fetch the access token."""
    app_id = get_setting(db, "dhan_app_id", "")
    app_secret = get_setting(db, "dhan_app_secret", "")
    if not tokenId or not app_id:
        return RedirectResponse(url="/?login=failed")
    try:
        token, client_id, _ = dhan_auth.consume_consent(app_id, app_secret, tokenId)
        if not token:
            raise RuntimeError("no access token returned")
        set_setting(db, "dhan_access_token", token)
        if client_id:
            set_setting(db, "dhan_client_id", client_id)
        set_setting(db, "dhan_connected", "yes")
        set_setting(db, "dhan_token_time", dt.datetime.utcnow().isoformat())
        db.add(LogEntry(message="Logged in to Dhan — access token received.", level="INFO"))
        db.commit()
        return RedirectResponse(url="/?login=ok")
    except Exception as e:
        db.add(LogEntry(message=f"Dhan login callback failed: {e}", level="ERROR"))
        db.commit()
        return RedirectResponse(url="/?login=failed")


@app.post("/api/broker/mode")
def set_broker_mode(payload: dict, db: Session = Depends(get_db)):
    mode = "DEMO" if str(payload.get("mode", "")).upper() == "DEMO" else "DHAN"
    set_setting(db, "broker_mode", mode)
    db.add(LogEntry(message=f"Broker mode set to {mode}", level="WARN"))
    db.commit()
    return {"mode": mode}


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
