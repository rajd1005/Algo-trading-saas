"""
Real-time price feed using Dhan's WebSocket Live Market Feed.

Instead of asking Dhan for prices every second (REST polling), this opens a
persistent WebSocket connection and Dhan PUSHES every price tick to us the
instant it changes — true real-time. The latest price for each instrument is
kept in an in-memory cache that the engine reads instantly (no network per check).

Protocol (DhanHQ v2):
  • connect:   wss://api-feed.dhan.co?version=2&token=..&clientId=..&authType=2
  • subscribe: JSON  {"RequestCode":15, "InstrumentCount":n, "InstrumentList":[...]}
               (RequestCode 15 = Ticker/LTP, max 100 instruments per message)
  • receive:   little-endian BINARY packets. Ticker packet (feed code 2, 16 bytes):
               [0]=code [1:3]=len [3]=segment [4:8]=securityId [8:12]=LTP(float32)
               [12:16]=lastTradeTime(int32)

Falls back silently if the websocket library or connection isn't available — the
engine then keeps using the REST feed.
"""
import json
import struct
import threading
import time

try:
    import websocket  # from the websocket-client package
    _HAVE_WS = True
except Exception:
    _HAVE_WS = False

# Dhan numeric exchange-segment codes -> the string names we use everywhere else.
SEG_INT_TO_STR = {
    0: "IDX_I", 1: "NSE_EQ", 2: "NSE_FNO", 3: "NSE_CURRENCY",
    4: "BSE_EQ", 5: "MCX_COMM", 7: "BSE_CURRENCY", 8: "BSE_FNO",
}


class DhanLiveFeed:
    def __init__(self):
        self._ws = None
        self._thread = None
        self._running = False
        self._client_id = None
        self._token = None
        self._lock = threading.Lock()
        self._ltp = {}            # (segment_str, security_id_str) -> price
        self._subscribed = set()  # the instruments we want streamed
        self.connected = False
        self.last_error = ""

    @property
    def available(self):
        return _HAVE_WS

    # ---------- lifecycle ----------
    def start(self):
        if self._running or not _HAVE_WS:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def configure(self, client_id, token):
        """Called whenever creds are known; reconnects if they changed."""
        if not _HAVE_WS:
            return
        if client_id == self._client_id and token == self._token:
            return
        self._client_id, self._token = client_id, token
        with self._lock:
            self._subscribed.clear()
            self._ltp.clear()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass  # the run loop will reconnect with the new creds

    def _url(self):
        return (f"wss://api-feed.dhan.co?version=2&token={self._token}"
                f"&clientId={self._client_id}&authType=2")

    def _run_loop(self):
        while self._running:
            if not self._client_id or not self._token:
                time.sleep(2)
                continue
            try:
                self._ws = websocket.WebSocketApp(
                    self._url(),
                    on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close)
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.last_error = str(e)
            self.connected = False
            time.sleep(3)  # backoff before reconnecting

    # ---------- websocket callbacks ----------
    def _on_open(self, ws):
        self.connected = True
        self.last_error = ""
        with self._lock:
            want = list(self._subscribed)
        if want:
            self._send_subscribe(want)

    def _on_error(self, ws, err):
        self.last_error = str(err)

    def _on_close(self, ws, *a):
        self.connected = False

    def _on_message(self, ws, message):
        if isinstance(message, (bytes, bytearray)):
            self._parse(message)

    def _parse(self, buf):
        n = len(buf)
        off = 0
        while off + 8 <= n:
            code = buf[off]
            length = int.from_bytes(buf[off + 1:off + 3], "little")
            if length <= 0 or off + length > n:
                break
            if code == 2 and off + 16 <= n:                 # Ticker / LTP packet
                seg = buf[off + 3]
                sec_id = int.from_bytes(buf[off + 4:off + 8], "little")
                ltp = struct.unpack_from("<f", buf, off + 8)[0]
                seg_str = SEG_INT_TO_STR.get(seg, str(seg))
                with self._lock:
                    self._ltp[(seg_str, str(sec_id))] = round(float(ltp), 2)
            elif code == 50:                                # server disconnect
                self.last_error = "Server sent disconnect packet"
            off += length

    # ---------- subscriptions ----------
    def ensure_subscribed(self, instruments):
        """instruments = list of (segment_str, security_id_str)."""
        if not _HAVE_WS:
            return
        new = []
        with self._lock:
            for it in instruments:
                if it not in self._subscribed:
                    self._subscribed.add(it)
                    new.append(it)
        if new and self.connected:
            self._send_subscribe(new)

    def _send_subscribe(self, instruments):
        for i in range(0, len(instruments), 100):
            batch = instruments[i:i + 100]
            msg = {
                "RequestCode": 15,
                "InstrumentCount": len(batch),
                "InstrumentList": [
                    {"ExchangeSegment": seg, "SecurityId": str(sid)} for seg, sid in batch
                ],
            }
            try:
                self._ws.send(json.dumps(msg))
            except Exception as e:
                self.last_error = str(e)

    # ---------- reads ----------
    def get_ltp(self, segment, security_id):
        with self._lock:
            return self._ltp.get((segment, str(security_id)), 0.0)


feed = DhanLiveFeed()
