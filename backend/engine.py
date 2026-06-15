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
import datetime as dt
import json
import math
import threading
import time

from database import SessionLocal
from models import Trade, LogEntry, Setting, Account, User
from usettings import uget, uset

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _ist_now():
    return dt.datetime.now(IST)


def _ist_today():
    return _ist_now().strftime("%Y-%m-%d")


def _ist_day_start_utc():
    """UTC datetime for 00:00 IST today (to filter 'today's' trades)."""
    midnight_ist = _ist_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ist.astimezone(dt.timezone.utc).replace(tzinfo=None)
from market_data import DhanMarketData, demo_market
import feeds
from brokers import PaperBroker, DhanBroker
from angel import AngelBroker, AngelMarketData
from zerodha import ZerodhaBroker, ZerodhaMarketData
from aliceblue import AliceBroker, AliceMarketData
import config


def level_price(side, ref, points, is_target):
    """Convert a points distance into an absolute price, given the entry ref.
    'points' are price units (the user's SL/target distance). Precision adapts to the
    price magnitude so sub-unit prices aren't flattened to 2 decimals."""
    if points <= 0 or ref <= 0:
        return 0.0
    raw = (ref + points) if ((side == "BUY") == is_target) else (ref - points)
    nd = 2 if ref >= 100 else (4 if ref >= 1 else 8)
    return round(raw, nd)


class TradingEngine:
    def __init__(self):
        self._thread = None
        self._running = False
        self.paper = PaperBroker()
        self._rest_cache = {}          # uid -> {instrument: last price}
        self._rest_cooldowns = {}      # uid -> cooldown-until ts (after a 429)
        self._rest_times = {}          # uid -> last REST poll ts
        self._demo = False             # DEMO mode (simulated prices + fake broker)
        self._demo_trade = True        # is the trading provider DEMO (paper)?
        self._trade_account_id = 0
        self._cur_uid = 0
        self._warned_no_broker = False

    # ---------- lifecycle ----------
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    # ---------- per-user settings helpers ----------
    def _get_setting(self, db, uid, key, default=""):
        return uget(db, uid, key, default)

    def _set_setting(self, db, uid, key, value):
        uset(db, uid, key, value)

    def _log(self, db, message, level="INFO", trade_id=0, uid=None):
        if uid is None:
            uid = getattr(self, "_cur_uid", 0)
        db.add(LogEntry(message=message, level=level, trade_id=trade_id, user_id=uid))

    def _kill_switch_on(self, db, uid) -> bool:
        return uget(db, uid, "kill_switch", "off") == "on"

    def _trade_creds(self, db):
        return (self._get_setting(db, "dhan_client_id", config.DHAN_CLIENT_ID),
                self._get_setting(db, "dhan_access_token", config.DHAN_ACCESS_TOKEN))

    def _data_creds(self, db):
        # When 'use same account' is on, data uses the trading creds; else its own.
        if self._get_setting(db, "use_same_account", "yes") == "yes":
            return self._trade_creds(db)
        return (self._get_setting(db, "data_client_id", ""),
                self._get_setting(db, "data_access_token", ""))

    def _market_data(self, db):
        cid, tok = self._data_creds(db)
        if not cid or not tok:
            return None
        return DhanMarketData(cid, tok)

    def _live_broker(self, db):
        cid, tok = self._trade_creds(db)
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

    def _providers(self, db, uid):
        return (uget(db, uid, "data_provider", "DEMO"),
                uget(db, uid, "trade_provider", "DEMO"))

    def _provider_account(self, db, value, uid=None):
        if not value or value == "DEMO":
            return None
        try:
            a = db.get(Account, int(value))
        except Exception:
            return None
        if a is not None and uid is not None and a.user_id != uid:
            return None
        return a

    def _trade_broker(self, db, value, uid=None):
        acc = self._provider_account(db, value, uid)
        if acc is None:
            return None     # DEMO -> paper
        creds = json.loads(acc.creds_json or "{}")
        if acc.broker == "DHAN" and creds.get("access_token"):
            return DhanBroker(acc.client_id, creds["access_token"])
        if acc.broker == "ANGEL" and creds.get("jwt"):
            return AngelBroker(acc.client_id, creds.get("api_key", ""), creds["jwt"])
        if acc.broker == "ZERODHA" and creds.get("access_token"):
            return ZerodhaBroker(creds.get("api_key", ""), creds["access_token"])
        if acc.broker == "ALICE" and creds.get("session_id"):
            return AliceBroker(acc.client_id, creds["session_id"])
        return None

    def _fetch_prices(self, db, uid, value, instruments, active):
        """Real-time first: read whatever the broker's WebSocket is already
        streaming, then REST-fetch only what the socket hasn't delivered yet.
        The REST path is the SAFETY NET — if a socket can't connect, every
        instrument is "missing" and prices keep flowing exactly as before."""
        acc = self._provider_account(db, value, uid)
        if acc is None:     # DEMO
            by_seg = {}
            for seg, sid in instruments:
                by_seg.setdefault(seg, []).append(sid)
            if active:
                self._set_setting(db, uid, "md_status", "ok:demo")
            return demo_market.get_ltp_batch(by_seg)

        # 1) Live ticks from the per-account WebSocket feed (empty if WS isn't
        #    available / hasn't connected yet — caller then falls back to REST).
        ws = {}
        try:
            ws = feeds.manager.snapshot(acc, instruments)
        except Exception:
            ws = {}
        missing = [i for i in instruments if i not in ws]

        # 2) REST fallback for the instruments the socket isn't streaming.
        rest = self._rest_fetch(db, uid, acc, missing, active) if missing else {}
        prices = dict(rest)
        prices.update(ws)        # fresh ticks win over the (older) REST snapshot

        # 3) Show "real-time (WebSocket)" ONLY while the socket is delivering fresh
        #    ticks — a connected-but-silent socket falls back to the REST status,
        #    so the dashboard never claims "real-time" while prices are stale.
        if active and ws:
            try:
                if feeds.manager.status(acc.id).get("streaming"):
                    self._set_setting(db, uid, "md_status", "ok:ws")
            except Exception:
                pass
        return prices

    def _rest_fetch(self, db, uid, acc, instruments, active):
        """Per-broker REST price fetch for a set of instruments (the WebSocket
        fallback). Sets md_status to ok:<broker> / an error string as before."""
        by_seg = {}
        for seg, sid in instruments:
            by_seg.setdefault(seg, []).append(sid)
        creds = json.loads(acc.creds_json or "{}")
        if acc.broker == "DHAN":
            cid, tok = acc.client_id, creds.get("access_token", "")
            if not cid or not tok:
                if active:
                    self._set_setting(db, uid, "md_status", "Data (Dhan) not connected — connect on the Broker tab.")
                return {}
            return self._collect_prices(db, uid, cid, tok, instruments)
        if acc.broker == "ANGEL":
            cid, key, jwt = acc.client_id, creds.get("api_key", ""), creds.get("jwt", "")
            if not cid or not key or not jwt:
                if active:
                    self._set_setting(db, uid, "md_status", "Data (Angel) not connected — connect on the Broker tab.")
                return {}
            md = AngelMarketData(cid, key, jwt)
            prices = md.get_ltp_batch(by_seg)
            if active:
                if prices:
                    self._set_setting(db, uid, "md_status", "ok:angel")
                elif md.last_error:
                    self._set_setting(db, uid, "md_status", f"Angel data error: {md.last_error[:160]}")
            return prices
        if acc.broker == "ZERODHA":
            key, tok = creds.get("api_key", ""), creds.get("access_token", "")
            if not key or not tok:
                if active:
                    self._set_setting(db, uid, "md_status", "Data (Zerodha) not connected — connect on the Broker tab.")
                return {}
            md = ZerodhaMarketData(key, tok)
            prices = md.get_ltp_batch(by_seg)
            if active:
                if prices:
                    self._set_setting(db, uid, "md_status", "ok:zerodha")
                elif md.last_error:
                    self._set_setting(db, uid, "md_status", f"Zerodha data error: {md.last_error[:160]}")
            return prices
        if acc.broker == "ALICE":
            cid, sid = acc.client_id, creds.get("session_id", "")
            if not cid or not sid:
                if active:
                    self._set_setting(db, uid, "md_status", "Data (Alice Blue) not connected — connect on the Broker tab.")
                return {}
            md = AliceMarketData(cid, sid)
            prices = md.get_ltp_batch(by_seg)
            if active:
                if prices:
                    self._set_setting(db, uid, "md_status", "ok:alice")
                elif md.last_error:
                    self._set_setting(db, uid, "md_status", f"Alice Blue data error: {md.last_error[:160]}")
            return prices
        return {}

    def _tick(self):
        db = SessionLocal()
        try:
            active_all = db.query(Trade).filter(Trade.status.in_(["PENDING", "OPEN"])).all()
            by_user = {}
            for t in active_all:
                by_user.setdefault(t.user_id or 0, []).append(t)
            # Which users are allowed to run (active + not expired). Cache lookups.
            for uid, active in by_user.items():
                u = db.get(User, uid) if uid else None
                if u is not None:
                    if u.status == "BLOCKED":
                        continue
                    expired = u.role != "SUPER_ADMIN" and u.plan_expiry and \
                        u.plan_expiry < dt.datetime.utcnow()
                    if expired:
                        continue       # plan expired -> halt this user's algos
                self._tick_user(db, uid, active)
            db.commit()
        finally:
            db.close()

    def _tick_user(self, db, uid, active):
        self._cur_uid = uid           # engine logs default to this tenant
        data_provider, trade_provider = self._providers(db, uid)
        _trade_acc = self._provider_account(db, trade_provider, uid)
        self._demo_trade = _trade_acc is None
        self._trade_account_id = _trade_acc.id if _trade_acc else 0
        instruments = list({(t.exchange_segment, str(t.security_id))
                            for t in active if t.security_id})

        prices = self._fetch_prices(db, uid, data_provider, instruments, active)
        kill = self._kill_switch_on(db, uid)
        live_broker = self._trade_broker(db, trade_provider, uid)

        # Pass 1: refresh latest price + live MTM on every OPEN trade.
        for t in active:
            if not t.security_id:
                continue
            price = prices.get((t.exchange_segment, str(t.security_id)), 0.0)
            if price <= 0:
                continue
            t.last_price = price
            if t.status == "OPEN":
                self._update_pnl(t, price)

        # Account-level daily target / drawdown + step profit-lock.
        self._check_daily_limits(db, uid, active, live_broker)
        self._check_global_lock(db, uid, active, live_broker)
        halted = self._daily_halted(db, uid)

        # Pass 2: per-trade entry/exit handling.
        for t in active:
            if not t.security_id:
                continue
            # Replication group trades live on OTHER accounts (their own broker);
            # replication.py manages their entries/exits — never the trade engine.
            if t.source in ("SLAVE", "MASTER"):
                continue
            price = prices.get((t.exchange_segment, str(t.security_id)), 0.0)
            if price <= 0:
                continue
            if t.status == "PENDING":
                self._handle_pending(db, t, price, kill, live_broker, halted)
            elif t.status == "OPEN":
                self._handle_open(db, t, price, kill, live_broker)

    # ---------- account-level daily risk ----------
    def _account_day_pnl(self, db, uid):
        """Combined realized+unrealized P&L for this user's trades today."""
        day_start = _ist_day_start_utc()
        rows = (db.query(Trade)
                .filter(Trade.user_id == uid,
                        Trade.status.in_(["OPEN", "CLOSED"]),
                        Trade.created_at >= day_start).all())
        return round(sum(t.pnl or 0 for t in rows), 2)

    def _daily_halted(self, db, uid):
        return (uget(db, uid, "daily_halt", "off") == "on"
                and uget(db, uid, "daily_halt_date", "") == _ist_today())

    def _reset_daily_if_new_day(self, db, uid):
        today = _ist_today()
        if uget(db, uid, "daily_risk_date", "") != today:
            self._set_setting(db, uid, "daily_risk_date", today)
            self._set_setting(db, uid, "daily_halt", "off")
            self._set_setting(db, uid, "daily_halt_date", "")
            self._set_setting(db, uid, "daily_halt_reason", "")
            self._set_setting(db, uid, "global_lock_floor", "0")

    def _halt_account(self, db, uid, active, live_broker, reason, exit_reason):
        """Square off all OPEN trades of this user and block new entries today."""
        for t in active:
            if t.status == "OPEN":
                broker = self._broker_for(t, live_broker)
                if broker is not None:
                    self._exit_all(db, t, t.last_price, broker, exit_reason)
        self._set_setting(db, uid, "daily_halt", "on")
        self._set_setting(db, uid, "daily_halt_date", _ist_today())
        self._set_setting(db, uid, "daily_halt_reason", reason)
        self._log(db, f"DAILY LIMIT: {reason} — squared off open positions and halted "
                      f"new trading for today.", "WARN", uid=uid)

    def _check_daily_limits(self, db, uid, active, live_broker):
        self._reset_daily_if_new_day(db, uid)
        if self._daily_halted(db, uid):
            return
        try:
            maxp = float(uget(db, uid, "daily_max_profit", "") or 0)
            maxl = float(uget(db, uid, "daily_max_loss", "") or 0)
        except Exception:
            maxp = maxl = 0.0
        if maxp <= 0 and maxl <= 0:
            return
        pnl = self._account_day_pnl(db, uid)
        if maxl > 0 and pnl <= -abs(maxl):
            self._halt_account(db, uid, active, live_broker,
                               f"Daily max loss ₹{abs(maxl):,.0f} hit (P&L ₹{pnl:,.0f})",
                               "DAILY_LOSS")
        elif maxp > 0 and pnl >= maxp:
            self._halt_account(db, uid, active, live_broker,
                               f"Daily max profit ₹{maxp:,.0f} hit (P&L ₹{pnl:,.0f})",
                               "DAILY_PROFIT")

    @staticmethod
    def _step_lock_floor(mtm, step, amount):
        """Auto step profit-lock: for every ₹step of profit, secure ₹amount."""
        if step <= 0 or amount <= 0 or mtm <= 0:
            return 0.0
        return math.floor(mtm / step) * amount

    def _check_global_lock(self, db, uid, active, live_broker):
        if self._daily_halted(db, uid):
            return
        try:
            step = float(uget(db, uid, "global_lock_step", "") or 0)
            amount = float(uget(db, uid, "global_lock_amount", "") or 0)
        except Exception:
            step = amount = 0.0
        if step <= 0 or amount <= 0:
            return
        pnl = self._account_day_pnl(db, uid)
        try:
            floor = float(uget(db, uid, "global_lock_floor", "0") or 0)
        except Exception:
            floor = 0.0
        new = self._step_lock_floor(pnl, step, amount)
        if new > floor:                       # ratchet up only
            floor = new
            self._set_setting(db, uid, "global_lock_floor", str(floor))
            self._log(db, f"Account profit-lock armed — securing ₹{floor:,.0f} "
                          f"(day P&L ₹{pnl:,.0f}).", "INFO", uid=uid)
        if floor > 0 and pnl <= floor:
            self._halt_account(db, uid, active, live_broker,
                               f"Profit lock triggered — securing ₹{floor:,.0f} (P&L ₹{pnl:,.0f})",
                               "GLOBAL_LOCK")

    def _collect_prices(self, db, uid, cid, tok, instruments):
        """Per-tenant Dhan prices via REST (the shared WebSocket feed can't serve
        several tenants' credentials at once, so each user polls its own REST)."""
        if not instruments:
            return {}
        cache = self._rest_cache.setdefault(uid, {})
        cool = self._rest_cooldowns.get(uid, 0.0)
        last = self._rest_times.get(uid, 0.0)
        now = time.time()
        prices = dict(cache)
        if now >= cool and (now - last) >= 1.0:
            self._rest_times[uid] = now
            md = DhanMarketData(cid, tok)
            by_seg = {}
            for seg, sid in instruments:
                by_seg.setdefault(seg, []).append(sid)
            rest = md.get_ltp_batch(by_seg)
            prices.update(rest)
            cache.update(rest)
            if md.last_error:
                if "429" in md.last_error or "Too many" in md.last_error:
                    self._rest_cooldowns[uid] = now + 30
                    self._log(db, "Dhan rate-limited the price feed (429) — backing off 30s.", "WARN", uid=uid)
                else:
                    self._log(db, f"REST price feed error: {md.last_error[:200]}", "ERROR", uid=uid)
                if not prices:
                    self._set_setting(db, uid, "md_status", f"Price feed error: {md.last_error[:160]}")
        if prices:
            self._set_setting(db, uid, "md_status", "ok:rest")
        return prices

    def _broker_for(self, t, live_broker):
        # TEST trades and the DEMO trading provider always use the paper broker.
        if t.mode == "TEST" or self._demo_trade:
            return self.paper
        return live_broker     # DhanBroker / AngelBroker (None if not connected)

    def _place_confirmed(self, db, t, broker, is_exit, qty):
        """Place an order and (for live Dhan) confirm what ACTUALLY happened.
        Returns a result whose .status is TRADED / REJECTED / ERROR / PENDING and
        whose .error holds the broker's exact reason on rejection. We retry only
        on network errors — never on a definitive broker rejection."""
        place = broker.place_exit if is_exit else broker.place_entry
        price = t.last_price or t.entry_fill_price or t.entry_price
        kind = "EXIT" if is_exit else "ENTRY"
        res = None
        for attempt in range(config.ORDER_RETRIES + 1):
            res = place(t, price, qty=qty)
            if res.ok:
                break
            if res.status == "REJECTED":
                return res                      # definitive rejection -> do not retry
            self._log(db, f"{kind} order network error (try {attempt + 1}): {res.error}",
                      "WARN", t.id)             # ERROR -> retryable
        if not res.ok:
            return res
        if is_exit and res.order_id:
            t.exit_order_id = res.order_id      # so external-sync won't mirror our own exit
        if broker.name == "PAPER":
            return res                          # paper/demo: filled at live price
        # Verify with the live broker (Dhan/Angel) what really happened.
        status, traded_price, _, reason = broker.confirm(res.order_id)
        res.status = status or res.status
        if status == "TRADED":
            if traded_price > 0:
                if abs(traded_price - res.fill_price) > 0.001:
                    self._log(db, f"{kind} reconciled to Dhan fill {traded_price} "
                                  f"(provisional {res.fill_price})", "INFO", t.id)
                res.fill_price = traded_price
            res.ok = True
            return res
        if status in ("REJECTED", "CANCELLED", "EXPIRED"):
            res.ok = False
            res.error = reason or status
            return res
        # pending/unknown: order is live but not yet confirmed filled.
        res.ok = True
        res.status = status or "PENDING"
        return res

    # ---------- entry ----------
    def _handle_pending(self, db, t, price, kill, live_broker, halted=False):
        # External (broker-terminal) orders are managed by the sync monitor, not
        # by us — never place an order on their behalf.
        if t.source == "EXTERNAL":
            return

        # Daily limit hit -> trading is halted for the day; hold all new entries.
        if halted and not t.broker_order_id:
            return

        # Algo trigger: on first sight, lock the direction from the reference price
        # so "fire when the LTP reaches this price" works from either side.
        if t.entry_type == "TRIGGER" and (t.trigger_price or 0) > 0 and not t.trigger_dir and price > 0:
            t.trigger_dir = "ABOVE" if price < t.trigger_price else "BELOW"

        broker = self._broker_for(t, live_broker)
        if broker is None:
            if not self._warned_no_broker:
                self._warned_no_broker = True
                self._log(db, "Trading provider not connected — cannot place orders.", "WARN", t.id)
            return

        # If we already placed a live entry order, just CONFIRM it (never place
        # a second order) — this prevents duplicate/ghost trades.
        if t.broker_order_id and broker.name in ("DHAN", "ANGEL", "ZERODHA", "ALICE"):
            status, traded_price, _, reason = broker.order_status(t.broker_order_id)
            if status == "TRADED":
                self._open_trade(db, t, traded_price or t.last_price, broker)
            elif status in ("REJECTED", "CANCELLED", "EXPIRED"):
                self._reject_trade(db, t, reason or status)
            return

        if kill or not self._entry_triggered(t, price):
            return

        # Log why an algo-tracked entry is firing (scheduled time / synthetic trigger).
        if t.entry_type == "SCHEDULED":
            self._log(db, f"Trade #{t.id}: scheduled time {t.scheduled_time} reached "
                          f"— sending {t.side} {t.symbol} market order [{broker.name}].", "INFO", t.id)
        elif t.entry_type == "TRIGGER":
            self._log(db, f"Trade #{t.id}: trigger hit (LTP {price} {t.trigger_dir or 'auto'} "
                          f"{t.trigger_price}) — sending {t.side} {t.symbol} market order "
                          f"[{broker.name}].", "INFO", t.id)

        res = self._place_confirmed(db, t, broker, is_exit=False, qty=t.quantity)
        if res.order_id:
            t.broker_order_id = res.order_id
        if res.ok and res.status == "TRADED":
            self._open_trade(db, t, res.fill_price, broker)
        elif res.status == "REJECTED":
            self._reject_trade(db, t, res.error)
        elif not res.ok:
            # network error after retries — treat as failed, don't assume open
            self._reject_trade(db, t, res.error, label="ENTRY_FAILED")
        else:
            # placed but not yet filled — stay PENDING and re-confirm next ticks
            self._log(db, f"Entry order placed for {t.symbol} (status {res.status}); "
                          f"awaiting fill confirmation…", "INFO", t.id)

    def _open_trade(self, db, t, fill_price, broker):
        """Mark a trade OPEN only after the broker confirms the fill."""
        t.status = "OPEN"
        t.entry_fill_price = fill_price
        t.broker = broker.name
        t.account_id = self._trade_account_id
        self._apply_levels(t)
        self._log(db, f"ENTRY {t.side} {t.symbol} x{t.quantity} @ {fill_price} "
                      f"[{t.mode}/{broker.name}]", "INFO", t.id)
        self._log_levels(db, t)

    def _reject_trade(self, db, t, reason, label="REJECTED"):
        t.status = "REJECTED"
        t.exit_reason = label
        self._log(db, f"{t.side} order {t.symbol} REJECTED. Broker reason: "
                      f"{reason or 'unknown'}", "ERROR", t.id)

    def _log_levels(self, db, t):
        """Audit log of the SL / target / trailing set on the new position."""
        parts = []
        if (t.stop_loss or 0) > 0:
            parts.append(f"SL at {t.stop_loss}")
        targets = self._targets(t)
        if targets:
            parts.append("Targets at " + ", ".join(str(x.get("price")) for x in targets))
        elif (t.target or 0) > 0:
            parts.append(f"Target at {t.target}")
        if (t.trail_sl or 0) > 0:
            parts.append(f"Trailing SL {t.trail_sl}pt ({t.trail_mode})")
        if parts:
            self._log(db, f"Trade #{t.id}: " + "; ".join(parts) + " (set at entry)", "INFO", t.id)

    def _entry_triggered(self, t, price):
        et = t.entry_type
        if et == "SCHEDULED":
            # Hold until the wall-clock time, then push a market order.
            if not t.scheduled_time:
                return True
            return _ist_now().strftime("%H:%M:%S") >= t.scheduled_time
        if et == "TRIGGER":
            # Algo-tracked synthetic limit: fire when LTP crosses the trigger.
            if (t.trigger_price or 0) <= 0:
                return True
            d = (t.trigger_dir or "").upper()
            if d == "ABOVE":
                return price >= t.trigger_price
            if d == "BELOW":
                return price <= t.trigger_price
            # auto: a BUY waits for a dip to/below; a SELL waits for a rise to/above.
            return price <= t.trigger_price if t.side == "BUY" else price >= t.trigger_price
        if et == "MARKET":
            return True
        if t.entry_price <= 0:                   # LIMIT
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
    def _exit_all(self, db, t, price, broker, reason):
        """Square off all remaining quantity at market. Returns True once CLOSED."""
        remaining = t.quantity - (t.exited_qty or 0)
        if remaining <= 0:
            t.status = "CLOSED"
            return True
        direction = 1 if t.side == "BUY" else -1
        res = self._place_confirmed(db, t, broker, is_exit=True, qty=remaining)
        if not res.ok:
            self._log(db, f"Exit failed for {t.symbol} ({reason}): {res.error}", "ERROR", t.id)
            return False
        pv = 1.0
        t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * remaining * pv
        t.exited_qty = t.quantity
        t.exit_fill_price = res.fill_price
        t.status = "CLOSED"
        t.exit_reason = reason
        t.pnl = round(t.realized_pnl, 2)
        self._log(db, f"EXIT ({reason}) {t.symbol} x{remaining} @ {res.fill_price} "
                      f"P&L={t.pnl:.2f} [{t.mode}/{broker.name}]", "INFO", t.id)
        return True

    def _check_trade_risk(self, db, t, price, broker):
        """Trade-level monetary risk on this position's live MTM (t.pnl). Returns
        True if the trade was squared off (max loss / max profit / profit-lock)."""
        mtm = t.pnl or 0
        if (t.max_loss_amt or 0) > 0 and mtm <= -abs(t.max_loss_amt):
            return self._exit_all(db, t, price, broker, "MAXLOSS")
        if (t.max_profit_amt or 0) > 0 and mtm >= t.max_profit_amt:
            return self._exit_all(db, t, price, broker, "MAXPROFIT")
        # Auto step profit-lock: for every ₹lock_step profit, secure ₹lock_amount.
        if (t.lock_step or 0) > 0 and (t.lock_amount or 0) > 0:
            new = self._step_lock_floor(mtm, t.lock_step, t.lock_amount)
            if new > (t.lock_floor or 0):     # ratchet up only
                t.lock_floor = new
                self._log(db, f"Trade #{t.id}: profit-lock armed — securing ₹{new:,.0f} "
                              f"(MTM ₹{mtm:,.0f}).", "INFO", t.id)
            if (t.lock_floor or 0) > 0 and mtm <= t.lock_floor:
                return self._exit_all(db, t, price, broker, "LOCK")
        return False

    def _handle_open(self, db, t, price, kill, live_broker):
        direction = 1 if t.side == "BUY" else -1
        pv = 1.0   # equity P&L: 1 point = one currency unit per qty
        remaining = t.quantity - (t.exited_qty or 0)
        if remaining <= 0:
            t.status = "CLOSED"
            return
        broker = self._broker_for(t, live_broker)
        if broker is None:
            self._update_pnl(t, price)     # keep P&L live even if exits can't fire
            return

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
                        old_sl = t.stop_loss or 0
                        t.stop_loss = new_sl
                        self._log(db, f"Trade #{t.id}: SL updated (trailing). "
                                      f"Old SL: {old_sl} -> New SL: {new_sl}", "INFO", t.id)
            else:
                ref = t.hwm if t.hwm else t.entry_fill_price
                steps = math.floor((ref - price) / step)
                if steps > 0:
                    t.hwm = ref - steps * step
                    new_sl = round((t.stop_loss or 0) - steps * step, 2)
                    if cap_entry:
                        new_sl = max(new_sl, t.entry_fill_price)
                    if not t.stop_loss or new_sl < t.stop_loss:
                        old_sl = t.stop_loss or 0
                        t.stop_loss = new_sl
                        self._log(db, f"Trade #{t.id}: SL updated (trailing). "
                                      f"Old SL: {old_sl} -> New SL: {new_sl}", "INFO", t.id)

        # 1) Trade-level monetary risk (max loss / max profit / step profit-lock)
        #    on this position's live MTM — square off if any threshold is hit.
        if self._check_trade_risk(db, t, price, broker):
            return

        # 2) Kill switch or stop-loss -> exit ALL remaining.
        sl_hit = (t.stop_loss or 0) > 0 and (price <= t.stop_loss if t.side == "BUY"
                                             else price >= t.stop_loss)
        if kill or sl_hit:
            reason = "KILL" if kill else ("TRAIL" if (t.trail_sl or 0) > 0 else "STOPLOSS")
            self._exit_all(db, t, price, broker, reason)
            return

        # 3) Targets.
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
                    t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * exit_qty * pv
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
                    t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * remaining * pv
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
        pv = 1.0   # equity P&L: 1 point = one currency unit per qty
        unrealized = (price - t.entry_fill_price) * direction * remaining * pv
        t.pnl = round((t.realized_pnl or 0) + unrealized, 2)


engine = TradingEngine()
