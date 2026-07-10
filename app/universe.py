"""Dynamic tradeable universe: the top-N altcoins by market cap that trade
against EUR on Kraken. Base pairs are always kept; a coin that leaves the
top-N but is still held stays in the set so its position can be sold."""
import json
import logging

import httpx

from . import config, db, ha, market

LOGGER = logging.getLogger(__name__)
CG_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

# stablecoins + wrapped/staked derivatives — not distinct alts worth trading
EXCLUDE = {
    "BTC", "XBT",
    "USDT", "USDC", "DAI", "TUSD", "BUSD", "USDP", "GUSD", "USDS", "USDE", "FDUSD",
    "PYUSD", "EURT", "EURC", "EURR", "EURS", "RLUSD",
    "WBTC", "WETH", "STETH", "WSTETH", "RETH", "WBETH", "CBETH",
}


def top_alt_pairs(n: int, http: httpx.Client | None = None) -> list[str]:
    """The top-n altcoin EUR pairs by market cap that exist on Kraken spot."""
    own = http is None
    http = http or httpx.Client(timeout=20)
    try:
        r = http.get(CG_MARKETS, params={"vs_currency": "eur", "order": "market_cap_desc",
                                         "per_page": 50, "page": 1})
        r.raise_for_status()
        coins = r.json()
    finally:
        if own:
            http.close()
    try:
        markets = market.exchange().load_markets()
    except Exception as e:  # noqa: BLE001 - no markets, no dynamic set (keep base only)
        LOGGER.warning("kraken markets load failed: %s", e)
        return []
    out = []
    for c in coins:
        sym = (c.get("symbol") or "").upper()
        if not sym or sym in EXCLUDE:
            continue
        pair = f"{sym}/EUR"
        m = markets.get(pair)
        if m and m.get("active") and m.get("spot"):
            out.append(pair)
            if len(out) >= n:
                break
    return out


def _held_pairs(conn) -> set[str]:
    from . import portfolio, sleeves
    held = set()
    for s in sleeves.ALL:
        for asset in portfolio.holdings(conn, config.mode(), s):
            if asset != "EUR":
                held.add(f"{asset}/EUR")
    return held


def current(conn) -> dict:
    dyn = json.loads(db.get_setting(conn, "dynamic_pairs", "[]") or "[]")
    return {"enabled": config.DYNAMIC_UNIVERSE_ENABLED, "top_n": config.DYNAMIC_TOP_N,
            "sell_floor_n": config.DYNAMIC_SELL_FLOOR_N,
            "base": config.BASE_PAIRS, "dynamic": dyn, "effective": config.PAIRS,
            "refreshed_at": db.get_setting(conn, "dynamic_pairs_at")}


def _auto_sell(conn, pairs: list[str]) -> list[dict]:
    """Force-liquidate every sleeve's holding of each pair (a coin that fell past
    the sell floor). Reuses the normal order path and writes an audit decision so
    the diary shows why. Dust below the €1 sell floor is left in place; a rejected
    order is logged and skipped — a bad sell never aborts the refresh."""
    from . import portfolio, sleeves
    mode = config.mode()
    prices = market.tickers(pairs)
    sold = []
    for pair in pairs:
        asset = pair.split("/")[0]
        price = prices.get(pair) or 0.0
        for s in sleeves.ALL:
            amount = portfolio.holdings(conn, mode, s).get(asset, 0.0)
            if amount <= 0:
                continue
            if amount * price < 1.0:  # exchange won't move dust; leave it
                LOGGER.info("auto-sell: %s in %s worth €%.2f is dust — skipped",
                            asset, s, amount * price)
                continue
            did = conn.execute(
                "INSERT INTO decisions(at, mode, sleeve, action, pair, fraction, reasoning, status, detail) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (_now(), mode, s, "sell", pair, 1.0,
                 f"auto-exit: {pair} left the top-{config.DYNAMIC_SELL_FLOOR_N}",
                 "pending", "forced by dynamic-universe sell floor")).lastrowid
            conn.commit()
            try:
                order = portfolio.execute(conn, mode, s, did, "sell", pair, 1.0, prices)
                conn.execute("UPDATE decisions SET status='executed' WHERE id=?", (did,))
                conn.commit()
                sold.append({"sleeve": s, "pair": pair, "eur": order["cost_eur"]})
                LOGGER.info("auto-sold %s from %s (€%.2f) — left top-%d",
                            pair, s, order["cost_eur"], config.DYNAMIC_SELL_FLOOR_N)
            except Exception as e:  # noqa: BLE001 - a rejected sell must not abort the refresh
                conn.execute("UPDATE decisions SET status='error', detail=? WHERE id=?",
                             (str(e), did))
                conn.commit()
                LOGGER.error("auto-sell failed %s/%s: %s", s, pair, e)
    return sold


def refresh(conn, notify: bool = True, http: httpx.Client | None = None) -> dict:
    """Re-detect the top-N buy set, force-sell held coins past the sell floor,
    keep still-held grace-band coins, then store, apply, alert."""
    if not config.DYNAMIC_UNIVERSE_ENABLED:
        return {"status": "disabled"}
    from datetime import datetime, timezone
    floor_n = max(config.DYNAMIC_SELL_FLOOR_N, config.DYNAMIC_TOP_N)
    try:
        ranked = top_alt_pairs(floor_n, http=http)   # ordered top-floor_n alts on Kraken
    except Exception as e:  # noqa: BLE001
        LOGGER.error("universe refresh failed: %s", e)
        return {"status": "error", "detail": str(e)}
    if not ranked:
        return {"status": "error", "detail": "no tradeable alt pairs resolved"}
    buy_top = ranked[:config.DYNAMIC_TOP_N]  # the coins it may BUY
    floor_set = set(ranked)                  # ranks 1..floor_n — anything held here is kept
    base = set(config.BASE_PAIRS)
    held = _held_pairs(conn)
    # a held alt below the floor (and never a base pair) is force-sold now
    to_sell = [p for p in held if p not in floor_set and p not in base]
    sold = _auto_sell(conn, to_sell) if to_sell else []
    # grace band: coins we STILL hold that sit in ranks N+1..floor_n stay sellable
    held_after = _held_pairs(conn)
    grace = [p for p in held_after if p in floor_set and p not in buy_top and p not in base]
    dynamic = list(dict.fromkeys(buy_top + grace))
    prev = json.loads(db.get_setting(conn, "dynamic_pairs", "[]") or "[]")
    db.set_setting(conn, "dynamic_pairs", json.dumps(dynamic))
    db.set_setting(conn, "dynamic_pairs_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    config.apply_universe(conn)
    added = [p for p in dynamic if p not in prev]
    removed = [p for p in prev if p not in dynamic]
    if notify and (added or removed or sold):
        msg = f"Top-{config.DYNAMIC_TOP_N} alts refreshed — universe now {', '.join(config.PAIRS)}."
        if added:
            msg += f" Added: {', '.join(added)}."
        if removed:
            msg += f" Dropped: {', '.join(removed)}."
        if sold:
            total = sum(x["eur"] for x in sold)
            names = ", ".join(f"{x['pair']} (€{x['eur']:.2f}, {x['sleeve']})" for x in sold)
            msg += f" Auto-sold past the top-{floor_n} floor: {names} — €{total:.2f} total."
        ha.notify("Magpie universe updated", msg)
    LOGGER.info("universe refresh: dynamic=%s added=%s removed=%s sold=%s",
                dynamic, added, removed, sold)
    return {"status": "ok", "effective": config.PAIRS, "dynamic": dynamic,
            "added": added, "removed": removed, "sold": sold}
