import os
import random
import tempfile

from app import arms, config, db, market, portfolio, sleeves

PRICES = {"BTC/EUR": 100_000.0, "ETH/EUR": 3_000.0}


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def stub_market(monkeypatch):
    """Arms must never reach the network for a decision, and never for a fill
    that isn't the real bot's."""
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 10.0)
    monkeypatch.setattr(market, "touch", lambda pair: {
        "bid": PRICES[pair], "ask": PRICES[pair], "last": PRICES[pair], "spread_pct": 0.0})
    monkeypatch.setattr(config, "PAIRS", ["BTC/EUR", "ETH/EUR"], raising=False)
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR", "ETH/EUR"], raising=False)


def row(pair, price, ema20, rsi=50.0, ret7=1.0):
    return {"pair": pair, "timeframe": "1d", "price": price, "ema20": ema20,
            "rsi14": rsi, "return_7_candles_pct": ret7}


# ---------- the feature is off unless asked for ----------

def test_off_by_default(monkeypatch):
    monkeypatch.setattr(config, "SHADOW_ARMS", "", raising=False)
    assert arms.enabled() == []
    conn, p = make_db()
    try:
        assert arms.run_all(conn, "paper", ["swing"], PRICES, []) == []
        modes = {r[0] for r in conn.execute("SELECT DISTINCT mode FROM holdings")}
        assert modes == {"paper", "live"}   # no shadow books came into existence
    finally:
        conn.close(); os.unlink(p)


def test_parse_drops_junk_but_keeps_good(monkeypatch):
    got = arms.parse("ema:rule:ema20,typo:rule:nosuch,bad,claude:llm:anthropic,flip:rule:random")
    assert [a["name"] for a in got] == ["ema", "flip"]        # junk dropped, not fatal
    assert got[0]["mode"] == "shadow:ema"


# ---------- the deciders ----------

def test_ema20_buys_above_sells_below():
    port = {"holdings": {"EUR": 20.0}}
    d = arms.decide_ema20(port, [row("BTC/EUR", 100, 90), row("ETH/EUR", 100, 110)],
                          "swing", random.Random(1))
    assert d["action"] == "buy" and d["pair"] == "BTC/EUR"    # only BTC is above its EMA20

    held = {"holdings": {"EUR": 0.0, "BTC": 0.001}}
    d = arms.decide_ema20(held, [row("BTC/EUR", 80, 90)], "swing", random.Random(1))
    assert d["action"] == "sell" and d["fraction"] == 1.0     # fell below -> full exit


def test_ema20_wont_buy_into_froth_or_with_no_cash():
    hot = [row("BTC/EUR", 100, 90, rsi=85)]
    assert arms.decide_ema20({"holdings": {"EUR": 20.0}}, hot, "swing", random.Random(1)) is None
    assert arms.decide_ema20({"holdings": {"EUR": 0.0}}, [row("BTC/EUR", 100, 90)],
                             "swing", random.Random(1)) is None


def test_dca_buys_a_slice_and_never_sells(monkeypatch):
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR"], raising=False)
    d = arms.decide_dca({"holdings": {"EUR": 20.0}}, [row("BTC/EUR", 100, 200)],
                        "swing", random.Random(1))
    assert d["action"] == "buy" and d["fraction"] == arms.DCA_FRACTION
    assert arms.decide_dca({"holdings": {"EUR": 0.0, "BTC": 1.0}}, [], "swing",
                           random.Random(1)) is None


def test_random_only_ever_proposes_possible_trades(monkeypatch):
    monkeypatch.setattr(config, "PAIRS", ["BTC/EUR", "ETH/EUR"], raising=False)
    data = [row("BTC/EUR", 100, 90), row("ETH/EUR", 100, 90)]
    for seed in range(40):
        rng = random.Random(seed)
        d = arms.decide_random({"holdings": {"EUR": 20.0}}, data, "swing", rng)
        assert d is None or d["action"] == "buy"          # nothing held -> can never sell
        d = arms.decide_random({"holdings": {"EUR": 0.0, "BTC": 0.001}}, data, "swing", rng)
        assert d is None or d["action"] == "sell"         # no cash -> can never buy


# ---------- capital parity ----------

def test_arm_seeds_from_the_real_stake_not_its_equity(monkeypatch):
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20", raising=False)
    conn, p = make_db()
    try:
        for s in sleeves.ACTIVE:   # pretend live is funded and already up on the day
            conn.execute("UPDATE sleeve_meta SET allocated=20, hwm=20 WHERE mode='live' AND sleeve=?", (s,))
            conn.execute("UPDATE holdings SET amount=999 WHERE mode='live' AND sleeve=? AND asset='EUR'", (s,))
        conn.commit()
        arms.ensure_seeded(conn, arms.enabled()[0], "live")
        for s in sleeves.ACTIVE:
            assert portfolio.holdings(conn, "shadow:ema", s)["EUR"] == 20   # the stake, not 999
        assert portfolio.holdings(conn, "shadow:ema", sleeves.VAULT) == {}  # vault profits-only
        assert db.get_setting(conn, "arm_since_ema")                        # dated for the leaderboard
    finally:
        conn.close(); os.unlink(p)


def _active_cash(conn, mode):
    return round(sum(portfolio.holdings(conn, mode, s).get("EUR", 0) for s in sleeves.ACTIVE), 2)


def test_topup_mirrors_to_an_existing_arm(monkeypatch):
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20,flip:rule:random", raising=False)
    monkeypatch.setattr(market, "tickers", lambda pairs: PRICES)
    conn, p = make_db()
    try:
        for arm in arms.enabled():                 # arms already running...
            arms.ensure_seeded(conn, arm, "paper")
        portfolio.apply_topup(conn, "paper", 30.0)  # ...then fresh cash lands
        arms.mirror_topup(conn, 30.0, "paper")
        for mode in ("shadow:ema", "shadow:flip"):
            assert _active_cash(conn, mode) == _active_cash(conn, "paper")
    finally:
        conn.close(); os.unlink(p)


def test_arm_seeded_during_a_topup_is_not_credited_twice(monkeypatch):
    """The real bot's stake is raised before the arms are mirrored, so an arm
    seeded in that same breath already has the new cash."""
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20", raising=False)
    monkeypatch.setattr(market, "tickers", lambda pairs: PRICES)
    conn, p = make_db()
    try:
        portfolio.apply_topup(conn, "paper", 30.0)
        arms.mirror_topup(conn, 30.0, "paper")     # seeds the arm for the first time
        assert _active_cash(conn, "shadow:ema") == _active_cash(conn, "paper")   # not +30 again
    finally:
        conn.close(); os.unlink(p)


# ---------- isolation: an arm must never hurt the real bot ----------

def test_a_broken_arm_cannot_touch_the_real_books(monkeypatch):
    stub_market(monkeypatch)
    monkeypatch.setattr(config, "SHADOW_ARMS", "boom:rule:ema20,ema:rule:ema20", raising=False)

    def explode(*a, **k):
        raise RuntimeError("decider went bang")

    real = dict(arms.DECIDERS)
    conn, p = make_db()
    try:
        before = portfolio.holdings(conn, "paper", "swing")
        # the first arm's decider blows up; the second must still trade
        calls = {"n": 0}

        def flaky(port, data, sleeve, rng):
            calls["n"] += 1
            if calls["n"] == 1:
                explode()
            return real["ema20"](port, data, sleeve, rng)

        monkeypatch.setitem(arms.DECIDERS, "ema20", flaky)
        data = [row("BTC/EUR", 100_000, 90_000), row("ETH/EUR", 3_000, 4_000)]
        results = arms.run_all(conn, "paper", ["swing"], PRICES, data)

        assert any(r["status"] == "error" for r in results)      # the broken arm errored
        assert any(r["status"] == "executed" for r in results)   # the healthy one traded on
        assert portfolio.holdings(conn, "paper", "swing") == before   # real books untouched
        assert "BTC" in portfolio.holdings(conn, "shadow:ema", "swing")
    finally:
        conn.close(); os.unlink(p)


def test_arms_trade_their_own_books_and_log_a_diary(monkeypatch):
    stub_market(monkeypatch)
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20", raising=False)
    conn, p = make_db()
    try:
        data = [row("BTC/EUR", 100_000, 90_000), row("ETH/EUR", 3_000, 4_000)]
        arms.run_all(conn, "paper", ["swing"], PRICES, data)
        assert "BTC" in portfolio.holdings(conn, "shadow:ema", "swing")
        d = conn.execute("SELECT * FROM decisions WHERE mode='shadow:ema'").fetchone()
        assert d["status"] == "executed" and d["action"] == "buy"
        assert "rule:ema20" in d["prompt"]        # the diary says who decided, and how
        o = conn.execute("SELECT * FROM orders WHERE mode='shadow:ema'").fetchone()
        assert o["exchange_id"] is None           # simulated: never went near Kraken
        assert conn.execute(
            "SELECT COUNT(*) c FROM snapshots WHERE mode='shadow:ema'").fetchone()["c"] == 4
    finally:
        conn.close(); os.unlink(p)


# ---------- the leaderboard (#32) ----------

def test_standings_ranks_bot_arms_and_hodl(monkeypatch):
    stub_market(monkeypatch)
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20,flip:rule:random", raising=False)
    monkeypatch.setattr(market, "tickers", lambda pairs: PRICES)
    conn, p = make_db()
    try:
        from app import ledger
        ledger.bench_init_if_needed(conn, "paper", 50.0, PRICES)
        data = [row("BTC/EUR", 100_000, 90_000), row("ETH/EUR", 3_000, 4_000)]
        arms.run_all(conn, "paper", ["swing"], PRICES, data)

        st = arms.standings(conn, "paper", PRICES)
        keys = [r["key"] for r in st]
        assert "magpie" in keys and "ema" in keys and "hodl" in keys
        assert st == sorted(st, key=lambda r: r["equity_eur"], reverse=True)   # ranked
        ema = next(r for r in st if r["key"] == "ema")
        assert ema["since"] and ema["trades"] == 1        # dated, and its trade is counted
        assert ema["curve"]                                # has its own equity history
        bot = next(r for r in st if r["key"] == "magpie")
        assert bot["trades"] == 0                          # the real bot traded nothing here
    finally:
        conn.close(); os.unlink(p)


def test_standings_skips_an_unseeded_arm(monkeypatch):
    """An arm configured but not yet run must not appear as a €0 loser."""
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20", raising=False)
    monkeypatch.setattr(market, "tickers", lambda pairs: PRICES)
    conn, p = make_db()
    try:
        assert [r["key"] for r in arms.standings(conn, "paper", PRICES)] == ["magpie"]
    finally:
        conn.close(); os.unlink(p)


def test_the_bot_has_a_since_date_too(monkeypatch):
    """The since column is the guard against a short record passing for skill —
    it must not be blank for the bot itself."""
    monkeypatch.setattr(config, "SHADOW_ARMS", "", raising=False)
    monkeypatch.setattr(market, "tickers", lambda pairs: PRICES)
    conn, p = make_db()
    try:
        portfolio.snapshot_all(conn, "paper", PRICES)
        bot = arms.standings(conn, "paper", PRICES)[0]
        assert bot["key"] == "magpie" and bot["since"]
    finally:
        conn.close(); os.unlink(p)
