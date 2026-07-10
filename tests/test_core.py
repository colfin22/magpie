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


def test_consecutive_failure_counter(monkeypatch):
    from app import engine, ha
    pushes = []
    monkeypatch.setattr(ha, "notify", lambda t, m: pushes.append(t) or True)
    conn, p = make_db()
    try:
        bad = [{"status": "error"}]
        for _ in range(config.ERROR_ALERT_AFTER - 1):
            engine._track_cycle_outcome(conn, bad, crashed=False)
        assert pushes == []  # not yet
        engine._track_cycle_outcome(conn, bad, crashed=False)
        assert len(pushes) == 1  # fires exactly at the threshold
        engine._track_cycle_outcome(conn, bad, crashed=False)
        assert len(pushes) == 1  # ...and only once
        engine._track_cycle_outcome(conn, [{"status": "held"}], crashed=False)
        assert db.get_setting(conn, "consecutive_failures") == "0"  # success resets
        engine._track_cycle_outcome(conn, [], crashed=True)
        assert db.get_setting(conn, "consecutive_failures") == "1"  # crashes count
    finally:
        conn.close(); os.unlink(p)


def test_benchmark_init_add_value(monkeypatch):
    from app import ledger, market
    conn, p = make_db()
    prices = {"BTC/EUR": 50_000.0, "ETH/EUR": 2_500.0}
    monkeypatch.setattr(market, "tickers", lambda pairs: prices)
    try:
        ledger.bench_init_if_needed(conn, "paper", 50.0, prices)
        v = ledger.bench_value(conn, "paper", prices)
        assert v["hodl_eur"] == pytest.approx(50.0)
        ledger.bench_init_if_needed(conn, "paper", 999.0, prices)  # no re-init
        assert ledger.bench_value(conn, "paper", prices)["invested"] == 50.0
        ledger.bench_add(conn, "paper", 30.0, prices)
        assert ledger.bench_value(conn, "paper", prices)["hodl_eur"] == pytest.approx(80.0)
        # hodl value moves with the market
        double = {"BTC/EUR": 100_000.0, "ETH/EUR": 5_000.0}
        assert ledger.bench_value(conn, "paper", double)["hodl_eur"] == pytest.approx(160.0)
    finally:
        conn.close(); os.unlink(p)


def test_round_trips_fifo():
    from app import ledger
    conn, p = make_db()
    try:
        rows = [
            ("2026-07-01T00:00:00+00:00", "buy", 0.001, 50_000.0, 0.125),
            ("2026-07-02T00:00:00+00:00", "buy", 0.001, 60_000.0, 0.15),
            ("2026-07-05T00:00:00+00:00", "sell", 0.0015, 70_000.0, 0.26),
        ]
        for at, side, amount, price, fee in rows:
            conn.execute("INSERT INTO orders(at, mode, sleeve, pair, side, amount, price, cost, fee) "
                         "VALUES(?,?,?,?,?,?,?,?,?)",
                         (at, "paper", "swing", "BTC/EUR", side, amount, price, amount * price, fee))
        conn.commit()
        trips = ledger.round_trips(conn, "paper")
        assert len(trips) == 2  # the sell closed lot 1 fully, lot 2 half
        full, half = sorted(trips, key=lambda t: t["entry_at"])
        assert full["entry_price"] == 50_000.0 and full["held_days"] == 4.0
        assert full["pnl_eur"] > 0
        assert half["entry_price"] == 60_000.0
        stats = ledger.trip_stats(trips)
        assert stats["closed_trades"] == 2 and stats["win_rate_pct"] == 100.0
    finally:
        conn.close(); os.unlink(p)


def test_reconcile_distributes_drift(monkeypatch):
    from app import ledger, ha
    monkeypatch.setattr(ha, "notify", lambda t, m: True)
    conn, p = make_db()
    try:
        prices = {"BTC/EUR": 50_000.0, "ETH/EUR": 2_500.0}
        # live books: swing holds 0.002 BTC, fortnight 0.001; exchange says 0.0031 total
        for sleeve, amt in (("swing", 0.002), ("fortnight", 0.001)):
            conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('live',?,?,?)",
                         (sleeve, "BTC", amt))
        conn.commit()
        actual = {"BTC": 0.0031, "EUR": 0.0, "ETH": 0.0}
        r = ledger.reconcile(conn, "live", prices, actual=actual)
        assert r["status"] == "ok"
        swing = conn.execute("SELECT amount FROM holdings WHERE mode='live' AND sleeve='swing' "
                             "AND asset='BTC'").fetchone()[0]
        fort = conn.execute("SELECT amount FROM holdings WHERE mode='live' AND sleeve='fortnight' "
                            "AND asset='BTC'").fetchone()[0]
        assert swing + fort == pytest.approx(0.0031)
        assert swing == pytest.approx(0.002 + 0.0001 * (2 / 3))  # proportional
        # EUR surplus above the top-up epsilon is NOT absorbed (top-up detector's job)
        r2 = ledger.reconcile(conn, "live", prices, actual={"BTC": 0.0031, "EUR": 25.0})
        assert all(a["asset"] != "EUR" for a in r2["adjusted"])
        # paper mode: books are truth
        assert ledger.reconcile(conn, "paper", prices)["status"] == "skipped"
    finally:
        conn.close(); os.unlink(p)


def test_settings_overrides_apply_and_mask(monkeypatch):
    from app import config
    monkeypatch.setattr(config, "GEMINI_API_KEY", "envsecret99", raising=False)
    monkeypatch.setattr(config, "SKIM_FRACTION", 0.5, raising=False)
    monkeypatch.setattr(config, "PAIRS", ["BTC/EUR", "ETH/EUR"], raising=False)
    conn, p = make_db()
    try:
        # a saved override lands on the module and casts correctly
        db.set_setting(conn, "cfg_SKIM_FRACTION", "0.3")
        db.set_setting(conn, "cfg_PAIRS", "BTC/EUR, SOL/EUR")
        db.set_setting(conn, "cfg_KRAKEN_API_KEY", "storedkeyWXYZ")
        config.apply_overrides(conn)
        assert config.SKIM_FRACTION == 0.3
        assert config.PAIRS == ["BTC/EUR", "SOL/EUR"]
        assert config.KRAKEN_API_KEY == "storedkeyWXYZ"
        # a bad stored value must not crash the load
        db.set_setting(conn, "cfg_SKIM_FRACTION", "junk")
        config.apply_overrides(conn)  # should swallow the cast error
    finally:
        conn.close(); os.unlink(p)
