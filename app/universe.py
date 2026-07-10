"""Dynamic tradeable universe: the top-N altcoins by market cap that trade
against EUR on Kraken. Base pairs are always kept; a coin that leaves the
top-N but is still held stays in the set so its position can be sold."""
import json
import logging

import httpx

from . import config, db, ha, market

LOGGER = logging.getLogger(__name__)
CG_MARKETS = "https://api.coingecko.com/api/v3/coins/markets"

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
            "base": config.BASE_PAIRS, "dynamic": dyn, "effective": config.PAIRS,
            "refreshed_at": db.get_setting(conn, "dynamic_pairs_at")}


def refresh(conn, notify: bool = True, http: httpx.Client | None = None) -> dict:
    """Re-detect the top-N alts, keep any still-held coins, store, apply, alert."""
    if not config.DYNAMIC_UNIVERSE_ENABLED:
        return {"status": "disabled"}
    from datetime import datetime, timezone
    try:
        top = top_alt_pairs(config.DYNAMIC_TOP_N, http=http)
    except Exception as e:  # noqa: BLE001
        LOGGER.error("universe refresh failed: %s", e)
        return {"status": "error", "detail": str(e)}
    if not top:
        return {"status": "error", "detail": "no tradeable alt pairs resolved"}
    held = _held_pairs(conn)
    # a held coin that fell out of the top-N stays sellable until it's closed
    dynamic = list(dict.fromkeys(top + [p for p in held if p not in top]))
    prev = json.loads(db.get_setting(conn, "dynamic_pairs", "[]") or "[]")
    db.set_setting(conn, "dynamic_pairs", json.dumps(dynamic))
    db.set_setting(conn, "dynamic_pairs_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    config.apply_universe(conn)
    added = [p for p in dynamic if p not in prev]
    removed = [p for p in prev if p not in dynamic]
    if notify and (added or removed):
        msg = f"Top-{config.DYNAMIC_TOP_N} alts refreshed — universe now {', '.join(config.PAIRS)}."
        if added:
            msg += f" Added: {', '.join(added)}."
        if removed:
            msg += f" Dropped: {', '.join(removed)}."
        ha.notify("Magpie universe updated", msg)
    LOGGER.info("universe refresh: dynamic=%s added=%s removed=%s", dynamic, added, removed)
    return {"status": "ok", "effective": config.PAIRS, "dynamic": dynamic,
            "added": added, "removed": removed}
