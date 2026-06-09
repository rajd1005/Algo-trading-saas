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
import json
import math
import threading
import time

from database import SessionLocal
from models import Trade, LogEntry, Setting
from market_data import DhanMarketData, demo_market
from live_feed import feed
from brokers import PaperBroker, DhanBroker
import config


def level_price(side, ref, points, is_target):
    """Convert a points distance into an absolute price, given the entry ref."""
    if points <= 0 or ref <= 0:
        return 0.0
    if side == "BUY":
        return round(ref + points, 2) if is_target else round(ref - points, 2)
    return round(ref - points, 2) if is_target else round(ref + points, 2)


class TradingEngine:
    def __init__(self):
        self._thread = None
        self._running = False
        self.paper = PaperBroker()
        self._last_rest = 0.0          # last time we used the REST fallback
        self._rest_cache = {}          # last REST prices (warmup / fallback)
        self._rest_cooldown = 0.0      # back off REST until this time (after a 429)
        self._demo = False             # DEMO mode (simulated prices + fake broker)
        self._last_feed_err = ""       # de-dupe feed errors in the log
        self._last_rest_err = ""

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

            self._demo = self._get_setting(db, "broker_mode", "DHAN") == "DEMO"
            instruments = list({(t.exchange_segment, str(t.security_id))
                                for t in active if t.security_id})

            if self._demo:
                by_seg = {}
                for seg, sid in instruments:
                    by_seg.setdefault(seg, []).append(sid)
                prices = demo_market.get_ltp_batch(by_seg)
                if active:
                    self._set_setting(db, "md_status", "ok:demo")
            else:
                cid = self._get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID)
                tok = self._get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN)
                if not cid or not tok:
                    if active:
                        self._set_setting(db, "md_status",
                                          "Dhan not connected — connect on the Broker tab to get prices.")
                    db.commit()
                    return
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

        # Log websocket feed errors (de-duplicated) so they appear in the log.
        if feed.last_error and feed.last_error != self._last_feed_err:
            self._last_feed_err = feed.last_error
            self._log(db, f"WebSocket price feed error: {feed.last_error[:250]}", "ERROR")

        prices = {}
        for it in instruments:
            p = feed.get_ltp(it[0], it[1])
            if p > 0:
                prices[it] = p

        missing = [it for it in instruments if it not in prices]
        md = DhanMarketData(cid, tok)
        now = time.time()
        # Only hit REST every ~2s, and not at all during a 429 cool-down. The
        # WebSocket feed is the primary source; REST is just a fallback.
        if missing and now >= self._rest_cooldown and (now - self._last_rest) >= 2.0:
            self._last_rest = now
            by_seg = {}
            for seg, sid in missing:
                by_seg.setdefault(seg, []).append(sid)
            rest = md.get_ltp_batch(by_seg)
            prices.update(rest)
            self._rest_cache.update(rest)
            if md.last_error:
                if "429" in md.last_error or "Too many" in md.last_error:
                    self._rest_cooldown = now + 30      # back off for 30s
                    self._log(db, "Dhan rate-limited the price feed (429) — backing off REST "
                                  "for 30s; using the WebSocket feed.", "WARN")
                elif md.last_error != self._last_rest_err:
                    self._last_rest_err = md.last_error
                    self._log(db, f"REST price feed error: {md.last_error[:250]}", "ERROR")
                if not prices:
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
        # DEMO mode never sends real orders; TEST mode is always paper.
        if self._demo or t.mode == "TEST":
            return self.paper
        return live_broker

    def _place_confirmed(self, db, t, broker, is_exit, qty):
        """Place an order and, for live Dhan, VERIFY it actually executed —
        retry on rejection and sync our records to Dhan's real traded price,
        so our order details match the broker's."""
        place = broker.place_exit if is_exit else broker.place_entry
        price = t.last_price or t.entry_fill_price or t.entry_price
        kind = "EXIT" if is_exit else "ENTRY"
        res = None
        attempt = 0
        while attempt <= config.ORDER_RETRIES:
            res = place(t, price, qty=qty)
            if not res.ok:
                attempt += 1
                self._log(db, f"{kind} order failed (try {attempt}): {res.error}", "ERROR", t.id)
                continue
            if broker.name != "DHAN":
                return res                          # paper/demo fills at live price
            status, traded_price, _ = broker.confirm(res.order_id)
            if status == "TRADED":
                if traded_price > 0:
                    if abs(traded_price - res.fill_price) > 0.001:
                        self._log(db, f"{kind} reconciled to Dhan fill {traded_price} "
                                      f"(provisional {res.fill_price})", "INFO", t.id)
                    res.fill_price = traded_price
                res.status = "TRADED"
                return res
            if status in ("REJECTED", "CANCELLED", "EXPIRED"):
                attempt += 1
                self._log(db, f"{kind} {status} by broker — re-placing ({attempt})", "WARN", t.id)
                continue
            # still pending/unknown: accept provisional fill but flag it
            self._log(db, f"{kind} order {res.order_id} status '{status or 'unknown'}'; "
                          f"recorded at live price {res.fill_price}", "WARN", t.id)
            return res
        return res

    # ---------- entry ----------
    def _handle_pending(self, db, t, price, kill, live_broker):
        if kill:
            return
        if not self._entry_triggered(t, price):
            return

        broker = self._broker_for(t, live_broker)
        res = self._place_confirmed(db, t, broker, is_exit=False, qty=t.quantity)
        if res.ok:
            t.status = "OPEN"
            t.entry_fill_price = res.fill_price
            t.broker_order_id = res.order_id
            self._apply_levels(t)        # compute SL/target prices from the fill
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

    # ---------- levels / targets ----------
    def _apply_levels(self, t):
        """Once entered, convert points into absolute SL/target prices using the
        real fill price. Targets are stored with absolute prices from here on."""
        # Fixed stop-loss: this is the starting stop that the trail will ratchet up.
        if t.sl_points and t.sl_points > 0:
            t.stop_loss = level_price(t.side, t.entry_fill_price, t.sl_points, False)
        if t.trail_sl and t.trail_sl > 0:
            t.hwm = t.entry_fill_price          # step reference for trailing
            if not (t.sl_points and t.sl_points > 0):
                # no fixed SL given -> start the stop a trail-distance away
                t.stop_loss = level_price(t.side, t.entry_fill_price, t.trail_sl, False)
        targets = self._targets(t)
        if targets:
            # Target points are CUMULATIVE: each target is measured from the
            # previous one. So points [100, 100] from entry 200 -> 300, then 400.
            running = 0.0
            for tg in targets:
                if not tg.get("price"):
                    running += tg.get("points", 0)
                    tg["price"] = level_price(t.side, t.entry_fill_price, running, True)
            t.targets_json = json.dumps(targets)
            t.target = targets[0].get("price", 0)
        elif t.target_points and t.target_points > 0:
            t.target = level_price(t.side, t.entry_fill_price, t.target_points, True)

    def _target_price(self, t, tg):
        return tg.get("price") or level_price(t.side, t.entry_fill_price, tg.get("points", 0), True)

    def _targets(self, t):
        if not t.targets_json:
            return []
        try:
            return json.loads(t.targets_json)
        except Exception:
            return []

    # ---------- exit ----------
    def _handle_open(self, db, t, price, kill, live_broker):
        direction = 1 if t.side == "BUY" else -1
        remaining = t.quantity - (t.exited_qty or 0)
        if remaining <= 0:
            t.status = "CLOSED"
            return
        broker = self._broker_for(t, live_broker)

        # 0) Trailing stop-loss: each time the price advances another trail-step,
        #    raise the stop-loss by the same step (it RESPECTS the fixed SL, which
        #    is just where the stop starts). e.g. SL 300, trail 5 -> 305, 310, 315...
        #    trail_mode ENTRY caps the trailed stop at entry (breakeven) then stops.
        if t.trail_sl and t.trail_sl > 0:
            step = t.trail_sl
            cap_entry = (t.trail_mode == "ENTRY")
            if t.side == "BUY":
                steps = math.floor((price - (t.hwm or 0)) / step)
                if steps > 0:
                    t.hwm = (t.hwm or 0) + steps * step
                    new_sl = round((t.stop_loss or 0) + steps * step, 2)
                    if cap_entry:
                        new_sl = min(new_sl, t.entry_fill_price)
                    if new_sl > (t.stop_loss or 0):
                        t.stop_loss = new_sl
                        self._log(db, f"Trailing SL → {new_sl} (price {price}) {t.symbol}", "INFO", t.id)
            else:
                ref = t.hwm if t.hwm else t.entry_fill_price
                steps = math.floor((ref - price) / step)
                if steps > 0:
                    t.hwm = ref - steps * step
                    new_sl = round((t.stop_loss or 0) - steps * step, 2)
                    if cap_entry:
                        new_sl = max(new_sl, t.entry_fill_price)
                    if not t.stop_loss or new_sl < t.stop_loss:
                        t.stop_loss = new_sl
                        self._log(db, f"Trailing SL → {new_sl} (price {price}) {t.symbol}", "INFO", t.id)

        # 1) Kill switch or stop-loss -> exit ALL remaining.
        sl_hit = (t.stop_loss or 0) > 0 and (price <= t.stop_loss if t.side == "BUY"
                                             else price >= t.stop_loss)
        if kill or sl_hit:
            res = self._place_confirmed(db, t, broker, is_exit=True, qty=remaining)
            if res.ok:
                t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * remaining
                t.exited_qty = t.quantity
                t.exit_fill_price = res.fill_price
                t.status = "CLOSED"
                t.exit_reason = "KILL" if kill else ("TRAIL" if (t.trail_sl or 0) > 0 else "STOPLOSS")
                t.pnl = round(t.realized_pnl, 2)
                self._log(db, f"EXIT ({t.exit_reason}) {t.symbol} x{remaining} @ {res.fill_price} "
                              f"P&L={t.pnl:.2f} [{t.mode}/{broker.name}]", "INFO", t.id)
            else:
                self._log(db, f"Exit failed for {t.symbol}: {res.error}", "ERROR", t.id)
            return

        # 2) Targets.
        targets = self._targets(t)
        if targets:
            changed = False
            for tg in targets:
                if tg.get("hit"):
                    continue
                tprice = self._target_price(t, tg)
                reached = price >= tprice if t.side == "BUY" else price <= tprice
                if not reached:
                    continue
                exit_qty = min(int(tg.get("qty", 0)), t.quantity - t.exited_qty)
                if exit_qty > 0:
                    res = self._place_confirmed(db, t, broker, is_exit=True, qty=exit_qty)
                    if not res.ok:
                        self._log(db, f"Target exit failed {t.symbol}: {res.error}", "ERROR", t.id)
                        continue
                    t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * exit_qty
                    t.exited_qty += exit_qty
                    t.exit_fill_price = res.fill_price
                    remaining_after = t.quantity - t.exited_qty
                    self._log(db, f"TARGET hit {t.symbol} x{exit_qty} @ {res.fill_price} "
                                  f"({remaining_after} qty left, booked P&L={t.realized_pnl:.2f}) "
                                  f"[{t.mode}/{broker.name}]", "INFO", t.id)
                tg["hit"] = True
                changed = True
            if changed:
                t.targets_json = json.dumps(targets)
            if t.exited_qty >= t.quantity:
                t.status = "CLOSED"
                t.exit_reason = "TARGET"
                t.pnl = round(t.realized_pnl, 2)
            return

        # 3) Single target -> exit all remaining.
        if (t.target or 0) > 0:
            reached = price >= t.target if t.side == "BUY" else price <= t.target
            if reached:
                res = self._place_confirmed(db, t, broker, is_exit=True, qty=remaining)
                if res.ok:
                    t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * remaining
                    t.exited_qty = t.quantity
                    t.exit_fill_price = res.fill_price
                    t.status = "CLOSED"
                    t.exit_reason = "TARGET"
                    t.pnl = round(t.realized_pnl, 2)
                    self._log(db, f"EXIT (TARGET) {t.symbol} x{remaining} @ {res.fill_price} "
                                  f"P&L={t.pnl:.2f} [{t.mode}/{broker.name}]", "INFO", t.id)
                else:
                    self._log(db, f"Exit failed for {t.symbol}: {res.error}", "ERROR", t.id)

    def _update_pnl(self, t, price):
        """Booked (realized) P&L plus unrealized on the remaining quantity."""
        if t.entry_fill_price <= 0:
            t.pnl = 0.0
            return
        direction = 1 if t.side == "BUY" else -1
        remaining = t.quantity - (t.exited_qty or 0)
        unrealized = (price - t.entry_fill_price) * direction * remaining
        t.pnl = round((t.realized_pnl or 0) + unrealized, 2)


engine = TradingEngine()
