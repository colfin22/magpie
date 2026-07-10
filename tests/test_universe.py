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


def test_refresh_keeps_grace_band_held_coin(monkeypatch):
    """A held coin below the buy top-N but still within the sell floor stays
    tradeable (the advisor's discretion), and is NOT force-sold."""
    conn, p = make_db()
    monkeypatch.setattr(config, "DYNAMIC_UNIVERSE_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_TOP_N", 2)
    monkeypatch.setattr(config, "DYNAMIC_SELL_FLOOR_N", 5)
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR"])
    monkeypatch.setattr(universe.ha, "notify", lambda t, m: True)
    # ADA sits at rank 5 — out of the top-2 buy set but inside the top-5 floor
    cg = FakeCG(["ETH", "SOL", "XRP", "DOGE", "ADA"])
    monkeypatch.setattr(universe.market, "exchange",
                        lambda: FakeMarkets(["ETH/EUR", "SOL/EUR", "XRP/EUR", "DOGE/EUR", "ADA/EUR"]))
    conn.execute("UPDATE holdings SET amount=1 WHERE mode='paper' AND sleeve='swing' AND asset='EUR'")
    conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper','swing','ADA',5)")
    conn.commit()
    r = universe.refresh(conn, http=cg)
    assert r["status"] == "ok"
    assert r["sold"] == []                                        # inside the floor -> not sold
    assert r["dynamic"] == ["ETH/EUR", "SOL/EUR", "ADA/EUR"]      # top-2 + held grace-band ADA
    assert config.PAIRS == ["BTC/EUR", "ETH/EUR", "SOL/EUR", "ADA/EUR"]
    config.apply_universe(db.connect(p))
    assert "ADA/EUR" in config.PAIRS
    conn.close(); os.unlink(p)


def test_refresh_force_sells_below_floor(monkeypatch):
    """A held coin that drops past the sell floor is force-liquidated at the
    refresh: order booked, holding cleared, coin dropped from the universe."""
    conn, p = make_db()
    monkeypatch.setattr(config, "DYNAMIC_UNIVERSE_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_TOP_N", 2)
    monkeypatch.setattr(config, "DYNAMIC_SELL_FLOOR_N", 3)
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR"])
    monkeypatch.setattr(universe.ha, "notify", lambda t, m: True)
    # ADA is not in the market-cap list at all -> below the floor
    cg = FakeCG(["ETH", "SOL", "XRP"])
    monkeypatch.setattr(universe.market, "exchange",
                        lambda: FakeMarkets(["ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"]))
    # paper fills read touch/tickers — worth €5, well above the €1 dust floor
    monkeypatch.setattr(universe.market, "tickers", lambda pairs: {q: 1.0 for q in pairs})
    monkeypatch.setattr(universe.market, "touch", lambda pair: {"bid": 1.0, "ask": 1.0})
    conn.execute("UPDATE holdings SET amount=1 WHERE mode='paper' AND sleeve='swing' AND asset='EUR'")
    conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper','swing','ADA',5)")
    conn.commit()
    r = universe.refresh(conn, http=cg)
    assert r["status"] == "ok"
    assert [x["pair"] for x in r["sold"]] == ["ADA/EUR"]          # forced exit happened
    assert "ADA/EUR" not in r["dynamic"]                          # gone from the universe
    from app import portfolio
    assert "ADA" not in portfolio.holdings(conn, "paper", "swing")  # position cleared
    assert conn.execute("SELECT COUNT(*) FROM orders WHERE pair='ADA/EUR' AND side='sell'").fetchone()[0] == 1
    conn.close(); os.unlink(p)


def test_refresh_leaves_sub_floor_dust(monkeypatch):
    """A sub-€1 position past the floor is left alone — the exchange won't move dust."""
    conn, p = make_db()
    monkeypatch.setattr(config, "DYNAMIC_UNIVERSE_ENABLED", True)
    monkeypatch.setattr(config, "DYNAMIC_TOP_N", 2)
    monkeypatch.setattr(config, "DYNAMIC_SELL_FLOOR_N", 3)
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR"])
    monkeypatch.setattr(universe.ha, "notify", lambda t, m: True)
    cg = FakeCG(["ETH", "SOL", "XRP"])
    monkeypatch.setattr(universe.market, "exchange",
                        lambda: FakeMarkets(["ETH/EUR", "SOL/EUR", "XRP/EUR", "ADA/EUR"]))
    monkeypatch.setattr(universe.market, "tickers", lambda pairs: {q: 1.0 for q in pairs})
    monkeypatch.setattr(universe.market, "touch", lambda pair: {"bid": 1.0, "ask": 1.0})
    conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper','swing','ADA',0.5)")
    conn.commit()
    r = universe.refresh(conn, http=cg)
    assert r["sold"] == []                                        # €0.50 dust left in place
    from app import portfolio
    assert portfolio.holdings(conn, "paper", "swing").get("ADA") == 0.5
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
