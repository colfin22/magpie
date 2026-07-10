"""Market data + indicators. Public Kraken endpoints via ccxt — no API key
needed for anything in this module, so paper mode runs without an account."""
import logging
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
