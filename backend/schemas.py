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
    entry_type: str = "MARKET"
    entry_price: float = 0.0
    sl_points: float = 0.0
    target_points: float = 0.0
    trail_sl: float = 0.0             # trailing stop distance in points (0 = off)
    targets: List[TargetIn] = []      # optional scale-out targets
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
    stop_loss: float
    target: float
    sl_points: float
    target_points: float
    trail_sl: float
    hwm: float
    targets_json: str
    exited_qty: int
    realized_pnl: float
    mode: str
    status: str
    entry_fill_price: float
    exit_fill_price: float
    last_price: float
    pnl: float
    exit_reason: str
    broker_order_id: str

    class Config:
        from_attributes = True


class ModifyTargetIn(BaseModel):
    price: float = 0.0      # absolute target price
    qty: int = 0            # quantity to exit at this target


class ModifyIn(BaseModel):
    """Edit stop-loss / targets on a running trade, using DIRECT prices."""
    stop_loss: Optional[float] = None              # absolute price
    trail_sl: Optional[float] = None               # trailing distance in points (0 = off)
    targets: Optional[List[ModifyTargetIn]] = None  # absolute-price scale-out targets


class BrokerConfigIn(BaseModel):
    dhan_client_id: str = ""
    dhan_access_token: str = ""


class SettingsIn(BaseModel):
    kill_switch: Optional[bool] = None
    default_mode: Optional[str] = None
