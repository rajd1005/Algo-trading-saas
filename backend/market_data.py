"""
Market data = "what is the current price of this symbol right now?"

Two providers:
  - SimMarketData : for TEST mode. Generates a realistic random-walk price so you
                    can watch entries, stop-losses and targets trigger without any
                    real broker or real money.
  - DhanMarketData: for LIVE mode. Asks Dhan for the real Last Traded Price (LTP).
"""
import random
import threading
import time

import requests

import config


class SimMarketData:
    """A fake but realistic market for TEST mode."""

    def __init__(self):
        self._prices = {}          # symbol -> current price
        self._base = {}            # symbol -> starting price
        self._lock = threading.Lock()

    def _ensure(self, symbol: str, hint_price: float = 0.0):
        if symbol not in self._prices:
            # Start near the user's entry price if given, else a sensible default.
            start = hint_price if hint_price > 0 else random.uniform(100, 500)
            self._prices[symbol] = start
            self._base[symbol] = start

    def get_ltp(self, symbol: str, hint_price: float = 0.0) -> float:
        """Return a price that drifts a little each call (a random walk)."""
        with self._lock:
            self._ensure(symbol, hint_price)
            price = self._prices[symbol]
            # Move by up to ~0.4% each tick, gently pulled toward the base price.
            drift = (self._base[symbol] - price) * 0.01
            shock = price * random.uniform(-0.004, 0.004)
            price = max(0.05, price + drift + shock)
            self._prices[symbol] = price
            return round(price, 2)

    def nudge(self, symbol: str, price: float):
        """Manually set a price (used by the 'simulate price' button)."""
        with self._lock:
            self._prices[symbol] = price
            self._base.setdefault(symbol, price)


class DhanMarketData:
    """Real prices from Dhan (LIVE mode)."""

    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.access_token = access_token
        self._cache = {}           # (segment, security_id) -> (price, ts)

    def get_ltp(self, exchange_segment: str, security_id: str) -> float:
        """Fetch the last traded price for one instrument from Dhan."""
        if not security_id:
            return 0.0
        # tiny cache to avoid hammering the API every single tick
        key = (exchange_segment, security_id)
        now = time.time()
        cached = self._cache.get(key)
        if cached and now - cached[1] < 0.5:
            return cached[0]

        url = f"{config.DHAN_API_BASE}/marketfeed/ltp"
        headers = {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
        }
        payload = {exchange_segment: [int(security_id)]}
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=5)
            r.raise_for_status()
            data = r.json().get("data", {})
            seg = data.get(exchange_segment, {})
            ltp = float(seg.get(str(security_id), {}).get("last_price", 0.0))
            self._cache[key] = (ltp, now)
            return ltp
        except Exception:
            # On any error, fall back to the last known price (don't crash engine).
            return cached[0] if cached else 0.0


# A single shared simulator instance for the whole app.
sim_market = SimMarketData()
