"""Derived views over the books: benchmark, round trips, reconciliation.

Nothing here places orders; it keeps the books honest and measurable.
"""
import json
import logging
from datetime import datetime, timezone

from . import config, db, ha, market, sleeves

LOGGER = logging.getLogger(__name__)
RECONCILE_ALERT_EUR = float(__import__("os").environ.get("RECONCILE_ALERT_EUR", "2.0"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- benchmark: the phantom hodler (#6) ----------

def _bench_key(mode: str) -> str:
    return f"bench_{mode}"


def bench_init_if_needed(conn, mode: str, total_eur: float, prices: dict[str, float]) -> None:
    """First sight of capital: the phantom splits it equally across the pairs
    at that day's prices and never trades again."""
    if db.get_setting(conn, _bench_key(mode)) or total_eur <= 0:
        return
    share = total_eur / len(config.PAIRS)
    b = {"assets": {p.split("/")[0]: share / prices[p] for p in config.PAIRS},
         "invested": total_eur, "since": _now()}
    db.set_setting(conn, _bench_key(mode), json.dumps(b))
    LOGGER.info("[%s] benchmark hodler initialised with €%.2f", mode, total_eur)


def bench_add(conn, mode: str, amount: float, prices: dict[str, float]) -> None:
    """Top-ups buy into the phantom portfolio at current prices too."""
    raw = db.get_setting(conn, _bench_key(mode))
    if not raw:
        return
    b = json.loads(raw)
    share = amount / len(config.PAIRS)
    for p in config.PAIRS:
        b["assets"][p.split("/")[0]] = b["assets"].get(p.split("/")[0], 0) + share / prices[p]
    b["invested"] += amount
    db.set_setting(conn, _bench_key(mode), json.dumps(b))


def bench_value(conn, mode: str, prices: dict[str, float]) -> dict | None:
    raw = db.get_setting(conn, _bench_key(mode))
    if not raw:
        return None
    b = json.loads(raw)
    value = sum(amt * prices[f"{asset}/{config.BASE_CURRENCY}"] for asset, amt in b["assets"].items()
                if f"{asset}/{config.BASE_CURRENCY}" in prices)
    return {"hodl_eur": round(value, 2), "invested": round(b["invested"], 2),
            "since": b["since"]}


# ---------- round trips: FIFO entry->exit pairing (#7) ----------

def round_trips(conn, mode: str) -> list[dict]:
    """Close buys against sells FIFO per sleeve+pair. Approximate: entry fees
    are already inside the booked amounts; exit fees pro-rated."""
    trips = []
    keys = conn.execute("SELECT DISTINCT sleeve, pair FROM orders WHERE mode=?", (mode,))
    for k in keys.fetchall():
        lots = []  # open lots: [amount_left, entry_price, entry_at]
        for o in conn.execute(
                "SELECT at, side, amount, price, fee FROM orders "
                "WHERE mode=? AND sleeve=? AND pair=? ORDER BY id", (mode, k["sleeve"], k["pair"])):
            if o["side"] == "buy":
                lots.append([o["amount"], o["price"], o["at"]])
                continue
            remaining, exit_fee_rate = o["amount"], (o["fee"] / (o["amount"] * o["price"])
                                                     if o["amount"] and o["price"] else 0)
            while remaining > 1e-12 and lots:
                lot = lots[0]
                q = min(remaining, lot[0])
                pnl = q * (o["price"] - lot[1]) - q * o["price"] * exit_fee_rate
                entry_cost = q * lot[1]
                held_days = round((datetime.fromisoformat(o["at"])
                                   - datetime.fromisoformat(lot[2])).total_seconds() / 86400, 1)
                trips.append({"sleeve": k["sleeve"], "pair": k["pair"],
                              "entry_at": lot[2], "exit_at": o["at"],
                              "entry_price": lot[1], "exit_price": o["price"],
                              "pnl_eur": round(pnl, 2),
                              "pnl_pct": round(pnl / entry_cost * 100, 2) if entry_cost else None,
                              "held_days": held_days})
                lot[0] -= q
                remaining -= q
                if lot[0] <= 1e-12:
                    lots.pop(0)
    trips.sort(key=lambda t: t["exit_at"], reverse=True)
    return trips


def trip_stats(trips: list[dict]) -> dict | None:
    if not trips:
        return None
    wins = [t for t in trips if t["pnl_eur"] > 0]
    losses = [t for t in trips if t["pnl_eur"] <= 0]
    return {"closed_trades": len(trips),
            "win_rate_pct": round(len(wins) / len(trips) * 100, 1),
            "avg_win_eur": round(sum(t["pnl_eur"] for t in wins) / len(wins), 2) if wins else 0,
            "avg_loss_eur": round(sum(t["pnl_eur"] for t in losses) / len(losses), 2) if losses else 0,
            "total_pnl_eur": round(sum(t["pnl_eur"] for t in trips), 2)}


# ---------- reconciliation: books vs exchange (#5) ----------

def reconcile(conn, mode: str, prices: dict[str, float],
              actual: dict[str, float] | None = None) -> dict:
    """Compare sleeve books against real exchange balances and absorb drift.

    Small drift is distributed across the sleeves holding that asset,
    proportional to their share. EUR surpluses above the top-up epsilon are
    left for the top-up detector. Large drift alerts — that's a human moving
    money or a bug, not dust."""
    if mode != "live":
        return {"status": "skipped", "detail": "paper books are the truth"}
    from . import portfolio
    if actual is None:
        actual = {k: float(v or 0) for k, v in
                  (market.exchange().fetch_balance().get("total") or {}).items()}
    assets = {config.BASE_CURRENCY} | {p.split("/")[0] for p in config.PAIRS}
    adjusted, drift_value = [], 0.0
    for asset in sorted(assets):
        booked = conn.execute(
            "SELECT COALESCE(SUM(amount),0) s FROM holdings WHERE mode='live' AND asset=?",
            (asset,)).fetchone()["s"]
        act = actual.get(asset, 0.0)
        drift = act - booked
        px = 1.0 if asset == config.BASE_CURRENCY else prices.get(f"{asset}/{config.BASE_CURRENCY}", 0.0)
        if asset == config.BASE_CURRENCY and drift > portfolio.TOPUP_EPSILON_EUR:
            continue  # a deposit — the top-up detector's job, not ours
        if abs(drift * px) < 0.05:
            continue  # dust
        holders = conn.execute(
            "SELECT sleeve, amount FROM holdings WHERE mode='live' AND asset=? AND amount>0",
            (asset,)).fetchall()
        total_held = sum(h["amount"] for h in holders) or 0
        if holders and total_held > 0:
            for h in holders:
                share = drift * (h["amount"] / total_held)
                conn.execute("UPDATE holdings SET amount=amount+? WHERE mode='live' AND sleeve=? AND asset=?",
                             (share, h["sleeve"], asset))
        else:  # nobody holds it — give it to swing's books
            conn.execute(
                "INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('live','swing',?,?) "
                "ON CONFLICT(mode, sleeve, asset) DO UPDATE SET amount=amount+excluded.amount",
                (asset, drift))
        adjusted.append({"asset": asset, "drift": round(drift, 8),
                         "drift_eur": round(drift * px, 2)})
        drift_value += abs(drift * px)
    conn.commit()
    if drift_value >= RECONCILE_ALERT_EUR:
        ha.notify("Magpie reconciliation drift",
                  f"Books were {config.symbol()}{drift_value:.2f} off exchange reality and have been "
                  f"corrected: {json.dumps(adjusted)}. Manual trades or a bug?")
    return {"status": "ok", "adjusted": adjusted, "drift_eur": round(drift_value, 2)}
