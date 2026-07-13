"""Sleeve portfolios + order execution + profit skimming + top-up detection.

Paper mode simulates fills at the live price with the taker fee mirrored.
Live mode places real market orders via ccxt — only reachable when
TRADING_ENABLED=true and Kraken keys are present (config.mode()).
Sleeves are virtual books over the single account in either mode.
"""
import json
import logging
from datetime import datetime, timezone

from . import config, db, market, sleeves, stops

LOGGER = logging.getLogger(__name__)
TOPUP_EPSILON_EUR = 1.0  # ignore dust/fee drift below this when detecting deposits
DUST_EUR = 1.0           # a holding worth less than this cannot be sold — it is not a position


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def holdings(conn, mode: str, sleeve: str) -> dict[str, float]:
    return {r["asset"]: r["amount"] for r in conn.execute(
        "SELECT asset, amount FROM holdings WHERE mode=? AND sleeve=? AND amount > 1e-12",
        (mode, sleeve))}


def _set(conn, mode: str, sleeve: str, asset: str, amount: float) -> None:
    conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES(?,?,?,?) "
                 "ON CONFLICT(mode, sleeve, asset) DO UPDATE SET amount=excluded.amount",
                 (mode, sleeve, asset, amount))


def valued(conn, mode: str, sleeve: str, prices: dict[str, float]) -> dict:
    """Value a sleeve. Sub-€1 crumbs are counted in the total (they are real) but
    reported separately as `dust` — they are below every exchange minimum, so they
    cannot be sold. Left among the holdings they read as a position: the brain sees
    "you hold BTC", proposes selling it, and the order can only ever be rejected."""
    h = holdings(conn, mode, sleeve)
    total = h.get(config.BASE_CURRENCY, 0.0)
    detail = {config.BASE_CURRENCY: round(h.get(config.BASE_CURRENCY, 0.0), 2)}
    dust = {}
    for pair, price in prices.items():
        asset = pair.split("/")[0]
        if h.get(asset):
            value = h[asset] * price
            total += value
            if value < DUST_EUR:
                dust[asset] = round(value, 2)      # counted, but not a position
                continue
            detail[asset] = {"amount": h[asset], "eur_value": round(value, 2)}
    if dust:
        detail["dust"] = dust
    meta = conn.execute("SELECT allocated, hwm FROM sleeve_meta WHERE mode=? AND sleeve=?",
                        (mode, sleeve)).fetchone()
    return {"sleeve": sleeve, "total_eur": round(total, 2), "holdings": detail,
            "allocated": meta["allocated"] if meta else 0.0,
            "hwm": meta["hwm"] if meta else 0.0}


def overview(conn, mode: str, prices: dict[str, float]) -> dict:
    per = [valued(conn, mode, s, prices) for s in sleeves.ALL]
    return {"total_eur": round(sum(v["total_eur"] for v in per), 2), "sleeves": per}


def snapshot_all(conn, mode: str, prices: dict[str, float]) -> dict:
    ov = overview(conn, mode, prices)
    for v in ov["sleeves"]:
        conn.execute("INSERT INTO snapshots(at, mode, sleeve, total_eur, holdings, prices) "
                     "VALUES(?,?,?,?,?,?)",
                     (_now(), mode, v["sleeve"], v["total_eur"], json.dumps(v["holdings"]),
                      json.dumps(prices)))
    conn.commit()
    return ov


def min_order_eur(pair: str) -> float:
    try:
        m = market.exchange().market(pair)
        cost_min = (m.get("limits", {}).get("cost") or {}).get("min")
        return float(cost_min) if cost_min else 10.0
    except Exception:  # noqa: BLE001 - fall back to a safe floor
        return 10.0


def _settle(ex, pair: str, order_ids: list[str]) -> dict:
    """Ask the exchange what actually happened across the orders we placed.

    The books used to MODEL the fill (assume the touch price, assume the fee comes
    out of the cash before buying). Kraken does neither: you receive the full
    amount you bought, the fee is charged on top in the quote currency, and the
    price is whatever you got. Modelling it made every trade disagree with reality
    and quietly leaked the difference into the nightly reconcile as "drift" (#39).
    """
    filled = cost = fee_quote = fee_base = 0.0
    for oid in [o for o in order_ids if o]:
        try:
            o = ex.fetch_order(oid, pair)
        except Exception as e:  # noqa: BLE001 - one unreadable order must not lose the rest
            LOGGER.warning("could not read order %s back: %s", oid, e)
            continue
        filled += float(o.get("filled") or 0)
        cost += float(o.get("cost") or 0)          # quote actually exchanged
        f = o.get("fee") or {}
        fees = o.get("fees") or ([f] if f else [])
        for one in fees:
            amt = float(one.get("cost") or 0)
            cur = (one.get("currency") or "").upper()
            if not amt:
                continue
            if cur == pair.split("/")[0].upper():
                fee_base += amt      # some venues charge the fee in the coin itself
            else:
                fee_quote += amt     # Kraken: in the quote currency (EUR)
    price = (cost / filled) if filled else 0.0
    return {"filled": filled, "cost": cost, "fee_quote": fee_quote,
            "fee_base": fee_base, "price": price}


def _live_fill(pair: str, side: str, amount: float, limit_price: float) -> dict:
    """Place a post-only limit at the touch; fall back to market if unfilled.

    Maker fills save ~0.15% per side over market orders — free money on every
    patient fill. Returns the SETTLED truth from the exchange, not an estimate.
    """
    import time as _t
    ex = market.exchange()
    ids: list[str] = []
    try:
        o = ex.create_order(pair, "limit", side, amount, limit_price, {"postOnly": True})
        ids.append(o.get("id"))
    except Exception as e:  # noqa: BLE001 - post-only rejected (would cross) -> just take
        LOGGER.info("post-only rejected (%s) — going to market", e)
        o = ex.create_order(pair, "market", side, amount)
        return {"id": o.get("id"), **_settle(ex, pair, [o.get("id")])}

    deadline = _t.time() + config.LIMIT_FILL_WAIT_S
    while _t.time() < deadline:
        _t.sleep(5)
        st = ex.fetch_order(ids[0], pair)
        if st.get("status") == "closed":
            return {"id": ids[0], **_settle(ex, pair, ids)}
    try:
        ex.cancel_order(ids[0], pair)
    except Exception:  # noqa: BLE001 - may have filled in the race; checked below
        pass
    st = ex.fetch_order(ids[0], pair)
    filled = float(st.get("filled") or 0)
    if filled >= amount * 0.999:
        return {"id": ids[0], **_settle(ex, pair, ids)}
    remainder = amount - filled
    LOGGER.info("limit unfilled after %ss (%.6f of %.6f) — market for the rest",
                config.LIMIT_FILL_WAIT_S, filled, amount)
    o2 = ex.create_order(pair, "market", side, remainder)
    ids.append(o2.get("id"))
    return {"id": o2.get("id"), **_settle(ex, pair, ids)}   # blended across both fills


def execute(conn, mode: str, sleeve: str, decision_id: int, action: str, pair: str,
            fraction: float, prices: dict[str, float], stop_pct: float | None = None) -> dict:
    """Execute a validated buy/sell inside one sleeve's books.

    Fills aim for maker pricing: a post-only limit at the current touch.

    In LIVE mode the books record what the exchange actually settled — the filled
    amount, the average price it really got, and the fee it really charged. In
    paper/shadow we mirror the same convention Kraken uses (#39): you receive the
    full amount you bought and the fee is charged ON TOP, so the total cash leaving
    the sleeve is exactly what it decided to spend."""
    t = market.touch(pair)
    asset = pair.split("/")[0]
    h = holdings(conn, mode, sleeve)
    if action == "buy":
        price = t["bid"]
        cash = h.get(config.BASE_CURRENCY, 0.0)
        spend = cash * fraction                      # the total this sleeve is willing to part with
        if spend < min_order_eur(pair):
            raise ValueError(f"buy of {config.symbol()}{spend:.2f} is under the {config.symbol()}{min_order_eur(pair):.0f} exchange minimum")
        # size so that cost + fee == spend, because the fee is charged on top
        amount = spend / (price * (1 + config.MAKER_FEE))
        if mode == "live":
            f = _live_fill(pair, "buy", amount, price)
            exchange_id = f["id"]
            amount = f["filled"] - f["fee_base"]     # what actually landed in the account
            price = f["price"] or price
            fee = f["fee_quote"]
            spend = f["cost"] + fee                  # what actually left it
        else:
            exchange_id = None
            fee = amount * price * config.MAKER_FEE
            spend = amount * price + fee
        if amount <= 0:
            raise ValueError(f"buy of {pair} filled nothing")
        _set(conn, mode, sleeve, config.BASE_CURRENCY, cash - spend)
        _set(conn, mode, sleeve, asset, h.get(asset, 0.0) + amount)
    elif action == "sell":
        # clear this sleeve's resting stops on this pair FIRST. The sleeves are
        # virtual books over one real account, so a stop left behind after its
        # position is gone would sell a DIFFERENT sleeve's coins. If the exchange
        # will not let go of it, refusing to sell is the safe failure (#35).
        resting = stops.open_stops(conn, mode, sleeve, pair)   # remember before cancelling
        if not stops.cancel_for(conn, mode, sleeve, pair):
            raise ValueError(f"refusing to sell {pair}: could not cancel this sleeve's resting "
                             f"stop-loss — selling anyway would orphan it onto another sleeve")
        price = t["ask"]
        amount = h.get(asset, 0.0) * fraction
        proceeds = amount * price
        if amount <= 0 or proceeds < 1.0:
            raise ValueError(f"nothing meaningful to sell ({asset} balance {h.get(asset, 0.0)})")
        if mode == "live":
            f = _live_fill(pair, "sell", amount, price)
            exchange_id = f["id"]
            amount = f["filled"]                     # what actually left the account
            price = f["price"] or price
            fee = f["fee_quote"]
            proceeds = f["cost"]                     # gross quote received
        else:
            exchange_id = None
            fee = proceeds * config.MAKER_FEE
        _set(conn, mode, sleeve, asset, h.get(asset, 0.0) - amount)
        _set(conn, mode, sleeve, config.BASE_CURRENCY, h.get(config.BASE_CURRENCY, 0.0) + proceeds - fee)
        # a PARTIAL sell leaves coins behind. Their stop was just cancelled, so put a
        # floor back under them at the original trigger — otherwise the remainder rides
        # naked and nobody notices (#35).
        remaining = h.get(asset, 0.0) - amount
        if resting and remaining > 0:
            stops.reprotect(conn, mode, sleeve, pair, remaining, resting[0], price)
        spend = proceeds
    else:
        raise ValueError(f"unknown action {action}")
    conn.execute(
        "INSERT INTO orders(at, mode, sleeve, decision_id, pair, side, amount, price, cost, fee, exchange_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, sleeve, decision_id, pair, action, amount, price, spend, fee, exchange_id))
    conn.commit()
    LOGGER.info("[%s/%s] %s %.8f %s @ %.2f (€%.2f, fee €%.2f)",
                mode, sleeve, action, amount, asset, price, spend, fee)
    stop = stops.place(conn, mode, sleeve, pair, amount, price, stop_pct) if action == "buy" else None
    return {"side": action, "pair": pair, "amount": amount, "price": price,
            "cost_eur": round(spend, 2), "fee_eur": round(fee, 2), "stop": stop}


def skim_profits(conn, mode: str, prices: dict[str, float]) -> list[dict]:
    """Move SKIM_FRACTION of each active sleeve's realised profit above its
    high-water mark into the vault. Only EUR actually in the sleeve moves —
    unrealised gains stay until sold. The mark ratchets to post-skim equity."""
    skimmed = []
    for s in sleeves.ACTIVE:
        v = valued(conn, mode, s, prices)
        profit = v["total_eur"] - v["hwm"]
        if profit <= 0.01:
            continue
        eur = holdings(conn, mode, s).get(config.BASE_CURRENCY, 0.0)
        amount = round(min(eur, profit * config.SKIM_FRACTION), 2)
        if amount < 0.5:  # not worth shuffling pennies
            continue
        _set(conn, mode, s, config.BASE_CURRENCY, eur - amount)
        vault_eur = holdings(conn, mode, sleeves.VAULT).get(config.BASE_CURRENCY, 0.0)
        _set(conn, mode, sleeves.VAULT, config.BASE_CURRENCY, vault_eur + amount)
        conn.execute("UPDATE sleeve_meta SET hwm=? WHERE mode=? AND sleeve=?",
                     (v["total_eur"] - amount, mode, s))
        conn.execute("INSERT INTO skims(at, mode, sleeve, amount) VALUES(?,?,?,?)",
                     (_now(), mode, s, amount))
        skimmed.append({"sleeve": s, "amount": amount})
        LOGGER.info("[%s] skimmed €%.2f from %s to vault", mode, amount, s)
    conn.commit()
    return skimmed


def booked_eur(conn, mode: str) -> float:
    row = conn.execute("SELECT SUM(amount) s FROM holdings WHERE mode=? AND asset='EUR'",
                       (mode,)).fetchone()
    return row["s"] or 0.0


def apply_topup(conn, mode: str, amount: float) -> dict:
    """Split a detected (or simulated) cash deposit equally across the active
    sleeves. Allocation and HWM rise with it so a top-up is never mistaken
    for skimmable profit. The vault stays profits-only."""
    per = round(amount / len(sleeves.ACTIVE), 2)
    for s in sleeves.ACTIVE:
        eur = holdings(conn, mode, s).get(config.BASE_CURRENCY, 0.0)
        _set(conn, mode, s, config.BASE_CURRENCY, eur + per)
        conn.execute("UPDATE sleeve_meta SET allocated=allocated+?, hwm=hwm+? "
                     "WHERE mode=? AND sleeve=?", (per, per, mode, s))
    conn.commit()
    try:  # the phantom hodler buys in with the same cash (#6)
        from . import ledger
        ledger.bench_add(conn, mode, amount, market.tickers(config.PAIRS))
    except Exception as e:  # noqa: BLE001 - benchmark is bookkeeping, never blocks a top-up
        LOGGER.warning("benchmark top-up skipped: %s", e)
    LOGGER.info("[%s] top-up €%.2f split across active sleeves (€%.2f each)", mode, amount, per)
    return {"topup_eur": amount, "per_sleeve": per}


def detect_topup(conn, mode: str) -> dict | None:
    """Live mode: any EUR on Kraken beyond what the sleeve books account for
    is a fresh deposit — split it. Paper mode has POST /api/topup instead."""
    if mode != "live":
        return None
    actual = float(market.exchange().fetch_balance().get("total", {}).get(config.BASE_CURRENCY) or 0.0)
    surplus = actual - booked_eur(conn, mode)
    if surplus > TOPUP_EPSILON_EUR:
        return apply_topup(conn, mode, round(surplus, 2))
    return None
