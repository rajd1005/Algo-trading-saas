"""
The trading engine — the heart of the app.

It runs forever in a background thread. On every tick (default every 1000ms) it:
  1. Reads the current price of each active trade.
  2. For PENDING trades: checks if the entry condition is met -> enters.
  3. For OPEN trades: checks stop-loss / target -> exits.
  4. Respects the global KILL SWITCH (stops new entries; you can also flat-all).

TEST trades use the simulated market + PaperBroker.
LIVE trades use real Dhan prices + DhanBroker.
"""
import threading
import time
import datetime as dt

from database import SessionLocal
from models import Trade, LogEntry, Setting
from market_data import sim_market, DhanMarketData
from brokers import PaperBroker, DhanBroker
import config


class TradingEngine:
    def __init__(self):
        self._thread = None
        self._running = False
        self.paper = PaperBroker()

    # ---------- lifecycle ----------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ---------- helpers ----------
    def _get_setting(self, db, key, default=""):
        row = db.get(Setting, key)
        return row.value if row else default

    def _log(self, db, message, level="INFO", trade_id=0):
        db.add(LogEntry(message=message, level=level, trade_id=trade_id))

    def _kill_switch_on(self, db) -> bool:
        return self._get_setting(db, "kill_switch", "off") == "on"

    def _live_broker(self, db):
        cid = self._get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
        tok = self._get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
        if not cid or not tok:
            return None, None
        return DhanBroker(cid, tok), DhanMarketData(cid, tok)

    # ---------- main loop ----------
    def _loop(self):
        while self._running:
            try:
                self._tick()
            except Exception as e:  # never let the engine die
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
            kill = self._kill_switch_on(db)
            live_broker, live_md = self._live_broker(db)

            active = db.query(Trade).filter(Trade.status.in_(["PENDING", "OPEN"])).all()
            for t in active:
                price = self._price_for(t, live_md)
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

    def _price_for(self, t, live_md):
        if t.mode == "LIVE":
            if live_md is None:
                return 0.0
            return live_md.get_ltp(t.exchange_segment, t.security_id)
        # TEST mode: simulated price, seeded near the entry price.
        return sim_market.get_ltp(t.symbol, hint_price=t.entry_price)

    def _broker_for(self, t, live_broker):
        return self.paper if t.mode == "TEST" else live_broker

    # ---------- entry ----------
    def _handle_pending(self, db, t, price, kill, live_broker):
        if kill:
            return  # kill switch blocks all new entries
        if not self._entry_triggered(t, price):
            return

        broker = self._broker_for(t, live_broker)
        if broker is None:
            self._log(db, "LIVE entry skipped: Dhan not connected.", "WARN", t.id)
            return

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
        # MARKET entry triggers immediately.
        if t.entry_type == "MARKET":
            return True
        # LIMIT entry: for a BUY, trigger when price <= entry_price (buy the dip);
        # for a SELL, trigger when price >= entry_price (sell the rip).
        if t.entry_price <= 0:
            return True
        if t.side == "BUY":
            return price <= t.entry_price
        return price >= t.entry_price

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
        else:  # SELL / short
            if t.stop_loss > 0 and price >= t.stop_loss:
                reason = "STOPLOSS"
            elif t.target > 0 and price <= t.target:
                reason = "TARGET"

        if reason is None:
            return

        broker = self._broker_for(t, live_broker)
        if broker is None:
            self._log(db, "LIVE exit skipped: Dhan not connected.", "WARN", t.id)
            return

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


# A single shared engine instance.
engine = TradingEngine()
