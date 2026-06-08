"""
Instrument master = every tradeable symbol on Dhan, organised the way a broker
app shows it:

  • search an UNDERLYING (NIFTY, BANKNIFTY, RELIANCE, ...)
  • for options: pick an EXPIRY, then see the full OPTION CHAIN (strikes x CE/PE)
  • for futures: pick an expiry
  • for equity: pick the stock directly

We download Dhan's public "detailed scrip master" CSV (no login needed), which
has clean UNDERLYING_SYMBOL / STRIKE / EXPIRY / OPTION_TYPE columns, and build
fast in-memory indexes from it. Refreshed automatically once a day.
"""
import csv
import io
import os
import threading
import datetime as dt

import requests

SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
# New filename so an old cache from a previous version is never reused by mistake.
CACHE_FILE = os.path.join(os.path.dirname(__file__), "instruments_detailed.csv")

# (exchange, segment-letter) -> the exchangeSegment string Dhan's API expects.
SEGMENT_MAP = {
    ("NSE", "E"): "NSE_EQ", ("NSE", "D"): "NSE_FNO", ("NSE", "C"): "NSE_CURRENCY",
    ("NSE", "I"): "IDX_I",
    ("BSE", "E"): "BSE_EQ", ("BSE", "D"): "BSE_FNO", ("BSE", "C"): "BSE_CURRENCY",
    ("BSE", "I"): "IDX_I",
    ("MCX", "M"): "MCX_COMM",
}


def _instr_type(instrument: str) -> str:
    i = (instrument or "").upper()
    if i.startswith("OPT"):
        return "OPTION"
    if i.startswith("FUT"):
        return "FUTURES"
    if i == "INDEX":
        return "INDEX"
    return "EQUITY"


def _to_float(s):
    try:
        return float(s)
    except Exception:
        return 0.0


class InstrumentStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._loaded_at = None
        self._loading = False
        # indexes
        self._equities = []          # list of equity/index rows (for Equity search)
        self._underlyings = {}       # underlying -> {display, has_option, has_future}
        self._opt = {}               # underlying -> expiry -> {strike: {"CE":row,"PE":row}}
        self._fut = {}               # underlying -> list of future rows
        self._by_id = {}             # security_id -> meta (for the Demo price simulator)
        self._spot_seed = {}         # underlying -> a realistic spot (median strike)

    # ---------- status ----------
    def status(self):
        return {
            "count": len(self._equities) + sum(
                len(v) for u in self._opt.values() for v in u.values()) * 2,
            "underlyings": len(self._underlyings),
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "loading": self._loading,
        }

    def ready(self):
        return len(self._underlyings) > 0

    # ---------- loading ----------
    def load_async(self):
        threading.Thread(target=self._load, daemon=True).start()

    def refresh(self):
        threading.Thread(target=self._load, kwargs={"force": True}, daemon=True).start()

    def _is_cache_fresh(self):
        if not os.path.exists(CACHE_FILE):
            return False
        age_h = (dt.datetime.now().timestamp() - os.path.getmtime(CACHE_FILE)) / 3600
        return age_h < 20

    def _download(self):
        r = requests.get(SCRIP_URL, timeout=90, headers={"User-Agent": "algo-trading"})
        r.raise_for_status()
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(r.text)

    def _load(self, force=False):
        if self._loading:
            return
        self._loading = True
        try:
            if force or not self._is_cache_fresh():
                self._download()
            self._parse(open(CACHE_FILE, "r", encoding="utf-8", errors="ignore").read())
            # Self-heal: if the cache was empty/old-format and produced nothing,
            # download a fresh copy once and re-parse.
            if not self.ready():
                self._download()
                self._parse(open(CACHE_FILE, "r", encoding="utf-8", errors="ignore").read())
            self._loaded_at = dt.datetime.utcnow()
        except Exception as e:
            print(f"[instruments] load failed: {e}")
            try:
                from database import SessionLocal
                from models import LogEntry
                db = SessionLocal()
                db.add(LogEntry(message=f"Symbol list download failed: {e}", level="ERROR"))
                db.commit()
                db.close()
            except Exception:
                pass
        finally:
            self._loading = False

    def _parse(self, text):
        equities, underlyings, opt, fut, by_id = [], {}, {}, {}, {}
        for r in csv.DictReader(io.StringIO(text)):
            exch = (r.get("EXCH_ID") or "").strip()
            seg = (r.get("SEGMENT") or "").strip()
            segment = SEGMENT_MAP.get((exch, seg))
            if not segment:
                continue
            sec_id = (r.get("SECURITY_ID") or "").strip()
            if not sec_id:
                continue
            itype = _instr_type(r.get("INSTRUMENT", ""))
            underlying = (r.get("UNDERLYING_SYMBOL") or "").strip()
            display = (r.get("DISPLAY_NAME") or r.get("SYMBOL_NAME") or "").strip()
            expiry = (r.get("SM_EXPIRY_DATE") or "").strip()[:10]
            row = {
                "security_id": sec_id,
                "exchange_segment": segment,
                "instrument_type": itype,
                "underlying": underlying,
                "symbol": display,
                "expiry": expiry,
                "strike": _to_float(r.get("STRIKE_PRICE")),
                "option_type": (r.get("OPTION_TYPE") or "").strip(),
                "lot_size": (r.get("LOT_SIZE") or "1").strip(),
            }

            by_id[sec_id] = {"strike": row["strike"], "option_type": row["option_type"],
                             "underlying": underlying, "instrument_type": itype}

            if itype in ("EQUITY", "INDEX"):
                row["_s"] = f"{display} {underlying}".lower()
                equities.append(row)
            elif itype == "OPTION":
                u = underlyings.setdefault(underlying, {"underlying": underlying,
                                                        "display": underlying,
                                                        "has_option": False, "has_future": False})
                u["has_option"] = True
                exp = opt.setdefault(underlying, {}).setdefault(expiry, {})
                pair = exp.setdefault(row["strike"], {"CE": None, "PE": None})
                if row["option_type"] in ("CE", "PE"):
                    pair[row["option_type"]] = row
            elif itype == "FUTURES":
                u = underlyings.setdefault(underlying, {"underlying": underlying,
                                                        "display": underlying,
                                                        "has_option": False, "has_future": False})
                u["has_future"] = True
                fut.setdefault(underlying, []).append(row)

        # A realistic "spot" per underlying = the middle strike of its options.
        import statistics
        spot_seed = {}
        for u, exps in opt.items():
            strikes = [s for e in exps.values() for s in e.keys() if s > 0]
            if strikes:
                spot_seed[u] = statistics.median(strikes)

        with self._lock:
            self._equities = equities
            self._underlyings = underlyings
            self._opt = opt
            self._fut = fut
            self._by_id = by_id
            self._spot_seed = spot_seed

    # ---------- searching ----------
    def search_underlyings(self, query, kind, limit=25):
        """kind = OPTION or FUTURES -> returns matching underlyings."""
        q = (query or "").strip().lower()
        if not q:
            return []
        flag = "has_option" if kind == "OPTION" else "has_future"
        out = []
        with self._lock:
            items = list(self._underlyings.values())
        for u in items:
            if not u[flag]:
                continue
            name = u["underlying"].lower()
            if q in name:
                out.append((0 if name.startswith(q) else 1, len(name),
                            {"underlying": u["underlying"], "display": u["display"], "kind": kind}))
        out.sort(key=lambda x: (x[0], x[1]))
        return [r for _, _, r in out[:limit]]

    def search_equities(self, query, limit=25):
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        out = []
        with self._lock:
            rows = self._equities
        for row in rows:
            if all(t in row["_s"] for t in terms):
                starts = 0 if row["_s"].startswith(terms[0]) else 1
                # prefer NSE over BSE for the same name
                exch_rank = 0 if row["exchange_segment"].startswith("NSE") else 1
                out.append((starts, exch_rank, len(row["symbol"]), row))
        out.sort(key=lambda x: (x[0], x[1], x[2]))
        return [{k: v for k, v in row.items() if not k.startswith("_")}
                for _, _, _, row in out[:limit]]

    # ---------- expiries / chain / futures ----------
    def expiries(self, underlying, kind="OPTION"):
        with self._lock:
            if kind == "OPTION":
                exps = list(self._opt.get(underlying, {}).keys())
            else:
                exps = sorted({r["expiry"] for r in self._fut.get(underlying, [])})
        return sorted([e for e in exps if e])

    def option_chain(self, underlying, expiry):
        with self._lock:
            by_strike = self._opt.get(underlying, {}).get(expiry, {})
            strikes = sorted(by_strike.keys())
            rows = []
            for s in strikes:
                pair = by_strike[s]
                rows.append({
                    "strike": s,
                    "ce": self._slim(pair.get("CE")),
                    "pe": self._slim(pair.get("PE")),
                })
        return {"underlying": underlying, "expiry": expiry,
                "expiries": self.expiries(underlying, "OPTION"), "strikes": rows}

    def futures(self, underlying):
        with self._lock:
            rows = sorted(self._fut.get(underlying, []), key=lambda r: r["expiry"])
        return [self._slim(r) for r in rows]

    # ---------- metadata (used by the Demo price simulator) ----------
    def get_meta(self, security_id):
        with self._lock:
            return self._by_id.get(str(security_id))

    def spot_seed(self, underlying):
        with self._lock:
            return self._spot_seed.get(underlying, 0.0)

    @staticmethod
    def _slim(row):
        if not row:
            return None
        return {
            "security_id": row["security_id"],
            "exchange_segment": row["exchange_segment"],
            "instrument_type": row["instrument_type"],
            "symbol": row["symbol"],
            "expiry": row["expiry"],
            "strike": row["strike"],
            "option_type": row["option_type"],
            "lot_size": row["lot_size"],
        }


store = InstrumentStore()
