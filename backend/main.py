"""
FastAPI application: the web server + REST API + serves the dashboard.

Run it with:   uvicorn main:app --host 0.0.0.0 --port 8000
(or just:      python main.py )
"""
import os

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

import config
from database import init_db, get_db, SessionLocal
from models import Trade, LogEntry, Setting
from schemas import TradeCreate, TradeOut, BrokerConfigIn, SettingsIn
from engine import engine
from instruments import store as instruments
from market_data import DhanMarketData
from brokers import verify_dhan_credentials

app = FastAPI(title="Algo Trading SaaS (India)")

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")


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
    db.close()
    instruments.load_async()   # download Dhan's symbol list in the background
    engine.start()


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
    t = Trade(**payload.model_dump())
    db.add(t)
    db.commit()
    db.refresh(t)
    db.add(LogEntry(message=f"Trade created: {t.symbol} [{t.mode}]", trade_id=t.id))
    db.commit()
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
    t.exit_fill_price = price
    t.status = "CLOSED"
    t.exit_reason = "MANUAL"
    direction = 1 if t.side == "BUY" else -1
    t.pnl = round((price - t.entry_fill_price) * direction * t.quantity, 2)
    db.add(LogEntry(message=f"Manual close {t.symbol} @ {price} P&L={t.pnl}", trade_id=t.id))
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
    cid = get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
    tok = get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
    items = payload.get("items", [])
    if not cid or not tok or not items:
        return {"connected": bool(cid and tok), "prices": {}}
    by_seg = {}
    for it in items:
        seg = it.get("exchange_segment")
        sid = str(it.get("security_id"))
        if seg and sid:
            by_seg.setdefault(seg, []).append(sid)
    md = DhanMarketData(cid, tok)
    res = md.get_ltp_batch(by_seg)
    prices = {sid: px for (seg, sid), px in res.items()}
    return {"connected": True, "prices": prices, "error": md.last_error}


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
    }


# ---------- logs ----------
@app.get("/api/logs")
def list_logs(limit: int = 100, db: Session = Depends(get_db)):
    rows = db.query(LogEntry).order_by(LogEntry.id.desc()).limit(limit).all()
    return [
        {"id": r.id, "time": r.created_at.isoformat(), "level": r.level,
         "trade_id": r.trade_id, "message": r.message}
        for r in rows
    ]


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
    return {
        "dhan_client_id": cid,
        # Never send the full token back to the browser; just say if it's set.
        "has_access_token": bool(tok),
        "connected": get_setting(db, "dhan_connected", "no") == "yes",
    }


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
