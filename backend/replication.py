"""
Trade Replication (Master-Slave / copy-trading) engine.

One MASTER account's fills are mirrored onto N SLAVE accounts inside a group:

  • A fill on the master account (from the app, a basket, an external broker
    webhook, the Demo/paper engine, or an instant/scheduled group order) is the
    trigger. Each active slave fires a MARKET order sized by its rule
    (Quantity Multiplier or Fixed Qty), translated to that slave's broker via the
    Universal Symbol Mapper, and dispatched CONCURRENTLY to minimise slippage.
  • FULL COPY: when the master closes, every slave's matching position is closed
    too. A "Square Off Group" panic button closes all slave positions instantly.

Everything runs IN-PROCESS (no Redis): a fast monitor thread detects master
fills/exits idempotently (repl_entry / repl_exit flags claimed under a lock), and
a precision scheduler thread fires scheduled group orders at the exact second.

Each slave execution becomes its own Trade row (source="SLAVE", group_id,
master_trade_id) so it gets live MTM and broker-confirmation for free. The
trading engine deliberately does NOT manage SLAVE/MASTER rows (they live on other
accounts) — this module places/closes them through each account's OWN broker.
"""
import datetime as dt
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from database import SessionLocal
from models import (ExecutionGroup, GroupSlave, GroupTradeLog, GroupScheduledOrder,
                    Trade, Account, LogEntry)
from usettings import uget

_lock = threading.Lock()


def _log(db, uid, msg, level="INFO", trade_id=0):
    db.add(LogEntry(message=msg, level=level, user_id=uid, trade_id=trade_id))


# ----------------------------------------------------------------------------
# Broker resolution (each account uses its OWN broker, not the user's default)
# ----------------------------------------------------------------------------
def _broker_for(db, account_id):
    """(broker, mode, error). account_id 0 => Demo paper. A real but unconnected
    account => (None, 'LIVE', reason) so the caller fails just that slave."""
    import engine as _eng
    if not account_id:
        return _eng.engine.paper, "TEST", ""
    acc = db.get(Account, account_id)
    if acc is None:
        return None, "LIVE", "account missing"
    broker = _eng.engine._trade_broker(db, str(acc.id), acc.user_id)
    if broker is None:
        return None, "LIVE", f"{acc.broker} not connected"
    return broker, "LIVE", ""


def _ltp(db, uid, seg, sid):
    import engine as _eng
    dp = uget(db, uid, "data_provider", "DEMO")
    try:
        pr = _eng.engine._fetch_prices(db, uid, dp, [(seg, str(sid))], active=False)
        return pr.get((seg, str(sid)), 0) or 0
    except Exception:
        return 0


def _place(db, t, broker, is_exit, qty, price):
    """Place + (for live) confirm one order. Returns (ok, fill_price, order_id, error)."""
    place = broker.place_exit if is_exit else broker.place_entry
    try:
        res = place(t, price, qty=qty)
    except Exception as e:
        return False, 0, "", f"network error: {e}"
    if broker.name == "PAPER":
        return True, (res.fill_price or price), (res.order_id or ""), ""
    if not res.ok:
        return False, 0, (res.order_id or ""), (res.error or "rejected")
    if not res.order_id:
        return True, (res.fill_price or price), "", ""
    status, traded, _, reason = broker.confirm(res.order_id)
    if status == "TRADED":
        return True, (traded or res.fill_price or price), res.order_id, ""
    if status in ("REJECTED", "CANCELLED", "EXPIRED"):
        return False, 0, res.order_id, (reason or status)
    return True, (res.fill_price or price), res.order_id, ""    # placed, awaiting fill


def _claim(db, trade_id, field):
    """Atomically set a master flag (repl_entry / repl_exit) once. True for the winner."""
    with _lock:
        t = db.get(Trade, trade_id)
        if not t or getattr(t, field):
            return False
        setattr(t, field, 1)
        db.commit()
        return True


# ----------------------------------------------------------------------------
# Lot size & slave sizing (each account uses ITS OWN broker's lot size)
# ----------------------------------------------------------------------------
def _to_int(v):
    try:
        return int(round(float(v)))
    except Exception:
        return 0


def _lot_for_account(db, account_id, security_id, exchange_segment, dhan_lot):
    """Lot size for a contract on a SPECIFIC account's broker (auto-updating from
    its master). Lot size is broker-specific (esp. MCX). Demo/Dhan -> the Dhan
    master lot; else translate to the broker and read its lot."""
    dhan_lot = _to_int(dhan_lot) or 1
    if not account_id:
        return dhan_lot
    acc = db.get(Account, account_id)
    if acc is None or acc.broker == "DHAN":
        return dhan_lot
    from instruments import store
    meta = store.get_meta(security_id) or {}
    try:
        if acc.broker == "ANGEL":
            import angel
            a = angel.mapper.translate(security_id, exchange_segment, meta)
            return _to_int(a and a.get("lotsize")) or dhan_lot
        if acc.broker == "ZERODHA":
            import zerodha
            z = zerodha.mapper.translate(security_id, exchange_segment, meta)
            return _to_int(z and z.get("lot_size")) or dhan_lot
        if acc.broker == "ALICE":
            import aliceblue
            al = aliceblue.mapper.translate(security_id, exchange_segment, meta)
            return _to_int(al and al.get("lot_size")) or dhan_lot
    except Exception:
        pass
    return dhan_lot


def _slave_qty(db, master_trade, slave):
    """(quantity, slave_lot_size). The number of LOTS is derived from the master,
    then converted to the SLAVE broker's own quantity units."""
    mlot = int(master_trade.lot_size or 1) or 1
    master_lots = max(1, round((master_trade.quantity or mlot) / mlot))
    if slave.condition_type == "FIXED":
        lots = max(1, int(round(slave.condition_value or 1)))
    else:
        lots = max(1, int(round(master_lots * (slave.condition_value or 1))))
    slot = _lot_for_account(db, slave.slave_account_id, master_trade.security_id,
                            master_trade.exchange_segment, mlot)
    return lots * slot, slot


# ----------------------------------------------------------------------------
# Entry fan-out (master filled -> slaves enter), concurrent
# ----------------------------------------------------------------------------
def _try_fan_entry(group_id, master_trade_id):
    db = SessionLocal()
    jobs = []
    try:
        if not _claim(db, master_trade_id, "repl_entry"):
            return
        g = db.get(ExecutionGroup, group_id)
        mt = db.get(Trade, master_trade_id)
        if not g or not mt or not g.is_active:
            return
        slaves = (db.query(GroupSlave)
                  .filter(GroupSlave.group_id == group_id, GroupSlave.is_active == 1).all())
        for s in slaves:
            qty, slot = _slave_qty(db, mt, s)
            st = Trade(symbol=mt.symbol, name=f"{g.name} · slave", security_id=mt.security_id,
                       exchange_segment=mt.exchange_segment, instrument_type=mt.instrument_type,
                       side=mt.side, quantity=qty, lot_size=slot, entry_type="MARKET",
                       mode=("TEST" if not s.slave_account_id else "LIVE"), status="PENDING",
                       source="SLAVE", account_id=s.slave_account_id, user_id=g.user_id,
                       group_id=g.id, master_trade_id=mt.id, last_price=mt.entry_fill_price or 0)
            db.add(st)
            db.flush()
            log = GroupTradeLog(group_id=g.id, user_id=g.user_id, master_trade_id=mt.id,
                                master_order_id=mt.broker_order_id or "",
                                slave_account_id=s.slave_account_id, slave_trade_id=st.id,
                                side=mt.side, action="ENTRY", status="PENDING")
            db.add(log)
            db.flush()
            jobs.append((st.id, log.id, s.slave_account_id, qty, mt.entry_fill_price or 0))
        db.commit()
        _log(db, g.user_id, f"Group '{g.name}': master filled — replicating to {len(jobs)} slave(s).", "INFO")
        db.commit()
    finally:
        db.close()
    if jobs:
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            for j in jobs:
                ex.submit(_exec_slave_entry, *j)


def _exec_slave_entry(slave_trade_id, log_id, account_id, qty, price):
    db = SessionLocal()
    try:
        st = db.get(Trade, slave_trade_id)
        log = db.get(GroupTradeLog, log_id)
        if not st:
            return
        broker, mode, err = _broker_for(db, account_id)
        if broker is None:
            _fail(db, st, log, err)
            db.commit()
            return
        # Per-slave margin ping: fail THIS slave only if it clearly can't afford it.
        if account_id:
            try:
                import margins
                acc = db.get(Account, account_id)
                m = margins.single_margin(db, acc, st.security_id, st.exchange_segment,
                                          st.instrument_type, st.side, qty, price)
                if m["verified"] and not m["ok"]:
                    _fail(db, st, log, f"insufficient margin (need ₹{m['required']:,.0f}, "
                                       f"have ₹{m['available']:,.0f})")
                    _log(db, st.user_id, f"Slave {acc.label or acc.broker}: insufficient margin "
                                         f"— skipped this account.", "WARN", st.id)
                    db.commit()
                    return
            except Exception:
                pass
        ok, fill, oid, e = _place(db, st, broker, False, qty, price)
        if ok:
            st.status = "OPEN"
            st.entry_fill_price = fill
            st.broker = broker.name
            st.broker_order_id = oid
            if log:
                log.status = "EXECUTED"
                log.slave_order_id = oid
            _log(db, st.user_id, f"Slave ENTRY {st.side} {st.symbol} x{qty} @ {fill} [{broker.name}]", "INFO", st.id)
        else:
            _fail(db, st, log, e)
            _log(db, st.user_id, f"Slave ENTRY FAILED {st.symbol} [{broker.name}]: {e}", "ERROR", st.id)
        db.commit()
    finally:
        db.close()


def _fail(db, st, log, err):
    st.status = "REJECTED"
    st.exit_reason = (err or "failed")[:200]
    if log:
        log.status = "FAILED"
        log.error_message = (err or "failed")[:300]


# ----------------------------------------------------------------------------
# Exit fan-out (master closed -> slaves close) + square-off, concurrent
# ----------------------------------------------------------------------------
def _try_fan_exit(group_id, master_trade_id):
    db = SessionLocal()
    try:
        if not _claim(db, master_trade_id, "repl_exit"):
            return
        ids = [t.id for t in db.query(Trade).filter(
            Trade.group_id == group_id, Trade.master_trade_id == master_trade_id,
            Trade.source == "SLAVE", Trade.status == "OPEN").all()]
    finally:
        db.close()
    _close_many(ids, "MASTER_EXIT")


def _close_many(trade_ids, reason):
    if not trade_ids:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(trade_ids))) as ex:
        for tid in trade_ids:
            ex.submit(_close_one, tid, reason)


def _close_one(trade_id, reason):
    db = SessionLocal()
    try:
        st = db.get(Trade, trade_id)
        if not st or st.status != "OPEN":
            return
        broker, mode, err = _broker_for(db, 0 if st.mode == "TEST" else st.account_id)
        if broker is None:
            _log(db, st.user_id, f"Group square-off: {err} — could not close {st.symbol}.", "ERROR", st.id)
            db.commit()
            return
        price = (_ltp(db, st.user_id, st.exchange_segment, st.security_id)
                 or st.last_price or st.entry_fill_price)
        remaining = int(st.quantity) - int(st.exited_qty or 0)
        ok, fill, oid, e = _place(db, st, broker, True, remaining, price)
        exit_side = "SELL" if st.side == "BUY" else "BUY"
        if ok:
            direction = 1 if st.side == "BUY" else -1
            pv = 1.0   # equity P&L: 1 point = one currency unit per qty
            st.realized_pnl = (st.realized_pnl or 0) + (price - st.entry_fill_price) * direction * remaining * pv
            st.exited_qty = st.quantity
            st.exit_fill_price = fill
            st.status = "CLOSED"
            st.exit_reason = reason
            st.pnl = round(st.realized_pnl, 2)
            db.add(GroupTradeLog(group_id=st.group_id, user_id=st.user_id,
                                 master_trade_id=st.master_trade_id, slave_account_id=st.account_id,
                                 slave_trade_id=st.id, slave_order_id=oid, side=exit_side,
                                 action="EXIT", status="EXECUTED"))
            _log(db, st.user_id, f"Slave EXIT ({reason}) {st.symbol} x{remaining} @ {fill} "
                                 f"P&L={st.pnl:.2f} [{broker.name}]", "INFO", st.id)
        else:
            db.add(GroupTradeLog(group_id=st.group_id, user_id=st.user_id,
                                 master_trade_id=st.master_trade_id, slave_account_id=st.account_id,
                                 slave_trade_id=st.id, side=exit_side, action="EXIT",
                                 status="FAILED", error_message=(e or "failed")[:300]))
            _log(db, st.user_id, f"Slave EXIT FAILED {st.symbol} [{broker.name}]: {e}", "ERROR", st.id)
        db.commit()
    finally:
        db.close()


def square_off_group(group_id):
    """Panic: close every open position (slaves + an instant-master) in the group."""
    db = SessionLocal()
    try:
        ids = [t.id for t in db.query(Trade).filter(
            Trade.group_id == group_id, Trade.status == "OPEN").all()]
        g = db.get(ExecutionGroup, group_id)
        if g:
            _log(db, g.user_id, f"Group '{g.name}': SQUARE OFF — closing {len(ids)} open position(s).", "WARN")
            db.commit()
    finally:
        db.close()
    threading.Thread(target=_close_many, args=(ids, "GROUP_SQOFF"), daemon=True).start()
    return len(ids)


def _kill_slaves(uid):
    db = SessionLocal()
    try:
        ids = [t.id for t in db.query(Trade).filter(
            Trade.user_id == uid, Trade.source == "SLAVE", Trade.status == "OPEN").all()]
    finally:
        db.close()
    _close_many(ids, "KILL")


# ----------------------------------------------------------------------------
# Master firing (instant + scheduled)
# ----------------------------------------------------------------------------
def fire_master(group_id, order):
    """Create + fill the master order on the group's master account, then cascade.
    order = {side, security_id, exchange_segment, instrument_type, symbol, lot_size, qty_lots}."""
    db = SessionLocal()
    master_id = None
    try:
        g = db.get(ExecutionGroup, group_id)
        if not g or not g.is_active:
            return
        broker, mode, err = _broker_for(db, g.master_account_id)
        if broker is None:
            _log(db, g.user_id, f"Group '{g.name}': master not connected ({err}) — cannot fire.", "ERROR")
            db.commit()
            return
        # The master fires on its OWN account's broker — size with that broker's lot.
        lot = _lot_for_account(db, g.master_account_id, str(order.get("security_id", "")),
                               order.get("exchange_segment", ""), int(order.get("lot_size", 1) or 1))
        qty = max(1, int(order.get("qty_lots", 1) or 1)) * lot
        side = "SELL" if str(order.get("side", "BUY")).upper() == "SELL" else "BUY"
        t = Trade(symbol=order.get("symbol", ""), name=f"{g.name} · master",
                  security_id=str(order.get("security_id", "")),
                  exchange_segment=order.get("exchange_segment", ""),
                  instrument_type=order.get("instrument_type", "OPTION"),
                  side=side, quantity=qty, lot_size=lot, entry_type="MARKET", mode=mode,
                  status="PENDING", source="MASTER", account_id=g.master_account_id,
                  user_id=g.user_id, group_id=g.id)
        db.add(t)
        db.flush()
        price = _ltp(db, g.user_id, t.exchange_segment, t.security_id)
        ok, fill, oid, e = _place(db, t, broker, False, qty, price or 0)
        if ok:
            t.status = "OPEN"
            t.entry_fill_price = fill
            t.broker = broker.name
            t.broker_order_id = oid
            master_id = t.id
            _log(db, g.user_id, f"Group '{g.name}': MASTER {side} {t.symbol} x{qty} @ {fill} "
                                f"[{broker.name}] — cascading to slaves.", "INFO", t.id)
        else:
            t.status = "REJECTED"
            t.exit_reason = e
            _log(db, g.user_id, f"Group '{g.name}': MASTER order rejected: {e}", "ERROR", t.id)
        db.commit()
    finally:
        db.close()
    if master_id:
        _try_fan_entry(group_id, master_id)


def execute_group(group_id, order):
    threading.Thread(target=fire_master, args=(group_id, order), daemon=True).start()


def _claim_sched(db, oid):
    with _lock:
        o = db.get(GroupScheduledOrder, oid)
        if not o or o.status != "PENDING":
            return None
        o.status = "EXECUTED"
        db.commit()
        return {"side": o.side, "security_id": o.security_id, "exchange_segment": o.exchange_segment,
                "instrument_type": o.instrument_type, "symbol": o.symbol,
                "lot_size": o.lot_size, "qty_lots": o.qty_lots, "group_id": o.group_id}


# ----------------------------------------------------------------------------
# Background threads: replication monitor + precision scheduler
# ----------------------------------------------------------------------------
class _Monitor:
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            time.sleep(0.4)
            try:
                self._scan()
            except Exception:
                pass

    def _scan(self):
        db = SessionLocal()
        fan_entry, fan_exit, kill_uids = [], [], set()
        try:
            for g in db.query(ExecutionGroup).filter(ExecutionGroup.is_active == 1).all():
                # A fresh master fill -> replicate entry.
                for mt in db.query(Trade).filter(
                        Trade.user_id == g.user_id, Trade.account_id == g.master_account_id,
                        Trade.status == "OPEN", Trade.repl_entry == 0, Trade.source != "SLAVE").all():
                    fan_entry.append((g.id, mt.id))
                # A master that had replicated and is now closed -> mirror the exit.
                for mt in db.query(Trade).filter(
                        Trade.user_id == g.user_id, Trade.account_id == g.master_account_id,
                        Trade.status == "CLOSED", Trade.repl_entry == 1, Trade.repl_exit == 0,
                        Trade.source != "SLAVE").all():
                    fan_exit.append((g.id, mt.id))
                if uget(db, g.user_id, "kill_switch", "off") == "on":
                    kill_uids.add(g.user_id)
        finally:
            db.close()
        for gid, mid in fan_entry:
            _try_fan_entry(gid, mid)
        for gid, mid in fan_exit:
            _try_fan_exit(gid, mid)
        for uid in kill_uids:
            _kill_slaves(uid)


class _Scheduler:
    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            sleep = 0.25
            try:
                db = SessionLocal()
                now = dt.datetime.utcnow()
                soonest = None
                due = db.query(GroupScheduledOrder).filter(
                    GroupScheduledOrder.status == "PENDING").all()
                for o in due:
                    if not o.scheduled_at:
                        continue
                    if o.scheduled_at <= now:
                        order = _claim_sched(db, o.id)
                        if order:
                            execute_group(order.pop("group_id"), order)
                    else:
                        d = (o.scheduled_at - now).total_seconds()
                        soonest = d if soonest is None else min(soonest, d)
                db.close()
                if soonest is not None:
                    sleep = max(0.02, min(0.25, soonest))
            except Exception:
                sleep = 0.5
            time.sleep(sleep)


monitor = _Monitor()
scheduler = _Scheduler()


def start():
    monitor.start()
    scheduler.start()
