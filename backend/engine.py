"""
The trading engine — the heart of the app.

It runs forever in a background thread. On every tick (default every 1000ms) it:
  1. Fetches the REAL LTP (last traded price) of every active trade from Dhan,
     in a single batched request.
  2. For PENDING trades: checks if the entry condition is met -> enters.
  3. For OPEN trades: checks stop-loss / target -> exits.
  4. Respects the global KILL SWITCH.

BOTH test and live trades use real Dhan prices. The only difference:
  - TEST -> PaperBroker (records a simulated fill, no real order)
  - LIVE -> DhanBroker  (sends a real order to Dhan)

So Dhan must be connected for the engine to do anything (that's where LTP
comes from).
"""
import threading
import time

from database import SessionLocal
from models import Trade, LogEntry, Setting
from market_data import DhanMarketData
from live_feed import feed
from brokers import PaperBroker, DhanBroker
import config


class TradingEngine:
    def __init__(self):
        self._thread = None
        self._running = False
        self.paper = PaperBroker()
        self._last_rest = 0.0          # last time we used the REST fallback
        self._rest_cache = {}          # last REST prices (warmup / fallback)

    # ---------- lifecycle ----------
    def start(self):
        if self._running:
            return
        self._running = True
        feed.start()                   # start the real-time WebSocket feed
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ---------- settings helpers ----------
    def _get_setting(self, db, key, default=""):
        row = db.get(Setting, key)
        return row.value if row else default

    def _set_setting(self, db, key, value):
        row = db.get(Setting, key)
        if row:
            if row.value != value:        # avoid a DB write every tick
                row.value = value
        else:
            db.add(Setting(key=key, value=value))

    def _log(self, db, message, level="INFO", trade_id=0):
        db.add(LogEntry(message=message, level=level, trade_id=trade_id))

    def _kill_switch_on(self, db) -> bool:
        return self._get_setting(db, "kill_switch", "off") == "on"

    def _market_data(self, db):
        cid = self._get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
        tok = self._get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
        if not cid or not tok:
            return None
        return DhanMarketData(cid, tok)

    def _live_broker(self, db):
        cid = self._get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
        tok = self._get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
        return DhanBroker(cid, tok)

    # ---------- main loop ----------
    def _loop(self):
        while self._running:
            try:
                self._tick()
            except Exception as e:
                try:
                    db = SessionLocal()
                    self._log(db, f"Engine error: {e}", level="ERROR")
                    db.commit()
                    db.close()
                except Exception:
                    pass
            time.sleep(config.ENGINE_INTERVAL_MS / 1000.0)

    def _tick(self):
        db = SessionLocal()
        try:
            active = db.query(Trade).filter(Trade.status.in_(["PENDING", "OPEN"])).all()

            cid = self._get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
            tok = self._get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
            if not cid or not tok:
                if active:
                    self._set_setting(db, "md_status",
                                      "Dhan not connected — connect on the Broker tab to get prices.")
                db.commit()
                return

            instruments = list({(t.exchange_segment, str(t.security_id))
                                for t in active if t.security_id})

            prices = self._collect_prices(db, cid, tok, instruments)

            kill = self._kill_switch_on(db)
            live_broker = self._live_broker(db)

            for t in active:
                if not t.security_id:
                    continue
                price = prices.get((t.exchange_segment, str(t.security_id)), 0.0)
                if price <= 0:
                    continue
                t.last_price = price

                if t.status == "PENDING":
                    self._handle_pending(db, t, price, kill, live_broker)
                elif t.status == "OPEN":
                    self._update_pnl(t, price)
                    self._handle_open(db, t, price, kill, live_broker)

            db.commit()
        finally:
            db.close()

    def _collect_prices(self, db, cid, tok, instruments):
        """Prefer the real-time WebSocket feed; fall back to REST for anything
        not yet streaming (and if the websocket isn't connected)."""
        feed.configure(cid, tok)
        feed.ensure_subscribed(instruments)

        prices = {}
        for it in instruments:
            p = feed.get_ltp(it[0], it[1])
            if p > 0:
                prices[it] = p

        missing = [it for it in instruments if it not in prices]
        md = DhanMarketData(cid, tok)
        # Only hit REST about once a second to respect rate limits.
        if missing and (time.time() - self._last_rest) >= 1.0:
            self._last_rest = time.time()
            by_seg = {}
            for seg, sid in missing:
                by_seg.setdefault(seg, []).append(sid)
            rest = md.get_ltp_batch(by_seg)
            prices.update(rest)
            self._rest_cache.update(rest)
            if md.last_error and not prices:
                self._set_setting(db, "md_status", f"Price feed error: {md.last_error[:200]}")
        # use last known REST price for anything still missing
        for it in missing:
            if it not in prices and it in self._rest_cache:
                prices[it] = self._rest_cache[it]

        # status banner
        if not instruments:
            pass
        elif feed.connected:
            self._set_setting(db, "md_status", "ok:ws")
        elif prices:
            self._set_setting(db, "md_status", "ok:rest")
        return prices

    def _broker_for(self, t, live_broker):
        return self.paper if t.mode == "TEST" else live_broker

    # ---------- entry ----------
    def _handle_pending(self, db, t, price, kill, live_broker):
        if kill:
            return
        if not self._entry_triggered(t, price):
            return

        broker = self._broker_for(t, live_broker)
        res = broker.place_entry(t, price)
        if res.ok:
            t.status = "OPEN"
            t.entry_fill_price = res.fill_price
            t.broker_order_id = res.order_id
            self._log(db, f"ENTRY {t.side} {t.symbol} x{t.quantity} @ {res.fill_price} "
                          f"[{t.mode}/{broker.name}]", "INFO", t.id)
        else:
            t.status = "CANCELLED"
            t.exit_reason = "ENTRY_FAILED"
            self._log(db, f"Entry failed for {t.symbol}: {res.error}", "ERROR", t.id)

    def _entry_triggered(self, t, price):
        if t.entry_type == "MARKET":
            return True
        if t.entry_price <= 0:
            return True
        if t.side == "BUY":
            return price <= t.entry_price       # buy at/below entry
        return price >= t.entry_price           # short at/above entry

    # ---------- exit ----------
    def _handle_open(self, db, t, price, kill, live_broker):
        reason = None
        if kill:
            reason = "KILL"
        elif t.side == "BUY":
            if t.stop_loss > 0 and price <= t.stop_loss:
                reason = "STOPLOSS"
            elif t.target > 0 and price >= t.target:
                reason = "TARGET"
        else:
            if t.stop_loss > 0 and price >= t.stop_loss:
                reason = "STOPLOSS"
            elif t.target > 0 and price <= t.target:
                reason = "TARGET"

        if reason is None:
            return

        broker = self._broker_for(t, live_broker)
        res = broker.place_exit(t, price)
        if res.ok:
            t.exit_fill_price = res.fill_price
            t.status = "CLOSED"
            t.exit_reason = reason
            self._update_pnl(t, res.fill_price)
            self._log(db, f"EXIT ({reason}) {t.symbol} @ {res.fill_price} "
                          f"P&L={t.pnl:.2f} [{t.mode}/{broker.name}]", "INFO", t.id)
        else:
            self._log(db, f"Exit failed for {t.symbol}: {res.error}", "ERROR", t.id)

    def _update_pnl(self, t, price):
        if t.entry_fill_price <= 0:
            t.pnl = 0.0
            return
        direction = 1 if t.side == "BUY" else -1
        t.pnl = round((price - t.entry_fill_price) * direction * t.quantity, 2)


engine = TradingEngine()
