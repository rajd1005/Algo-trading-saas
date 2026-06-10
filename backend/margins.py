"""
Pre-execution margin checks for a basket.

For each Trading Provider we translate the legs to the broker's own token format
and call that broker's Margin Calculator API to get the TOTAL required margin,
plus the account's available funds. If the broker's margin API can't be reached
(or that broker doesn't expose one), we fall back to a rough local estimate and
mark the result `verified=False` — the caller (warn-but-allow policy) then warns
the user instead of blocking.
"""
import json

import requests

import config
from instruments import store


# ----------------------------------------------------------------------------
# Local fallback estimate (used when the broker margin API is unavailable)
# ----------------------------------------------------------------------------
def _ref_price(leg):
    px = float(leg.price or 0)
    if px > 0:
        return px
    meta = store.get_meta(leg.security_id) or {}
    # a coarse stand-in so the estimate isn't zero for market orders
    return float(meta.get("strike") or 0) * 0.02 or 50.0


def _estimate(legs):
    """Rough SPAN-style estimate: option BUY = premium; shorts / futures ~ 15%
    of notional. Deliberately conservative; only used when we can't verify."""
    total = 0.0
    for l in legs:
        meta = store.get_meta(l.security_id) or {}
        itype = (l.instrument_type or meta.get("instrument_type") or "").upper()
        qty = int(l.quantity or 0)
        px = _ref_price(l)
        if itype == "OPTION" and l.transaction_type == "BUY":
            total += px * qty                       # premium outflow
        else:
            total += px * qty * 0.15                # short option / futures span-ish
    return round(total, 2)


# ----------------------------------------------------------------------------
# Per-broker required-margin calls
# ----------------------------------------------------------------------------
def _zerodha_required(creds, legs):
    import zerodha as z
    key, tok = creds.get("api_key", ""), creds.get("access_token", "")
    orders = []
    for l in legs:
        zz = z.mapper.translate(l.security_id, l.exchange_segment, store.get_meta(l.security_id))
        if not zz:
            continue
        orders.append({
            "exchange": zz["exchange"], "tradingsymbol": zz["tradingsymbol"],
            "transaction_type": l.transaction_type, "variety": "regular",
            "product": "MIS", "order_type": "LIMIT" if l.order_type == "LIMIT" else "MARKET",
            "quantity": int(l.quantity), "price": float(l.price or 0),
        })
    if not orders:
        return None, "no legs mapped to Zerodha"
    r = requests.post(f"{z.API_BASE}/margins/basket", data=json.dumps(orders),
                      headers={**z._headers(key, tok), "Content-Type": "application/json"},
                      params={"consider_positions": "true"}, timeout=8)
    d = r.json()
    if d.get("status") == "success":
        return float(d["data"]["final"]["total"]), ""
    return None, d.get("message") or "Zerodha margin API error"


def _dhan_required(client_id, token, legs):
    total, hit = 0.0, 0
    headers = {"access-token": token, "client-id": client_id, "Content-Type": "application/json"}
    for l in legs:
        body = {"dhanClientId": client_id, "exchangeSegment": l.exchange_segment,
                "transactionType": l.transaction_type, "quantity": int(l.quantity),
                "productType": "INTRADAY", "securityId": str(l.security_id),
                "price": float(l.price or 0), "triggerPrice": float(l.trigger_price or 0)}
        r = requests.post(f"{config.DHAN_API_BASE}/margincalculator", json=body,
                          headers=headers, timeout=6)
        d = r.json()
        m = d.get("totalMargin") or d.get("total_margin")
        if m is not None:
            total += float(m); hit += 1
    if not hit:
        return None, "Dhan margin API returned nothing"
    return round(total, 2), ""


def _angel_required(creds, legs):
    import angel as a
    positions = []
    for l in legs:
        am = a.mapper.translate(l.security_id, l.exchange_segment, store.get_meta(l.security_id))
        if not am:
            continue
        positions.append({
            "exchange": am["exchange"], "qty": int(l.quantity), "price": float(l.price or 0),
            "productType": "INTRADAY", "token": str(am["token"]),
            "tradeType": l.transaction_type,
            "orderType": "LIMIT" if l.order_type == "LIMIT" else "MARKET",
        })
    if not positions:
        return None, "no legs mapped to Angel"
    r = requests.post(f"{a.API_BASE}/rest/secure/angelbroking/margin/v1/batch",
                      json={"positions": positions},
                      headers=a._auth_headers(creds.get("api_key", ""), creds.get("jwt", "")), timeout=8)
    d = r.json()
    if d.get("status") and isinstance(d.get("data"), dict):
        return float(d["data"].get("totalMarginRequired") or 0), ""
    return None, d.get("message") or "Angel margin API error"


def _required(acc, creds, legs):
    """(value, verified, detail). verified=False -> a fallback estimate."""
    try:
        if acc.broker == "ZERODHA":
            v, err = _zerodha_required(creds, legs)
        elif acc.broker == "DHAN":
            v, err = _dhan_required(acc.client_id, creds.get("access_token", ""), legs)
        elif acc.broker == "ANGEL":
            v, err = _angel_required(creds, legs)
        else:                                   # ALICE (no batch margin API wired)
            v, err = None, "no margin API for this broker"
        if v is not None:
            return round(float(v), 2), True, ""
        return _estimate(legs), False, err or "estimate"
    except Exception as e:
        return _estimate(legs), False, str(e)[:160]


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def basket_margin(db, acc, legs):
    """Returns {required, available, ok, verified, detail}.
    `ok` = available >= required (only meaningful when verified)."""
    try:
        creds = json.loads(acc.creds_json or "{}")
    except Exception:
        creds = {}
    required, verified, detail = _required(acc, creds, legs)

    available, avail_ok = 0.0, False
    try:
        import engine as _eng
        broker = _eng.engine._trade_broker(db, str(acc.id), acc.user_id)
        if broker is not None and hasattr(broker, "fund_limit"):
            avail_ok, available = broker.fund_limit()
            available = round(float(available or 0), 2)
    except Exception:
        pass

    ok = bool(verified and avail_ok and available >= required)
    return {"required": required, "available": available, "ok": ok,
            "verified": bool(verified and avail_ok), "detail": detail}
