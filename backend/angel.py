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
