"""
Angel One (SmartAPI) integration: universal symbol mapper, market data, broker
and TOTP login. The rest of the app works in Dhan-symbol space (security_id +
exchange_segment); this module translates that to Angel's tokens at the edges,
so mixing brokers (e.g. Dhan data + Angel execution) works seamlessly.
"""
import datetime as dt
import threading

import requests

try:
    import pyotp
    _HAVE_OTP = True
except Exception:
    _HAVE_OTP = False

SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
API_BASE = "https://apiconnect.angelone.in"

# Dhan exchange_segment -> Angel exch_seg
DHAN_TO_ANGEL_EXCH = {
    "NSE_EQ": "NSE", "NSE_FNO": "NFO", "BSE_EQ": "BSE", "BSE_FNO": "BFO",
    "MCX_COMM": "MCX", "NSE_CURRENCY": "CDS", "BSE_CURRENCY": "BCD", "IDX_I": "NSE",
}
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def _angel_expiry(dhan_expiry):
    """Dhan '2026-06-30' -> Angel '30JUN2026'."""
    try:
        d = dt.datetime.strptime(dhan_expiry[:10], "%Y-%m-%d")
        return f"{d.day:02d}{_MONTHS[d.month - 1]}{d.year}"
    except Exception:
        return ""


# ----------------------------------------------------------------------------
# Universal symbol mapper (Dhan <-> Angel)
# ----------------------------------------------------------------------------
class AngelMapper:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_token = {}      # (exch, token) -> angel row
        self._by_opt = {}        # (exch, name, expiry, strike, opttype) -> row
        self._by_fut = {}        # (exch, name, expiry) -> row
        self._by_eq = {}         # (exch, name) -> row
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
            r = requests.get(SCRIP_URL, timeout=90, headers={"User-Agent": "algo"})
            r.raise_for_status()
            rows = r.json()
            by_token, by_opt, by_fut, by_eq = {}, {}, {}, {}
            for x in rows:
                exch = x.get("exch_seg", "")
                tok = str(x.get("token", ""))
                name = (x.get("name") or "").upper()
                sym = x.get("symbol") or ""
                itype = x.get("instrumenttype", "")
                by_token[(exch, tok)] = x
                if itype.startswith("OPT"):
                    opttype = sym[-2:].upper() if sym[-2:].upper() in ("CE", "PE") else ""
                    try:
                        strike = int(round(float(x.get("strike", 0)) / 100))
                    except Exception:
                        strike = 0
                    by_opt[(exch, name, x.get("expiry", ""), strike, opttype)] = x
                elif itype.startswith("FUT"):
                    by_fut[(exch, name, x.get("expiry", ""))] = x
                elif itype == "" or itype == "AMXIDX":     # equities/indices
                    by_eq[(exch, name)] = x
            with self._lock:
                self._by_token, self._by_opt = by_token, by_opt
                self._by_fut, self._by_eq = by_fut, by_eq
            self.loaded_at = dt.datetime.utcnow()
        except Exception as e:
            print(f"[angel] master load failed: {e}")
        finally:
            self.loading = False

    def translate(self, security_id, exchange_segment, meta=None):
        """Dhan (security_id, segment[, meta]) -> Angel {token, tradingsymbol,
        exchange, lotsize} or None."""
        exch = DHAN_TO_ANGEL_EXCH.get(exchange_segment)
        if not exch:
            return None
        with self._lock:
            # 1) token usually matches the exchange token directly
            row = self._by_token.get((exch, str(security_id)))
            # 2) fall back to attribute match
            if row is None and meta:
                itype = meta.get("instrument_type")
                name = (meta.get("underlying") or "").upper()
                exp = _angel_expiry(meta.get("expiry", ""))
                if itype == "OPTION":
                    strike = int(round(meta.get("strike", 0)))
                    row = self._by_opt.get((exch, name, exp, strike, meta.get("option_type", "")))
                elif itype == "FUTURES":
                    row = self._by_fut.get((exch, name, exp))
                else:
                    row = self._by_eq.get((exch, name))
        if not row:
            return None
        return {"token": str(row["token"]), "tradingsymbol": row["symbol"],
                "exchange": row["exch_seg"], "lotsize": row.get("lotsize", "1")}


mapper = AngelMapper()


def normalize_order(o):
    """One Angel order dict -> the Dhan-like shape our sync code expects."""
    seg = {v: k for k, v in DHAN_TO_ANGEL_EXCH.items()}.get(o.get("exchange", ""), "")
    return {
        "orderId": o.get("orderid"), "orderStatus": str(o.get("status", "")).upper(),
        "transactionType": o.get("transactiontype", ""),
        "tradingSymbol": o.get("tradingsymbol", ""), "securityId": str(o.get("symboltoken", "")),
        "exchangeSegment": seg, "quantity": o.get("quantity", 0),
        "price": o.get("price", 0), "averageTradedPrice": o.get("averageprice", 0),
        "omsErrorDescription": o.get("text", ""),
    }


# ----------------------------------------------------------------------------
# Angel One authentication (TOTP login)
# ----------------------------------------------------------------------------
def _auth_headers(api_key, jwt=None):
    h = {
        "Content-Type": "application/json", "Accept": "application/json",
        "X-UserType": "USER", "X-SourceID": "WEB",
        "X-ClientLocalIP": "127.0.0.1", "X-ClientPublicIP": "127.0.0.1",
        "X-MACAddress": "00:00:00:00:00:00", "X-PrivateKey": api_key,
    }
    if jwt:
        h["Authorization"] = f"Bearer {jwt}"
    return h


def renew(api_key, jwt, refresh_token):
    """Refresh the Angel session token. Returns (ok, new_jwt_or_error)."""
    url = f"{API_BASE}/rest/auth/angelbroking/jwt/v1/generateTokens"
    try:
        r = requests.post(url, json={"refreshToken": refresh_token},
                          headers=_auth_headers(api_key, jwt), timeout=10)
        d = r.json()
        if d.get("status") and d.get("data", {}).get("jwtToken"):
            return True, d["data"]["jwtToken"]
        return False, d.get("message") or "renew failed"
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------------
# Angel market data (LTP) — presented in Dhan-symbol space
# ----------------------------------------------------------------------------
class AngelMarketData:
    def __init__(self, client_id, api_key, jwt):
        self.client_id, self.api_key, self.jwt = client_id, api_key, jwt
        self.last_error = ""

    def get_ltp_batch(self, by_segment):
        """Input Dhan-style {NSE_FNO:[ids]}; output {(dhan_seg, id): ltp}."""
        from instruments import store
        # translate each dhan instrument -> angel (exchange, token), remember mapping
        ang_tokens, back = {}, {}
        for seg, ids in by_segment.items():
            for sid in ids:
                a = mapper.translate(sid, seg, store.get_meta(sid))
                if a:
                    ang_tokens.setdefault(a["exchange"], []).append(a["token"])
                    back[(a["exchange"], a["token"])] = (seg, str(sid))
        if not ang_tokens:
            return {}
        url = f"{API_BASE}/rest/secure/angelbroking/market/v1/quote"
        out = {}
        try:
            r = requests.post(url, json={"mode": "LTP", "exchangeTokens": ang_tokens},
                              headers=_auth_headers(self.api_key, self.jwt), timeout=6)
            d = r.json()
            for item in d.get("data", {}).get("fetched", []):
                key = (item.get("exchange"), str(item.get("symbolToken")))
                if key in back:
                    out[back[key]] = float(item.get("ltp") or 0)
            self.last_error = "" if out else (d.get("message") or "")
        except Exception as e:
            self.last_error = str(e)
        return out

    def get_ltp(self, exchange_segment, security_id):
        return self.get_ltp_batch({exchange_segment: [security_id]}).get(
            (exchange_segment, str(security_id)), 0.0)


# ----------------------------------------------------------------------------
# Angel broker (orders)
# ----------------------------------------------------------------------------
_ANGEL_STATUS = {
    "complete": "TRADED", "executed": "TRADED",
    "rejected": "REJECTED", "cancelled": "CANCELLED",
    "open": "PENDING", "trigger pending": "PENDING", "validation pending": "PENDING",
    "open pending": "PENDING", "modified": "PENDING",
}


class AngelBroker:
    name = "ANGEL"

    def __init__(self, client_id, api_key, jwt):
        self.client_id, self.api_key, self.jwt = client_id, api_key, jwt

    def _angel_for(self, trade):
        from instruments import store
        return mapper.translate(trade.security_id, trade.exchange_segment,
                                store.get_meta(trade.security_id))

    def _place(self, trade, side, current_price, qty=None):
        from brokers import OrderResult, _extract_reason
        a = self._angel_for(trade)
        if not a:
            return OrderResult(ok=False, status="REJECTED",
                               error="Symbol not available / mapped on Angel One")
        order_type = "MARKET" if trade.entry_type == "MARKET" else "LIMIT"
        payload = {
            "variety": "NORMAL", "tradingsymbol": a["tradingsymbol"],
            "symboltoken": a["token"], "transactiontype": side,
            "exchange": a["exchange"], "ordertype": order_type,
            "producttype": "INTRADAY", "duration": "DAY",
            "price": str(trade.entry_price) if order_type == "LIMIT" else "0",
            "quantity": str(int(qty if qty else trade.quantity)),
        }
        url = f"{API_BASE}/rest/secure/angelbroking/order/v1/placeOrder"
        try:
            r = requests.post(url, json=payload, headers=_auth_headers(self.api_key, self.jwt), timeout=8)
        except Exception as e:
            return OrderResult(ok=False, error=f"Network error: {e}", status="ERROR")
        try:
            d = r.json()
        except Exception:
            return OrderResult(ok=False, error="Unreadable Angel response", status="ERROR")
        if d.get("status") and d.get("data", {}).get("orderid"):
            return OrderResult(ok=True, fill_price=current_price,
                               order_id=str(d["data"]["orderid"]), status="PENDING")
        return OrderResult(ok=False, status="REJECTED",
                           error=d.get("message") or _extract_reason(r))

    def place_entry(self, trade, current_price, qty=None):
        return self._place(trade, trade.side, current_price, qty)

    def place_exit(self, trade, current_price, qty=None):
        return self._place(trade, "SELL" if trade.side == "BUY" else "BUY", current_price, qty)

    def _order_book(self):
        url = f"{API_BASE}/rest/secure/angelbroking/order/v1/getOrderBook"
        r = requests.get(url, headers=_auth_headers(self.api_key, self.jwt), timeout=6)
        return r.json().get("data") or []

    def order_status(self, order_id):
        try:
            for o in self._order_book():
                if str(o.get("orderid")) == str(order_id):
                    st = _ANGEL_STATUS.get(str(o.get("status", "")).lower(), "")
                    return (st, float(o.get("averageprice") or 0),
                            int(float(o.get("filledshares") or 0)), o.get("text") or "")
        except Exception:
            pass
        return "", 0.0, 0, ""

    def confirm(self, order_id):
        import time
        import config
        last = ("", 0.0, 0, "")
        for _ in range(config.ORDER_POLLS):
            last = self.order_status(order_id)
            if last[0] in ("TRADED", "REJECTED", "CANCELLED", "EXPIRED"):
                return last
            time.sleep(config.ORDER_POLL_DELAY)
        return last

    def fund_limit(self):
        url = f"{API_BASE}/rest/secure/angelbroking/user/v1/getRMS"
        try:
            r = requests.get(url, headers=_auth_headers(self.api_key, self.jwt), timeout=6)
            d = r.json()
            return True, float(d.get("data", {}).get("availablecash") or 0)
        except Exception:
            return False, 0.0

    def get_orders(self):
        """Return Angel's order book normalised into Dhan-like dicts for sync."""
        try:
            return [normalize_order(o) for o in self._order_book()]
        except Exception:
            return []


def login(client_id, pin, api_key, totp_secret):
    """SmartAPI password+TOTP login. Returns (ok, data_or_error)."""
    if not _HAVE_OTP:
        return False, "pyotp not installed"
    try:
        totp = pyotp.TOTP(totp_secret.strip()).now()
    except Exception as e:
        return False, f"Bad TOTP secret: {e}"
    url = f"{API_BASE}/rest/auth/angelbroking/user/v1/loginByPassword"
    body = {"clientcode": client_id, "password": pin, "totp": totp}
    try:
        r = requests.post(url, json=body, headers=_auth_headers(api_key), timeout=10)
        d = r.json()
        if d.get("status") and d.get("data", {}).get("jwtToken"):
            data = d["data"]
            return True, {"jwt": data["jwtToken"], "refresh": data.get("refreshToken", ""),
                          "feed": data.get("feedToken", "")}
        return False, d.get("message") or "Login failed"
    except Exception as e:
        return False, str(e)
