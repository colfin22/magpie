"""Selectable base currency: symbol, pair resolution, and the one-time lock."""
import os
import tempfile

import pytest

from app import config, db, universe


def make_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(p), p


class FakeMarkets:
    def __init__(self, pairs):
        self._m = {x: {"active": True, "spot": True} for x in pairs}

    def load_markets(self):
        return self._m


def test_symbol(monkeypatch):
    for ccy, sym in [("EUR", "€"), ("USD", "$"), ("GBP", "£")]:
        monkeypatch.setattr(config, "BASE_CURRENCY", ccy)
        assert config.symbol() == sym
    monkeypatch.setattr(config, "BASE_CURRENCY", "SEK")   # unknown -> code + space
    assert config.symbol() == "SEK "


def test_resolve_pair_uses_base_currency(monkeypatch):
    monkeypatch.setattr(config, "BASE_CURRENCY", "USD")
    assert universe.resolve_pair("ada") == "ADA/USD"
    assert universe.resolve_pair("SOL/USD") == "SOL/USD"
    with pytest.raises(ValueError):
        universe.resolve_pair("ada/eur")                  # wrong quote for a USD bot


def test_autolock_on_trade_history():
    conn, p = make_db()
    try:
        assert config.currency_locked(conn) is False
        conn.execute(
            "INSERT INTO orders(at, mode, sleeve, decision_id, pair, side, amount, price, cost, fee, exchange_id) "
            "VALUES('2026-01-01T00:00:00','live','swing',1,'BTC/EUR','buy',0.001,50000,50,0.2,NULL)")
        conn.commit()
        config.autolock_currency(conn)                    # history -> lock to current currency
        assert config.currency_locked(conn) is True
        assert db.get_setting(conn, "base_currency") == "EUR"
    finally:
        conn.close(); os.unlink(p)


def test_currency_endpoints_set_and_lock(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from app import main
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "c.db"))
    monkeypatch.setattr(config, "DASHBOARD_PASSWORD", "")
    monkeypatch.setattr(config, "BASE_CURRENCY", "EUR")
    monkeypatch.setattr(universe.market, "exchange", lambda: FakeMarkets(["BTC/USD", "ETH/USD"]))
    c = TestClient(main.app)
    assert c.get("/api/currency").json()["locked"] is False
    assert c.post("/api/currency/set", json={"currency": "XYZ"}).status_code == 400   # unsupported
    r = c.post("/api/currency/set", json={"currency": "usd"})
    assert r.status_code == 200 and r.json()["currency"] == "USD" and r.json()["symbol"] == "$"
    assert config.BASE_CURRENCY == "USD"                 # applied live
    assert c.get("/api/currency").json()["locked"] is True
    assert c.post("/api/currency/set", json={"currency": "gbp"}).status_code == 400   # locked now


def test_timezone_drives_sleeve_due(monkeypatch):
    from datetime import datetime, timezone
    from app import sleeves
    # 10:00 UTC is 06:00 in New York (EDT, UTC-4) but 11:00 in Dublin (IST, UTC+1)
    t = datetime(2026, 7, 6, 10, 0, tzinfo=timezone.utc)   # a Monday
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")
    assert sleeves.due("fortnight", t) is True             # 06:00 NY -> daily slot fires
    assert sleeves.due("quarter", t) is True               # Monday 06:00 NY
    monkeypatch.setattr(config, "TIMEZONE", "Europe/Dublin")
    assert sleeves.due("fortnight", t) is False            # 11:00 Dublin -> not the slot
    assert sleeves.due("swing", t) is True                 # swing every cycle regardless


def test_timezone_validation(monkeypatch):
    assert config._cast("TIMEZONE", "America/New_York") == "America/New_York"
    assert config._cast("TIMEZONE", "") == "Europe/Dublin"
    with pytest.raises(ValueError):
        config._cast("TIMEZONE", "Mars/Olympus_Mons")


def test_tz_helper(monkeypatch):
    monkeypatch.setattr(config, "TIMEZONE", "Europe/London")
    assert str(config.tz()) == "Europe/London"
