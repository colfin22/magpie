import os
import tempfile
from datetime import datetime, timedelta, timezone

from app import db, engine, scoring


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def iso(dt):
    return dt.isoformat(timespec="seconds")


def midnight(days_ago: int):
    """Candles are day-aligned in reality; decisions land midday between them."""
    n = datetime.now(timezone.utc)
    return n.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days_ago)


def add_decision(conn, at, sleeve, action, pair, conf, status="executed"):
    cur = conn.execute(
        "INSERT INTO decisions(at, mode, sleeve, action, pair, confidence, status) "
        "VALUES(?,?,?,?,?,?,?)", (iso(at), "paper", sleeve, action, pair, conf, status))
    conn.commit()
    return cur.lastrowid


def add_candle(conn, pair, when, close):
    conn.execute("INSERT INTO candles(pair, ts, open, high, low, close, volume) "
                 "VALUES(?,?,?,?,?,?,?)",
                 (pair, int(when.timestamp() * 1000), close, close, close, close, 1.0))
    conn.commit()


def test_buy_that_went_up_is_correct_and_one_that_fell_is_not():
    conn, p = make_db()
    d0 = midnight(5)                        # a swing call, horizon 3d -> fully elapsed
    then = d0 + timedelta(hours=12)
    try:
        add_candle(conn, "BTC/EUR", d0, 100.0)
        add_candle(conn, "BTC/EUR", d0 + timedelta(days=3), 110.0)
        add_decision(conn, then, "swing", "buy", "BTC/EUR", 0.9)
        add_decision(conn, then, "swing", "sell", "BTC/EUR", 0.6)
        assert scoring.grade(conn)["graded"] == 2
        rows = {r["action"]: r for r in conn.execute("SELECT * FROM scores")}
        assert rows["buy"]["correct"] == 1 and round(rows["buy"]["move_pct"]) == 10
        assert rows["sell"]["correct"] == 0        # sold, and it rose — a bad call
    finally:
        conn.close(); os.unlink(p)


def test_a_call_inside_its_horizon_is_not_graded_yet():
    """Grading early would score noise as skill."""
    conn, p = make_db()
    d0 = midnight(1)                        # swing horizon is 3d — too soon
    then = d0 + timedelta(hours=12)
    try:
        add_candle(conn, "BTC/EUR", d0, 100.0)
        add_candle(conn, "BTC/EUR", midnight(0), 130.0)
        add_decision(conn, then, "swing", "buy", "BTC/EUR", 0.9)
        assert scoring.grade(conn)["graded"] == 0
        assert conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_holds_and_the_vault_are_never_graded():
    conn, p = make_db()
    d0 = midnight(120)
    then = d0 + timedelta(hours=12)
    try:
        add_candle(conn, "BTC/EUR", d0, 100.0)
        add_candle(conn, "BTC/EUR", d0 + timedelta(days=90), 150.0)
        add_decision(conn, then, "swing", "hold", None, 0.8, status="held")
        add_decision(conn, then, "vault", "buy", "BTC/EUR", 0.8)   # a year+ horizon
        assert scoring.grade(conn)["graded"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_grading_is_idempotent():
    conn, p = make_db()
    d0 = midnight(5)
    then = d0 + timedelta(hours=12)
    try:
        add_candle(conn, "BTC/EUR", d0, 100.0)
        add_candle(conn, "BTC/EUR", d0 + timedelta(days=3), 110.0)
        add_decision(conn, then, "swing", "buy", "BTC/EUR", 0.9)
        assert scoring.grade(conn)["graded"] == 1
        assert scoring.grade(conn)["graded"] == 0        # a second pass marks nothing twice
        assert conn.execute("SELECT COUNT(*) c FROM scores").fetchone()["c"] == 1
    finally:
        conn.close(); os.unlink(p)


def test_calibration_buckets_by_stated_confidence():
    conn, p = make_db()
    d0 = midnight(5)
    then = d0 + timedelta(hours=12)
    try:
        add_candle(conn, "BTC/EUR", d0, 100.0)
        add_candle(conn, "BTC/EUR", d0 + timedelta(days=3), 110.0)     # price rose 10%
        add_decision(conn, then, "swing", "buy", "BTC/EUR", 0.95)      # confident + right
        add_decision(conn, then, "swing", "buy", "BTC/EUR", 0.9)       # confident + right
        add_decision(conn, then, "swing", "sell", "BTC/EUR", 0.6)      # unsure + wrong
        scoring.grade(conn)
        c = scoring.calibration(conn, "paper")
        assert c["graded"] == 3 and c["hit_rate_pct"] == 66.7
        top = next(b for b in c["buckets"] if b["bucket"] == "≥0.85")
        mid = next(b for b in c["buckets"] if b["bucket"] == "0.5–0.7")
        assert top["n"] == 2 and top["hit_rate_pct"] == 100.0
        assert mid["n"] == 1 and mid["hit_rate_pct"] == 0.0
        assert "coin flip is 50%" in scoring.summary_line(conn, "paper")
    finally:
        conn.close(); os.unlink(p)


def test_arms_get_a_calibration_record_too():
    """Scoring is mode-agnostic, so every shadow arm is marked as well."""
    conn, p = make_db()
    d0 = midnight(5)
    then = d0 + timedelta(hours=12)
    try:
        add_candle(conn, "BTC/EUR", d0, 100.0)
        add_candle(conn, "BTC/EUR", d0 + timedelta(days=3), 90.0)
        conn.execute("INSERT INTO decisions(at, mode, sleeve, action, pair, confidence, status) "
                     "VALUES(?,?,?,?,?,?,?)",
                     (iso(then), "shadow:coinflip", "swing", "buy", "BTC/EUR", 0.5, "executed"))
        conn.commit()
        scoring.grade(conn)
        c = scoring.calibration(conn, "shadow:coinflip")
        assert c["graded"] == 1 and c["hit_rate_pct"] == 0.0    # bought, it fell
    finally:
        conn.close(); os.unlink(p)


def test_review_prompt_carries_the_measured_record():
    assert "{calibration}" in engine.REVIEW_PROMPT
    assert "measured, not remembered" in engine.REVIEW_PROMPT


# --- #73: a liquidation is not a prediction ----------------------------------

def test_a_stop_loss_sell_is_not_graded():
    """A stop firing writes an executed 'sell' decision with confidence=NULL, because no
    model claimed anything. Grading it measured the MARKET, not the bot: a stop fires
    AFTER a fall, so its forward move is systematically biased, and it was being folded
    into a hit rate captioned 'every buy/sell is a falsifiable claim about direction'.
    A liquidation is the opposite of a claim (#73)."""
    conn, p = make_db()
    try:
        for d in range(10, -1, -1):
            add_candle(conn, "BTC/EUR", midnight(d), 100.0)
        forced = add_decision(conn, midnight(9) + timedelta(hours=12), "swing", "sell",
                              "BTC/EUR", None)          # a stop-loss: no confidence
        brain = add_decision(conn, midnight(9) + timedelta(hours=12), "swing", "buy",
                             "BTC/EUR", 0.8)            # a real call
        scoring.grade(conn)
        graded = [r[0] for r in conn.execute("SELECT decision_id FROM scores")]
        assert brain in graded
        assert forced not in graded, "a forced exit was graded as if the brain predicted it"
    finally:
        conn.close(); os.unlink(p)
