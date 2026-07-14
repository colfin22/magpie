"""Exchange-side stop-losses (#35).

The point of a stop that lives AT KRAKEN is that it works when the bot does not:
during the six hours between cycles, through an LLM outage, a crashed container,
a Proxmox reboot, or while everyone is asleep. A stop that only exists in this
process is worth very little.

This is not a position cap and not a circuit breaker on the strategy — it is a
floor under a single position, at a distance the brain chose and the config
clamps. Off unless STOP_LOSS_ENABLED.

THE FOOTGUN, and why the code is shaped like this: the sleeves are virtual books
over ONE real Kraken account. A resting stop does not know about sleeves — it
just sells coins. So an orphaned stop (one whose position has already been sold
by its own sleeve) would happily sell coins belonging to a DIFFERENT sleeve. Every
sell therefore cancels that sleeve's stops for that pair FIRST, and cancellation
failing is treated as a reason not to sell, not as a shrug.
"""
import logging
from datetime import datetime, timezone

from . import config, market

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clamp_pct(pct: float | None) -> float:
    """The brain proposes a distance; the config decides what is sane."""
    if pct is None:
        pct = config.STOP_LOSS_PCT
    return max(config.STOP_LOSS_MIN_PCT, min(config.STOP_LOSS_MAX_PCT, float(pct)))


def open_stops(conn, mode: str, sleeve: str | None = None, pair: str | None = None) -> list[dict]:
    q = "SELECT * FROM stops WHERE mode=? AND status='open'"
    args: list = [mode]
    if sleeve:
        q += " AND sleeve=?"; args.append(sleeve)
    if pair:
        q += " AND pair=?"; args.append(pair)
    return [dict(r) for r in conn.execute(q, args)]


def place(conn, mode: str, sleeve: str, pair: str, amount: float, entry_price: float,
          pct: float | None = None) -> dict | None:
    """Rest a stop-loss sell under a position we just bought."""
    if not config.STOP_LOSS_ENABLED or amount <= 0:
        return None
    pct = clamp_pct(pct)
    stop_price = round(entry_price * (1 - pct / 100), 8)
    exchange_id = None
    if mode == "live":
        try:
            ex = market.exchange()
            amt = float(ex.amount_to_precision(pair, amount))
            # ccxt's unified shape: a MARKET sell carrying stopLossPrice. ccxt then sets
            # Kraken's ordertype=stop-loss and puts the trigger in `price` itself.
            # Passing type="stop-loss" positionally does NOT work — ccxt never fills in
            # the price field and Kraken rejects it with "Invalid arguments:price".
            o = ex.create_order(pair, "market", "sell", amt, None,
                                {"stopLossPrice": float(ex.price_to_precision(pair, stop_price))})
            exchange_id = o.get("id")
        except Exception as e:  # noqa: BLE001 - an unplaceable stop must not undo the buy
            LOGGER.warning("could not place stop for %s/%s: %s", sleeve, pair, e)
            return None
    cur = conn.execute(
        "INSERT INTO stops(mode, sleeve, pair, amount, stop_price, entry_price, pct, "
        "exchange_id, placed_at, status) VALUES(?,?,?,?,?,?,?,?,?,'open')",
        (mode, sleeve, pair, amount, stop_price, entry_price, pct, exchange_id, _now()))
    conn.commit()
    LOGGER.info("[%s/%s] stop placed: sell %.8f %s at %.2f (-%.1f%%)",
                mode, sleeve, amount, pair, stop_price, pct)
    return {"id": cur.lastrowid, "pair": pair, "amount": amount,
            "stop_price": stop_price, "pct": pct, "exchange_id": exchange_id}


def cancel(conn, stop: dict) -> bool:
    """Cancel one stop. Returns False if the exchange would not let go of it —
    the caller MUST NOT then sell, or the stop is orphaned onto another sleeve's coins."""
    if stop["mode"] == "live" and stop["exchange_id"]:
        try:
            market.exchange().cancel_order(stop["exchange_id"], stop["pair"])
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            if "unknown order" in msg or "not found" in msg or "invalid order" in msg:
                LOGGER.info("stop %s already gone at the exchange — treating as cancelled",
                            stop["exchange_id"])
            else:
                LOGGER.warning("could not cancel stop %s: %s", stop["exchange_id"], e)
                return False
    conn.execute("UPDATE stops SET status='cancelled', closed_at=? WHERE id=?",
                 (_now(), stop["id"]))
    conn.commit()
    return True


def cancel_for(conn, mode: str, sleeve: str, pair: str) -> bool:
    """Clear a sleeve's stops on a pair before it sells that pair itself.

    All-or-nothing: if any stop refuses to cancel, say so, because selling anyway
    is what orphans a stop onto another sleeve's coins."""
    ok = True
    for s in open_stops(conn, mode, sleeve, pair):
        ok = cancel(conn, s) and ok
    return ok


def _book_fill(conn, stop: dict, price: float, settled: dict | None = None) -> dict:
    """A stop fired: the coins are gone from the real account, so make the books
    say so — and leave an audit row, exactly as a forced sell does. Silent
    absorption by the nightly reconcile would erase the fact it ever happened.

    `settled` is what the EXCHANGE says actually happened. Book that, not a model of
    it (#78). This path used to compute `proceeds = amount * price` and
    `fee = proceeds * TAKER_FEE` — the very mistake #39 fixed everywhere else. The
    modelled fee does not match Kraken's real one, so every stop fill left a small
    surplus of unbooked EUR at the exchange, which the top-up detector then had to
    decide what to do with. Drift absorption must be the exception, not the accounting.
    """
    from . import portfolio   # local import: portfolio imports this module

    mode, sleeve, pair = stop["mode"], stop["sleeve"], stop["pair"]
    asset = pair.split("/")[0]
    h = portfolio.holdings(conn, mode, sleeve)
    if settled and settled.get("filled"):
        amount = settled["filled"]                     # what the exchange really sold
        proceeds = settled["cost"]                     # quote it really returned
        fee = settled["fee_quote"]                     # the fee it really charged
        price = settled.get("price") or price
    else:                                              # paper/shadow: model it
        amount = min(stop["amount"], h.get(asset, 0.0))
        proceeds = amount * price
        fee = proceeds * config.TAKER_FEE              # a triggered stop takes liquidity
    portfolio._set(conn, mode, sleeve, asset, h.get(asset, 0.0) - amount)
    portfolio._set(conn, mode, sleeve, config.BASE_CURRENCY,
                   h.get(config.BASE_CURRENCY, 0.0) + proceeds - fee)
    cur = conn.execute(
        "INSERT INTO decisions(at, mode, sleeve, action, pair, fraction, confidence, "
        "reasoning, status, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, sleeve, "sell", pair, 1.0, None,
         f"stop-loss triggered at {price:.2f} ({stop['pct']:.1f}% below entry "
         f"{stop['entry_price']:.2f})", "executed", "stop-loss"))
    conn.execute(
        "INSERT INTO orders(at, mode, sleeve, decision_id, pair, side, amount, price, cost, fee, "
        "exchange_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, sleeve, cur.lastrowid, pair, "sell", amount, price, proceeds, fee,
         stop["exchange_id"]))
    conn.execute("UPDATE stops SET status='filled', closed_at=?, fill_price=? WHERE id=?",
                 (_now(), price, stop["id"]))
    conn.commit()
    LOGGER.warning("[%s/%s] STOP-LOSS FIRED: sold %.8f %s at %.2f", mode, sleeve, amount, asset, price)
    return {"sleeve": sleeve, "pair": pair, "amount": amount, "price": price,
            "proceeds_eur": round(proceeds - fee, 2)}


def sync(conn, mode: str, prices: dict[str, float]) -> list[dict]:
    """Called at the top of a cycle: has anything fired while we were away?

    Live: ask the exchange what became of each resting stop. Paper/shadow: a stop
    fires if the price is at or below it. Either way the books are made honest
    before the brain is asked to decide anything.
    """
    fired = []
    for stop in open_stops(conn, mode):
        try:
            if mode == "live" and stop["exchange_id"]:
                o = market.exchange().fetch_order(stop["exchange_id"], stop["pair"])
                status = (o.get("status") or "").lower()
                if status == "closed" and float(o.get("filled") or 0) > 0:
                    price = float(o.get("average") or o.get("price") or stop["stop_price"])
                    # book what the exchange SETTLED, not a model of it (#78)
                    from . import portfolio
                    try:
                        settled = portfolio._settle(market.exchange(), stop["pair"],
                                                    [stop["exchange_id"]])
                    except Exception as e:  # noqa: BLE001 - fall back to the modelled fill
                        LOGGER.warning("could not read back stop fill %s (%s) — booking the "
                                       "modelled fill instead", stop["exchange_id"], e)
                        settled = None
                    fired.append(_book_fill(conn, stop, price, settled))
                elif status in ("canceled", "cancelled", "expired", "rejected"):
                    conn.execute("UPDATE stops SET status='cancelled', closed_at=? WHERE id=?",
                                 (_now(), stop["id"]))
                    conn.commit()
            else:
                price = prices.get(stop["pair"])
                if price and price <= stop["stop_price"]:
                    fired.append(_book_fill(conn, stop, stop["stop_price"]))
        except Exception as e:  # noqa: BLE001 - one bad stop must not stop the cycle
            LOGGER.warning("could not sync stop %s: %s", stop["id"], e)
    return fired


MIN_STOP_VALUE = 5.0   # not worth resting an order over dust


def reprotect(conn, mode: str, sleeve: str, pair: str, remaining: float,
              template: dict, price: float | None) -> dict | None:
    """After a PARTIAL sell, rest a stop again over what is left.

    A sell cancels the sleeve's stops on the pair (the orphan guard), but the
    brain routinely sells only part of a position — so without this the
    remainder is left with no floor at all, silently, until that sleeve next
    happens to buy the same coin. Re-rest at the ORIGINAL entry and distance, so
    the floor stays where it was rather than ratcheting to the new price.
    """
    if not config.STOP_LOSS_ENABLED or remaining <= 0 or not template:
        return None
    if price and remaining * price < MIN_STOP_VALUE:
        LOGGER.info("[%s/%s] remainder of %s is dust — no stop re-rested", mode, sleeve, pair)
        return None
    return place(conn, mode, sleeve, pair, remaining, template["entry_price"], template["pct"])


def cancel_all(conn, mode: str) -> int:
    n = 0
    for s in open_stops(conn, mode):
        if cancel(conn, s):
            n += 1
    return n
