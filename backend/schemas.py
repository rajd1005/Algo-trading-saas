"""
Pydantic schemas = the shape of data going in/out of the web API.
"""
from typing import Optional
from pydantic import BaseModel


class TradeCreate(BaseModel):
    name: str = ""
    symbol: str
    security_id: str = ""
    exchange_segment: str = "NSE_FNO"
    instrument_type: str = "OPTION"
    side: str = "BUY"
    quantity: int = 1
    entry_type: str = "MARKET"
    entry_price: float = 0.0
    stop_loss: float = 0.0
    target: float = 0.0
    mode: str = "TEST"


class TradeOut(TradeCreate):
    id: int
    status: str
    entry_fill_price: float
    exit_fill_price: float
    last_price: float
    pnl: float
    exit_reason: str
    broker_order_id: str

    class Config:
        from_attributes = True


class BrokerConfigIn(BaseModel):
    dhan_client_id: str = ""
    dhan_access_token: str = ""


class SettingsIn(BaseModel):
    kill_switch: Optional[bool] = None
    default_mode: Optional[str] = None
