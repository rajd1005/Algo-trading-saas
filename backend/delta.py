"""
Delta Exchange (India) integration: crypto-derivatives broker + market data.

Unlike the Indian-equity brokers (Dhan/Angel/Zerodha/Alice), Delta trades crypto
futures & options keyed by its OWN product id / symbol (e.g. BTCUSD) — there is no
NSE/BSE security id to translate. The user types Delta's symbol (or numeric product
id) into the trade's `security_id` field; this module resolves it to Delta's
product_id and signs every private request with the API key + secret (HMAC-SHA256).

A lightweight product map (downloaded from /v2/products) is kept purely to translate
symbol <-> product_id and to read each contract's metadata; it is an internal detail,
not a user-facing instrument picker.
"""
import datetime as dt
import hashlib
import hmac
import json
import threading
import time
import urllib.parse

import requests

# Delta India REST base. The global platform is api.delta.exchange; override via
# config/env if ever needed, but India is the default for this app.
API_BASE = "https://api.india.delta.exchange"

# Synthetic exchange segment used for Delta trades (Delta has no NSE-style segment;
# routing is by the trading ACCOUNT, so this value is only a price-dict key).
SEGMENT = "DELTA"

# Delta order state -> our internal status.
_DELTA_STATUS = {
    "closed": "TRADED", "filled": "TRADED",
    "open": "PENDING", "pending": "PENDING",
    "cancelled": "CANCELLED", "rejected": "REJECTED",
}


def _err(resp):
    """Pull a human-readable error out of a Delta error response."""
    try:
        d = resp.json()
        e = d.get("error") if isinstance(d, dict) else None
        if isinstance(e, dict):
            ctx = e.get("context") or {}
            return str(e.get("code") or "") + (f" {ctx}" if ctx else "")
        if e:
            return str(e)
        return str(d)[:300]
    except Exception:
        try:
            return resp.text[:300]
        except Exception:
            return "Unknown Delta error"


def _sign(api_secret, method, path, query="", body="", ts=None):
    """Delta HMAC-SHA256: signature over method + timestamp + path + query + body."""
    ts = ts or str(int(time.time()))
    message = method + ts + path + query + body
    sig = hmac.new(api_secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return ts, sig


def request(api_key, api_secret, method, path, params=None, body_dict=None, timeout=8):
    """Send one SIGNED request to Delta. The body is serialized once and the exact
    string is both signed and sent, so the signature always matches."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    body = json.dumps(body_dict, separators=(",", ":")) if body_dict is not None else ""
    ts, sig = _sign(api_secret, method, path, query, body)
    headers = {
        "api-key": api_key,
        "timestamp": ts,
        "signature": sig,
        "User-Agent": "rd-algo",
        "Content-Type": "application/json",
    }
    url = API_BASE + path + query
    if method == "GET":
        return requests.get(url, headers=headers, timeout=timeout)
    if method == "DELETE":
        return requests.delete(url, headers=headers, data=body, timeout=timeout)
    return requests.post(url, headers=headers, data=body, timeout=timeout)


# ----------------------------------------------------------------------------
# Product map (symbol <-> product_id), downloaded from the public /v2/products
# ----------------------------------------------------------------------------
class DeltaMapper:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_symbol = {}     # "BTCUSD" -> row
        self._by_id = {}         # "27" -> row
        self._opt = {}           # underlying -> expiry -> strike -> {"CE": o, "PE": o}
        self.loaded_at = None
        self.loading = False
        self.last_error = ""

    def ready(self):
        return bool(self._by_id)

    def status(self):
        return {"count": len(self._by_id),
                "loaded_at": self.loaded_at.isoformat() if self.loaded_at else None,
                "loading": self.loading, "error": self.last_error}

    def load_async(self):
        threading.Thread(target=self._load, daemon=True).start()

    @staticmethod
    def _log(msg, level="WARN"):
        try:
            from database import SessionLocal
            from models import LogEntry
            db = SessionLocal()
            db.add(LogEntry(message=msg, level=level))
            db.commit(); db.close()
        except Exception:
            pass

    def _load(self):
        if self.loading:
            return
        self.loading = True
        by_symbol, by_id, opt = {}, {}, {}
        try:
            after = None
            for _ in range(40):                 # safety cap on pagination
                params = {"page_size": "1000"}
                if after:
                    params["after"] = after
                r = requests.get(f"{API_BASE}/v2/products",
                                 params=params, headers={"User-Agent": "rd-algo"}, timeout=30)
                if r.status_code != 200:
                    self.last_error = f"Delta products download failed (HTTP {r.status_code})"
                    break
                d = r.json()
                for p in d.get("result", []) or []:
                    sym = str(p.get("symbol", "")).upper().strip()
                    pid = p.get("id")
                    if not sym or pid is None:
                        continue
                    ctype = str(p.get("contract_type", ""))
                    row = {"product_id": int(pid), "symbol": sym,
                           "contract_value": p.get("contract_value"),
                           "tick_size": p.get("tick_size"),
                           "contract_type": ctype,
                           "lot_size": 1}
                    by_symbol[sym] = row
                    by_id[str(pid)] = row
                    if ctype in ("call_options", "put_options"):
                        self._index_option(opt, p, sym, int(pid), ctype)
                after = (d.get("meta") or {}).get("after")
                if not after:
                    break
            if by_id:
                with self._lock:
                    self._by_symbol, self._by_id, self._opt = by_symbol, by_id, opt
                self.loaded_at = dt.datetime.utcnow()
                self.last_error = ""
            elif not self.last_error:
                self.last_error = "Delta returned no products."
        except Exception as e:
            self.last_error = f"Delta product list load failed: {e}"
            self._log(self.last_error, "ERROR")
        finally:
            self.loading = False

    def resolve(self, security_id):
        """A Delta symbol or numeric product id -> {product_id, symbol, ...} or None.
        Falls back to a bare numeric id even before the product map has loaded."""
        sid = str(security_id or "").strip()
        if not sid:
            return None
        with self._lock:
            row = self._by_id.get(sid) or self._by_symbol.get(sid.upper())
        if row:
            return dict(row)
        if sid.isdigit():               # numeric id usable for orders without the map
            return {"product_id": int(sid), "symbol": "", "lot_size": 1}
        return None

    def symbol_for(self, security_id):
        r = self.resolve(security_id)
        if r and r.get("symbol"):
            return r["symbol"]
        sid = str(security_id or "").strip()
        return sid.upper() if sid and not sid.isdigit() else ""

    def search(self, query, limit=25):
        """Find Delta products whose symbol matches all whitespace-separated terms.
        Symbol-prefix matches and shorter symbols rank first (e.g. 'BTC' -> BTCUSD)."""
        q = (query or "").strip().upper()
        if not q:
            return []
        terms = q.split()
        with self._lock:
            rows = list(self._by_symbol.values())
        out = []
        for r in rows:
            sym = r.get("symbol", "")
            if all(t in sym for t in terms):
                starts = 0 if sym.startswith(terms[0]) else 1
                out.append((starts, len(sym), sym, r))
        out.sort(key=lambda x: (x[0], x[1], x[2]))
        # Options are picked via the chain, not free-text search — keep them out.
        return [{"symbol": r["symbol"], "product_id": r["product_id"],
                 "contract_type": r.get("contract_type", ""),
                 "contract_value": r.get("contract_value")}
                for _, _, _, r in out[:limit]
                if not str(r.get("contract_type", "")).endswith("options")]

    # ---------- options chain ----------
    @staticmethod
    def _index_option(opt, p, sym, pid, ctype):
        """Add one Delta option product to the chain index (opt[under][exp][strike])."""
        ua = p.get("underlying_asset")
        under = (ua.get("symbol") if isinstance(ua, dict) else "") or ""
        if not under:                          # fallback: 'C-BTC-95000-280625' -> BTC
            parts = sym.split("-")
            under = parts[1] if len(parts) >= 2 else ""
        under = under.upper()
        expiry = str(p.get("settlement_time") or "")[:10]
        try:
            strike = int(round(float(p.get("strike_price") or 0)))
        except Exception:
            strike = 0
        if not under or not expiry or not strike:
            return
        otype = "CE" if ctype == "call_options" else "PE"
        opt.setdefault(under, {}).setdefault(expiry, {}).setdefault(strike, {})[otype] = \
            {"symbol": sym, "product_id": pid, "contract_value": p.get("contract_value")}

    @staticmethod
    def _opt_slim(o):
        if not o:
            return None
        return {"symbol": o["symbol"], "security_id": o["symbol"],
                "exchange_segment": SEGMENT, "instrument_type": "OPTION",
                "lot_size": 1, "product_id": o["product_id"],
                "contract_value": o.get("contract_value")}

    def option_underlyings(self):
        with self._lock:
            return sorted(self._opt.keys())

    def option_expiries(self, underlying):
        with self._lock:
            exps = sorted((self._opt.get((underlying or "").upper()) or {}).keys())
        today = dt.datetime.utcnow().strftime("%Y-%m-%d")
        return [e for e in exps if e and e >= today]

    def option_chain(self, underlying, expiry):
        with self._lock:
            by_strike = (self._opt.get((underlying or "").upper()) or {}).get(expiry, {})
            rows = [{"strike": s, "ce": self._opt_slim(by_strike[s].get("CE")),
                     "pe": self._opt_slim(by_strike[s].get("PE"))}
                    for s in sorted(by_strike.keys())]
        return rows


mapper = DeltaMapper()


def point_value(exchange_segment, security_id):
    """P&L multiplier per contract per point. Per the product owner's decision, Delta
    crypto contracts are valued at **$1 per contract per point** (1 contract = $1) — a
    flat 1.0 multiplier, so PnL = ±qty × (exit − entry). The exchange's raw
    `contract_value` (e.g. 0.001 BTC) is intentionally NOT used. Equity/options are
    also 1.0. Kept as a hook so a per-product multiplier can be reintroduced later.
    """
    return 1.0


def normalize_order(o):
    """One Delta order dict -> the Dhan-like shape our external-order sync expects."""
    state = str(o.get("state", "")).lower()
    return {
        "orderId": str(o.get("id", "")),
        "orderStatus": _DELTA_STATUS.get(state, state.upper()),
        "transactionType": "BUY" if o.get("side") == "buy" else "SELL",
        "tradingSymbol": o.get("product_symbol", ""),
        "securityId": str(o.get("product_id", "")),
        "exchangeSegment": SEGMENT,
        "quantity": o.get("size", 0),
        "price": o.get("limit_price") or 0,
        "averageTradedPrice": o.get("average_fill_price") or 0,
        "omsErrorDescription": "",
    }


# ----------------------------------------------------------------------------
# Authentication (key-based: api_key + api_secret sign each request)
# ----------------------------------------------------------------------------
def verify(api_key, api_secret):
    """Validate the credentials with a signed read-only call. (ok, message)."""
    try:
        r = request(api_key, api_secret, "GET", "/v2/wallet/balances", timeout=10)
        if r.status_code == 200:
            return True, "Connected to Delta Exchange."
        return False, f"Delta rejected the credentials (HTTP {r.status_code}): {_err(r)}"
    except Exception as e:
        return False, f"Could not reach Delta: {e}"


# ----------------------------------------------------------------------------
# Market data (LTP) — public per-symbol ticker (no auth needed)
# ----------------------------------------------------------------------------
def _ticker_price(symbol):
    r = requests.get(f"{API_BASE}/v2/tickers/{symbol}",
                     headers={"User-Agent": "rd-algo"}, timeout=5)
    d = r.json().get("result", {}) if r.status_code == 200 else {}
    px = d.get("mark_price") or d.get("close") or d.get("spot_price") or 0
    return float(px or 0)


class DeltaMarketData:
    def __init__(self, api_key="", api_secret=""):
        self.api_key, self.api_secret = api_key, api_secret
        self.last_error = ""

    def get_ltp_batch(self, by_segment):
        if not mapper.ready():
            mapper.load_async()
        out, err = {}, ""
        items = []
        for seg, ids in by_segment.items():
            for sid in ids:
                sym = mapper.symbol_for(sid)
                if sym:
                    items.append((seg, str(sid), sym))
        if not items:
            self.last_error = "No matching Delta product for these symbols — check the symbol/product id."
            return {}
        for seg, sid, sym in items[:40]:        # cap (per-symbol REST)
            try:
                out[(seg, sid)] = _ticker_price(sym)
            except Exception as e:
                err = str(e)
            time.sleep(0.03)
        self.last_error = "" if out else err
        return out

    def get_ltp(self, exchange_segment, security_id):
        return self.get_ltp_batch({exchange_segment: [security_id]}).get(
            (exchange_segment, str(security_id)), 0.0)


# ----------------------------------------------------------------------------
# Broker (orders)
# ----------------------------------------------------------------------------
class DeltaBroker:
    name = "DELTA"

    def __init__(self, api_key, api_secret):
        self.api_key, self.api_secret = api_key, api_secret

    def _request(self, method, path, params=None, body_dict=None, timeout=8):
        return request(self.api_key, self.api_secret, method, path,
                       params=params, body_dict=body_dict, timeout=timeout)

    def _place(self, trade, side, current_price, qty=None):
        from brokers import OrderResult
        row = mapper.resolve(trade.security_id)
        if not row:
            return OrderResult(ok=False, status="REJECTED",
                               error=f"Delta product not found for '{trade.security_id}'")
        order_type = "limit_order" if trade.entry_type == "LIMIT" else "market_order"
        body = {
            "product_id": int(row["product_id"]),
            "size": int(qty if qty else trade.quantity),
            "side": "buy" if side == "BUY" else "sell",
            "order_type": order_type,
        }
        if order_type == "limit_order":
            body["limit_price"] = str(trade.entry_price)
        try:
            r = self._request("POST", "/v2/orders", body_dict=body)
        except Exception as e:
            return OrderResult(ok=False, error=f"Network error: {e}", status="ERROR")
        try:
            d = r.json()
        except Exception:
            return OrderResult(ok=False, error="Unreadable Delta response", status="ERROR")
        if r.status_code >= 400 or not d.get("success"):
            return OrderResult(ok=False, status="REJECTED", error=_err(r))
        res = d.get("result", {}) or {}
        oid = str(res.get("id", ""))
        status = _DELTA_STATUS.get(str(res.get("state", "")).lower(), "PENDING")
        avg = float(res.get("average_fill_price") or 0)
        return OrderResult(ok=True, fill_price=avg or current_price, order_id=oid, status=status)

    def place_entry(self, trade, current_price, qty=None):
        return self._place(trade, trade.side, current_price, qty)

    def place_exit(self, trade, current_price, qty=None):
        return self._place(trade, "SELL" if trade.side == "BUY" else "BUY", current_price, qty)

    def _find_order(self, order_id):
        """Locate one order in the live book, then in recent history."""
        for path in ("/v2/orders", "/v2/orders/history"):
            try:
                r = self._request("GET", path, params={"page_size": "100"})
                rows = r.json().get("result", []) if r.status_code == 200 else []
                for o in rows:
                    if str(o.get("id")) == str(order_id):
                        return o
            except Exception:
                pass
        return None

    def order_status(self, order_id):
        o = self._find_order(order_id)
        if not o:
            return "", 0.0, 0, ""
        st = _DELTA_STATUS.get(str(o.get("state", "")).lower(), "")
        avg = float(o.get("average_fill_price") or 0)
        size = int(o.get("size") or 0)
        unfilled = int(o.get("unfilled_size") or 0)
        return st, avg, max(0, size - unfilled), ""

    def confirm(self, order_id):
        import config
        last = ("", 0.0, 0, "")
        for _ in range(config.ORDER_POLLS):
            last = self.order_status(order_id)
            if last[0] in ("TRADED", "REJECTED", "CANCELLED", "EXPIRED"):
                return last
            time.sleep(config.ORDER_POLL_DELAY)
        return last

    def cancel_order(self, order_id):
        """Best-effort cancel of a resting Delta order."""
        o = self._find_order(order_id)
        pid = o.get("product_id") if o else None
        try:
            r = self._request("DELETE", "/v2/orders",
                              body_dict={"id": int(order_id), "product_id": int(pid)} if pid
                              else {"id": int(order_id)})
            return r.status_code < 300
        except Exception:
            return False

    def fund_limit(self):
        """(ok, available_balance). Prefers the INR/USDT settling wallet."""
        try:
            r = self._request("GET", "/v2/wallet/balances", timeout=6)
            rows = r.json().get("result", []) if r.status_code == 200 else []
            if not rows:
                return (r.status_code == 200), 0.0
            pref = {"INR": 0, "USDT": 1, "USDC": 2, "USD": 3}
            rows.sort(key=lambda w: pref.get(str(w.get("asset_symbol", "")).upper(), 9))
            bal = rows[0].get("available_balance") or rows[0].get("balance") or 0
            return True, float(bal)
        except Exception:
            return False, 0.0

    def get_positions(self):
        try:
            r = self._request("GET", "/v2/positions/margined", timeout=6)
            data = r.json().get("result", []) if r.status_code == 200 else []
            return data if isinstance(data, list) else []
        except Exception:
            return []

    def get_orders(self):
        """Recent order book (live + history), normalized to the Dhan-like shape."""
        out = []
        for path in ("/v2/orders", "/v2/orders/history"):
            try:
                r = self._request("GET", path, params={"page_size": "100"})
                rows = r.json().get("result", []) if r.status_code == 200 else []
                out.extend(normalize_order(o) for o in rows)
            except Exception:
                pass
        return out
