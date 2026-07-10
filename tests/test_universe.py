import json
import os
import tempfile

import pytest

from app import config, db, universe


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


class FakeCG:
    """Stand-in CoinGecko markets response, market-cap ordered."""
    def __init__(self, symbols):
        self.symbols = symbols

    def get(self, url, params):
        data = [{"symbol": s.lower(), "market_cap_rank": i + 1} for i, s in enumerate(self.symbols)]

        class R:
            def raise_for_status(self_):
                pass

            def json(self_, _d=data):
                return _d
        return R()


class FakeMarkets:
    def __init__(self, pairs):
        self._m = {p: {"active": True, "spot": True} for p in pairs}

    def load_markets(self):
        return self._m


def test_top_alt_pairs_filters_and_ranks(monkeypatch):
    # market-cap order: BTC (excluded), ETH, USDT (stable, excluded), SOL, XRP, WBTC (excluded), ADA, DOGE
    cg = FakeCG(["BTC", "ETH", "USDT", "SOL", "XRP", "WBTC", "ADA", "DOGE"])
    # Kraken has all except XRP (so XRP is skipped as untradeable)
    monkeypatch.setattr(universe.market, "exchange",
                        lambda: FakeMarkets(["ETH/EUR", "SOL/EUR", "ADA/EUR", "DOGE/EUR"]))
    got = universe.top_alt_pairs(3, http=cg)
    assert got == ["ETH/EUR", "SOL/EUR", "ADA/EUR"]  # BTC/USDT/WBTC excluded, XRP not on Kraken


def test_refresh_disabled_is_noop():
    conn, p = make_db()
    monkey = config.DYNAMIC_UNIVERSE_ENABLED
    config.DYNAMIC_UNIVERSE_ENABLED = False
    try:
        assert universe.refresh(conn)["status"] == "disabled"
    finally:
        config.DYNAMIC_UNIVERSE_ENABLED = monkey
        conn.close(); os.unlink(p)


def test_refresh_keeps_held_coins_and_applies(monkeypatch):
    conn, p = make_db()
    monkeypatch.setattr(config, "DYNAMIC_UNIVERSE_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_TOP_N", 2)
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR"])
    monkeypatch.setattr(universe.ha, "notify", lambda t, m: True)
    cg = FakeCG(["ETH", "SOL", "XRP"])
    monkeypatch.setattr(universe.market, "exchange",
                        lambda: FakeMarkets(["ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"]))
    # we still hold ADA (not in the new top-2) — it must stay tradeable to be sold
    from app import portfolio
    conn.execute("UPDATE holdings SET amount=1 WHERE mode='paper' AND sleeve='swing' AND asset='EUR'")
    conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper','swing','ADA',5)")
    conn.commit()
    r = universe.refresh(conn, http=cg)
    assert r["status"] == "ok"
    assert r["dynamic"] == ["ETH/EUR", "SOL/EUR", "ADA/EUR"]      # top-2 + held ADA
    assert config.PAIRS == ["BTC/EUR", "ETH/EUR", "SOL/EUR", "ADA/EUR"]  # base + dynamic, deduped
    # stored + re-applied on a fresh connect
    config.apply_universe(db.connect(p))
    assert "ADA/EUR" in config.PAIRS
    del portfolio  # noqa
    conn.close(); os.unlink(p)


def test_apply_universe_base_only_when_disabled(monkeypatch):
    conn, p = make_db()
    monkeypatch.setattr(config, "DYNAMIC_UNIVERSE_ENABLED", False)
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR", "ETH/EUR"])
    db.set_setting(conn, "dynamic_pairs", json.dumps(["SOL/EUR"]))
    try:
        config.apply_universe(conn)
        assert config.PAIRS == ["BTC/EUR", "ETH/EUR"]  # dynamic ignored while disabled
    finally:
        conn.close(); os.unlink(p)
