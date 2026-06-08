"""
Instrument master = the full list of every tradeable symbol on Dhan.

We download Dhan's public "scrip master" CSV (no login needed), keep it in
memory, and let the dashboard SEARCH it. When you pick a symbol, we already
know its Security ID + Exchange Segment, so you never type those by hand.

This list is refreshed automatically once a day (symbols/expiries change daily).
"""
import csv
import io
import os
import threading
import datetime as dt

import requests

# Dhan's public symbol file (compact version is enough and smaller).
SCRIP_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
CACHE_FILE = os.path.join(os.path.dirname(__file__), "instruments.csv")

# (exchange, segment-letter) -> the exchangeSegment string Dhan's API expects.
SEGMENT_MAP = {
    ("NSE", "E"): "NSE_EQ",
    ("NSE", "D"): "NSE_FNO",
    ("NSE", "C"): "NSE_CURRENCY",
    ("NSE", "I"): "IDX_I",
    ("BSE", "E"): "BSE_EQ",
    ("BSE", "D"): "BSE_FNO",
    ("BSE", "C"): "BSE_CURRENCY",
    ("BSE", "I"): "IDX_I",
    ("MCX", "M"): "MCX_COMM",
}


def _instrument_type(name: str, option_type: str) -> str:
    n = (name or "").upper()
    if n.startswith("OPT") or option_type in ("CE", "PE"):
        return "OPTION"
    if n.startswith("FUT"):
        return "FUTURES"
    if n == "INDEX":
        return "INDEX"
    return "EQUITY"


class InstrumentStore:
    def __init__(self):
        self._rows = []                 # list of dicts (one per symbol)
        self._lock = threading.Lock()
        self._loaded_at = None
        self._loading = False

    # ---------- status ----------
    def status(self):
        return {
            "count": len(self._rows),
            "loaded_at": self._loaded_at.isoformat() if self._loaded_at else None,
            "loading": self._loading,
        }

    def ready(self) -> bool:
        return len(self._rows) > 0

    # ---------- loading ----------
    def load_async(self):
        """Load in a background thread so the web server starts instantly."""
        t = threading.Thread(target=self._load, daemon=True)
        t.start()

    def _is_cache_fresh(self) -> bool:
        if not os.path.exists(CACHE_FILE):
            return False
        age_hours = (dt.datetime.now().timestamp() - os.path.getmtime(CACHE_FILE)) / 3600
        return age_hours < 20  # refresh roughly once a day

    def _load(self, force: bool = False):
        if self._loading:
            return
        self._loading = True
        try:
            if force or not self._is_cache_fresh():
                self._download()
            text = open(CACHE_FILE, "r", encoding="utf-8", errors="ignore").read()
            self._parse(text)
            self._loaded_at = dt.datetime.utcnow()
        except Exception as e:
            print(f"[instruments] load failed: {e}")
        finally:
            self._loading = False

    def refresh(self):
        """Force a fresh download (used by the 'Refresh symbols' button)."""
        threading.Thread(target=self._load, kwargs={"force": True}, daemon=True).start()

    def _download(self):
        r = requests.get(SCRIP_URL, timeout=60, headers={"User-Agent": "algo-trading"})
        r.raise_for_status()
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            f.write(r.text)

    def _parse(self, text: str):
        rows = []
        reader = csv.DictReader(io.StringIO(text))
        for r in reader:
            exch = r.get("SEM_EXM_EXCH_ID", "").strip()
            seg = r.get("SEM_SEGMENT", "").strip()
            segment = SEGMENT_MAP.get((exch, seg))
            if not segment:
                continue  # skip anything we can't price/trade via the API
            sec_id = r.get("SEM_SMST_SECURITY_ID", "").strip()
            if not sec_id:
                continue
            custom = r.get("SEM_CUSTOM_SYMBOL", "").strip()
            tsym = r.get("SEM_TRADING_SYMBOL", "").strip()
            sname = r.get("SM_SYMBOL_NAME", "").strip()
            display = custom or tsym or sname
            rows.append({
                "security_id": sec_id,
                "exchange_segment": segment,
                "instrument_type": _instrument_type(
                    r.get("SEM_INSTRUMENT_NAME", ""), r.get("SEM_OPTION_TYPE", "")),
                "symbol": display,
                "trading_symbol": tsym,
                "lot_size": r.get("SEM_LOT_UNITS", "") or "1",
                "expiry": r.get("SEM_EXPIRY_DATE", "").strip(),
                # precomputed lowercase blob for fast searching
                "_s": f"{display} {tsym} {sname}".lower(),
            })
        with self._lock:
            self._rows = rows

    # ---------- search ----------
    def search(self, query: str, limit: int = 25):
        q = (query or "").strip().lower()
        if not q:
            return []
        terms = q.split()
        out = []
        with self._lock:
            rows = self._rows
        for row in rows:
            blob = row["_s"]
            if all(t in blob for t in terms):
                # rank: exact-ish (starts with) and shorter symbols first
                starts = 0 if blob.startswith(terms[0]) else 1
                out.append((starts, len(row["symbol"]), row))
                if len(out) > 2000:      # cap work on very broad queries
                    break
        out.sort(key=lambda x: (x[0], x[1]))
        return [
            {k: v for k, v in row.items() if not k.startswith("_")}
            for _, _, row in out[:limit]
        ]

    def get(self, security_id: str, exchange_segment: str = ""):
        with self._lock:
            for row in self._rows:
                if row["security_id"] == str(security_id) and (
                        not exchange_segment or row["exchange_segment"] == exchange_segment):
                    return row
        return None


# Shared instance.
store = InstrumentStore()
