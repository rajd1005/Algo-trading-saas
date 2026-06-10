"""
Real-time tick-by-tick price feeds — one persistent WebSocket per connected
broker ACCOUNT (so each tenant streams with its own credentials).

Design
------
- Every broker pushes ticks over its own WebSocket protocol (Dhan/Angel/Zerodha
  binary, Alice Blue JSON). Each client here connects, subscribes by the broker's
  own token (translated from our Dhan-space security_id via the symbol mappers),
  and caches the latest LTP keyed by (dhan_segment, dhan_security_id) so the rest
  of the app — which works in Dhan space — reads prices uniformly.
- A single FeedManager keeps one feed per account, started lazily, subscribed to
  whatever instruments are asked for, and stopped after a few idle minutes.
- SAFETY NET: this is an *enhancement*. The engine and /api/ltp still fall back to
  the REST quote APIs for anything the socket isn't streaming yet (or if the
  socket can't connect at all), so prices never stop flowing.

The broker socket protocols can't be exercised without live logins; they're coded
to each broker's published spec and verified on the deployment.
"""
import hashlib
import json
import struct
import threading
import time

try:
    import websocket  # websocket-client
    _HAVE_WS = True
except Exception:
    _HAVE_WS = False

import angel
import zerodha
import aliceblue

IDLE_STOP_SECS = 300       # stop a feed after 5 min with no reads/subscribes


# ============================================================================
# Base feed: lifecycle, cache, idle handling. Subclasses implement the protocol.
# ============================================================================
class BaseFeed:
    broker = "BASE"

    def __init__(self, account_id, creds, client_id):
        self.account_id = account_id
        self.creds = creds or {}
        self.client_id = client_id
        self._ws = None
        self._running = False
        self._thread = None
        self._lock = threading.Lock()
        self._ltp = {}                 # (seg, secid) -> price
        self._want = set()             # (seg, secid) we intend to stream
        self._subscribed = set()       # already sent to the broker
        self._tok2inst = {}            # broker subscribe-key -> (seg, secid)
        self.connected = False
        self.last_error = ""
        self.last_used = time.time()
        self.last_tick = 0.0           # when we last stored a real tick (freshness)

    # ---- lifecycle ----
    def start(self):
        if self._running or not _HAVE_WS:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self.connected = False

    def _run_loop(self):
        backoff = 3
        while self._running:
            try:
                self._ws = websocket.WebSocketApp(
                    self._url(), header=self._headers(),
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close)
                # A connection that opened resets the backoff; one that fails fast grows it.
                started = time.time()
                self._ws.run_forever(ping_interval=25, ping_timeout=10)
                backoff = 3 if (time.time() - started) > 30 else min(backoff * 2, 30)
            except Exception as e:
                self.last_error = str(e)
                backoff = min(backoff * 2, 30)
            self.connected = False
            if self._running:
                time.sleep(backoff)    # exponential backoff so a bad socket can't hammer

    # ---- reads / subscriptions (used by the manager) ----
    def subscribe(self, instruments):
        """instruments = iterable of (segment, security_id)."""
        self.last_used = time.time()
        new = []
        with self._lock:
            for it in instruments:
                it = (it[0], str(it[1]))
                if it not in self._want:
                    self._want.add(it)
                    new.append(it)
        if new and self.connected:
            self._do_subscribe(new)

    def snapshot(self, instruments):
        self.last_used = time.time()
        with self._lock:
            return {it: self._ltp[(it[0], str(it[1]))]
                    for it in ((s, str(i)) for s, i in instruments)
                    if it in self._ltp}

    def idle(self):
        return (time.time() - self.last_used) > IDLE_STOP_SECS

    # ---- websocket callbacks ----
    def _on_open(self, ws):
        self.connected = True
        self.last_error = ""
        with self._lock:
            want = list(self._want)
            self._subscribed.clear()
        if want:
            self._do_subscribe(want)

    def _on_error(self, ws, err):
        self.last_error = str(err)[:200]

    def _on_close(self, ws, *a):
        self.connected = False

    def _on_message(self, ws, message):
        try:
            self._parse(message)
        except Exception as e:
            self.last_error = f"parse: {e}"

    def _store(self, seg, secid, price):
        if price and price > 0:
            with self._lock:
                self._ltp[(seg, str(secid))] = round(float(price), 2)
            self.last_tick = time.time()

    # ---- to be implemented per broker ----
    def _url(self):  raise NotImplementedError
    def _headers(self):  return None
    def _do_subscribe(self, instruments):  raise NotImplementedError
    def _parse(self, message):  raise NotImplementedError


# ============================================================================
# Dhan — binary Live Market Feed (RequestCode 15 = LTP)
# ============================================================================
_DHAN_SEG_INT = {0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY",
                 4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO"}


class DhanFeed(BaseFeed):
    broker = "DHAN"

    def _url(self):
        tok = self.creds.get("access_token", "")
        return (f"wss://api-feed.dhan.co?version=2&token={tok}"
                f"&clientId={self.client_id}&authType=2")

    def _do_subscribe(self, instruments):
        with self._lock:
            instruments = [i for i in instruments if i not in self._subscribed]
            self._subscribed.update(instruments)
        for i in range(0, len(instruments), 100):
            batch = instruments[i:i + 100]
            msg = {"RequestCode": 15, "InstrumentCount": len(batch),
                   "InstrumentList": [{"ExchangeSegment": s, "SecurityId": str(sid)} for s, sid in batch]}
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                self.last_error = str(e)

    def _parse(self, buf):
        if not isinstance(buf, (bytes, bytearray)):
            return
        n, off = len(buf), 0
        while off + 8 <= n:
            code = buf[off]
            length = int.from_bytes(buf[off + 1:off + 3], "little")
            if length <= 0 or off + length > n:
                break
            if code == 2 and off + 16 <= n:                 # Ticker / LTP
                seg = _DHAN_SEG_INT.get(buf[off + 3], str(buf[off + 3]))
                sec = int.from_bytes(buf[off + 4:off + 8], "little")
                ltp = struct.unpack_from("<f", buf, off + 8)[0]
                self._store(seg, sec, ltp)
            off += length


# ============================================================================
# Angel One — SmartStream v2 (binary, mode 1 = LTP)
# ============================================================================
# Dhan segment -> Angel SmartStream exchangeType
_ANGEL_EXCH_TYPE = {"NSE_EQ": 1, "IDX_I": 1, "NSE_FNO": 2, "BSE_EQ": 3,
                    "BSE_FNO": 4, "MCX_COMM": 5, "NSE_CURRENCY": 13, "BSE_CURRENCY": 13}
_ANGEL_TYPE_SEG = {1: "NSE_EQ", 2: "NSE_FNO", 3: "BSE_EQ", 4: "BSE_FNO", 5: "MCX_COMM", 13: "NSE_CURRENCY"}


class AngelFeed(BaseFeed):
    broker = "ANGEL"

    def _url(self):
        return "wss://smartapisocket.angelone.in/smart-stream"

    def _headers(self):
        return ["Authorization: " + self.creds.get("jwt", ""),
                "x-api-key: " + self.creds.get("api_key", ""),
                "x-client-code: " + str(self.client_id),
                "x-feed-token: " + self.creds.get("feed", "")]

    def _do_subscribe(self, instruments):
        from instruments import store
        by_type = {}
        with self._lock:
            for seg, sid in instruments:
                if (seg, sid) in self._subscribed:
                    continue
                a = angel.mapper.translate(sid, seg, store.get_meta(sid))
                et = _ANGEL_EXCH_TYPE.get(seg)
                if a and et:
                    by_type.setdefault(et, []).append(a["token"])
                    self._tok2inst[(et, str(a["token"]))] = (seg, str(sid))
                    self._subscribed.add((seg, sid))
        if not by_type:
            return
        msg = {"correlationID": f"a{self.account_id}", "action": 1,
               "params": {"mode": 1, "tokenList": [
                   {"exchangeType": et, "tokens": toks} for et, toks in by_type.items()]}}
        try:
            self._ws.send(json.dumps(msg))
        except Exception as e:
            self.last_error = str(e)

    def _parse(self, message):
        if not isinstance(message, (bytes, bytearray)) or len(message) < 51:
            return
        mode = message[0]
        if mode != 1:
            return
        et = message[1]
        token = message[2:27].split(b"\x00", 1)[0].decode(errors="ignore")
        ltp = int.from_bytes(message[43:51], "little", signed=True) / 100.0
        inst = self._tok2inst.get((et, token))
        if inst is None:
            seg = _ANGEL_TYPE_SEG.get(et)
            inst = (seg, token) if seg else None
        if inst:
            self._store(inst[0], inst[1], ltp)


# ============================================================================
# Zerodha — KiteTicker (binary; subscribes by instrument_token; LTP mode)
# ============================================================================
class ZerodhaFeed(BaseFeed):
    broker = "ZERODHA"

    def _url(self):
        return (f"wss://ws.kite.trade?api_key={self.creds.get('api_key', '')}"
                f"&access_token={self.creds.get('access_token', '')}")

    def _do_subscribe(self, instruments):
        from instruments import store
        tokens = []
        with self._lock:
            for seg, sid in instruments:
                if (seg, sid) in self._subscribed:
                    continue
                z = zerodha.mapper.translate(sid, seg, store.get_meta(sid))
                if z and z.get("instrument_token"):
                    it = int(z["instrument_token"])
                    tokens.append(it)
                    self._tok2inst[it] = (seg, str(sid))
                    self._subscribed.add((seg, sid))
        if not tokens:
            return
        try:
            self._ws.send(json.dumps({"a": "subscribe", "v": tokens}))
            self._ws.send(json.dumps({"a": "mode", "v": ["ltp", tokens]}))
        except Exception as e:
            self.last_error = str(e)

    def _parse(self, message):
        if not isinstance(message, (bytes, bytearray)) or len(message) < 2:
            return
        count = int.from_bytes(message[0:2], "big")
        off = 2
        for _ in range(count):
            if off + 2 > len(message):
                break
            plen = int.from_bytes(message[off:off + 2], "big"); off += 2
            if off + plen > len(message):
                break
            pkt = message[off:off + plen]; off += plen
            if plen >= 8:
                tok = int.from_bytes(pkt[0:4], "big")
                ltp = int.from_bytes(pkt[4:8], "big") / 100.0
                inst = self._tok2inst.get(tok)
                if inst:
                    self._store(inst[0], inst[1], ltp)


# ============================================================================
# Alice Blue — Noren WebSocket (JSON)
# ============================================================================
_ALICE_DHAN_EXCH = {"NSE_EQ": "NSE", "NSE_FNO": "NFO", "BSE_EQ": "BSE", "BSE_FNO": "BFO",
                    "MCX_COMM": "MCX", "NSE_CURRENCY": "CDS", "IDX_I": "NSE"}
_ALICE_EXCH_DHAN = {v: k for k, v in _ALICE_DHAN_EXCH.items()}


class AliceFeed(BaseFeed):
    broker = "ALICE"

    def _url(self):
        return "wss://ws1.aliceblueonline.com/NorenWS/"

    def _on_open(self, ws):
        # Noren handshake first; subscriptions go out on the 'ck' ack.
        sess = self.creds.get("session_id", "")
        su = hashlib.sha256(sess.encode()).hexdigest()
        try:
            ws.send(json.dumps({"t": "c", "uid": self.client_id, "actid": self.client_id,
                                "source": "API", "susertoken": su}))
        except Exception as e:
            self.last_error = str(e)

    def _do_subscribe(self, instruments):
        from instruments import store
        keys = []
        with self._lock:
            for seg, sid in instruments:
                if (seg, sid) in self._subscribed:
                    continue
                al = aliceblue.mapper.translate(sid, seg, store.get_meta(sid))
                if al and al.get("token"):
                    ex = al["exchange"]
                    keys.append(f"{ex}|{al['token']}")
                    self._tok2inst[(ex, str(al["token"]))] = (seg, str(sid))
                    self._subscribed.add((seg, sid))
        if keys:
            try:
                self._ws.send(json.dumps({"t": "t", "k": "#".join(keys)}))
            except Exception as e:
                self.last_error = str(e)

    def _parse(self, message):
        try:
            d = json.loads(message)
        except Exception:
            return
        t = d.get("t")
        if t == "ck":                                   # connect ack -> subscribe
            self.connected = True
            with self._lock:
                want = list(self._want); self._subscribed.clear()
            if want:
                self._do_subscribe(want)
            return
        if t in ("tf", "tk") and ("lp" in d):           # touchline / tick
            ex, tok = d.get("e", ""), str(d.get("tk", ""))
            inst = self._tok2inst.get((ex, tok))
            if inst is None:
                seg = _ALICE_EXCH_DHAN.get(ex)
                inst = (seg, tok) if seg else None
            if inst:
                try:
                    self._store(inst[0], inst[1], float(d["lp"]))
                except Exception:
                    pass


_FEED_CLASSES = {"DHAN": DhanFeed, "ANGEL": AngelFeed, "ZERODHA": ZerodhaFeed, "ALICE": AliceFeed}


# ============================================================================
# Manager — one feed per account, lazily started, idle-stopped
# ============================================================================
class FeedManager:
    def __init__(self):
        self._feeds = {}               # account_id -> feed
        self._lock = threading.Lock()
        threading.Thread(target=self._reaper, daemon=True).start()

    @property
    def available(self):
        return _HAVE_WS

    def _reaper(self):
        while True:
            time.sleep(60)
            try:
                with self._lock:
                    dead = [aid for aid, f in self._feeds.items() if f.idle()]
                    for aid in dead:
                        self._feeds.pop(aid).stop()
            except Exception:
                pass

    def _feed_for(self, account):
        """account = SQLAlchemy Account (must be connected). Returns a started feed."""
        cls = _FEED_CLASSES.get(account.broker)
        if cls is None or not _HAVE_WS:
            return None
        try:
            creds = json.loads(account.creds_json or "{}")
        except Exception:
            creds = {}
        with self._lock:
            f = self._feeds.get(account.id)
            if f is None or f.creds != creds:
                if f is not None:
                    f.stop()
                f = cls(account.id, creds, account.client_id)
                self._feeds[account.id] = f
                f.start()
            return f

    def snapshot(self, account, instruments, subscribe=True):
        """Ensure the account's feed streams these instruments; return cached
        {(seg, secid): price}. Empty if WS unavailable — caller does REST fallback."""
        f = self._feed_for(account)
        if f is None:
            return {}
        if subscribe:
            f.subscribe(instruments)
        return f.snapshot(instruments)

    def snapshot_cached(self, account_id, instruments):
        """Read cached ticks for an already-running feed WITHOUT creating one.
        Used by the SSE push loop (no DB / ORM needed). Empty if no live feed."""
        f = self._feeds.get(account_id)
        return f.snapshot(instruments) if f else {}

    # Consider the socket "streaming" only if a real tick arrived recently — a
    # connected-but-silent socket must NOT be shown as live real-time prices.
    FRESH_SECS = 6

    def status(self, account_id):
        f = self._feeds.get(account_id)
        if not f:
            return {"connected": False, "streaming": False, "error": ""}
        streaming = bool(f.connected and (time.time() - f.last_tick) < self.FRESH_SECS)
        return {"connected": f.connected, "streaming": streaming, "error": f.last_error}

    def stop(self, account_id):
        with self._lock:
            f = self._feeds.pop(account_id, None)
        if f:
            f.stop()


manager = FeedManager()
