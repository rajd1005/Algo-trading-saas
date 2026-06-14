"""
Alice Blue (ANT API) integration: universal symbol mapper, market data, broker
and key-based login. Presented in Dhan-symbol space and translated to Alice's
token/tradingsymbol at the edges (like the Angel/Zerodha modules).
"""
import csv
import datetime as dt
import hashlib
import io
import threading
import time

import requests

MASTER_URL = "https://v2api.aliceblueonline.com/restpy/static/contract_master/{exch}.csv"
API_BASE = "https://ant.aliceblueonline.com/rest/AliceBlueAPIService/api"
_EXCHANGES = ["NSE", "NFO", "BSE", "BFO", "MCX", "CDS"]

DHAN_TO_ALICE_EXCH = {
    "NSE_EQ": "NSE", "NSE_FNO": "NFO", "BSE_EQ": "BSE", "BSE_FNO": "BFO",
    "MCX_COMM": "MCX", "NSE_CURRENCY": "CDS", "BSE_CURRENCY": "BCD", "IDX_I": "NSE",
}


def _hdr(user_id, session_id):
    return {"Authorization": f"Bearer {user_id} {session_id}", "Content-Type": "application/json"}


# ----------------------------------------------------------------------------
# Universal symbol mapper (Dhan <-> Alice Blue)
# ----------------------------------------------------------------------------
class AliceMapper:
    def __init__(self):
        self._lock = threading.Lock()
        self._by_token = {}
        self._by_opt = {}
        self._by_fut = {}
        self._by_eq = {}
        self.loaded_at = None
        self.loading = False
        self.last_error = ""

    def ready(self):
        return bool(self._by_token)

    def status(self):
        return {"count": len(self._by_token),
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

    def _fetch(self, exch):
        """Download one exchange's contract master, with retries."""
        for attempt in range(4):
            try:
                r = requests.get(MASTER_URL.format(exch=exch), timeout=60,
                                 headers={"User-Agent": "algo"})
                if r.status_code == 200:
                    return r.text
            except Exception as e:
                self.last_error = f"Alice Blue {exch} download failed: {e}"
            time.sleep(2 * (attempt + 1))
        return None

    def _load(self):
        if self.loading:
            return
        self.loading = True
        by_token, by_opt, by_fut, by_eq = {}, {}, {}, {}
        failed = []
        try:
            for exch in _EXCHANGES:
                text = self._fetch(exch)
                if text is None:
                    failed.append(exch)
                    continue
                try:
                    for row in csv.DictReader(io.StringIO(text)):
                        tok = str(row.get("Token", "")).strip()
                        if not tok:
                            continue
                        name = (row.get("Symbol") or "").upper().strip()
                        tsym = (row.get("Trading Symbol") or "").strip()
                        itype = (row.get("Instrument Type") or "").upper()
                        opt = (row.get("Option Type") or "").upper()
                        slim = {"tradingsymbol": tsym, "exchange": exch, "token": tok,
                                "lot_size": row.get("Lot Size", "1")}
                        by_token[(exch, tok)] = slim
                        expiry = (row.get("Expiry Date") or "")[:10]
                        try:
                            strike = int(round(float(row.get("Strike Price") or 0)))
                        except Exception:
                            strike = 0
                        if opt in ("CE", "PE"):
                            by_opt[(exch, name, expiry, strike, opt)] = slim
                        elif itype.startswith("FUT"):
                            by_fut[(exch, name, expiry)] = slim
                        else:
                            by_eq[(exch, name)] = slim
                except Exception:
                    continue
            with self._lock:
                self._by_token, self._by_opt = by_token, by_opt
                self._by_fut, self._by_eq = by_fut, by_eq
            self.loaded_at = dt.datetime.utcnow()
            if "NFO" in failed:     # NFO drives the option chain
                self._log("Alice Blue NFO symbol list could not be downloaded — option "
                          "prices unavailable until it loads.", "ERROR")
            self.last_error = "" if not failed else f"Could not load: {', '.join(failed)}"
        except Exception as e:
            self.last_error = f"Alice Blue symbol list load failed: {e}"
            self._log(self.last_error, "ERROR")
        finally:
            self.loading = False

    def translate(self, security_id, exchange_segment, meta=None):
        exch = DHAN_TO_ALICE_EXCH.get(exchange_segment)
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


mapper = AliceMapper()


def normalize_order(o):
    """One Alice order dict -> the Dhan-like shape our sync code expects."""
    seg = {v: k for k, v in DHAN_TO_ALICE_EXCH.items()}.get(o.get("Exchange") or o.get("Exseg", ""), "")
    return {
        "orderId": o.get("Nstordno") or o.get("nestordno"),
        "orderStatus": str(o.get("Status", "")).upper(),
        "transactionType": o.get("Trantype", ""),
        "tradingSymbol": o.get("Trsym", ""), "securityId": str(o.get("token", "")),
        "exchangeSegment": seg, "quantity": o.get("Qty", 0),
        "price": o.get("Prc", 0), "averageTradedPrice": o.get("Avgprc", 0),
        "omsErrorDescription": o.get("RejReason") or o.get("rejreason", ""),
    }


# ----------------------------------------------------------------------------
# Authentication (key-based: userId + apiKey -> sessionID)
# ----------------------------------------------------------------------------
def login(user_id, api_key):
    """Returns (ok, session_id_or_error). No browser/TOTP needed."""
    try:
        r = requests.post(f"{API_BASE}/customer/getAPIEncpkey",
                          json={"userId": user_id}, timeout=10)
        enc = r.json().get("encKey")
        if not enc:
            return False, r.json().get("emsg") or "no encKey"
        checksum = hashlib.sha256((user_id + api_key + enc).encode()).hexdigest()
        r2 = requests.post(f"{API_BASE}/customer/getUserSID",
                           json={"userId": user_id, "userData": checksum}, timeout=10)
        d = r2.json()
        if d.get("stat") == "Ok" and d.get("sessionID"):
            return True, d["sessionID"]
        return False, d.get("emsg") or "login failed"
    except Exception as e:
        return False, str(e)


# ----------------------------------------------------------------------------
# Market data (LTP) — per-scrip (Alice has no REST batch quote)
# ----------------------------------------------------------------------------
class AliceMarketData:
    def __init__(self, user_id, session_id):
        self.user_id, self.session_id = user_id, session_id
        self.last_error = ""

    def get_ltp_batch(self, by_segment):
        from instruments import store
        if not mapper.ready():
            mapper.load_async()
            self.last_error = "Alice Blue symbol list is still downloading — prices will appear shortly."
            return {}
        items = []
        for seg, ids in by_segment.items():
            for sid in ids:
                a = mapper.translate(sid, seg, store.get_meta(sid))
                if a:
                    items.append((seg, str(sid), a["exchange"], a["token"]))
        if not items:
            self.last_error = "No matching Alice Blue contracts for these strikes — pick one nearer ATM."
            return {}
        out, err = {}, ""
        for seg, sid, exch, tok in items[:40]:    # cap (per-scrip REST is slow)
            try:
                r = requests.post(f"{API_BASE}/ScripDetails/getScripQuoteDetails",
                                  json={"exch": exch, "symbol": tok},
                                  headers=_hdr(self.user_id, self.session_id), timeout=5)
                d = r.json()
                ltp = d.get("LTP") or d.get("Ltp") or d.get("ltp") or 0
                out[(seg, sid)] = float(ltp)
            except Exception as e:
                err = str(e)
            time.sleep(0.05)
        self.last_error = "" if out else err
        return out

    def get_ltp(self, exchange_segment, security_id):
        return self.get_ltp_batch({exchange_segment: [security_id]}).get(
            (exchange_segment, str(security_id)), 0.0)


# ----------------------------------------------------------------------------
# Broker (orders)
# ----------------------------------------------------------------------------
_ALICE_STATUS = {
    "COMPLETE": "TRADED", "REJECTED": "REJECTED", "CANCELLED": "CANCELLED",
    "OPEN": "PENDING", "TRIGGER PENDING": "PENDING", "OPEN PENDING": "PENDING",
    "AFTER MARKET ORDER REQ RECEIVED": "PENDING",
}


class AliceBroker:
    name = "ALICE"

    def __init__(self, user_id, session_id):
        self.user_id, self.session_id = user_id, session_id

    def _alice_for(self, trade):
        from instruments import store
        return mapper.translate(trade.security_id, trade.exchange_segment,
                                store.get_meta(trade.security_id))

    def _place(self, trade, side, current_price, qty=None):
        from brokers import OrderResult
        a = self._alice_for(trade)
        if not a:
            return OrderResult(ok=False, status="REJECTED",
                               error="Symbol not available / mapped on Alice Blue")
        prctyp = "L" if trade.entry_type == "LIMIT" else "MKT"
        order = [{
            "complexty": "regular", "discqty": "0", "exch": a["exchange"],
            "pCode": "MIS", "prctyp": prctyp,
            "price": str(trade.entry_price) if prctyp == "L" else "0",
            "qty": str(int(qty if qty else trade.quantity)), "ret": "DAY",
            "symbol_id": a["token"], "trading_symbol": a["tradingsymbol"],
            "transtype": side, "trigPrice": "0",
        }]
        try:
            r = requests.post(f"{API_BASE}/placeOrder/executePlaceOrder", json=order,
                              headers=_hdr(self.user_id, self.session_id), timeout=8)
        except Exception as e:
            return OrderResult(ok=False, error=f"Network error: {e}", status="ERROR")
        try:
            d = r.json()
            d = d[0] if isinstance(d, list) and d else d
        except Exception:
            return OrderResult(ok=False, error="Unreadable Alice response", status="ERROR")
        if d.get("stat") == "Ok" and d.get("NOrdNo"):
            return OrderResult(ok=True, fill_price=current_price,
                               order_id=str(d["NOrdNo"]), status="PENDING")
        return OrderResult(ok=False, status="REJECTED", error=d.get("emsg") or "rejected")

    def place_entry(self, trade, current_price, qty=None):
        return self._place(trade, trade.side, current_price, qty)

    def place_exit(self, trade, current_price, qty=None):
        return self._place(trade, "SELL" if trade.side == "BUY" else "BUY", current_price, qty)

    def _orders(self):
        r = requests.get(f"{API_BASE}/placeOrder/fetchOrderBook",
                         headers=_hdr(self.user_id, self.session_id), timeout=6)
        d = r.json()
        return d if isinstance(d, list) else []

    def order_status(self, order_id):
        try:
            for o in self._orders():
                if str(o.get("Nstordno")) == str(order_id):
                    st = _ALICE_STATUS.get(str(o.get("Status", "")).upper(), "")
                    return (st, float(o.get("Avgprc") or 0),
                            int(float(o.get("Fillshares") or 0)), o.get("RejReason") or "")
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
            r = requests.post(f"{API_BASE}/limits/getRmsLimits", json={},
                              headers=_hdr(self.user_id, self.session_id), timeout=6)
            d = r.json()
            item = d[0] if isinstance(d, list) and d else d
            bal = item.get("net") or item.get("cashmarginavailable") or 0
            return True, float(bal)
        except Exception:
            return False, 0.0

    def get_orders(self):
        try:
            return [normalize_order(o) for o in self._orders()]
        except Exception:
            return []

    def open_positions(self):
        """(ok, {"<exch>:<token>": signed_net_qty}) for non-flat positions, so a
        square-off done in Alice's terminal can be detected. ok=False on failure."""
        try:
            r = requests.post(f"{API_BASE}/positionAndHoldings/positionBook",
                              json={"ret": "NET"},
                              headers=_hdr(self.user_id, self.session_id), timeout=6)
            if r.status_code != 200:
                return False, {}
            d = r.json()
            if not isinstance(d, list):     # error responses come back as a dict (emsg/stat)
                return False, {}
            out = {}
            for p in d:
                try:
                    net = float(p.get("Netqty") or p.get("netqty") or 0)
                except Exception:
                    net = 0.0
                if net == 0:
                    try:
                        net = float(p.get("Bqty") or 0) - float(p.get("Sqty") or 0)
                    except Exception:
                        net = 0.0
                tok = str(p.get("Token") or p.get("token") or "")
                exch = str(p.get("Exchange") or p.get("Exseg") or "")
                if tok and abs(net) > 0:
                    out[f"{exch}:{tok}"] = net
            return True, out
        except Exception:
            return False, {}

    def position_key(self, trade):
        a = self._alice_for(trade)
        return f"{a['exchange']}:{a['token']}" if a else None
