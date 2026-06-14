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
import uuid as _uuid

from sqlalchemy import Column, Integer, String, Float, DateTime, Text

from database import Base


def _uuid4():
    return _uuid.uuid4().hex


class User(Base):
    """A tenant account. All trades / accounts / settings are scoped to a user."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    uuid = Column(String, default=_uuid4, unique=True, index=True)   # for webhook URLs
    email = Column(String, unique=True, index=True, default="")
    password_hash = Column(String, default="")
    role = Column(String, default="USER")             # USER / SUPER_ADMIN
    status = Column(String, default="ACTIVE")         # ACTIVE / BLOCKED
    plan_name = Column(String, default="Trial")
    plan_expiry = Column(DateTime, default=None, nullable=True)
    session_token = Column(String, default="")        # current device (1-device login)
    created_at = Column(DateTime, default=lambda: dt.datetime.utcnow())


class Plan(Base):
    """Admin-defined subscription plan (duration-based)."""
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, default="")
    days = Column(Integer, default=30)
    price = Column(Float, default=0.0)
    active = Column(Integer, default=1)


class OtpCode(Base):
    """A short-lived email OTP for registration / password reset."""
    __tablename__ = "otp_codes"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, index=True, default="")
    code_hash = Column(String, default="")
    purpose = Column(String, default="REGISTER")      # REGISTER / RESET
    expires_at = Column(DateTime, default=None, nullable=True)
    attempts = Column(Integer, default=0)
    created_at = Column(DateTime, default=lambda: dt.datetime.utcnow())


class EmailTemplate(Base):
    """Admin-editable subject/body for each system email."""
    __tablename__ = "email_templates"

    key = Column(String, primary_key=True)            # otp_register / otp_reset / welcome / expiry
    subject = Column(String, default="")
    body_html = Column(Text, default="")


class UserSetting(Base):
    """Per-user key/value settings (kill switch, providers, daily limits, etc.)."""
    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, default=0)
    key = Column(String, index=True, default="")
    value = Column(Text, default="")

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
    leverage = Column(Float, default=0.0)           # forex/crypto leverage (0 = broker default)

    # --- Entry / Exit rules ---
    entry_type = Column(String, default="MARKET")  # MARKET / LIMIT / SCHEDULED / TRIGGER
    entry_price = Column(Float, default=0.0)       # trigger/limit price for entry
    stop_loss = Column(Float, default=0.0)         # absolute price (computed from points)
    target = Column(Float, default=0.0)            # absolute price (computed from points)

    # --- Scheduling / algo-tracked triggers (apply while PENDING) ---
    scheduled_time = Column(String, default="")    # "HH:MM:SS" IST; push market at this time
    trigger_price = Column(Float, default=0.0)     # synthetic-limit price tracked by the algo
    trigger_dir = Column(String, default="")       # ABOVE / BELOW (which way LTP must cross)

    # --- Trade-level monetary risk (on this trade's live MTM) ---
    max_profit_amt = Column(Float, default=0.0)    # auto square-off if MTM >= this (₹)
    max_loss_amt = Column(Float, default=0.0)      # auto square-off if MTM <= -this (₹)
    # Auto step profit-lock: "for every ₹lock_step profit, secure ₹lock_amount".
    lock_step = Column(Float, default=0.0)
    lock_amount = Column(Float, default=0.0)
    profit_lock_json = Column(Text, default="")    # legacy (tiers); no longer used
    lock_floor = Column(Float, default=0.0)        # currently-armed locked-profit floor (₹)

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

    broker_order_id = Column(String, default="")   # id returned by broker (LIVE) — entry
    exit_order_id = Column(String, default="")      # broker id of OUR exit order (so the
    #                                                 external-order sync never mirrors our
    #                                                 own square-off back as a new trade)
    # Position-netted brokers (Delta) have no per-order open/closed status, so we
    # confirm the broker is actually HOLDING this trade's position at least once
    # before we ever let a 'flat product' reading close it. This stops a freshly
    # RE-OPENED symbol from being wrongly marked CLOSED off an old/stale position.
    broker_pos_seen = Column(Integer, default=0)    # 1 once the live position was seen
    broker = Column(String, default="")             # which broker executed it (PAPER/DHAN/ANGEL)
    account_id = Column(Integer, default=0)         # which broker account (0 = Demo/paper)
    user_id = Column(Integer, index=True, default=0)  # tenant owner
    source = Column(String, default="ALGO")         # ALGO / EXTERNAL / BASKET / SLAVE / MASTER
    basket_id = Column(Integer, default=0, index=True)  # owning basket (0 = standalone)
    leg_id = Column(Integer, default=0)             # owning basket leg
    group_id = Column(Integer, default=0, index=True)   # replication group (slave trades)
    master_trade_id = Column(Integer, default=0)    # the master Trade this slave mirrors
    repl_entry = Column(Integer, default=0)         # master: entry already fanned out to slaves
    repl_exit = Column(Integer, default=0)          # master: exit already fanned out to slaves

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
    user_id = Column(Integer, index=True, default=0)   # tenant owner (0 = system/global)
    message = Column(Text, default="")


class Setting(Base):
    """Key/value store for global settings (kill switch, broker creds, etc.)."""
    __tablename__ = "settings"

    key = Column(String, primary_key=True)
    value = Column(Text, default="")


class Account(Base):
    """A configured broker login (Dhan / Angel / Zerodha / Alice). Several allowed."""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, default=0)   # tenant owner
    broker = Column(String, default="DHAN")        # DHAN / ANGEL / ZERODHA / ALICE
    client_id = Column(String, default="")
    label = Column(String, default="")             # e.g. "Dhan · 1100000000"
    creds_json = Column(Text, default="{}")        # broker-specific secrets/tokens
    connected = Column(Integer, default=0)
    token_time = Column(DateTime, default=None, nullable=True)
    created_at = Column(DateTime, default=_now)


class SymbolPreset(Base):
    """Per-symbol default trade settings, auto-filled into the New Trade form."""
    __tablename__ = "symbol_presets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, default=0)   # tenant owner
    symbol = Column(String, index=True, default="")    # underlying, UPPER (BANKNIFTY…)
    kind = Column(String, default="OPTION")            # OPTION / FUTURES / EQUITY
    lots = Column(Integer, default=0)                  # default lots to pre-fill (0 = keep)
    sl_points = Column(Float, default=0.0)
    trail_sl = Column(Float, default=0.0)
    trail_mode = Column(String, default="CONTINUE")
    target_points = Column(Float, default=0.0)         # single target (points)
    targets_json = Column(Text, default="")            # multi-target points list, e.g. [100,100]
    max_profit_amt = Column(Float, default=0.0)
    max_loss_amt = Column(Float, default=0.0)
    lock_step = Column(Float, default=0.0)             # auto profit-lock: every ₹step…
    lock_amount = Column(Float, default=0.0)           # …secure ₹amount
    profit_lock_json = Column(Text, default="")        # legacy (tiers); no longer used
    created_at = Column(DateTime, default=_now)


class Basket(Base):
    """A named group of order legs that fire together (instantly or scheduled)."""
    __tablename__ = "user_baskets"

    id = Column(Integer, primary_key=True, index=True)   # basket_id
    user_id = Column(Integer, index=True, default=0)     # tenant owner
    name = Column(String, default="")                    # basket_name
    mode = Column(String, default="TEST")                # TEST (paper) / LIVE
    is_active = Column(Integer, default=1)

    # --- scheduling ---
    is_scheduled = Column(Integer, default=0)
    scheduled_at = Column(DateTime, default=None, nullable=True)   # UTC instant to fire
    schedule_status = Column(String, default="")         # PENDING/EXECUTED/FAILED/CANCELLED
    timezone = Column(String, default="IST")

    # --- basket-level step profit-lock on the COMBINED MTM ---
    lock_step = Column(Float, default=0.0)               # for every ₹step of basket profit…
    lock_amount = Column(Float, default=0.0)             # …secure ₹amount
    lock_floor = Column(Float, default=0.0)              # currently-armed secured floor (₹)

    # --- execution bookkeeping ---
    exec_status = Column(String, default="")             # DISPATCHING/EXECUTED/PARTIAL/FAILED
    last_exec_at = Column(DateTime, default=None, nullable=True)
    created_at = Column(DateTime, default=_now)


class BasketLeg(Base):
    """One order inside a basket. Becomes a Trade row when the basket executes."""
    __tablename__ = "basket_legs"

    id = Column(Integer, primary_key=True, index=True)   # leg_id
    basket_id = Column(Integer, index=True, default=0)
    user_id = Column(Integer, index=True, default=0)
    seq = Column(Integer, default=0)                     # order within the basket

    symbol = Column(String, default="")                  # universal symbol label
    security_id = Column(String, default="")             # Dhan-space security id
    exchange_segment = Column(String, default="")        # NSE_EQ / NSE_FNO / …
    instrument_type = Column(String, default="OPTION")   # OPTION / FUTURES / EQUITY / INDEX
    underlying = Column(String, default="")
    lot_size = Column(Integer, default=1)

    transaction_type = Column(String, default="BUY")     # BUY / SELL
    order_type = Column(String, default="MARKET")        # legacy display: MARKET / LIMIT / SL
    quantity = Column(Integer, default=1)
    price = Column(Float, default=0.0)                   # entry / limit price
    trigger_price = Column(Float, default=0.0)           # algo trigger price

    # --- full per-leg trade config (mirrors the New Trade form) ---
    entry_type = Column(String, default="MARKET")        # MARKET / LIMIT / SCHEDULED / TRIGGER
    scheduled_time = Column(String, default="")          # "HH:MM:SS" IST (entry_type SCHEDULED)
    trigger_dir = Column(String, default="")             # ABOVE / BELOW (algo trigger)
    sl_points = Column(Float, default=0.0)
    target_points = Column(Float, default=0.0)
    trail_sl = Column(Float, default=0.0)
    trail_mode = Column(String, default="CONTINUE")
    targets_json = Column(Text, default="")              # scale-out targets [{points,qty}]
    max_profit_amt = Column(Float, default=0.0)
    max_loss_amt = Column(Float, default=0.0)
    lock_step = Column(Float, default=0.0)
    lock_amount = Column(Float, default=0.0)

    # --- live state ---
    status = Column(String, default="PENDING")           # PENDING/EXECUTED/FAILED/CANCELLED/CLOSED
    trade_id = Column(Integer, default=0)                # linked Trade row
    broker_order_id = Column(String, default="")
    fill_price = Column(Float, default=0.0)
    error = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class Watchlist(Base):
    """Saved instruments for one-click loading into the New Trade form."""
    __tablename__ = "watchlist"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, default=0)   # tenant owner
    symbol = Column(String, default="")
    security_id = Column(String, default="")
    exchange_segment = Column(String, default="")
    instrument_type = Column(String, default="")       # OPTION / FUTURES / EQUITY / INDEX
    underlying = Column(String, default="")            # for preset matching on load
    lot_size = Column(Integer, default=1)
    created_at = Column(DateTime, default=_now)


class ExecutionGroup(Base):
    """A copy-trading group: one Master account whose fills are replicated to slaves."""
    __tablename__ = "execution_groups"

    id = Column(Integer, primary_key=True, index=True)   # group_id
    user_id = Column(Integer, index=True, default=0)
    name = Column(String, default="")
    master_account_id = Column(Integer, default=0)       # 0 = Demo / paper master
    is_active = Column(Integer, default=1)               # master ON/OFF for the whole group
    created_at = Column(DateTime, default=_now)


class GroupSlave(Base):
    """A follower account inside a group, with its sizing rule."""
    __tablename__ = "group_slaves"

    id = Column(Integer, primary_key=True, index=True)   # slave_row_id
    group_id = Column(Integer, index=True, default=0)
    user_id = Column(Integer, index=True, default=0)
    slave_account_id = Column(Integer, default=0)        # 0 = Demo / paper slave
    condition_type = Column(String, default="MULTIPLIER")  # MULTIPLIER / FIXED
    condition_value = Column(Float, default=1.0)         # multiplier (x lots) or fixed lots
    is_active = Column(Integer, default=1)               # individual slave ON/OFF
    created_at = Column(DateTime, default=_now)


class GroupTradeLog(Base):
    """Audit map of each parent (master) fill to its child (slave) executions."""
    __tablename__ = "group_trades_log"

    id = Column(Integer, primary_key=True, index=True)   # log_id
    group_id = Column(Integer, index=True, default=0)
    user_id = Column(Integer, index=True, default=0)
    master_trade_id = Column(Integer, default=0)
    master_order_id = Column(String, default="")
    slave_account_id = Column(Integer, default=0)
    slave_trade_id = Column(Integer, default=0)
    slave_order_id = Column(String, default="")
    side = Column(String, default="")                    # BUY / SELL
    action = Column(String, default="ENTRY")             # ENTRY / EXIT
    status = Column(String, default="PENDING")           # PENDING / EXECUTED / FAILED
    error_message = Column(String, default="")
    created_at = Column(DateTime, default=_now)


class GroupScheduledOrder(Base):
    """A master order queued to fire for a whole group at an exact time (IST)."""
    __tablename__ = "group_scheduled_orders"

    id = Column(Integer, primary_key=True, index=True)
    group_id = Column(Integer, index=True, default=0)
    user_id = Column(Integer, index=True, default=0)
    side = Column(String, default="BUY")
    symbol = Column(String, default="")
    security_id = Column(String, default="")
    exchange_segment = Column(String, default="")
    instrument_type = Column(String, default="OPTION")
    lot_size = Column(Integer, default=1)
    qty_lots = Column(Integer, default=1)
    scheduled_at = Column(DateTime, default=None, nullable=True)   # UTC
    status = Column(String, default="PENDING")           # PENDING / EXECUTED / CANCELLED / FAILED
    created_at = Column(DateTime, default=_now)
