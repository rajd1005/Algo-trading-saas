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
    entry_type: str = "MARKET"
    entry_price: float = 0.0
    sl_points: float = 0.0
    target_points: float = 0.0
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
    entry_type: str
    entry_price: float
    stop_loss: float
    target: float
    sl_points: float
    target_points: float
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


class ModifyIn(BaseModel):
    """Edit stop-loss / target (in points) on a running trade."""
    sl_points: Optional[float] = None
    target_points: Optional[float] = None


class BrokerConfigIn(BaseModel):
    dhan_client_id: str = ""
    dhan_access_token: str = ""


class SettingsIn(BaseModel):
    kill_switch: Optional[bool] = None
    default_mode: Optional[str] = None
