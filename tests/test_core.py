import os
import tempfile

import pytest

from app import advisor, config, db, market, portfolio


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def test_paper_account_seeds_once():
    conn, p = make_db()
    try:
        assert portfolio.holdings(conn, "paper")["EUR"] == config.START_BALANCE_EUR
        conn2 = db.connect(p)  # reconnect must not top the account back up
        conn2.execute("UPDATE holdings SET amount=12 WHERE mode='paper' AND asset='EUR'")
        conn2.commit()
        assert db.connect(p).execute(
            "SELECT amount FROM holdings WHERE mode='paper' AND asset='EUR'").fetchone()[0] == 12
    finally:
        conn.close(); os.unlink(p)


def test_indicators():
    series = list(range(1, 301))  # steadily rising
    assert market.ema(series, 20) is not None
    assert market.ema([1, 2], 200) is None
    assert market.rsi([float(x) for x in series]) == 100.0
    assert market.pct_return([100.0, 110.0], 1) == pytest.approx(10.0)


def test_validate_rejects_bad_answers():
    for bad in [
        "not json at all",
        '{"action": "yolo"}',
        '{"action": "buy", "pair": "DOGE/EUR", "fraction": 0.5}',   # outside universe
        '{"action": "buy", "pair": "BTC/EUR", "fraction": 1.7}',    # fraction > 1
        '{"action": "buy", "pair": "BTC/EUR", "fraction": null}',
    ]:
        with pytest.raises(advisor.AdvisorError):
            advisor.validate(bad)


def test_validate_accepts_good_answers():
    d = advisor.validate('{"action": "buy", "pair": "BTC/EUR", "fraction": 0.5, '
                         '"confidence": 0.7, "reasoning": "trend up"}')
    assert d["action"] == "buy" and d["pair"] == "BTC/EUR" and d["fraction"] == 0.5
    h = advisor.validate('{"action": "hold", "confidence": 0.9, "reasoning": "chop"}')
    assert h["action"] == "hold" and h["pair"] is None


def test_paper_buy_sell_roundtrip(monkeypatch):
    conn, p = make_db()
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 10.0)
    try:
        prices = {"BTC/EUR": 100_000.0}
        order = portfolio.execute(conn, "paper", 1, "buy", "BTC/EUR", 0.8, prices)
        assert order["cost_eur"] == pytest.approx(40.0)
        h = portfolio.holdings(conn, "paper")
        assert h["EUR"] == pytest.approx(10.0)
        assert h["BTC"] > 0
        # under-minimum buy refused
        with pytest.raises(ValueError):
            portfolio.execute(conn, "paper", 2, "buy", "BTC/EUR", 0.5, prices)
        portfolio.execute(conn, "paper", 3, "sell", "BTC/EUR", 1.0, prices)
        h = portfolio.holdings(conn, "paper")
        assert "BTC" not in h
        # bought at fee, sold at fee: should be a little under €50
        assert 49.0 < h["EUR"] < 50.0
    finally:
        conn.close(); os.unlink(p)


def test_prompt_mentions_the_rules():
    prompt = advisor.build_prompt({"total_eur": 50}, [], [], 10.0)
    assert "BTC/EUR" in prompt and "HOLD" in prompt and "taker fee" in prompt
