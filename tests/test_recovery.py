"""Recovery from a crash mid-fill (#40).

execute() can spend 90s waiting for a maker fill. A deploy or crash inside that
window leaves the decision `pending` and possibly a LIVE order resting at Kraken
that the books have never seen. If it fills, the coins arrive with no audit row
and the nightly reconcile launders them into "drift" — attributed to whichever
sleeves happen to hold that asset, which are the wrong ones.
"""
import os
import tempfile

from app import config, db, market, portfolio


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


def pending_decision(conn, sleeve="quarter", pair="TRX/EUR", side="buy"):
    cur = conn.execute(
        "INSERT INTO decisions(at, mode, sleeve, action, pair, fraction, status) "
        "VALUES('2026-07-13T05:42:51+00:00','live',?,?,?,0.5,'pending')", (sleeve, side, pair))
    conn.execute("INSERT INTO inflight(exchange_id, decision_id, at, mode, sleeve, pair, side) "
                 "VALUES('OFNJQ7',?,'2026-07-13T05:42:51+00:00','live',?,?,?)",
                 (cur.lastrowid, sleeve, pair, side))
    conn.commit()
    return cur.lastrowid


class Ex:
    """A fake Kraken with one in-flight order."""

    def __init__(self, status, filled, cost=0.0, fee=0.0):
        self.o = {"id": "OFNJQ7", "status": status, "filled": filled, "cost": cost,
                  "price": 0.29, "fee": {"cost": fee, "currency": "EUR"}}
        self.cancelled = []

    def fetch_order(self, oid, pair):
        return self.o

    def cancel_order(self, oid, pair):
        self.cancelled.append(oid)
        self.o["status"] = "canceled"


def test_an_order_that_filled_while_we_were_dead_is_adopted(monkeypatch):
    """It filled. The coins are real. Book them — with an audit row — rather than
    letting reconcile launder them into anonymous drift."""
    conn, p = make_db()
    ex = Ex("closed", filled=54.6912871, cost=15.8604, fee=0.0635)
    monkeypatch.setattr(market, "exchange", lambda: ex)
    try:
        conn.execute("UPDATE holdings SET amount=31.80 WHERE mode='live' AND sleeve='quarter' AND asset='EUR'")
        conn.commit()
        did = pending_decision(conn)
        out = portfolio.recover_inflight(conn, "live")

        assert out[0]["outcome"] == "adopted"
        h = portfolio.holdings(conn, "live", "quarter")
        assert round(h["TRX"], 7) == 54.6912871              # exactly what the exchange gave us
        assert round(h["EUR"], 4) == round(31.80 - (15.8604 + 0.0635), 4)
        o = conn.execute("SELECT * FROM orders WHERE decision_id=?", (did,)).fetchone()
        assert o["side"] == "buy" and o["exchange_id"] == "OFNJQ7"    # the audit row exists
        d = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        assert d["status"] == "executed" and "recovered" in d["detail"]
        assert conn.execute("SELECT COUNT(*) c FROM inflight").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_an_order_still_resting_is_cancelled_not_left_orphaned(monkeypatch):
    """Still open. Nobody is coming back for it — cancel it, or it fills later with
    no audit trail and the books never know."""
    conn, p = make_db()
    ex = Ex("open", filled=0.0)
    monkeypatch.setattr(market, "exchange", lambda: ex)
    try:
        did = pending_decision(conn)
        before = portfolio.holdings(conn, "live", "quarter")
        out = portfolio.recover_inflight(conn, "live")

        assert out[0]["outcome"] == "cancelled"
        assert ex.cancelled == ["OFNJQ7"]                      # actually cancelled at the exchange
        assert portfolio.holdings(conn, "live", "quarter") == before   # no money moved
        d = conn.execute("SELECT * FROM decisions WHERE id=?", (did,)).fetchone()
        assert d["status"] == "error" and "no money moved" in d["detail"]
        assert conn.execute("SELECT COUNT(*) c FROM inflight").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_a_partial_fill_is_booked_for_exactly_what_filled(monkeypatch):
    conn, p = make_db()
    ex = Ex("canceled", filled=20.0, cost=5.8, fee=0.02)      # only part of it got done
    monkeypatch.setattr(market, "exchange", lambda: ex)
    try:
        conn.execute("UPDATE holdings SET amount=31.80 WHERE mode='live' AND sleeve='quarter' AND asset='EUR'")
        conn.commit()
        pending_decision(conn)
        portfolio.recover_inflight(conn, "live")
        h = portfolio.holdings(conn, "live", "quarter")
        assert h["TRX"] == 20.0                                # what filled, not what was asked
        assert round(h["EUR"], 4) == round(31.80 - (5.8 + 0.02), 4)
    finally:
        conn.close(); os.unlink(p)


def test_a_trade_that_completed_normally_is_left_alone(monkeypatch):
    """If execute() already booked it, recovery must not book it twice."""
    conn, p = make_db()
    ex = Ex("closed", filled=54.0, cost=15.0, fee=0.05)
    monkeypatch.setattr(market, "exchange", lambda: ex)
    try:
        did = pending_decision(conn)
        conn.execute("INSERT INTO orders(at, mode, sleeve, decision_id, pair, side, amount, price, "
                     "cost, fee) VALUES('x','live','quarter',?,'TRX/EUR','buy',54.0,0.29,15.0,0.05)",
                     (did,))
        conn.commit()
        assert portfolio.recover_inflight(conn, "live") == []          # nothing to do
        assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 1   # not doubled
        assert conn.execute("SELECT COUNT(*) c FROM inflight").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_paper_mode_has_nothing_to_recover(monkeypatch):
    conn, p = make_db()
    monkeypatch.setattr(market, "exchange", lambda: (_ for _ in ()).throw(
        AssertionError("paper recovery must never call the exchange")))
    try:
        assert portfolio.recover_inflight(conn, "paper") == []
    finally:
        conn.close(); os.unlink(p)
