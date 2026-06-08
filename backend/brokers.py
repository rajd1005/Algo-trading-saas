"""
Brokers = "actually place / exit an order."

  - PaperBroker : TEST mode. Pretends to fill the order at the current price.
                  No real money, no Dhan account needed.
  - DhanBroker  : LIVE mode. Sends a real order to Dhan via their REST API.

Both expose the same two methods (place_entry / place_exit) so the engine
doesn't care which one it's talking to.
"""
import requests

import config


class OrderResult:
    def __init__(self, ok: bool, fill_price: float = 0.0, order_id: str = "", error: str = ""):
        self.ok = ok
        self.fill_price = fill_price
        self.order_id = order_id
        self.error = error


class PaperBroker:
    """Simulated broker for TEST mode."""

    name = "PAPER"

    def place_entry(self, trade, current_price: float, qty=None) -> OrderResult:
        # Fill instantly at the current simulated price.
        return OrderResult(ok=True, fill_price=current_price, order_id=f"PAPER-E-{trade.id}")

    def place_exit(self, trade, current_price: float, qty=None) -> OrderResult:
        return OrderResult(ok=True, fill_price=current_price, order_id=f"PAPER-X-{trade.id}")


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
        order_type = "MARKET" if trade.entry_type == "MARKET" else "LIMIT"
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
            r.raise_for_status()
            data = r.json()
            order_id = str(data.get("orderId", ""))
            # We use current_price as the recorded fill estimate; real fill price
            # would come from an order-status poll (a good future enhancement).
            return OrderResult(ok=True, fill_price=current_price, order_id=order_id)
        except Exception as e:
            detail = ""
            try:
                detail = r.text  # type: ignore
            except Exception:
                pass
            return OrderResult(ok=False, error=f"{e} {detail}".strip())

    def place_entry(self, trade, current_price: float, qty=None) -> OrderResult:
        return self._place(trade, trade.side, current_price, qty)

    def place_exit(self, trade, current_price: float, qty=None) -> OrderResult:
        # Exit is the opposite of the entry side.
        exit_side = "SELL" if trade.side == "BUY" else "BUY"
        return self._place(trade, exit_side, current_price, qty)


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
