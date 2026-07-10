"""Sleeve portfolios + order execution + profit skimming + top-up detection.

Paper mode simulates fills at the live price with the taker fee mirrored.
Live mode places real market orders via ccxt — only reachable when
TRADING_ENABLED=true and Kraken keys are present (config.mode()).
Sleeves are virtual books over the single account in either mode.
"""
import json
import logging
from datetime import datetime, timezone

from . import config, db, market, sleeves

LOGGER = logging.getLogger(__name__)
TOPUP_EPSILON_EUR = 1.0  # ignore dust/fee drift below this when detecting deposits


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
    h = holdings(conn, mode, sleeve)
    total = h.get("EUR", 0.0)
    detail = {"EUR": round(h.get("EUR", 0.0), 2)}
    for pair, price in prices.items():
        asset = pair.split("/")[0]
        if h.get(asset):
            value = h[asset] * price
            total += value
            detail[asset] = {"amount": h[asset], "eur_value": round(value, 2)}
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


def _live_fill(pair: str, side: str, amount: float, limit_price: float) -> tuple[str | None, float]:
    """Place a post-only limit at the touch; fall back to market if unfilled.

    Returns (exchange_id, fee_rate_actually_paid). Maker fills save ~0.15%
    per side over market orders — free money on every patient fill.
    """
    import time as _t
    ex = market.exchange()
    try:
        o = ex.create_order(pair, "limit", side, amount, limit_price, {"postOnly": True})
    except Exception as e:  # noqa: BLE001 - post-only rejected (would cross) -> just take
        LOGGER.info("post-only rejected (%s) — going to market", e)
        o = ex.create_order(pair, "market", side, amount)
        return o.get("id"), config.TAKER_FEE
    deadline = _t.time() + config.LIMIT_FILL_WAIT_S
    while _t.time() < deadline:
        _t.sleep(5)
        st = ex.fetch_order(o["id"], pair)
        if st.get("status") == "closed":
            return o["id"], config.MAKER_FEE
    try:
        ex.cancel_order(o["id"], pair)
    except Exception:  # noqa: BLE001 - may have filled in the race; checked below
        pass
    st = ex.fetch_order(o["id"], pair)
    filled = float(st.get("filled") or 0)
    if filled >= amount * 0.999:
        return o["id"], config.MAKER_FEE
    remainder = amount - filled
    LOGGER.info("limit unfilled after %ss (%.6f of %.6f) — market for the rest",
                config.LIMIT_FILL_WAIT_S, filled, amount)
    o2 = ex.create_order(pair, "market", side, remainder)
    # blended fee, weighted by how much each path filled
    rate = (filled * config.MAKER_FEE + remainder * config.TAKER_FEE) / amount
    return o2.get("id"), rate


def execute(conn, mode: str, sleeve: str, decision_id: int, action: str, pair: str,
            fraction: float, prices: dict[str, float]) -> dict:
    """Execute a validated buy/sell inside one sleeve's books.

    Fills aim for maker pricing: a post-only limit at the current touch
    (paper mode assumes the maker fill at that price)."""
    t = market.touch(pair)
    asset = pair.split("/")[0]
    h = holdings(conn, mode, sleeve)
    if action == "buy":
        price = t["bid"]
        spend = h.get("EUR", 0.0) * fraction
        if spend < min_order_eur(pair):
            raise ValueError(f"buy of €{spend:.2f} is under the €{min_order_eur(pair):.0f} exchange minimum")
        amount = (spend * (1 - config.MAKER_FEE)) / price
        if mode == "live":
            exchange_id, fee_rate = _live_fill(pair, "buy", amount, price)
        else:
            exchange_id, fee_rate = None, config.MAKER_FEE
        fee = spend * fee_rate
        amount = (spend - fee) / price
        _set(conn, mode, sleeve, "EUR", h.get("EUR", 0.0) - spend)
        _set(conn, mode, sleeve, asset, h.get(asset, 0.0) + amount)
    elif action == "sell":
        price = t["ask"]
        amount = h.get(asset, 0.0) * fraction
        proceeds = amount * price
        if amount <= 0 or proceeds < 1.0:
            raise ValueError(f"nothing meaningful to sell ({asset} balance {h.get(asset, 0.0)})")
        if mode == "live":
            exchange_id, fee_rate = _live_fill(pair, "sell", amount, price)
        else:
            exchange_id, fee_rate = None, config.MAKER_FEE
        fee = proceeds * fee_rate
        _set(conn, mode, sleeve, asset, h.get(asset, 0.0) - amount)
        _set(conn, mode, sleeve, "EUR", h.get("EUR", 0.0) + proceeds - fee)
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
    return {"side": action, "pair": pair, "amount": amount, "price": price,
            "cost_eur": round(spend, 2), "fee_eur": round(fee, 2)}


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
        eur = holdings(conn, mode, s).get("EUR", 0.0)
        amount = round(min(eur, profit * config.SKIM_FRACTION), 2)
        if amount < 0.5:  # not worth shuffling pennies
            continue
        _set(conn, mode, s, "EUR", eur - amount)
        vault_eur = holdings(conn, mode, sleeves.VAULT).get("EUR", 0.0)
        _set(conn, mode, sleeves.VAULT, "EUR", vault_eur + amount)
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
        eur = holdings(conn, mode, s).get("EUR", 0.0)
        _set(conn, mode, s, "EUR", eur + per)
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
    actual = float(market.exchange().fetch_balance().get("total", {}).get("EUR") or 0.0)
    surplus = actual - booked_eur(conn, mode)
    if surplus > TOPUP_EPSILON_EUR:
        return apply_topup(conn, mode, round(surplus, 2))
    return None
