"""Mark the homework (#33).

Every decision already carries a confidence and a reasoning; nothing ever
checked whether any of it was right. This grades each decision against what the
market actually did over that sleeve's own horizon, using the candles already
in the DB — no new feeds, no new network calls, no money at risk.

The question worth answering is not just "is the brain right more often than
not", but **does its confidence mean anything** — is a 0.9 call actually better
than a 0.6 call, or is the number decoration? So the report is a hit rate
bucketed by confidence, and it is fed back into the monthly self-review, which
until now was the model marking its own homework from memory.

Holds are not graded: a hold makes no falsifiable claim about direction.
"""
import logging
from datetime import datetime, timedelta, timezone

from . import config

LOGGER = logging.getLogger(__name__)

# how long each sleeve's call is given to come good — its own mandate's horizon
HORIZON_DAYS = {"swing": 3, "fortnight": 10, "quarter": 90}   # vault: not graded
BUCKETS = [(0.0, 0.5, "≤0.5"), (0.5, 0.7, "0.5–0.7"), (0.7, 0.85, "0.7–0.85"), (0.85, 1.01, "≥0.85")]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _close_at(conn, pair: str, when: datetime) -> float | None:
    """The last daily close at or before `when`. Candles are stored under the
    bare pair for the 1d timeframe."""
    row = conn.execute(
        "SELECT close FROM candles WHERE pair=? AND ts<=? ORDER BY ts DESC LIMIT 1",
        (pair, int(when.timestamp() * 1000))).fetchone()
    return row["close"] if row else None


def _correct(action: str, move_pct: float) -> bool:
    """A buy claims the price goes up; a sell claims it goes down. That is the
    whole prediction — no credit for being right about something else."""
    return move_pct > 0 if action == "buy" else move_pct < 0


def grade(conn, limit: int = 500) -> dict:
    """Grade every ungraded decision whose horizon has now fully elapsed.

    Graded across ALL modes, so the shadow arms get a calibration record too.
    """
    # confidence IS NOT NULL = the BRAIN made this call (#73). A stop-loss firing and a
    # universe auto-sell both write an executed 'sell' decision row with confidence=NULL,
    # because no model claimed anything. Grading them measured the market, not the bot:
    # a stop fires AFTER a fall, so its forward move is systematically biased, and it was
    # being folded into a hit rate captioned "every buy/sell is a falsifiable claim about
    # direction". A liquidation is the opposite of a claim.
    rows = conn.execute(
        "SELECT d.id, d.at, d.mode, d.sleeve, d.action, d.pair, d.confidence "
        "FROM decisions d LEFT JOIN scores s ON s.decision_id = d.id "
        "WHERE s.decision_id IS NULL AND d.action IN ('buy','sell') "
        "AND d.status='executed' AND d.pair IS NOT NULL AND d.confidence IS NOT NULL "
        "ORDER BY d.id LIMIT ?", (limit,)).fetchall()
    graded = skipped = 0
    for d in rows:
        days = HORIZON_DAYS.get(d["sleeve"])
        if not days:
            continue                                    # the vault makes no short-term claim
        at = datetime.fromisoformat(d["at"])
        end = at + timedelta(days=days)
        if end > _now():
            continue                                    # too soon to tell — leave it ungraded
        entry = _close_at(conn, d["pair"], at)
        exit_ = _close_at(conn, d["pair"], end)
        if not entry or not exit_:
            skipped += 1                                # no candle history for that pair/date
            continue
        move = (exit_ / entry - 1) * 100
        conn.execute(
            "INSERT INTO scores(decision_id, at, graded_at, mode, sleeve, pair, action, "
            "confidence, horizon_days, entry_price, exit_price, move_pct, correct) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d["id"], d["at"], _now().isoformat(timespec="seconds"), d["mode"], d["sleeve"],
             d["pair"], d["action"], d["confidence"], days, entry, exit_, round(move, 3),
             1 if _correct(d["action"], move) else 0))
        graded += 1
    conn.commit()
    if graded:
        LOGGER.info("graded %d decisions (%d skipped for missing candles)", graded, skipped)
    return {"graded": graded, "skipped": skipped}


def calibration(conn, mode: str) -> dict | None:
    """Hit rate by confidence bucket. The point of the bucketing: a brain whose
    0.9 calls land no better than its 0.6 calls is not confident, it is noisy."""
    rows = conn.execute(
        "SELECT confidence, correct, move_pct, sleeve FROM scores WHERE mode=?",
        (mode,)).fetchall()
    if not rows:
        return None
    out = []
    for lo, hi, label in BUCKETS:
        b = [r for r in rows if r["confidence"] is not None and lo <= r["confidence"] < hi]
        if not b:
            continue
        out.append({"bucket": label, "n": len(b),
                    "hit_rate_pct": round(sum(r["correct"] for r in b) / len(b) * 100, 1),
                    "avg_move_pct": round(sum(r["move_pct"] for r in b) / len(b), 2)})
    hits = sum(r["correct"] for r in rows)
    return {"graded": len(rows),
            "hit_rate_pct": round(hits / len(rows) * 100, 1),
            "avg_move_pct": round(sum(r["move_pct"] for r in rows) / len(rows), 2),
            "buckets": out}


def summary_line(conn, mode: str) -> str:
    """One honest sentence for the review prompt and the dashboard."""
    c = calibration(conn, mode)
    if not c:
        return "No decisions have been graded yet."
    bits = ", ".join(f"{b['bucket']}: {b['hit_rate_pct']}% of {b['n']}" for b in c["buckets"])
    return (f"{c['graded']} graded calls, {c['hit_rate_pct']}% correct overall "
            f"(a coin flip is 50%). By stated confidence — {bits}.")
