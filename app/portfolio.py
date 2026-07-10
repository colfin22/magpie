"""Portfolio state + order execution.

Paper mode simulates fills at the live price with the taker fee mirrored.
Live mode places real market orders via ccxt — only reachable when
TRADING_ENABLED=true and Kraken keys are present (config.mode()).
"""
import json
import logging
from datetime import datetime, timezone

from . import config, db, market

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def holdings(conn, mode: str) -> dict[str, float]:
    return {r["asset"]: r["amount"] for r in
            conn.execute("SELECT asset, amount FROM holdings WHERE mode=? AND amount > 1e-12", (mode,))}


def _set(conn, mode: str, asset: str, amount: float) -> None:
    conn.execute("INSERT INTO holdings(mode, asset, amount) VALUES(?,?,?) "
                 "ON CONFLICT(mode, asset) DO UPDATE SET amount=excluded.amount",
                 (mode, asset, amount))


def valued(conn, mode: str, prices: dict[str, float]) -> dict:
    h = holdings(conn, mode)
    total = h.get("EUR", 0.0)
    detail = {"EUR": round(h.get("EUR", 0.0), 2)}
    for pair, price in prices.items():
        asset = pair.split("/")[0]
        if h.get(asset):
            value = h[asset] * price
            total += value
            detail[asset] = {"amount": h[asset], "eur_value": round(value, 2)}
    return {"total_eur": round(total, 2), "holdings": detail}


def snapshot(conn, mode: str, prices: dict[str, float]) -> dict:
    v = valued(conn, mode, prices)
    conn.execute("INSERT INTO snapshots(at, mode, total_eur, holdings, prices) VALUES(?,?,?,?,?)",
                 (_now(), mode, v["total_eur"], json.dumps(v["holdings"]), json.dumps(prices)))
    conn.commit()
    return v


def min_order_eur(pair: str) -> float:
    """Exchange minimum for a market buy, in EUR terms (approximate guard)."""
    try:
        m = market.exchange().market(pair)
        cost_min = (m.get("limits", {}).get("cost") or {}).get("min")
        return float(cost_min) if cost_min else 10.0
    except Exception:  # noqa: BLE001 - fall back to a safe floor
        return 10.0


def execute(conn, mode: str, decision_id: int, action: str, pair: str,
            fraction: float, prices: dict[str, float]) -> dict:
    """Execute a validated buy/sell. Returns an order summary dict."""
    price = prices[pair]
    asset = pair.split("/")[0]
    h = holdings(conn, mode)
    if action == "buy":
        spend = h.get("EUR", 0.0) * fraction
        if spend < min_order_eur(pair):
            raise ValueError(f"buy of €{spend:.2f} is under the €{min_order_eur(pair):.0f} exchange minimum")
        fee = spend * config.TAKER_FEE
        amount = (spend - fee) / price
        if mode == "live":
            order = market.exchange().create_market_buy_order(pair, amount)
            exchange_id = order.get("id")
        else:
            exchange_id = None
        _set(conn, mode, "EUR", h.get("EUR", 0.0) - spend)
        _set(conn, mode, asset, h.get(asset, 0.0) + amount)
    elif action == "sell":
        amount = h.get(asset, 0.0) * fraction
        proceeds = amount * price
        if amount <= 0 or proceeds < 1.0:
            raise ValueError(f"nothing meaningful to sell ({asset} balance {h.get(asset, 0.0)})")
        fee = proceeds * config.TAKER_FEE
        if mode == "live":
            order = market.exchange().create_market_sell_order(pair, amount)
            exchange_id = order.get("id")
        else:
            exchange_id = None
        _set(conn, mode, asset, h.get(asset, 0.0) - amount)
        _set(conn, mode, "EUR", h.get("EUR", 0.0) + proceeds - fee)
        spend = proceeds
    else:
        raise ValueError(f"unknown action {action}")
    conn.execute(
        "INSERT INTO orders(at, mode, decision_id, pair, side, amount, price, cost, fee, exchange_id) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, decision_id, pair, action, amount, price, spend, fee, exchange_id))
    conn.commit()
    LOGGER.info("%s %s: %s %.8f %s @ %.2f (€%.2f, fee €%.2f)",
                mode, action, pair, amount, asset, price, spend, fee)
    return {"side": action, "pair": pair, "amount": amount, "price": price,
            "cost_eur": round(spend, 2), "fee_eur": round(fee, 2)}
