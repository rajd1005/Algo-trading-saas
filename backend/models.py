"""
Database tables (models).

The main one is `Trade` — a single instruction that says:
"enter this symbol at X, with stop-loss at Y and target at Z, in TEST or LIVE mode."
The engine watches each Trade and moves it through these states automatically:

    PENDING  -> waiting for the entry condition to be met
    OPEN     -> we are in the position; engine watches SL / target
    CLOSED   -> exited (hit SL, hit target, or manually closed)
    CANCELLED-> cancelled before entry
"""
import datetime as dt

from sqlalchemy import Column, Integer, String, Float, DateTime, Text

from database import Base

# India time, for grouping logs by trading day.
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _now():
    return dt.datetime.utcnow()


def _ist_date():
    return dt.datetime.now(IST).strftime("%Y-%m-%d")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="")              # a friendly label

    # --- What to trade ---
    symbol = Column(String, nullable=False)        # e.g. NIFTY 25000 CE, RELIANCE
    security_id = Column(String, default="")       # Dhan's id (needed for LIVE)
    exchange_segment = Column(String, default="NSE_FNO")  # NSE_EQ / NSE_FNO / etc.
    instrument_type = Column(String, default="OPTION")    # OPTION / FUTURES / EQUITY
    side = Column(String, default="BUY")           # BUY (long) or SELL (short)
    quantity = Column(Integer, default=1)
    lot_size = Column(Integer, default=1)           # contract lot size (for lots math)

    # --- Entry / Exit rules ---
    entry_type = Column(String, default="MARKET")  # MARKET or LIMIT
    entry_price = Column(Float, default=0.0)       # trigger/limit price for entry
    stop_loss = Column(Float, default=0.0)         # absolute price (computed from points)
    target = Column(Float, default=0.0)            # absolute price (computed from points)

    # Stop-loss / target are entered as POINTS; the absolute prices above are
    # computed from the actual entry fill price when the trade enters.
    sl_points = Column(Float, default=0.0)
    target_points = Column(Float, default=0.0)
    trail_sl = Column(Float, default=0.0)          # trailing stop distance in points (0 = off)
    trail_mode = Column(String, default="CONTINUE")  # CONTINUE or ENTRY (trail only up to breakeven)
    hwm = Column(Float, default=0.0)               # high-water mark for trailing
    # Optional multiple (scale-out) targets, JSON: [{"points":x,"qty":n,"hit":false}]
    targets_json = Column(Text, default="")
    exited_qty = Column(Integer, default=0)        # qty already booked via partial targets
    realized_pnl = Column(Float, default=0.0)      # P&L locked in from partial exits

    mode = Column(String, default="TEST")          # TEST or LIVE

    # --- Live state, updated by the engine ---
    status = Column(String, default="PENDING")     # PENDING/OPEN/CLOSED/CANCELLED
    entry_fill_price = Column(Float, default=0.0)
    exit_fill_price = Column(Float, default=0.0)
    last_price = Column(Float, default=0.0)        # most recent seen price
    pnl = Column(Float, default=0.0)
    exit_reason = Column(String, default="")       # TARGET / STOPLOSS / MANUAL / KILL

    broker_order_id = Column(String, default="")   # id returned by broker (LIVE)
    broker = Column(String, default="")             # which broker executed it (PAPER/DHAN)
    source = Column(String, default="ALGO")         # ALGO or EXTERNAL (synced from broker)

    created_at = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class LogEntry(Base):
    """A simple audit trail of everything the engine does."""
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    created_at = Column(DateTime, default=_now)
    day = Column(String, default=_ist_date, index=True)   # India date, for day-wise view
    level = Column(String, default="INFO")         # INFO / WARN / ERROR
    trade_id = Column(Integer, default=0)
    message = Column(Text, default="")


class Setting(Base):
    """Key/value store for global settings (kill switch, broker creds, etc.)."""
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, default="")


class Account(Base):
    """A configured broker login (Dhan or Angel). Users can add several."""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    broker = Column(String, default="DHAN")        # DHAN / ANGEL
    client_id = Column(String, default="")
    label = Column(String, default="")             # e.g. "Dhan · 1100000000"
    creds_json = Column(Text, default="{}")        # broker-specific secrets/tokens
    connected = Column(Integer, default=0)
    token_time = Column(DateTime, default=None, nullable=True)
    created_at = Column(DateTime, default=_now)
