"""Market data + indicators. Public Kraken endpoints via ccxt — no API key
needed for anything in this module, so paper mode runs without an account."""
import logging
import re
from datetime import datetime, timezone

import ccxt
import httpx

from . import config, db

LOGGER = logging.getLogger(__name__)
_exchange = None


def exchange() -> ccxt.kraken:
    global _exchange
    if _exchange is None:
        creds = {}
        if config.KRAKEN_API_KEY and config.KRAKEN_API_SECRET:
            creds = {"apiKey": config.KRAKEN_API_KEY, "secret": config.KRAKEN_API_SECRET}
        _exchange = ccxt.kraken(creds | {"enableRateLimit": True})
    return _exchange


def _key(pair: str, timeframe: str) -> str:
    """Candle storage key: daily under the bare pair (back-compat), others suffixed."""
    return pair if timeframe == "1d" else f"{pair}@{timeframe}"


def refresh_candles(conn, pair: str, timeframe: str = "1d", limit: int = 400) -> int:
    rows = exchange().fetch_ohlcv(pair, timeframe=timeframe, limit=limit)
    key = _key(pair, timeframe)
    conn.executemany(
        "INSERT INTO candles(pair, ts, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(pair, ts) DO UPDATE SET open=excluded.open, high=excluded.high, "
        "low=excluded.low, close=excluded.close, volume=excluded.volume",
        [(key, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])
    conn.commit()
    return len(rows)


def closes(conn, pair: str, n: int = 400, timeframe: str = "1d") -> list[float]:
    rows = conn.execute("SELECT close FROM candles WHERE pair=? ORDER BY ts DESC LIMIT ?",
                        (_key(pair, timeframe), n)).fetchall()
    return [r["close"] for r in reversed(rows)]


def tickers(pairs: list[str]) -> dict[str, float]:
    out = {}
    for p in pairs:
        out[p] = float(exchange().fetch_ticker(p)["last"])
    return out


def touch(pair: str) -> dict:
    """Best bid/ask + spread — where a maker order would sit."""
    t = exchange().fetch_ticker(pair)
    bid, ask = float(t["bid"]), float(t["ask"])
    return {"bid": bid, "ask": ask, "last": float(t["last"]),
            "spread_pct": round((ask - bid) / ask * 100, 4) if ask else None}


def fear_greed() -> dict | None:
    """Crypto Fear & Greed index (alternative.me, free). None on any failure."""
    try:
        r = httpx.get("https://api.alternative.me/fng/?limit=2", timeout=10)
        r.raise_for_status()
        rows = r.json()["data"]
        return {"today": {"value": int(rows[0]["value"]),
                          "label": rows[0]["value_classification"]},
                "yesterday": {"value": int(rows[1]["value"]),
                              "label": rows[1]["value_classification"]}}
    except Exception as e:  # noqa: BLE001 - strictly optional garnish
        LOGGER.info("fear/greed unavailable: %s", e)
        return None


# ---------- richer context (#34): what the derivatives market thinks ----------
# All of this is READ-ONLY sentiment. The bot stays spot-only and long-only; we
# look at the perpetuals market without ever trading it.

FUTURES_TICKERS = "https://futures.kraken.com/derivatives/api/v3/tickers"
SPOT_TO_FUTURES_BASE = {"BTC": "XBT"}   # Kraken calls bitcoin XBT on the futures book


def _fut_base(pair: str) -> str:
    b = pair.split("/")[0]
    return SPOT_TO_FUTURES_BASE.get(b, b)


def funding(pairs: list[str]) -> dict | None:
    """Perp funding rate + open interest per pair. None on any failure.

    Funding is the price longs pay shorts to hold their position: persistently
    positive means the crowd is leveraged long, which is a classic warning the
    bot could not see before. Kraken quotes funding as an ABSOLUTE rate, so it
    is normalised against the mark price here — otherwise the number is
    meaningless across assets of different prices.
    """
    try:
        r = httpx.get(FUTURES_TICKERS, timeout=10)
        r.raise_for_status()
        by_base = {}
        for t in r.json().get("tickers", []):
            if t.get("tag") != "perpetual" or not t.get("symbol", "").startswith("PF_"):
                continue
            base = (t.get("pair") or "").split(":")[0]
            if base:
                by_base[base] = t
        out = {}
        for pair in pairs:
            t = by_base.get(_fut_base(pair))
            mark = float(t.get("markPrice") or 0) if t else 0
            if not t or not mark:
                continue
            out[pair] = {
                "funding_rate_pct_per_hour": round(float(t.get("fundingRate") or 0) / mark * 100, 5),
                "predicted_funding_pct_per_hour":
                    round(float(t.get("fundingRatePrediction") or 0) / mark * 100, 5),
                "open_interest": float(t.get("openInterest") or 0),
                "note": "positive funding = leveraged longs are paying to stay in "
                        "(crowded long); negative = crowded short",
            }
        return out or None
    except Exception as e:  # noqa: BLE001 - strictly optional garnish, never a failed cycle
        LOGGER.info("funding/open-interest unavailable: %s", e)
        return None


def depth(pair: str, band_pct: float = 1.0) -> dict | None:
    """Resting bid vs ask size within a band of mid — who is actually there.

    A short-horizon signal, useful mainly to the swing sleeve."""
    try:
        ob = exchange().fetch_order_book(pair, limit=100)
        bids, asks = ob.get("bids") or [], ob.get("asks") or []
        if not bids or not asks:
            return None
        mid = (bids[0][0] + asks[0][0]) / 2
        lo, hi = mid * (1 - band_pct / 100), mid * (1 + band_pct / 100)
        bid_sz = sum(a for p, a, *_ in bids if p >= lo)
        ask_sz = sum(a for p, a, *_ in asks if p <= hi)
        total = bid_sz + ask_sz
        if not total:
            return None
        return {"band_pct": band_pct,
                "bid_size": round(bid_sz, 6), "ask_size": round(ask_sz, 6),
                "imbalance": round((bid_sz - ask_sz) / total, 3),
                "note": "imbalance > 0 = more resting buyers than sellers within the band"}
    except Exception as e:  # noqa: BLE001
        LOGGER.info("depth unavailable for %s: %s", pair, e)
        return None


_TITLE = re.compile(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S | re.I)


def headlines(limit: int = 10) -> list[str] | None:
    """Recent headlines from NEWS_RSS_URL (any RSS feed). Off unless a URL is set.

    Deliberately opt-in: crypto headlines are mostly noise and shilling, and an
    LLM is suggestible — this is the one context source that can plausibly make
    decisions WORSE, so it is something to A/B with a shadow arm, not to switch
    on and assume is an improvement."""
    url = getattr(config, "NEWS_RSS_URL", "") or ""
    if not url:
        return None
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True)
        r.raise_for_status()
        titles = [t.strip() for t in _TITLE.findall(r.text)]
        return titles[1:limit + 1] or None    # [0] is the feed's own title
    except Exception as e:  # noqa: BLE001
        LOGGER.info("news feed unavailable: %s", e)
        return None


def ema(series: list[float], n: int) -> float | None:
    if len(series) < n:
        return None
    k = 2 / (n + 1)
    e = sum(series[:n]) / n
    for x in series[n:]:
        e = x * k + e * (1 - k)
    return e


def rsi(series: list[float], n: int = 14) -> float | None:
    if len(series) < n + 1:
        return None
    gains = losses = 0.0
    for a, b in zip(series[-n - 1:-1], series[-n:]):
        d = b - a
        gains += max(d, 0)
        losses += max(-d, 0)
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def pct_return(series: list[float], days: int) -> float | None:
    if len(series) <= days:
        return None
    return (series[-1] / series[-1 - days] - 1) * 100


def summary(conn, pair: str, timeframe: str = "1d") -> dict:
    s = closes(conn, pair, timeframe=timeframe)
    if not s:
        return {"pair": pair, "timeframe": timeframe, "error": "no candle data"}
    return {
        "pair": pair,
        "timeframe": timeframe,
        "price": s[-1],
        "ema20": ema(s, 20), "ema50": ema(s, 50), "ema200": ema(s, 200),
        "rsi14": rsi(s),
        "return_1_candle_pct": pct_return(s, 1),
        "return_7_candles_pct": pct_return(s, 7),
        "return_30_candles_pct": pct_return(s, 30),
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
