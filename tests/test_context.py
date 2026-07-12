"""Richer decision context (#34): funding/open interest, book depth, headlines.

The load-bearing property is FAIL-SOFT. Extra context is garnish — a dead feed
must degrade the prompt, never the cycle.
"""
import os
import tempfile

import httpx
import pytest

from app import config, db, engine, market

FUT = {"tickers": [
    {"symbol": "PF_XBTUSD", "tag": "perpetual", "pair": "XBT:USD",
     "fundingRate": 0.58, "fundingRatePrediction": 1.16, "openInterest": 1986.0,
     "markPrice": 58000.0},
    {"symbol": "PF_ETHUSD", "tag": "perpetual", "pair": "ETH:USD",
     "fundingRate": -0.001, "fundingRatePrediction": 0.0, "openInterest": 20972.0,
     "markPrice": 2000.0},
    {"symbol": "PI_XBTUSD", "tag": "inverse", "pair": "XBT:USD", "markPrice": 58000.0},
]}


class FakeResp:
    def __init__(self, data=None, text=""):
        self._data, self.text = data, text

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


def test_funding_is_normalised_against_mark_price(monkeypatch):
    """Kraken quotes funding ABSOLUTE. Un-normalised it is meaningless across
    assets — 0.58 on BTC and 0.58 on a €2 alt are wildly different things."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(FUT))
    f = market.funding(["BTC/EUR", "ETH/EUR"])
    assert f["BTC/EUR"]["funding_rate_pct_per_hour"] == round(0.58 / 58000 * 100, 5)
    assert f["BTC/EUR"]["open_interest"] == 1986.0          # BTC is XBT on the futures book
    assert f["ETH/EUR"]["funding_rate_pct_per_hour"] < 0     # crowded short
    assert f["BTC/EUR"]["predicted_funding_pct_per_hour"] > 0


def test_funding_ignores_non_perp_contracts(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(FUT))
    f = market.funding(["BTC/EUR"])
    assert f["BTC/EUR"]["open_interest"] == 1986.0   # the PI_ inverse row is not mistaken for it


def test_funding_pair_with_no_perp_is_simply_absent(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(FUT))
    assert market.funding(["NOSUCH/EUR"]) is None


def test_every_context_source_fails_soft(monkeypatch):
    """A dead feed returns None. It must never raise into the cycle."""
    def dead(*a, **k):
        raise httpx.ConnectError("feed is down")

    monkeypatch.setattr(httpx, "get", dead)
    monkeypatch.setattr(config, "NEWS_RSS_URL", "http://feed.invalid/rss", raising=False)
    assert market.funding(["BTC/EUR"]) is None
    assert market.headlines() is None

    class DeadEx:
        def fetch_order_book(self, *a, **k):
            raise RuntimeError("exchange down")

    monkeypatch.setattr(market, "exchange", lambda: DeadEx())
    assert market.depth("BTC/EUR") is None


def test_depth_imbalance(monkeypatch):
    class Ex:
        def fetch_order_book(self, pair, limit=100):
            return {"bids": [[99.0, 3.0], [90.0, 5.0]],    # 90 is outside the 1% band
                    "asks": [[101.0, 1.0], [110.0, 9.0]]}  # 110 too

    monkeypatch.setattr(market, "exchange", lambda: Ex())
    d = market.depth("BTC/EUR")
    assert d["bid_size"] == 3.0 and d["ask_size"] == 1.0     # only the in-band levels count
    assert d["imbalance"] == 0.5                             # (3-1)/4 — buyers outweigh sellers


def test_news_is_off_without_a_feed_url(monkeypatch):
    monkeypatch.setattr(config, "NEWS_RSS_URL", "", raising=False)
    called = {"n": 0}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: called.__setitem__("n", 1))
    assert market.headlines() is None
    assert called["n"] == 0     # not even fetched


def test_news_parses_titles_and_drops_the_feed_title(monkeypatch):
    rss = ("<rss><channel><title>My Feed</title>"
           "<item><title>BTC rips higher</title></item>"
           "<item><title><![CDATA[ETH flips]]></title></item>"
           "</channel></rss>")
    monkeypatch.setattr(config, "NEWS_RSS_URL", "http://feed/rss", raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: FakeResp(text=rss))
    assert market.headlines() == ["BTC rips higher", "ETH flips"]


def test_toggles_off_means_no_calls_and_no_keys(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
    conn = db.connect(path)
    try:
        monkeypatch.setattr(config, "CONTEXT_FUNDING", False, raising=False)
        monkeypatch.setattr(config, "CONTEXT_DEPTH", False, raising=False)
        monkeypatch.setattr(config, "NEWS_RSS_URL", "", raising=False)
        monkeypatch.setattr(config, "PAIRS", ["BTC/EUR"], raising=False)
        monkeypatch.setattr(market, "refresh_candles", lambda *a, **k: 0)
        monkeypatch.setattr(market, "tickers", lambda p: {"BTC/EUR": 100.0})
        monkeypatch.setattr(market, "summary", lambda *a, **k: {"pair": "BTC/EUR"})
        monkeypatch.setattr(market, "fear_greed", lambda: None)
        monkeypatch.setattr(market, "touch", lambda p: {"bid": 1, "ask": 1})
        monkeypatch.setattr(market, "funding", lambda p: pytest.fail("funding called while off"))
        monkeypatch.setattr(market, "depth", lambda p: pytest.fail("depth called while off"))
        _, _, extras = engine._market_context(conn, False)
        assert "perp_funding_and_open_interest" not in extras
        assert "orderbook_depth" not in extras and "recent_headlines" not in extras
    finally:
        conn.close(); os.unlink(path)
