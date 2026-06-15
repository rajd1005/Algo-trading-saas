"""
Pydantic schemas = the shape of data going in/out of the web API.
"""
from typing import Optional, List
from pydantic import BaseModel


class TargetIn(BaseModel):
    points: float = 0.0     # distance from entry, in points
    qty: int = 0            # how much quantity to exit at this target


class TradeCreate(BaseModel):
    symbol: str
    security_id: str = ""
    exchange_segment: str = "NSE_FNO"
    instrument_type: str = "OPTION"
    side: str = "BUY"
    quantity: int = 1
    lot_size: int = 1
    leverage: float = 0.0             # legacy field, unused (Forex broker removed)
    entry_type: str = "MARKET"        # MARKET / LIMIT / SCHEDULED / TRIGGER
    entry_price: float = 0.0
    scheduled_time: str = ""          # "HH:MM:SS" IST (entry_type SCHEDULED)
    trigger_price: float = 0.0        # algo-tracked synthetic limit (entry_type TRIGGER)
    trigger_dir: str = ""             # ABOVE / BELOW
    sl_points: float = 0.0
    target_points: float = 0.0
    trail_sl: float = 0.0             # trailing stop distance in points (0 = off)
    trail_mode: str = "CONTINUE"      # CONTINUE or ENTRY (trail only up to breakeven)
    targets: List[TargetIn] = []      # optional scale-out targets
    max_profit_amt: float = 0.0       # trade-level square-off on MTM (₹)
    max_loss_amt: float = 0.0
    lock_step: float = 0.0            # auto profit-lock: for every ₹lock_step profit…
    lock_amount: float = 0.0          # …secure ₹lock_amount
    mode: str = "TEST"
    name: str = ""


class TradeOut(BaseModel):
    id: int
    name: str
    symbol: str
    security_id: str
    exchange_segment: str
    instrument_type: str
    side: str
    quantity: int
    lot_size: int
    entry_type: str
    entry_price: float
    scheduled_time: str
    trigger_price: float
    trigger_dir: str
    stop_loss: float
    target: float
    sl_points: float
    target_points: float
    trail_sl: float
    trail_mode: str
    hwm: float
    targets_json: str
    exited_qty: int
    realized_pnl: float
    max_profit_amt: float
    max_loss_amt: float
    lock_step: float
    lock_amount: float
    lock_floor: float
    mode: str
    status: str
    entry_fill_price: float
    exit_fill_price: float
    last_price: float
    pnl: float
    exit_reason: str
    broker_order_id: str
    broker: str
    source: str

    class Config:
        from_attributes = True


class ModifyTargetIn(BaseModel):
    price: float = 0.0      # absolute target price
    qty: int = 0            # quantity to exit at this target


class ModifyIn(BaseModel):
    """Edit stop-loss / targets / risk on a running or pending trade."""
    stop_loss: Optional[float] = None              # absolute price
    trail_sl: Optional[float] = None               # trailing distance in points (0 = off)
    trail_mode: Optional[str] = None               # CONTINUE or ENTRY
    targets: Optional[List[ModifyTargetIn]] = None  # absolute-price scale-out targets
    max_profit_amt: Optional[float] = None         # trade-level MTM square-off (₹); 0 = off
    max_loss_amt: Optional[float] = None
    lock_step: Optional[float] = None              # auto profit-lock step (₹); 0 = off
    lock_amount: Optional[float] = None
    # Pending-only edits:
    scheduled_time: Optional[str] = None           # "HH:MM:SS" IST
    trigger_price: Optional[float] = None
    trigger_dir: Optional[str] = None


class BrokerConfigIn(BaseModel):
    dhan_client_id: str = ""
    dhan_access_token: str = ""


class SettingsIn(BaseModel):
    kill_switch: Optional[bool] = None
    default_mode: Optional[str] = None
    daily_max_profit: Optional[float] = None       # account/day square-off & halt (₹); 0 = off
    daily_max_loss: Optional[float] = None
    global_lock_step: Optional[float] = None       # account-level auto profit-lock step (₹)
    global_lock_amount: Optional[float] = None
    auto_squareoff_time: Optional[str] = None      # "HH:MM:SS" IST; "" / "off" disables


class WatchlistIn(BaseModel):
    symbol: str
    security_id: str = ""
    exchange_segment: str = ""
    instrument_type: str = ""
    underlying: str = ""
    lot_size: int = 1


class SymbolPresetIn(BaseModel):
    symbol: str
    kind: str = "OPTION"        # OPTION / FUTURES / EQUITY
    lots: int = 0
    sl_points: float = 0.0
    trail_sl: float = 0.0
    trail_mode: str = "CONTINUE"
    target_points: float = 0.0
    targets: List[float] = []        # multi-target points (cumulative), e.g. [100, 100]
    max_profit_amt: float = 0.0
    max_loss_amt: float = 0.0
    lock_step: float = 0.0
    lock_amount: float = 0.0
