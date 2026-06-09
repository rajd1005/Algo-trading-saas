"""
Brokers = "actually place / exit an order."

  - PaperBroker : TEST mode. Pretends to fill the order at the current price.
                  No real money, no Dhan account needed.
  - DhanBroker  : LIVE mode. Sends a real order to Dhan via their REST API.

Both expose the same two methods (place_entry / place_exit) so the engine
doesn't care which one it's talking to.
"""
import time

import requests

import config


class OrderResult:
    def __init__(self, ok: bool, fill_price: float = 0.0, order_id: str = "",
                 error: str = "", status: str = "TRADED", traded_qty: int = 0):
        self.ok = ok
        self.fill_price = fill_price
        self.order_id = order_id
        self.error = error
        self.status = status          # TRADED / PENDING / REJECTED / ...
        self.traded_qty = traded_qty


def _extract_reason(resp):
    """Pull the human-readable rejection reason out of a broker response."""
    try:
        d = resp.json()
        if isinstance(d, dict):
            for k in ("errorMessage", "message", "error_desc", "reject_reason",
                      "omsErrorDescription", "remarks", "errorType"):
                if d.get(k):
                    return str(d[k])
        return str(d)[:300]
    except Exception:
        try:
            return resp.text[:300]
        except Exception:
            return "Unknown broker error"


class PaperBroker:
    """Simulated broker for TEST / DEMO mode."""

    name = "PAPER"

    def place_entry(self, trade, current_price: float, qty=None) -> OrderResult:
        # Fill instantly at the current (live) price.
        return OrderResult(ok=True, fill_price=current_price, order_id=f"PAPER-E-{trade.id}",
                           status="TRADED", traded_qty=int(qty or trade.quantity))

    def place_exit(self, trade, current_price: float, qty=None) -> OrderResult:
        return OrderResult(ok=True, fill_price=current_price, order_id=f"PAPER-X-{trade.id}",
                           status="TRADED", traded_qty=int(qty or trade.quantity))

    def order_status(self, order_id):
        return ("TRADED", 0.0, 0, "")

    def confirm(self, order_id):
        return ("TRADED", 0.0, 0, "")


class DhanBroker:
    """Real broker for LIVE mode (Dhan API v2)."""

    name = "DHAN"

    def __init__(self, client_id: str, access_token: str):
        self.client_id = client_id
        self.access_token = access_token

    def _headers(self):
        return {
            "access-token": self.access_token,
            "client-id": self.client_id,
            "Content-Type": "application/json",
        }

    def _place(self, trade, side: str, current_price: float, qty=None) -> OrderResult:
        """Send one order to Dhan. `side` is BUY or SELL."""
        url = f"{config.DHAN_API_BASE}/orders"
        order_type = "LIMIT" if trade.entry_type == "LIMIT" else "MARKET"
        payload = {
            "dhanClientId": self.client_id,
            "transactionType": side,
            "exchangeSegment": trade.exchange_segment,
            "productType": "INTRADAY",
            "orderType": order_type,
            "validity": "DAY",
            "securityId": str(trade.security_id),
            "quantity": int(qty if qty else trade.quantity),
            "price": float(trade.entry_price) if order_type == "LIMIT" else 0,
        }
        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=8)
        except Exception as e:
            # No response = network/timeout problem -> retryable.
            return OrderResult(ok=False, error=f"Network error: {e}", status="ERROR")
        if r.status_code >= 400:
            # The broker gave a definitive rejection (e.g. insufficient funds).
            return OrderResult(ok=False, error=_extract_reason(r), status="REJECTED")
        try:
            data = r.json()
        except Exception:
            return OrderResult(ok=False, error="Unreadable broker response", status="ERROR")
        order_id = str(data.get("orderId", ""))
        status = str(data.get("orderStatus", "PENDING")).upper()
        # current_price is provisional; confirm() then fetches the real fill.
        return OrderResult(ok=True, fill_price=current_price, order_id=order_id, status=status)

    def place_entry(self, trade, current_price: float, qty=None) -> OrderResult:
        return self._place(trade, trade.side, current_price, qty)

    def place_exit(self, trade, current_price: float, qty=None) -> OrderResult:
        # Exit is the opposite of the entry side.
        exit_side = "SELL" if trade.side == "BUY" else "BUY"
        return self._place(trade, exit_side, current_price, qty)

    def order_status(self, order_id):
        """Fetch one order's status, real traded price/qty, and reject reason."""
        url = f"{config.DHAN_API_BASE}/orders/{order_id}"
        try:
            r = requests.get(url, headers=self._headers(), timeout=6)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):           # Dhan returns a list for this call
                data = data[0] if data else {}
            status = str(data.get("orderStatus", "")).upper()
            traded_price = float(data.get("averageTradedPrice") or data.get("price") or 0)
            traded_qty = int(data.get("filledQty") or data.get("tradedQty") or 0)
            reason = (data.get("omsErrorDescription") or data.get("text")
                      or data.get("errorMessage") or "")
            return status, traded_price, traded_qty, reason
        except Exception:
            return "", 0.0, 0, ""

    def confirm(self, order_id):
        """Poll until the order reaches a final state (TRADED/REJECTED/...)."""
        last = ("", 0.0, 0, "")
        for _ in range(config.ORDER_POLLS):
            last = self.order_status(order_id)
            if last[0] in ("TRADED", "REJECTED", "CANCELLED", "EXPIRED"):
                return last
            time.sleep(config.ORDER_POLL_DELAY)
        return last

    def fund_limit(self):
        """Return (ok, available_balance) from Dhan."""
        url = f"{config.DHAN_API_BASE}/fundlimit"
        try:
            r = requests.get(url, headers=self._headers(), timeout=6)
            r.raise_for_status()
            d = r.json()
            bal = (d.get("availabelBalance")          # Dhan's known spelling
                   or d.get("availableBalance")
                   or d.get("withdrawableBalance") or 0)
            return True, float(bal)
        except Exception:
            return False, 0.0

    def get_positions(self):
        """Return the list of open positions held at Dhan (for external sync)."""
        url = f"{config.DHAN_API_BASE}/positions"
        try:
            r = requests.get(url, headers=self._headers(), timeout=6)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def get_orders(self):
        """Return today's full order book from Dhan (all statuses)."""
        url = f"{config.DHAN_API_BASE}/orders"
        try:
            r = requests.get(url, headers=self._headers(), timeout=6)
            r.raise_for_status()
            data = r.json()
            return data if isinstance(data, list) else []
        except Exception:
            return []


def verify_dhan_credentials(client_id: str, access_token: str) -> tuple[bool, str]:
    """
    Check that a Dhan client id + access token are valid by calling a read-only
    endpoint (fund limits). Used by the 'Connect / Authenticate' button.
    """
    url = f"{config.DHAN_API_BASE}/fundlimit"
    headers = {
        "access-token": access_token,
        "client-id": client_id,
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code == 200:
            return True, "Connected to Dhan successfully."
        return False, f"Dhan rejected the credentials (HTTP {r.status_code}): {r.text}"
    except Exception as e:
        return False, f"Could not reach Dhan: {e}"
