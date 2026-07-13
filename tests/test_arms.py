import os
import random
import tempfile

from app import advisor, arms, config, db, market, portfolio, sleeves

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
    got = arms.parse("ema:rule:ema20,typo:rule:nosuch,bad,nope:llm:notaprovider,flip:rule:random")
    assert [a["name"] for a in got] == ["ema", "flip"]        # junk dropped, not fatal
    assert got[0]["mode"] == "shadow:ema"


def test_parse_llm_arms(monkeypatch):
    got = arms.parse("claude:llm:openrouter@anthropic/claude-sonnet-5,plain:llm:gemini")
    assert got[0]["kind"] == "llm" and got[0]["provider"] == "openrouter"
    assert got[0]["model"] == "anthropic/claude-sonnet-5"     # '@' splits; '/' lives in the id
    assert got[1]["provider"] == "gemini" and got[1]["model"] is None   # provider default model


def test_an_llm_arm_gets_the_identical_prompt_and_its_own_brain(monkeypatch):
    """The bake-off is only fair if the prompt is not the variable."""
    stub_market(monkeypatch)
    monkeypatch.setattr(config, "SHADOW_ARMS", "claude:llm:openrouter@anthropic/claude-sonnet-5",
                        raising=False)
    seen = {}

    def fake_ask(prompt, deep=False, provider=None, model=None, **kw):
        seen.update(prompt=prompt, provider=provider, model=model, deep=deep)
        return ('{"action":"buy","pair":"BTC/EUR","fraction":0.9,'   # 0.5 of a €16 sleeve
                '"confidence":0.7,"reasoning":"rival brain says buy"}')  # is under the €10 min

    monkeypatch.setattr(arms.advisor, "ask", fake_ask)
    conn, p = make_db()
    try:
        data = [row("BTC/EUR", 100_000, 90_000), row("ETH/EUR", 3_000, 4_000)]
        res = arms.run_all(conn, "paper", ["swing"], PRICES, data, extras={"fear_greed_index": 61})
        assert res[0]["status"] == "executed"
        assert seen["provider"] == "openrouter"                  # its own brain...
        assert seen["model"] == "anthropic/claude-sonnet-5"
        assert sleeves.MANDATES["swing"] in seen["prompt"]       # ...on the real bot's prompt
        assert "fear_greed_index" in seen["prompt"]              # same context, same instant
        assert "BTC" in portfolio.holdings(conn, "shadow:claude", "swing")
        d = conn.execute("SELECT * FROM decisions WHERE mode='shadow:claude'").fetchone()
        assert d["reasoning"] == "rival brain says buy"          # its reasoning, in its own diary
    finally:
        conn.close(); os.unlink(p)


def test_a_dead_rival_brain_only_holds_its_own_arm(monkeypatch):
    stub_market(monkeypatch)
    monkeypatch.setattr(config, "SHADOW_ARMS", "claude:llm:openrouter,ema:rule:ema20", raising=False)

    def dead(*a, **k):
        raise arms.advisor.AdvisorError("openrouter is down")

    monkeypatch.setattr(arms.advisor, "ask", dead)
    conn, p = make_db()
    try:
        data = [row("BTC/EUR", 100_000, 90_000), row("ETH/EUR", 3_000, 4_000)]
        res = arms.run_all(conn, "paper", ["swing"], PRICES, data)
        by = {r["arm"]: r["status"] for r in res}
        assert by["claude"] == "error"          # the dead brain errors...
        assert by["ema"] == "executed"          # ...and nobody else notices
        assert portfolio.holdings(conn, "shadow:claude", "swing").get("BTC") is None
    finally:
        conn.close(); os.unlink(p)


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
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 10.0)
    d = arms.decide_dca({"holdings": {"EUR": 100.0}}, [row("BTC/EUR", 100, 200)],
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


def test_a_rival_brains_HOLD_keeps_its_reasoning(monkeypatch):
    """A hold is a real decision with real reasoning — the most interesting thing
    an llm arm produces. Collapsing it to a bare 'held' throws that away."""
    stub_market(monkeypatch)
    monkeypatch.setattr(config, "SHADOW_ARMS", "claude:llm:openrouter", raising=False)
    monkeypatch.setattr(arms.advisor, "ask", lambda *a, **k:
                        '{"action":"hold","pair":null,"fraction":null,'
                        '"confidence":0.4,"reasoning":"RSI is stretched; no edge here"}')
    conn, p = make_db()
    try:
        res = arms.run_all(conn, "paper", ["swing"], PRICES, [row("BTC/EUR", 100_000, 90_000)])
        assert res[0]["status"] == "held"
        d = conn.execute("SELECT * FROM decisions WHERE mode='shadow:claude'").fetchone()
        assert d["action"] == "hold" and d["confidence"] == 0.4
        assert "RSI is stretched" in d["reasoning"]          # its words, in its own diary
        assert "RSI is stretched" in d["response_raw"]       # and the raw answer kept
    finally:
        conn.close(); os.unlink(p)


def test_dca_holds_rather_than_erroring_once_its_slice_is_below_the_minimum(monkeypatch):
    """It spends a fraction of what's LEFT, so its slice shrinks forever. Without
    this it proposed a sub-minimum buy every cycle, which could only be rejected —
    an error row on every cycle, for the rest of time."""
    monkeypatch.setattr(config, "BASE_PAIRS", ["BTC/EUR"], raising=False)
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 10.0)
    fat = arms.decide_dca({"holdings": {"EUR": 100.0}}, [], "swing", random.Random(1))
    assert fat["action"] == "buy"                                   # 20% of 100 = €20, fine
    thin = arms.decide_dca({"holdings": {"EUR": 40.0}}, [], "swing", random.Random(1))
    assert thin is None                                             # 20% of 40 = €8, under the min


# ---------- the brain is never its own control (#46) ----------

ALL_LLMS = ("ema:rule:ema20,"
            "gemini:llm:gemini,"
            "claude:llm:openrouter@anthropic/claude-sonnet-5,"
            "deepseek:llm:openrouter@deepseek/deepseek-chat")


def _names(monkeypatch, provider, model=""):
    """The arms that actually run, given who the brain is."""
    monkeypatch.setattr(config, "SHADOW_ARMS", ALL_LLMS, raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDER", provider, raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", model, raising=False)
    return [a["name"] for a in arms.enabled()]


def test_gemini_brain_does_not_also_run_as_an_arm(monkeypatch):
    # today's live config: gemini is the brain, so it must not be its own rival
    assert _names(monkeypatch, "gemini") == ["ema", "claude", "deepseek"]


def test_displaced_gemini_becomes_an_arm(monkeypatch):
    # switch the brain to another provider and gemini keeps being measured —
    # the model with the longest record must not vanish from the comparison
    names = _names(monkeypatch, "openrouter")
    assert "gemini" in names


def test_a_different_model_of_the_same_provider_is_still_a_rival(monkeypatch):
    # the brain is gemini-2.5-flash; an arm on gemini-pro is a GENUINE rival,
    # so matching on provider alone would wrongly silence it
    monkeypatch.setattr(config, "SHADOW_ARMS", "rival:llm:gemini@gemini-pro", raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "gemini-2.5-flash", raising=False)
    assert [a["name"] for a in arms.enabled()] == ["rival"]

    # ...but the SAME model, named explicitly, is the brain and must be dropped
    monkeypatch.setattr(config, "SHADOW_ARMS", "twin:llm:gemini@gemini-2.5-flash", raising=False)
    assert arms.enabled() == []


def test_rule_arms_are_never_confused_for_the_brain(monkeypatch):
    monkeypatch.setattr(config, "SHADOW_ARMS", "ema:rule:ema20,dca:rule:dca", raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini", raising=False)
    assert [a["name"] for a in arms.enabled()] == ["ema", "dca"]


def test_same_model_through_a_different_gateway_is_still_the_brain(monkeypatch):
    # brain = Claude direct; arm = the SAME Claude, routed via openrouter.
    # Same weights, same prompt — one contestant, and it must not judge itself.
    monkeypatch.setattr(config, "SHADOW_ARMS",
                        "claude:llm:openrouter@anthropic/claude-sonnet-5", raising=False)
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "", raising=False)
    assert arms.enabled() == []


def test_brain_model_override_does_not_leak_into_an_arm(monkeypatch):
    # LLM_MODEL is the BRAIN's override. If it leaked, this gemini arm would be
    # called with claude's model id — the arm silently running the wrong model.
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic", raising=False)
    monkeypatch.setattr(config, "LLM_MODEL", "claude-sonnet-5", raising=False)
    assert advisor.effective_model("gemini") == config.GEMINI_MODEL
    assert advisor.effective_model() == "claude-sonnet-5"
