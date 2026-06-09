"""
Zerodha (Kite Connect) integration: universal symbol mapper, market data,
broker and OAuth-style login. Like the Angel module, everything is presented in
Dhan-symbol space (security_id + exchange_segment) and translated to Kite's
tradingsymbol/exchange at the edges.
"""
import csv
import datetime as dt
import hashlib
import io
import threading
import time

import requests

INSTRUMENTS_URL = "https://api.kite.trade/instruments"
API_BASE = "https://api.kite.trade"
LOGIN_BASE = "https://kite.zerodha.com/connect/login"

DHAN_TO_KITE_EXCH = {
    "NSE_EQ": "NSE", "NSE_FNO": "NFO", "BSE_EQ": "BSE", "BSE_FNO": "BFO",
    "MCX_COMM": "MCX", "NSE_CURRENCY": "CDS", "BSE_CURRENCY": "BCD", "IDX_I": "NSE",
}


def _headers(api_key, access_token):
    return {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}


# ----------------------------------------------------------------------------
# Universal symbol mapper (Dhan <-> Zerodha)
# ----------------------------------------------------------------------------
class ZerodhaMapper:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_token = {}      # (exch, exchange_token) -> row dict
        self._by_opt = {}
        self._by_fut = {}
        self._by_eq = {}
        self.loaded_at = None
        self.loading = False

    def ready(self):
        return bool(self._by_token)

    def status(self):
        return {"count": len(self._by_token),
                "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
                "loading": self.loading}

    def load_async(self):
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        if self.loading:
            return
        self.loading = True
        try:
            r = requests.get(INSTRUMENTS_URL, timeout=90, headers={"User-Agent": "algo"})
            r.raise_for_status()
            by_token, by_opt, by_fut, by_eq = {}, {}, {}, {}
            for row in csv.DictReader(io.StringIO(r.text)):
                exch = row.get("exchange", "")
                etok = str(row.get("exchange_token", ""))
                name = (row.get("name") or "").upper()
                itype = row.get("instrument_type", "")
                slim = {"tradingsymbol": row.get("tradingsymbol", ""), "exchange": exch,
                        "instrument_token": row.get("instrument_token", ""),
                        "lot_size": row.get("lot_size", "1")}
                by_token[(exch, etok)] = slim
                expiry = (row.get("expiry") or "")[:10]
                try:
                    strike = int(round(float(row.get("strike") or 0)))
                except Exception:
                    strike = 0
                if itype in ("CE", "PE"):
                    by_opt[(exch, name, expiry, strike, itype)] = slim
                elif itype == "FUT":
                    by_fut[(exch, name, expiry)] = slim
                elif itype == "EQ":
                    by_eq[(exch, name)] = slim
            with self._lock:
                self._by_token, self._by_opt = by_token, by_opt
                self._by_fut, self._by_eq = by_fut, by_eq
            self.loaded_at = dt.datetime.utcnow()
        except Exception as e:
            print(f"[zerodha] master load failed: {e}")
        finally:
            self.loading = False

    def translate(self, security_id, exchange_segment, meta=None):
        exch = DHAN_TO_KITE_EXCH.get(exchange_segment)
        if not exch:
            return None
        with self._lock:
            row = self._by_token.get((exch, str(security_id)))
            if row is None and meta:
                itype = meta.get("instrument_type")
                name = (meta.get("underlying") or "").upper()
                exp = (meta.get("expiry") or "")[:10]
                if itype == "OPTION":
                    strike = int(round(meta.get("strike", 0)))
                    row = self._by_opt.get((exch, name, exp, strike, meta.get("option_type", "")))
                elif itype == "FUTURES":
                    row = self._by_fut.get((exch, name, exp))
                else:
                    row = self._by_eq.get((exch, name))
        return dict(row) if row else None


mapper = ZerodhaMapper()


def normalize_order(o):
    """One Kite order dict -> the Dhan-like shape our sync code expects."""
    seg = {v: k for k, v in DHAN_TO_KITE_EXCH.items()}.get(o.get("exchange", ""), "")
    return {
        "orderId": o.get("order_id"), "orderStatus": str(o.get("status", "")).upper(),
        "transactionType": o.get("transaction_type", ""),
        "tradingSymbol": o.get("tradingsymbol", ""), "securityId": str(o.get("instrument_token", "")),
        "exchangeSegment": seg, "quantity": o.get("quantity", 0),
        "price": o.get("price", 0), "averageTradedPrice": o.get("average_price", 0),
        "omsErrorDescription": o.get("status_message", ""),
    }


# ----------------------------------------------------------------------------
# Authentication (Kite Connect login)
# ----------------------------------------------------------------------------
def login_url(api_key):
    return f"{LOGIN_BASE}?v=3&api_key={api_key}"


def exchange_request_token(api_key, api_secret, request_token):
    """Swap the request_token (from the redirect) for an access token."""
    checksum = hashlib.sha256((api_key + request_token + api_secret).encode()).hexdigest()
    try:
        r = requests.post(f"{API_BASE}/session/token",
                          data={"api_key": api_key, "request_token": request_token, "checksum": checksum},
                          headers={"X-Kite-Version": "3"}, timeout=10)
        d = r.json()
        if d.get("status") == "success" and d.get("data", {}).get("access_token"):
            return True, d["data"]["access_token"]
        return False, d.get("message") or "token exchange failed"
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------------
# Market data (LTP)
# ----------------------------------------------------------------------------
class ZerodhaMarketData:
    def __init__(self, api_key, access_token):
        self.api_key, self.access_token = api_key, access_token
        self.last_error = ""

    def get_ltp_batch(self, by_segment):
        from instruments import store
        keys, back = [], {}
        for seg, ids in by_segment.items():
            for sid in ids:
                a = mapper.translate(sid, seg, store.get_meta(sid))
                if a:
                    k = f"{a['exchange']}:{a['tradingsymbol']}"
                    keys.append(k)
                    back[k] = (seg, str(sid))
        if not keys:
            self.last_error = "No symbols could be mapped to Zerodha."
            return {}
        out, err = {}, ""
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            try:
                r = requests.get(f"{API_BASE}/quote/ltp",
                                 params=[("i", k) for k in chunk],
                                 headers=_headers(self.api_key, self.access_token), timeout=6)
                d = r.json()
                for k, v in d.get("data", {}).items():
                    if k in back:
                        out[back[k]] = float(v.get("last_price") or 0)
                if d.get("status") != "success":
                    err = d.get("message") or err
            except Exception as e:
                err = str(e)
        self.last_error = "" if out else err
        return out

    def get_ltp(self, exchange_segment, security_id):
        return self.get_ltp_batch({exchange_segment: [security_id]}).get(
            (exchange_segment, str(security_id)), 0.0)


# ----------------------------------------------------------------------------
# Broker (orders)
# ----------------------------------------------------------------------------
_KITE_STATUS = {
    "COMPLETE": "TRADED", "REJECTED": "REJECTED", "CANCELLED": "CANCELLED",
    "OPEN": "PENDING", "TRIGGER PENDING": "PENDING", "PUT ORDER REQ RECEIVED": "PENDING",
    "VALIDATION PENDING": "PENDING", "OPEN PENDING": "PENDING", "MODIFY PENDING": "PENDING",
}


class ZerodhaBroker:
    name = "ZERODHA"

    def __init__(self, api_key, access_token):
        self.api_key, self.access_token = api_key, access_token

    def _kite_for(self, trade):
        from instruments import store
        return mapper.translate(trade.security_id, trade.exchange_segment,
                                store.get_meta(trade.security_id))

    def _place(self, trade, side, current_price, qty=None):
        from brokers import OrderResult, _extract_reason
        k = self._kite_for(trade)
        if not k:
            return OrderResult(ok=False, status="REJECTED",
                               error="Symbol not available / mapped on Zerodha")
        order_type = "MARKET" if trade.entry_type == "MARKET" else "LIMIT"
        data = {
            "tradingsymbol": k["tradingsymbol"], "exchange": k["exchange"],
            "transaction_type": side, "order_type": order_type,
            "quantity": str(int(qty if qty else trade.quantity)),
            "product": "MIS", "validity": "DAY",
        }
        if order_type == "LIMIT":
            data["price"] = str(trade.entry_price)
        try:
            r = requests.post(f"{API_BASE}/orders/regular", data=data,
                              headers=_headers(self.api_key, self.access_token), timeout=8)
        except Exception as e:
            return OrderResult(ok=False, error=f"Network error: {e}", status="ERROR")
        try:
            d = r.json()
        except Exception:
            return OrderResult(ok=False, error="Unreadable Zerodha response", status="ERROR")
        if d.get("status") == "success" and d.get("data", {}).get("order_id"):
            return OrderResult(ok=True, fill_price=current_price,
                               order_id=str(d["data"]["order_id"]), status="PENDING")
        return OrderResult(ok=False, status="REJECTED", error=d.get("message") or _extract_reason(r))

    def place_entry(self, trade, current_price, qty=None):
        return self._place(trade, trade.side, current_price, qty)

    def place_exit(self, trade, current_price, qty=None):
        return self._place(trade, "SELL" if trade.side == "BUY" else "BUY", current_price, qty)

    def _orders(self):
        r = requests.get(f"{API_BASE}/orders", headers=_headers(self.api_key, self.access_token), timeout=6)
        return r.json().get("data") or []

    def order_status(self, order_id):
        try:
            for o in self._orders():
                if str(o.get("order_id")) == str(order_id):
                    st = _KITE_STATUS.get(str(o.get("status", "")).upper(), "")
                    return (st, float(o.get("average_price") or 0),
                            int(float(o.get("filled_quantity") or 0)), o.get("status_message") or "")
        except Exception:
            pass
        return "", 0.0, 0, ""

    def confirm(self, order_id):
        import config
        last = ("", 0.0, 0, "")
        for _ in range(config.ORDER_POLLS):
            last = self.order_status(order_id)
            if last[0] in ("TRADED", "REJECTED", "CANCELLED", "EXPIRED"):
                return last
            time.sleep(config.ORDER_POLL_DELAY)
        return last

    def fund_limit(self):
        try:
            r = requests.get(f"{API_BASE}/user/margins", headers=_headers(self.api_key, self.access_token), timeout=6)
            d = r.json().get("data", {})
            eq = d.get("equity", {})
            bal = eq.get("net") or eq.get("available", {}).get("live_balance") or 0
            return True, float(bal)
        except Exception:
            return False, 0.0

    def get_orders(self):
        try:
            return [normalize_order(o) for o in self._orders()]
        except Exception:
            return []
