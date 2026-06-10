"""
Auto Square-Off: at each user's configured time (IST; default 15:15:00 — 15 min
before the 15:30 close), force-close every open position, cancel pending orders,
and block new trades for the rest of the day.

The clock is LOCKED to Asia/Kolkata (fixed +5:30; India has no DST) so it lines
up with NSE/BSE/MCX no matter where the server runs. "Halted for the day" reuses
the engine's daily-halt flag — it is date-keyed, so it auto-clears at midnight
IST and is already enforced by every order-placement guard (manual, basket,
group and algo entries).
"""
import datetime as dt
import threading
import time

from database import SessionLocal
from models import User, Trade, LogEntry
from usettings import uget, uset

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _ist_now():
    return dt.datetime.now(IST)


def _secs(s):
    try:
        p = [int(x) for x in str(s).split(":")] + [0, 0]
        return p[0] * 3600 + p[1] * 60 + p[2]
    except Exception:
        return None


def _log(db, uid, msg, level="INFO", trade_id=0):
    db.add(LogEntry(message=msg, level=level, user_id=uid, trade_id=trade_id))


def _halt_for_day(db, uid, today, sq):
    """Flip the daily-halt flag (== is_halted_for_the_day). Date-keyed: it blocks
    new trades for the rest of today and clears itself at midnight IST."""
    uset(db, uid, "daily_halt", "on")
    uset(db, uid, "daily_halt_date", today)
    uset(db, uid, "daily_risk_date", today)     # stop the engine's new-day reset clearing it
    uset(db, uid, "daily_halt_reason", f"Auto square-off at {sq} IST")


def _squareoff_user(db, uid, sq, today):
    import replication
    import baskets
    opens = db.query(Trade).filter(Trade.user_id == uid, Trade.status == "OPEN").all()
    pends = db.query(Trade).filter(Trade.user_id == uid, Trade.status == "PENDING").all()

    # Suppress the replication exit-mirror for this user's masters — we close
    # everything here, so the monitor must not also fan out a second exit.
    for mt in db.query(Trade).filter(Trade.user_id == uid, Trade.repl_entry == 1,
                                     Trade.repl_exit == 0).all():
        try:
            replication._claim(db, mt.id, "repl_exit")
        except Exception:
            pass

    # 1) Cancel pending orders (cancel the resting broker order, then mark cancelled).
    cancelled = 0
    for t in pends:
        try:
            if t.broker_order_id and t.account_id:
                broker, _mode, _err = replication._broker_for(db, t.account_id)
                if broker is not None and hasattr(broker, "cancel_order"):
                    broker.cancel_order(t.broker_order_id)
        except Exception:
            pass
        t.status = "CANCELLED"
        t.exit_reason = "AUTO_SQOFF"
        cancelled += 1

    # 2) Square off open positions at MARKET, each via its OWN account's broker.
    closed = 0
    for t in opens:
        try:
            broker, _mode, err = replication._broker_for(db, t.account_id)
            if broker is None:
                _log(db, uid, f"Auto square-off: {err} — could not close {t.symbol}.", "ERROR", t.id)
                continue
            price = (replication._ltp(db, uid, t.exchange_segment, t.security_id)
                     or t.last_price or t.entry_fill_price)
            if baskets._exit_trade(db, t, broker, price, "AUTO_SQOFF"):
                closed += 1
        except Exception as e:
            _log(db, uid, f"Auto square-off error on {t.symbol}: {e}", "ERROR", t.id)

    _halt_for_day(db, uid, today, sq)
    if closed or cancelled:
        _log(db, uid, f"⏰ Auto square-off at {sq} IST — closed {closed} position(s), "
                      f"cancelled {cancelled} pending order(s). New trades are blocked "
                      f"for the rest of today.", "WARN")
    else:
        _log(db, uid, f"⏰ Auto square-off time {sq} IST reached — no open positions. "
                      f"New trades are blocked for the rest of today.", "INFO")


def _tick():
    db = SessionLocal()
    try:
        now = _ist_now()
        now_secs = now.hour * 3600 + now.minute * 60 + now.second
        today = now.strftime("%Y-%m-%d")
        for u in db.query(User).filter(User.status == "ACTIVE").all():
            if u.role != "SUPER_ADMIN" and u.plan_expiry and u.plan_expiry < dt.datetime.utcnow():
                continue
            sq = uget(db, u.id, "auto_squareoff_time", "15:15:00")
            if not sq or str(sq).lower() == "off":
                continue
            sqs = _secs(sq)
            if sqs is None:
                continue
            if uget(db, u.id, "auto_sqoff_date", "") == today:   # already done today
                continue
            # Fire only inside [square-off time, 16:00) so a late restart can't
            # trigger an auto square-off in the middle of the night.
            if now_secs < sqs or now_secs >= 16 * 3600:
                continue
            uset(db, u.id, "auto_sqoff_date", today)             # claim before acting (once/day)
            db.commit()
            try:
                _squareoff_user(db, u.id, sq, today)
                db.commit()
            except Exception as e:
                db.rollback()
                _log(db, u.id, f"Auto square-off failed: {e}", "ERROR")
                db.commit()
    finally:
        db.close()


def _loop():
    while True:
        time.sleep(20)            # ~3 checks per minute
        try:
            _tick()
        except Exception:
            pass


def start():
    threading.Thread(target=_loop, daemon=True).start()
