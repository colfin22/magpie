import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app import advisor, config, db, market, portfolio, sleeves

TZ = ZoneInfo("Europe/Dublin")


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def test_sleeves_seed_once():
    conn, p = make_db()
    try:
        per = round(config.START_BALANCE_EUR / 3, 2)
        for s in sleeves.ACTIVE:
            assert portfolio.holdings(conn, "paper", s)["EUR"] == per
        assert portfolio.holdings(conn, "paper", sleeves.VAULT) == {}  # vault starts empty
        conn2 = db.connect(p)  # reconnect must not re-seed
        conn2.execute("UPDATE holdings SET amount=1 WHERE sleeve='swing' AND asset='EUR'")
        conn2.commit()
        assert db.connect(p).execute(
            "SELECT amount FROM holdings WHERE sleeve='swing' AND asset='EUR'").fetchone()[0] == 1
    finally:
        conn.close(); os.unlink(p)


def test_due_cadences():
    mon6 = datetime(2026, 7, 6, 6, 30, tzinfo=TZ)     # a Monday, 06:xx
    tue12 = datetime(2026, 7, 7, 12, 30, tzinfo=TZ)
    first6 = datetime(2026, 8, 1, 6, 30, tzinfo=TZ)   # 1st of month, 06:xx
    assert sleeves.due("swing", tue12) and sleeves.due("swing", mon6)
    assert sleeves.due("fortnight", mon6) and not sleeves.due("fortnight", tue12)
    assert sleeves.due("quarter", mon6) and not sleeves.due("quarter", tue12)
    assert sleeves.due("vault", first6) and not sleeves.due("vault", mon6)


def test_validate_rejects_bad_answers():
    for bad in ["nope", '{"action": "yolo"}',
                '{"action": "buy", "pair": "DOGE/EUR", "fraction": 0.5}',
                '{"action": "buy", "pair": "BTC/EUR", "fraction": 1.7}']:
        with pytest.raises(advisor.AdvisorError):
            advisor.validate(bad)


def test_prompt_carries_mandate():
    prompt = advisor.build_prompt({"total_eur": 16}, [], [], 10.0,
                                  mandate=sleeves.MANDATES["vault"])
    assert "VAULT" in prompt and "BTC/EUR" in prompt


def test_sleeve_buy_sell_isolated(monkeypatch):
    conn, p = make_db()
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 10.0)
    try:
        prices = {"BTC/EUR": 100_000.0}
        portfolio.execute(conn, "paper", "swing", 1, "buy", "BTC/EUR", 0.9, prices)
        assert "BTC" in portfolio.holdings(conn, "paper", "swing")
        # other sleeves untouched
        assert portfolio.holdings(conn, "paper", "fortnight").get("BTC") is None
    finally:
        conn.close(); os.unlink(p)


def test_skim_moves_profit_to_vault(monkeypatch):
    conn, p = make_db()
    try:
        prices = {}
        # simulate swing doubling its money, all realised (in EUR)
        per = round(config.START_BALANCE_EUR / 3, 2)
        conn.execute("UPDATE holdings SET amount=? WHERE sleeve='swing' AND asset='EUR'", (per * 2,))
        conn.commit()
        skims = portfolio.skim_profits(conn, "paper", prices)
        assert len(skims) == 1 and skims[0]["sleeve"] == "swing"
        expected = round(per * config.SKIM_FRACTION, 2)
        assert skims[0]["amount"] == pytest.approx(expected, abs=0.01)
        assert portfolio.holdings(conn, "paper", "vault")["EUR"] == pytest.approx(expected, abs=0.01)
        # hwm ratcheted: immediate second skim finds nothing
        assert portfolio.skim_profits(conn, "paper", prices) == []
        # losses never skim
        conn.execute("UPDATE holdings SET amount=1 WHERE sleeve='fortnight' AND asset='EUR'")
        conn.commit()
        assert portfolio.skim_profits(conn, "paper", prices) == []
    finally:
        conn.close(); os.unlink(p)


def test_topup_splits_three_ways_and_raises_hwm():
    conn, p = make_db()
    try:
        before_hwm = conn.execute(
            "SELECT hwm FROM sleeve_meta WHERE sleeve='swing'").fetchone()[0]
        r = portfolio.apply_topup(conn, "paper", 30.0)
        assert r["per_sleeve"] == 10.0
        for s in sleeves.ACTIVE:
            assert portfolio.holdings(conn, "paper", s)["EUR"] == pytest.approx(
                round(config.START_BALANCE_EUR / 3, 2) + 10.0)
        after_hwm = conn.execute(
            "SELECT hwm FROM sleeve_meta WHERE sleeve='swing'").fetchone()[0]
        assert after_hwm == pytest.approx(before_hwm + 10.0)
        # vault got nothing (profits only)
        assert portfolio.holdings(conn, "paper", "vault") == {}
        # a top-up must not register as skimmable profit
        assert portfolio.skim_profits(conn, "paper", {}) == []
    finally:
        conn.close(); os.unlink(p)


def test_indicators():
    series = [float(x) for x in range(1, 301)]
    assert market.ema(series, 20) is not None
    assert market.ema([1.0, 2.0], 200) is None
    assert market.rsi(series) == 100.0
    assert market.pct_return([100.0, 110.0], 1) == pytest.approx(10.0)
