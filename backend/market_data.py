"""
Market data = the real Last Traded Price (LTP) from Dhan.

Both TEST (paper) and LIVE trades use these REAL prices — the only difference
is that TEST mode does not place a real order. So you need Dhan connected for
either mode to work (LTP comes from Dhan).

To be efficient we fetch the LTP of ALL active trades in ONE request per tick,
grouped by exchange segment, instead of one request per symbol.
"""
import math
import random
import threading
import time

import requests

import config
from instruments import store


class DhanMarketData:
    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.access_token = access_token
        self.last_error = ""

    def _headers(self):
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
        }

    def get_ltp_batch(self, by_segment: dict) -> dict:
        """
        Input : {"NSE_FNO": ["49081", ...], "NSE_EQ": ["2885"], ...}
        Output: {("NSE_FNO", "49081"): 123.45, ...}
        """
        # Build the request body Dhan expects: integers grouped by segment.
        payload = {}
        for seg, ids in by_segment.items():
            clean = [int(i) for i in ids if str(i).strip().isdigit()]
            if clean:
                payload[seg] = clean
        if not payload:
            return {}

        url = f"{config.DHAN_API_BASE}/marketfeed/ltp"
        out = {}
        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=6)
            r.raise_for_status()
            data = r.json().get("data", {})
            for seg, items in data.items():
                for sec_id, info in items.items():
                    price = info.get("last_price")
                    if price is not None:
                        out[(seg, str(sec_id))] = float(price)
            self.last_error = ""
        except Exception as e:
            detail = ""
            try:
                detail = r.text  # type: ignore
            except Exception:
                pass
            self.last_error = f"{e} {detail}".strip()
        return out

    def get_ltp(self, exchange_segment: str, security_id: str) -> float:
        res = self.get_ltp_batch({exchange_segment: [security_id]})
        return res.get((exchange_segment, str(security_id)), 0.0)


class DemoMarketData:
    """
    Fully simulated prices for DEMO mode — no Dhan, no subscription, no real money.
    Options are priced realistically (intrinsic value + a bell-shaped time value
    peaking at-the-money) so the option chain, ATM detection, stop-loss and target
    all behave like the real thing.
    """
    def __init__(self):
        self._spot = {}        # underlying -> current simulated spot
        self._base = {}        # security_id -> base price (non-options)
        self._lock = threading.Lock()
        self.last_error = ""
        self.direction = 0     # +1 = drift up, -1 = drift down, 0 = flat/random

    def set_direction(self, d):
        self.direction = 1 if d > 0 else (-1 if d < 0 else 0)

    def reset(self):
        with self._lock:
            self._spot.clear()
            self._base.clear()

    def _drift(self):
        # ~0.15% per tick push in the chosen direction (so SL/targets are reachable fast)
        return 0.0015 * self.direction

    def _spot_for(self, underlying):
        with self._lock:
            if underlying not in self._spot:
                seed = store.spot_seed(underlying) or 1000.0
                self._spot[underlying] = seed
            # gentle random walk + optional directional drift
            self._spot[underlying] *= (1 + self._drift() + random.uniform(-0.00025, 0.00025))
            return self._spot[underlying]

    def _base_for(self, security_id):
        with self._lock:
            if security_id not in self._base:
                self._base[security_id] = random.uniform(100, 1500)
            self._base[security_id] *= (1 + self._drift() + random.uniform(-0.0004, 0.0004))
            return self._base[security_id]

    def _price(self, security_id, spot_cache):
        meta = store.get_meta(security_id)
        if meta and meta["instrument_type"] == "OPTION" and meta["strike"] > 0:
            u = meta["underlying"]
            spot = spot_cache.get(u)
            if spot is None:
                spot = spot_cache[u] = self._spot_for(u)
            strike = meta["strike"]
            if meta["option_type"] == "CE":
                intrinsic = max(0.0, spot - strike)
            else:
                intrinsic = max(0.0, strike - spot)
            width = max(spot * 0.04, 1.0)
            tv = spot * 0.015 * math.exp(-((strike - spot) / width) ** 2) + spot * 0.002
            price = (intrinsic + tv) * (1 + random.uniform(-0.0015, 0.0015))
            return round(max(0.05, price), 2)
        # equities / futures / index -> simple random walk
        return round(self._base_for(security_id), 2)

    def get_ltp_batch(self, by_segment: dict) -> dict:
        out = {}
        spot_cache = {}            # one walked spot per underlying per call
        for seg, ids in by_segment.items():
            for sid in ids:
                out[(seg, str(sid))] = self._price(str(sid), spot_cache)
        return out

    def get_ltp(self, exchange_segment: str, security_id: str) -> float:
        return self.get_ltp_batch({exchange_segment: [security_id]}).get(
            (exchange_segment, str(security_id)), 0.0)


# Shared instances.
demo_market = DemoMarketData()
