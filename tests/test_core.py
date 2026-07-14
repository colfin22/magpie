import os
import tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app import advisor, config, db, engine, market, portfolio, sleeves

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


# --- short retry of failed sleeves, systemd-timer driven (#30 follow-up) ----

def test_note_retry_state_marks_pending_on_error():
    conn, p = make_db()
    try:
        engine._note_retry_state(conn, [{"sleeve": "swing", "status": "error"}], fresh_cycle=True)
        assert db.get_setting(conn, "retry_attempts") == "0"   # fresh budget
        assert db.get_setting(conn, "retry_cycle_at")          # an indicative time is recorded
    finally:
        conn.close(); os.unlink(p)


def test_note_retry_state_clears_for_held_or_no_key():
    conn, p = make_db()
    try:
        engine._note_retry_state(conn, [
            {"sleeve": "swing", "status": "held"},
            {"sleeve": "fortnight", "status": "no_key"}], fresh_cycle=True)
        assert not db.get_setting(conn, "retry_cycle_at")      # nothing to retry
    finally:
        conn.close(); os.unlink(p)


def test_note_retry_state_no_pending_past_budget():
    conn, p = make_db()
    try:
        db.set_setting(conn, "retry_attempts", str(engine.CYCLE_RETRY_MAX))
        engine._note_retry_state(conn, [{"sleeve": "swing", "status": "error"}], fresh_cycle=False)
        assert not db.get_setting(conn, "retry_cycle_at")      # budget spent
    finally:
        conn.close(); os.unlink(p)


def test_retry_sleeves_bypasses_due(monkeypatch):
    conn, p = make_db()
    monkeypatch.setattr(engine, "_market_context", lambda c, s4h: ({}, [], {}))
    ran = []
    monkeypatch.setattr(engine, "run_sleeve",
                        lambda c, m, s, pr, md, ex: (ran.append(s), {"sleeve": s, "status": "held"})[1])
    try:
        out = engine.retry_sleeves(conn, "paper", ["swing", "bogus"])
        assert ran == ["swing"]                     # unknown sleeve filtered out
        assert out == [{"sleeve": "swing", "status": "held"}]
    finally:
        conn.close(); os.unlink(p)


def test_latest_failed_sleeves():
    conn, p = make_db()
    try:
        engine._record(conn, "paper", "swing", "held", "old")
        engine._record(conn, "paper", "swing", "error", "boom")   # swing's latest = error
        engine._record(conn, "paper", "fortnight", "held", "fine")
        assert engine._latest_failed_sleeves(conn, "paper") == ["swing"]
    finally:
        conn.close(); os.unlink(p)


def _retry_now_setup(monkeypatch, path, failed):
    orig = db.connect
    monkeypatch.setattr(db, "connect", lambda *a, **k: orig(path))  # fresh conn per call
    monkeypatch.setattr(config, "mode", lambda: "paper")
    monkeypatch.setattr(engine, "_latest_failed_sleeves", lambda c, m: list(failed))
    monkeypatch.setattr(engine, "retry_sleeves",
                        lambda c, m, names: [{"sleeve": n, "status": "error"} for n in names])
    monkeypatch.setattr(engine, "_track_cycle_outcome", lambda *a, **k: None)


def test_retry_now_reruns_failed_and_counts(monkeypatch):
    conn, p = make_db()
    _retry_now_setup(monkeypatch, p, ["swing"])
    try:
        out = engine.retry_now()
        assert out["status"] == "ok" and out["retried"] == ["swing"] and out["attempt"] == 1
        assert db.get_setting(conn, "retry_attempts") == "1"
    finally:
        conn.close(); os.unlink(p)


def test_retry_now_nothing_to_retry_resets(monkeypatch):
    conn, p = make_db()
    _retry_now_setup(monkeypatch, p, [])
    db.set_setting(conn, "retry_attempts", "2")
    try:
        assert engine.retry_now()["status"] == "nothing-to-retry"
        assert db.get_setting(conn, "retry_attempts") == "0"    # budget reset
    finally:
        conn.close(); os.unlink(p)


def test_retry_now_stops_at_budget_but_force_overrides(monkeypatch):
    conn, p = make_db()
    _retry_now_setup(monkeypatch, p, ["swing"])
    db.set_setting(conn, "retry_attempts", str(engine.CYCLE_RETRY_MAX))
    try:
        assert engine.retry_now()["status"] == "exhausted"      # cap reached
        assert engine.retry_now(force=True)["status"] == "ok"   # manual override runs anyway
    finally:
        conn.close(); os.unlink(p)


def test_unsellable_dust_is_counted_but_is_not_a_position(monkeypatch):
    """The €0.26 of BTC that reconcile found on the real account is below every
    exchange minimum. Left among the holdings the brain sees 'you hold BTC',
    proposes selling it, and the order can only ever be rejected."""
    conn, p = make_db()
    try:
        conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper','swing','BTC',?)",
                     (0.0000045,))       # ~€0.26 at 58k
        conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES('paper','swing','ETH',?)",
                     (0.01,))            # ~€30 — a real position
        conn.commit()
        v = portfolio.valued(conn, "paper", "swing", {"BTC/EUR": 58_000.0, "ETH/EUR": 3_000.0})
        assert "BTC" not in v["holdings"]              # not offered to the brain as sellable
        assert v["holdings"]["dust"]["BTC"] == 0.26    # ...but declared, not hidden
        assert "ETH" in v["holdings"]                  # a real position is untouched
        assert v["total_eur"] == round(16.67 + 0.26 + 30.0, 2)   # and still counted in the total
    finally:
        conn.close(); os.unlink(p)


def test_live_books_what_the_exchange_settled_not_what_we_modelled(monkeypatch):
    """The books used to MODEL the fill: assume the touch price, and assume the fee
    comes out of the cash before buying. Kraken does neither — you receive the full
    amount you bought, the fee is charged on top, and the price is whatever you got.
    Every trade therefore disagreed with reality (#39)."""
    conn, p = make_db()
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 1.0)
    monkeypatch.setattr(market, "touch", lambda pair: {"bid": 0.29, "ask": 0.29, "last": 0.29})
    # the exchange says: 54.6912871 TRX landed, at 0.29, and it charged €0.0635 on top
    monkeypatch.setattr(portfolio, "_live_fill", lambda pair, side, amount, px, *a, **k: {
        "id": "O2KK2U", "filled": 54.6912871, "cost": 15.8604, "price": 0.29,
        "fee_quote": 0.0635, "fee_base": 0.0})
    try:
        conn.execute("UPDATE holdings SET amount=31.80 WHERE mode='live' AND sleeve='quarter' AND asset='EUR'")
        conn.commit()
        out = portfolio.execute(conn, "live", "quarter", 1, "buy", "TRX/EUR", 0.5,
                                {"TRX/EUR": 0.29})
        h = portfolio.holdings(conn, "live", "quarter")
        assert h["TRX"] == 54.6912871                      # exactly what Kraken credited
        assert round(h["EUR"], 4) == round(31.80 - (15.8604 + 0.0635), 4)   # cost + fee, on top
        assert out["fee_eur"] == 0.06                      # the fee it really charged
    finally:
        conn.close(); os.unlink(p)


def test_paper_mirrors_krakens_fee_convention(monkeypatch):
    """Simulation must match reality or the shadow arms are measuring a different game:
    the fee is charged ON TOP, so the cash leaving the sleeve is exactly what it spent."""
    conn, p = make_db()
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 1.0)
    monkeypatch.setattr(market, "touch", lambda pair: {"bid": 100.0, "ask": 100.0, "last": 100.0})
    try:
        before = portfolio.holdings(conn, "paper", "swing")["EUR"]
        out = portfolio.execute(conn, "paper", "swing", 1, "buy", "BTC/EUR", 0.5,
                                {"BTC/EUR": 100.0})
        after = portfolio.holdings(conn, "paper", "swing")
        spent = before - after["EUR"]
        assert round(spent, 6) == round(before * 0.5, 6)          # exactly the budget, no more
        # coins bought + the fee charged on top == the cash that left (out["fee_eur"] is
        # rounded for display, so check against the real arithmetic, not the display value)
        cost = after["BTC"] * 100.0
        assert round(cost * (1 + config.MAKER_FEE), 6) == round(spent, 6)
        assert out["fee_eur"] == round(cost * config.MAKER_FEE, 2)
    finally:
        conn.close(); os.unlink(p)


def test_backup_writes_a_file_that_opens_and_prunes_old_ones(monkeypatch, tmp_path):
    """A WAL-mode DB copied live may not restore. VACUUM INTO always produces a
    file that opens — this is the audit trail for real money (#41)."""
    conn, p = make_db()
    monkeypatch.setattr(config, "BACKUP_DIR", str(tmp_path), raising=False)
    monkeypatch.setattr(config, "BACKUP_KEEP", 2, raising=False)
    try:
        conn.execute("INSERT INTO decisions(at, mode, sleeve, status) "
                     "VALUES('2026-07-13T06:00:00+00:00','live','swing','held')")
        conn.commit()
        out = db.backup(conn, p)
        assert out["bytes"] > 0
        restored = db.connect(out["file"])          # the copy actually opens...
        assert restored.execute("SELECT COUNT(*) c FROM decisions").fetchone()["c"] == 1
        restored.close()                           # ...and carries the ledger

        for _ in range(3):                         # retention holds the line
            out = db.backup(conn, p)
        assert out["kept"] == 2 and out["pruned"]
        assert len(list(tmp_path.glob("magpie-*.db"))) == 2
    finally:
        conn.close(); os.unlink(p)


def test_the_diary_shows_only_the_real_bot_and_only_24h(monkeypatch):
    """Two ways this panel lied: it showed EVERY mode (20 of the last 30 rows were
    shadow arms, rendered identically to trades that moved real money), and it had no
    time bound at all."""
    from fastapi.testclient import TestClient
    from app import main
    conn, p = make_db()
    orig = db.connect
    monkeypatch.setattr(config, "mode", lambda: "live")
    # TestClient serves on another thread, and a sqlite connection belongs to the
    # thread that made it — hand out a fresh connection to the same file
    monkeypatch.setattr(db, "connect", lambda *a, **k: orig(p))
    monkeypatch.setattr(market, "tickers", lambda pairs: {"BTC/EUR": 100.0})
    try:
        conn.execute("INSERT INTO decisions(at, mode, sleeve, action, status, reasoning) "
                     "VALUES(datetime('now','-2 hours'),'live','swing','buy','executed','real')")
        conn.execute("INSERT INTO decisions(at, mode, sleeve, action, status, reasoning) "
                     "VALUES(datetime('now','-2 hours'),'shadow:coinflip','swing','buy','executed','a coin flip')")
        conn.execute("INSERT INTO decisions(at, mode, sleeve, action, status, reasoning) "
                     "VALUES(datetime('now','-3 days'),'live','swing','sell','executed','ancient')")
        conn.commit()

        d = TestClient(main.app).get("/api/state").json()["decisions"]
        said = [x["reasoning"] for x in d]
        assert "real" in said                    # the bot's own recent decision
        assert "a coin flip" not in said         # a shadow arm must never look like a real trade
        assert "ancient" not in said             # older than 24h
    finally:
        conn.close(); os.unlink(p)


# ---------- an undeployed top-up is the quiet way a sleeve falls asleep (#48) ----------

def test_topup_is_recorded_and_flagged_until_the_sleeve_buys():
    conn, p = make_db()
    try:
        portfolio.apply_topup(conn, "paper", 47.90)
        row = conn.execute("SELECT amount, per_sleeve FROM topups").fetchone()
        assert row["amount"] == 47.90
        assert row["per_sleeve"] == round(47.90 / 3, 2)

        # nothing bought since -> the cash is still sitting there, and the brain is told
        t = portfolio.undeployed_topup(conn, "paper", "swing")
        assert t and t["per_sleeve"] == round(47.90 / 3, 2)

        # ...the sleeve buys -> the money did its job, and the note stops
        # the app stamps every row with an ISO-8601 'T' timestamp; SQLite's datetime()
        # emits a SPACE separator, which sorts BEFORE 'T' and would silently break the
        # "has this sleeve bought since?" comparison. Write it the way production does.
        later = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(timespec="seconds")
        conn.execute("INSERT INTO orders (at, mode, sleeve, pair, side, amount, price, cost, fee) "
                     "VALUES (?,'paper','swing','TRX/EUR','buy',1,1,1,0)", (later,))
        conn.commit()
        assert portfolio.undeployed_topup(conn, "paper", "swing") is None

        # ...but the OTHER sleeves' cash is still asleep
        assert portfolio.undeployed_topup(conn, "paper", "fortnight") is not None
    finally:
        conn.close(); os.unlink(p)


def test_no_topup_means_nothing_to_flag():
    conn, p = make_db()
    try:
        assert portfolio.undeployed_topup(conn, "paper", "swing") is None
    finally:
        conn.close(); os.unlink(p)


def test_the_vault_is_never_told_about_a_topup_it_did_not_receive():
    """apply_topup funds sleeves.ACTIVE only — the vault is profits-only and gets €0.
    Telling it 'a top-up was added to this sleeve' is a lie about money it never saw,
    and the cash block exists to PUSH a sleeve to deploy (#50)."""
    conn, p = make_db()
    try:
        portfolio.apply_topup(conn, "paper", 300.0)
        assert "vault" not in sleeves.ACTIVE
        vault = conn.execute("SELECT allocated FROM sleeve_meta WHERE mode='paper' "
                             "AND sleeve='vault'").fetchone()
        assert vault["allocated"] == 0.0                     # it received nothing...
        assert portfolio.undeployed_topup(conn, "paper", "vault") is None   # ...and is told nothing
        assert portfolio.undeployed_topup(conn, "paper", "swing") is not None
    finally:
        conn.close(); os.unlink(p)


def test_trade_notification_is_never_cut_mid_word():
    """The push hard-sliced the reasoning at 140 chars — mid-word, no ellipsis — so the
    message did not look truncated, it looked broken, and the reason for the trade was
    thrown away. A real reasoning must now arrive whole."""
    from app.engine import _clip
    real = ("SOL/EUR is approaching oversold territory on the 4-hour chart (RSI 30.5) while "
            "resting above its 4h EMA200 support. Negative funding rates and a strong positive "
            "orderbook imbalance suggest a high probability of a short-term bounce toward the "
            "4h EMA20, offering enough margin to clear the fee hurdle.")
    assert _clip(real) == real, "a real 299-char reasoning must arrive in full"

    runaway = "word " * 400                      # only a pathological one is trimmed
    out = _clip(runaway)
    assert out.endswith("…")                     # and it SAYS it was trimmed
    assert not out.rstrip("…").endswith("wor")   # never mid-word
    assert _clip("") == "" and _clip("short") == "short"


def test_a_garbage_answer_cannot_kill_the_other_sleeves(monkeypatch):
    """#63 blast radius. run_sleeve caught only AdvisorError, so a validator crash
    escaped the list comprehension in run_cycle and killed the entire cycle: the
    sleeve wrote NO decision row at all (not even 'invalid'), and a missing row reads
    exactly like a considered quiet day. One bad answer must cost one sleeve, and the
    others must still decide."""
    conn, p = make_db()
    try:
        # swing's brain returns a JSON list; fortnight's answers properly
        answers = {"swing": '[{"action": "buy"}]',
                   "fortnight": '{"action": "hold", "confidence": 0.5, "reasoning": "ok"}'}
        asked = {"sleeve": None}
        monkeypatch.setattr(advisor, "build_prompt",
                            lambda *a, **kw: "prompt for " + (asked["sleeve"] or "?"))
        monkeypatch.setattr(advisor, "ask",
                            lambda prompt, deep=False: answers[asked["sleeve"]])

        results = []
        for sleeve in ("swing", "fortnight"):
            asked["sleeve"] = sleeve
            results.append(engine.run_sleeve(conn, "paper", sleeve, {}, [], {}))

        assert results[0]["status"] == "invalid"      # the bad one fails honestly...
        assert results[1]["status"] == "held"         # ...and the next sleeve still decides

        rows = conn.execute(
            "SELECT sleeve, status, response_raw FROM decisions WHERE mode='paper' ORDER BY sleeve"
        ).fetchall()
        got = {r[0]: (r[1], r[2]) for r in rows}
        assert got["swing"][0] == "invalid"           # a ROW EXISTS -- not silence
        assert '[{"action": "buy"}]' in got["swing"][1]   # and it keeps the raw answer
        assert got["fortnight"][0] == "held"
    finally:
        conn.close(); os.unlink(p)


# --- #64: a Kraken blip must not take the cycle down --------------------------

def test_tickers_retries_a_transient_timeout(monkeypatch):
    """Kraken's public endpoints blip. One 10s read timeout on one pair used to raise
    straight out of run_cycle and kill the whole cycle (#64)."""
    import ccxt
    calls = []

    class Flaky:
        def fetch_ticker(self, pair):
            calls.append(pair)
            if len(calls) < 3:
                raise ccxt.RequestTimeout("kraken GET /0/public/Ticker")
            return {"last": 100.0}

    monkeypatch.setattr(market, "exchange", lambda: Flaky())
    monkeypatch.setattr(market.time, "sleep", lambda _s: None)   # don't actually wait
    assert market.tickers(["BTC/EUR"]) == {"BTC/EUR": 100.0}
    assert len(calls) == 3                                        # two failures, then good


def test_tickers_gives_up_after_the_retries(monkeypatch):
    import ccxt

    class Dead:
        def fetch_ticker(self, pair):
            raise ccxt.RequestTimeout("kraken down")

    monkeypatch.setattr(market, "exchange", lambda: Dead())
    monkeypatch.setattr(market.time, "sleep", lambda _s: None)
    with pytest.raises(ccxt.RequestTimeout):
        market.tickers(["BTC/EUR"])


def test_a_dead_exchange_ends_the_cycle_loudly_not_silently(monkeypatch):
    """#64 blast radius. An unreachable exchange must not CRASH run_cycle: a cycle that
    dies before the sleeve loop writes no decision row at all, and a sleeve with no row
    is invisible to the retry path -- the slot is lost and the diary shows a quiet day.
    Without prices the bot genuinely cannot decide; it must SAY so, on the record."""
    conn, p = make_db()
    try:
        real_connect = db.connect                      # engine.db IS db -- grab it first
        monkeypatch.setattr(engine.db, "connect", lambda *a, **k: real_connect(p))
        monkeypatch.setattr(engine.config, "mode", lambda: "paper")

        def dead(*a, **k):
            raise RuntimeError("kraken GET /0/public/Ticker: read timed out")

        monkeypatch.setattr(engine, "_market_context", dead)

        out = engine.run_cycle()                       # must NOT raise
        assert out["status"] == "error"
        assert "market data unavailable" in out["detail"]

        check = real_connect(p)                        # run_cycle closed its own handle
        rows = check.execute(
            "SELECT sleeve, status, detail FROM decisions WHERE mode='paper'").fetchall()
        assert len(rows) == 1                          # a row EXISTS -- not silence
        assert rows[0][1] == "error"
        assert "market data unavailable" in rows[0][2]

        # and health can see it
        assert db.get_setting(check, "consecutive_failures") == "1"
        check.close()
    finally:
        conn.close(); os.unlink(p)


# --- #65: a malformed brain answer is as transient as a 503 -------------------

def test_invalid_is_retried_like_an_error():
    """The brain answering with a JSON array is the model fluffing ONE response — the
    same prompt routinely answers cleanly on the next ask. It used to be excluded from
    the retry path, so a live sleeve lost its whole decision slot while the retry timer
    sat there with nothing to do (#65)."""
    conn, p = make_db()
    try:
        engine._record(conn, "live", "swing", "invalid", "expected a JSON object, got list")
        engine._record(conn, "live", "fortnight", "error", "gemini 503")
        engine._record(conn, "live", "quarter", "held", "chose to hold")
        assert sorted(engine._latest_failed_sleeves(conn, "live")) == ["fortnight", "swing"]
    finally:
        conn.close(); os.unlink(p)


def test_no_key_is_still_not_retried():
    """A missing key is a PERMANENT configuration fault, not a transient one.
    Retrying it would just hammer a wall."""
    conn, p = make_db()
    try:
        engine._record(conn, "live", "swing", "no_key", "no GEMINI_API_KEY")
        assert engine._latest_failed_sleeves(conn, "live") == []
    finally:
        conn.close(); os.unlink(p)


def test_a_good_decision_is_not_retried():
    conn, p = make_db()
    try:
        engine._record(conn, "live", "swing", "held", "chose to hold")
        engine._record(conn, "live", "fortnight", "executed", "")
        assert engine._latest_failed_sleeves(conn, "live") == []
    finally:
        conn.close(); os.unlink(p)


def test_invalid_schedules_a_retry():
    """_note_retry_state gates whether a retry is PENDING at all — 'invalid' was
    excluded there too, so nothing was ever scheduled."""
    conn, p = make_db()
    try:
        engine._note_retry_state(conn, [{"sleeve": "swing", "status": "invalid"}],
                                 fresh_cycle=True)
        assert db.get_setting(conn, "retry_cycle_at")        # a retry IS pending
    finally:
        conn.close(); os.unlink(p)


# --- #66: a fired stop must be booked as a SALE before top-ups are detected ----

def test_stops_are_synced_before_topups_are_detected():
    """A stop firing between cycles leaves its EUR proceeds sitting at the exchange.
    detect_topup() is only 'EUR beyond what the books account for = a deposit', so if it
    runs FIRST that money is split across the sleeves and RATCHETS THE HWMs (which never
    come back down, so real profit silently stops being skimmed) -- and then stops.sync
    books the very same sale a second time. /api/reconcile always had this ordering;
    run_cycle did not (#66)."""
    import inspect
    src = inspect.getsource(engine.run_cycle)
    sync_at = src.index("stops.sync(conn, mode, prices)")
    topup_at = src.index("portfolio.detect_topup(conn, mode)")
    assert sync_at < topup_at, "detect_topup must not run before stops.sync -- see #66"


# --- #68: only one book-mutating run at a time --------------------------------

def test_a_second_concurrent_run_is_refused_not_doubled(monkeypatch):
    """/api/cycle and /api/cycle/retry are sync handlers, so two systemd timers run in
    PARALLEL in the threadpool. The retry path picks candidates from the last COMMITTED
    decision row -- written only after the LLM call returns -- so a slow retry could
    still be deciding while the next one fired, read the same stale 'error', and
    re-decide the same sleeve: TWO REAL BUYS, one of which the books never saw (#68)."""
    import threading
    from fastapi.testclient import TestClient
    from app import main

    started, release = threading.Event(), threading.Event()
    calls = []

    def slow_cycle(now=None):
        calls.append("cycle")
        started.set()
        release.wait(timeout=5)          # hold the lock
        return {"status": "ok"}

    monkeypatch.setattr(main.engine, "run_cycle", slow_cycle)
    monkeypatch.setattr(main.engine, "retry_now", lambda force=False: calls.append("retry"))

    client = TestClient(main.app)
    first = threading.Thread(target=lambda: client.post("/api/cycle"))
    first.start()
    assert started.wait(timeout=5)

    # a retry firing while the cycle is mid-flight must be REFUSED, not run
    r = client.post("/api/cycle/retry")
    assert r.json()["status"] == "busy"
    assert "retry" not in calls, "the retry ran concurrently with a cycle -- #68"

    release.set()
    first.join(timeout=5)
    assert calls == ["cycle"]

    # and the lock is released afterwards -- a failure must not wedge the bot forever
    r2 = client.post("/api/cycle")
    assert r2.json()["status"] == "ok"


def test_the_lock_is_released_when_a_run_raises(monkeypatch):
    from fastapi.testclient import TestClient
    from app import main

    def boom(now=None):
        raise RuntimeError("cycle exploded")

    monkeypatch.setattr(main.engine, "run_cycle", boom)
    client = TestClient(main.app, raise_server_exceptions=False)
    client.post("/api/cycle")                       # 500s

    monkeypatch.setattr(main.engine, "run_cycle", lambda now=None: {"status": "ok"})
    assert client.post("/api/cycle").json()["status"] == "ok"   # not wedged
