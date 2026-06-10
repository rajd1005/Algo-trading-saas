"""
Basket execution engine.

A basket is a group of order legs fired together — instantly ("Execute Now") or
at an exact scheduled second. Everything runs IN-PROCESS (no Redis/Celery):

  • Scheduler thread      — wakes with sub-second precision and fires due baskets.
  • Dispatch              — places every leg with a small micro-delay between them
                            (broker rate-limit safety), creating a Trade row per leg
                            so the existing engine handles fills / MTM / exits.
  • Monitor thread        — mirrors each leg's status from its Trade, and applies the
                            basket-level step profit-lock on the COMBINED MTM.
  • Square-off / Retry    — close all open legs, or re-fire only the failed ones.

Double-execution is prevented by an in-process claim lock: a scheduled basket can
be fired by exactly one of {scheduler, manual "Execute Now"} — whoever claims it
first under the lock wins; the other becomes a no-op.
"""
import datetime as dt
import json
import math
import threading
import time

from database import SessionLocal
from models import Basket, BasketLeg, Trade, LogEntry
from usettings import uget

MICRO_DELAY = 0.15          # 150ms between legs — respects broker rate limits
_claim_lock = threading.Lock()


def _log(db, uid, msg, level="INFO", trade_id=0):
    db.add(LogEntry(message=msg, level=level, trade_id=trade_id, user_id=uid))


def _broker_for(db, uid, mode):
    """(broker, account_id). TEST always uses paper; LIVE uses the trade provider."""
    import engine as _eng
    if mode == "TEST":
        return _eng.engine.paper, 0
    tp = uget(db, uid, "trade_provider", "DEMO")
    acc = _eng.engine._provider_account(db, tp, uid)
    broker = _eng.engine._trade_broker(db, tp, uid) or _eng.engine.paper
    return broker, (acc.id if acc else 0)


def _prices(db, uid, legs_or_trades):
    """LTPs for a set of legs/trades keyed (segment, security_id)."""
    import engine as _eng
    dp = uget(db, uid, "data_provider", "DEMO")
    instruments = list({(x.exchange_segment, str(x.security_id))
                        for x in legs_or_trades if x.security_id})
    try:
        return _eng.engine._fetch_prices(db, uid, dp, instruments, active=False)
    except Exception:
        return {}


# ----------------------------------------------------------------------------
# Pre-execution (JIT) validation
# ----------------------------------------------------------------------------
def jit_validate(db, basket, legs):
    """(ok, reason). Blocks only on kill switch / daily halt / no broker. Margin is
    warn-but-allow: we log a warning but never block on it."""
    uid = basket.user_id
    if not legs:
        return False, "No legs to execute."
    import engine as _eng
    if _eng.engine._daily_halted(db, uid):
        return False, "Daily limit halt is active — trading is stopped for today."
    if basket.mode == "LIVE":
        if uget(db, uid, "kill_switch", "off") == "on":
            return False, "Kill switch is ON."
        acc = _eng.engine._provider_account(db, uget(db, uid, "trade_provider", "DEMO"), uid)
        if acc is None:
            return False, "No live trading broker is connected."
        try:
            import margins
            m = margins.basket_margin(db, acc, legs)
            if m["verified"] and not m["ok"]:
                _log(db, uid, f"Basket '{basket.name}': margin looks short "
                              f"(need ₹{m['required']:,.0f}, available ₹{m['available']:,.0f}) "
                              f"— proceeding; broker will reject any underfunded legs.", "WARN")
            elif not m["verified"]:
                _log(db, uid, f"Basket '{basket.name}': margin could not be verified "
                              f"({m['detail']}) — proceeding.", "WARN")
        except Exception as e:
            _log(db, uid, f"Basket '{basket.name}': margin check skipped ({e}).", "WARN")
    return True, ""


# ----------------------------------------------------------------------------
# Leg placement
# ----------------------------------------------------------------------------
def _place_leg(db, basket, leg, broker, account_id, ltp):
    """Create the leg's Trade row (with its full SL/target/risk config) and place
    its entry order. SCHEDULED/TRIGGER legs rest PENDING — the engine fires them
    when their time / trigger condition is met, just like a normal trade."""
    import engine as _eng
    entry_type = (leg.entry_type or "MARKET").upper()
    if entry_type not in ("MARKET", "LIMIT", "SCHEDULED", "TRIGGER"):
        entry_type = {"LIMIT": "LIMIT", "SL": "TRIGGER"}.get(leg.order_type, "MARKET")
    t = Trade(
        symbol=leg.symbol, name=f"{basket.name} · {leg.symbol}",
        security_id=leg.security_id, exchange_segment=leg.exchange_segment,
        instrument_type=leg.instrument_type or "OPTION",
        side=leg.transaction_type, quantity=int(leg.quantity or 1),
        lot_size=int(float(leg.lot_size or 1)) or 1,
        entry_type=entry_type, entry_price=float(leg.price or 0),
        scheduled_time=leg.scheduled_time or "",
        trigger_price=float(leg.trigger_price or leg.price or 0) if entry_type == "TRIGGER" else 0,
        trigger_dir=leg.trigger_dir or "",
        sl_points=float(leg.sl_points or 0), target_points=float(leg.target_points or 0),
        trail_sl=float(leg.trail_sl or 0), trail_mode=leg.trail_mode or "CONTINUE",
        targets_json=leg.targets_json or "",
        max_profit_amt=float(leg.max_profit_amt or 0), max_loss_amt=float(leg.max_loss_amt or 0),
        lock_step=float(leg.lock_step or 0), lock_amount=float(leg.lock_amount or 0),
        mode=basket.mode, status="PENDING", source="BASKET",
        basket_id=basket.id, leg_id=leg.id, user_id=basket.user_id,
        account_id=account_id, last_price=float(ltp or 0),
    )
    db.add(t)
    db.flush()
    leg.trade_id = t.id
    leg.broker_order_id = ""
    leg.error = ""

    # Conditional entries rest PENDING — the engine fires them on time/trigger.
    if entry_type in ("SCHEDULED", "TRIGGER"):
        leg.status = "PENDING"
        return

    price = float(ltp or leg.price or t.entry_price or 0)
    res = broker.place_entry(t, price, qty=int(leg.quantity or 1))
    if res.order_id:
        t.broker_order_id = res.order_id
        leg.broker_order_id = res.order_id
    if broker.name == "PAPER":
        t.status = "OPEN"
        t.entry_fill_price = res.fill_price or price
        t.broker = "PAPER"
        _eng.engine._apply_levels(t)     # turn SL/target points into absolute prices
        leg.status = "EXECUTED"
        leg.fill_price = t.entry_fill_price
    elif res.ok:
        t.broker = broker.name           # accepted; engine's confirm path finalises the fill
        leg.status = "EXECUTED"
        leg.fill_price = res.fill_price or 0
    else:
        t.status = "REJECTED"
        t.exit_reason = res.error or "REJECTED"
        leg.status = "FAILED"
        leg.error = (res.error or "Rejected")[:200]


def _dispatch(basket_id, only_failed=False):
    """Fire a basket's legs with a micro-delay between each. Runs in its own thread."""
    db = SessionLocal()
    try:
        basket = db.get(Basket, basket_id)
        if not basket:
            return
        uid = basket.user_id
        states = ["FAILED"] if only_failed else ["PENDING", "FAILED"]
        legs = (db.query(BasketLeg)
                .filter(BasketLeg.basket_id == basket_id, BasketLeg.status.in_(states))
                .order_by(BasketLeg.seq, BasketLeg.id).all())
        ok, why = jit_validate(db, basket, legs)
        if not ok:
            basket.exec_status = "FAILED"
            if basket.schedule_status == "EXECUTED":
                basket.schedule_status = "FAILED"
            _log(db, uid, f"Basket '{basket.name}' execution aborted: {why}", "ERROR")
            db.commit()
            return

        broker, account_id = _broker_for(db, uid, basket.mode)
        basket.exec_status = "DISPATCHING"
        basket.last_exec_at = dt.datetime.utcnow()
        db.commit()

        ltps = _prices(db, uid, legs)
        fired = failed = 0
        for leg in legs:
            try:
                px = ltps.get((leg.exchange_segment, str(leg.security_id)), 0)
                _place_leg(db, basket, leg, broker, account_id, px)
                if leg.status == "FAILED":
                    failed += 1
                else:
                    fired += 1
            except Exception as e:
                leg.status = "FAILED"
                leg.error = str(e)[:200]
                failed += 1
            db.commit()
            time.sleep(MICRO_DELAY)             # broker rate-limit micro-delay

        basket.exec_status = ("EXECUTED" if not failed else
                              "PARTIAL" if fired else "FAILED")
        _log(db, uid, f"Basket '{basket.name}' dispatched: {fired} sent, {failed} failed "
                      f"[{basket.mode}/{broker.name}].",
                      "INFO" if not failed else "WARN")
        db.commit()
    except Exception as e:
        try:
            _log(db, 0, f"Basket dispatch error: {e}", "ERROR")
            db.commit()
        except Exception:
            pass
    finally:
        db.close()


# ----------------------------------------------------------------------------
# Public actions (called from the API)
# ----------------------------------------------------------------------------
def execute_now(basket_id):
    """Fire immediately in a background thread (instant dispatch)."""
    threading.Thread(target=_dispatch, args=(basket_id, False), daemon=True).start()


def retry_failed(basket_id):
    threading.Thread(target=_dispatch, args=(basket_id, True), daemon=True).start()


def claim_schedule(db, basket_id, as_status):
    """Atomically move schedule_status PENDING -> as_status. Returns True for the
    single caller that won the claim (prevents double-execution)."""
    with _claim_lock:
        b = db.get(Basket, basket_id)
        if not b or b.schedule_status != "PENDING":
            return False
        b.schedule_status = as_status
        b.is_scheduled = 0
        db.commit()
        return True


def cancel_schedule(db, basket_id):
    return claim_schedule(db, basket_id, "CANCELLED")


def square_off(basket_id):
    threading.Thread(target=_square_off, args=(basket_id,), daemon=True).start()


def _exit_trade(db, t, broker, price, reason):
    """Self-contained square-off of one Trade (no engine instance state)."""
    remaining = int(t.quantity) - int(t.exited_qty or 0)
    if remaining <= 0:
        t.status = "CLOSED"
        return True
    direction = 1 if t.side == "BUY" else -1
    res = broker.place_exit(t, price, qty=remaining)
    if res.ok and broker.name != "PAPER" and res.order_id:
        st, tp, _, _ = broker.confirm(res.order_id)
        if tp:
            res.fill_price = tp
    if not res.ok:
        _log(db, t.user_id, f"Basket square-off failed for {t.symbol}: {res.error}", "ERROR", t.id)
        return False
    t.realized_pnl = (t.realized_pnl or 0) + (price - t.entry_fill_price) * direction * remaining
    t.exited_qty = t.quantity
    t.exit_fill_price = res.fill_price
    t.status = "CLOSED"
    t.exit_reason = reason
    t.pnl = round(t.realized_pnl, 2)
    _log(db, t.user_id, f"EXIT ({reason}) {t.symbol} x{remaining} @ {res.fill_price} "
                        f"P&L={t.pnl:.2f} [{t.mode}/{broker.name}]", "INFO", t.id)
    return True


def _close_basket_open_trades(db, basket, reason):
    uid = basket.user_id
    open_trades = (db.query(Trade)
                   .filter(Trade.basket_id == basket.id, Trade.status == "OPEN").all())
    if not open_trades:
        return 0
    import engine as _eng
    tp = uget(db, uid, "trade_provider", "DEMO")
    live_broker = _eng.engine._trade_broker(db, tp, uid)
    prices = _prices(db, uid, open_trades)
    n = 0
    for t in open_trades:
        broker = _eng.engine.paper if (t.mode == "TEST" or live_broker is None) else live_broker
        px = prices.get((t.exchange_segment, str(t.security_id)), t.last_price or 0)
        if _exit_trade(db, t, broker, px, reason):
            n += 1
        leg = db.query(BasketLeg).filter(BasketLeg.trade_id == t.id).first()
        if leg:
            leg.status = "CLOSED"
    return n


def _square_off(basket_id):
    db = SessionLocal()
    try:
        basket = db.get(Basket, basket_id)
        if not basket:
            return
        n = _close_basket_open_trades(db, basket, "BASKET_SQOFF")
        basket.exec_status = "EXECUTED" if not n else basket.exec_status
        _log(db, basket.user_id,
             f"Basket '{basket.name}': squared off {n} open leg(s)." if n
             else f"Basket '{basket.name}': nothing open to square off.", "INFO")
        db.commit()
    finally:
        db.close()


# ----------------------------------------------------------------------------
# Background threads: high-precision scheduler + leg/MTM monitor
# ----------------------------------------------------------------------------
class _Scheduler:
    def __init__(self):
        self._running = False

    def start(self):
        if self._running:
            return
        self._running = True
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while self._running:
            sleep = 0.25
            try:
                db = SessionLocal()
                now = dt.datetime.utcnow()
                due = (db.query(Basket)
                       .filter(Basket.is_scheduled == 1, Basket.schedule_status == "PENDING").all())
                soonest = None
                for b in due:
                    if not b.scheduled_at:
                        continue
                    if b.scheduled_at <= now:
                        if claim_schedule(db, b.id, "EXECUTED"):
                            execute_now(b.id)
                    else:
                        d = (b.scheduled_at - now).total_seconds()
                        soonest = d if soonest is None else min(soonest, d)
                db.close()
                if soonest is not None:
                    sleep = max(0.02, min(0.25, soonest))   # tighten as the fire time nears
            except Exception:
                sleep = 0.5
            time.sleep(sleep)


class _Monitor:
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(1.5)
            try:
                db = SessionLocal()
                self._mirror_legs(db)
                self._check_locks(db)
                db.commit()
                db.close()
            except Exception:
                pass

    def _mirror_legs(self, db):
        """Reflect each leg's live status from its Trade (covers webhook + engine fills)."""
        legs = (db.query(BasketLeg)
                .filter(BasketLeg.trade_id > 0, BasketLeg.status.in_(["PENDING", "EXECUTED"])).all())
        for leg in legs:
            t = db.get(Trade, leg.trade_id)
            if not t:
                continue
            if t.broker_order_id and not leg.broker_order_id:
                leg.broker_order_id = t.broker_order_id
            if t.status == "OPEN":
                leg.status = "EXECUTED"
                if t.entry_fill_price and not leg.fill_price:
                    leg.fill_price = t.entry_fill_price
            elif t.status == "REJECTED":
                leg.status = "FAILED"
                leg.error = leg.error or t.exit_reason or "Rejected"
            elif t.status == "CLOSED":
                leg.status = "CLOSED"

    def _check_locks(self, db):
        """Basket-level step profit-lock on the COMBINED MTM of all open legs."""
        baskets = (db.query(Basket)
                   .filter(Basket.is_active == 1, Basket.lock_step > 0, Basket.lock_amount > 0).all())
        for b in baskets:
            open_trades = (db.query(Trade)
                           .filter(Trade.basket_id == b.id, Trade.status == "OPEN").all())
            if not open_trades:
                continue
            mtm = round(sum(t.pnl or 0 for t in open_trades), 2)
            new = math.floor(mtm / b.lock_step) * b.lock_amount if mtm > 0 else 0
            if new > (b.lock_floor or 0):
                b.lock_floor = new
                _log(db, b.user_id, f"Basket '{b.name}': profit-lock armed — securing "
                                    f"₹{new:,.0f} (MTM ₹{mtm:,.0f}).", "INFO")
            if (b.lock_floor or 0) > 0 and mtm <= b.lock_floor:
                n = _close_basket_open_trades(db, b, "BASKET_LOCK")
                b.exec_status = "EXECUTED"
                _log(db, b.user_id, f"Basket '{b.name}': profit-lock triggered — secured "
                                    f"₹{b.lock_floor:,.0f} (MTM ₹{mtm:,.0f}); closed {n} leg(s).", "WARN")


scheduler = _Scheduler()
monitor = _Monitor()


def start():
    scheduler.start()
    monitor.start()
