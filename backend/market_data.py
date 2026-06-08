"""
Market data = the real Last Traded Price (LTP) from Dhan.

Both TEST (paper) and LIVE trades use these REAL prices — the only difference
is that TEST mode does not place a real order. So you need Dhan connected for
either mode to work (LTP comes from Dhan).

To be efficient we fetch the LTP of ALL active trades in ONE request per tick,
grouped by exchange segment, instead of one request per symbol.
"""
import time

import requests

import config


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
