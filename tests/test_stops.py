"""Exchange-side stop-losses (#35).

The dangerous properties, and so the ones tested hardest:
  - an orphaned stop would sell a DIFFERENT sleeve's coins (one real account,
    virtual sleeve books), so a sell must cancel its stops first — or not sell;
  - a fired stop must be booked as a SALE, never silently absorbed as drift.
"""
import os
import tempfile

import pytest

from app import advisor, config, db, ledger, market, portfolio, stops

PRICES = {"BTC/EUR": 100_000.0}


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return db.connect(path), path


@pytest.fixture(autouse=True)
def _market(monkeypatch):
    monkeypatch.setattr(portfolio, "min_order_eur", lambda pair: 1.0)
    monkeypatch.setattr(market, "touch", lambda pair: {
        "bid": PRICES[pair], "ask": PRICES[pair], "last": PRICES[pair], "spread_pct": 0.0})
    monkeypatch.setattr(config, "PAIRS", ["BTC/EUR"], raising=False)
    monkeypatch.setattr(config, "STOP_LOSS_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "STOP_LOSS_PCT", 8.0, raising=False)
    monkeypatch.setattr(config, "STOP_LOSS_MIN_PCT", 2.0, raising=False)
    monkeypatch.setattr(config, "STOP_LOSS_MAX_PCT", 30.0, raising=False)


def buy(conn, sleeve="swing", fraction=0.9, stop_pct=None):
    return portfolio.execute(conn, "paper", sleeve, 1, "buy", "BTC/EUR", fraction, PRICES,
                             stop_pct=stop_pct)


def test_off_by_default_places_nothing(monkeypatch):
    monkeypatch.setattr(config, "STOP_LOSS_ENABLED", False, raising=False)
    conn, p = make_db()
    try:
        assert buy(conn)["stop"] is None
        assert conn.execute("SELECT COUNT(*) c FROM stops").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_a_buy_rests_a_stop_below_entry():
    conn, p = make_db()
    try:
        st = buy(conn)["stop"]
        assert st["stop_price"] == 92_000.0 and st["pct"] == 8.0   # 8% under the fill
        assert len(stops.open_stops(conn, "paper", "swing", "BTC/EUR")) == 1
    finally:
        conn.close(); os.unlink(p)


def test_the_brain_proposes_and_the_config_clamps():
    assert stops.clamp_pct(0.5) == 2.0      # absurdly tight -> clamped up
    assert stops.clamp_pct(99) == 30.0      # absurdly wide -> clamped down
    assert stops.clamp_pct(12) == 12.0      # sensible -> respected
    assert stops.clamp_pct(None) == 8.0     # silent -> the configured default


def test_a_junk_stop_from_the_model_never_kills_a_good_decision():
    d = advisor.validate('{"action":"buy","pair":"BTC/EUR","fraction":0.5,'
                         '"confidence":0.8,"stop_loss_pct":"nonsense","reasoning":"x"}')
    assert d["action"] == "buy" and d["stop_loss_pct"] is None


def test_selling_cancels_that_sleeves_stop_first():
    conn, p = make_db()
    try:
        buy(conn)
        portfolio.execute(conn, "paper", "swing", 2, "sell", "BTC/EUR", 1.0, PRICES)
        assert stops.open_stops(conn, "paper", "swing", "BTC/EUR") == []
        row = conn.execute("SELECT status FROM stops").fetchone()
        assert row["status"] == "cancelled"     # not left resting on nobody's coins
    finally:
        conn.close(); os.unlink(p)


def test_a_sell_is_REFUSED_if_the_stop_will_not_cancel(monkeypatch):
    """The orphan footgun: selling anyway would leave a stop resting over
    another sleeve's coins. Refusing to sell is the safe failure."""
    conn, p = make_db()
    try:
        buy(conn)
        monkeypatch.setattr(stops, "cancel", lambda c, s: False)   # exchange won't let go
        with pytest.raises(ValueError, match="orphan"):
            portfolio.execute(conn, "paper", "swing", 2, "sell", "BTC/EUR", 1.0, PRICES)
        assert portfolio.holdings(conn, "paper", "swing").get("BTC")   # still held — nothing sold
        assert len(stops.open_stops(conn, "paper", "swing")) == 1     # stop still ours
    finally:
        conn.close(); os.unlink(p)


def test_one_sleeves_stop_is_untouched_by_anothers_sell():
    conn, p = make_db()
    try:
        buy(conn, sleeve="swing")
        buy(conn, sleeve="fortnight")
        portfolio.execute(conn, "paper", "swing", 3, "sell", "BTC/EUR", 1.0, PRICES)
        assert stops.open_stops(conn, "paper", "swing") == []
        assert len(stops.open_stops(conn, "paper", "fortnight")) == 1   # not collateral damage
    finally:
        conn.close(); os.unlink(p)


def test_a_fired_stop_is_booked_as_a_sale_with_an_audit_row():
    conn, p = make_db()
    try:
        buy(conn)
        before = portfolio.holdings(conn, "paper", "swing")
        fired = stops.sync(conn, "paper", {"BTC/EUR": 91_000.0})   # gapped below the 92k stop
        assert len(fired) == 1
        h = portfolio.holdings(conn, "paper", "swing")
        assert h.get("BTC", 0) == 0 and h["EUR"] > 0        # position closed, cash back
        assert before["BTC"] > 0
        d = conn.execute("SELECT * FROM decisions WHERE detail='stop-loss'").fetchone()
        assert d["action"] == "sell" and d["status"] == "executed"   # visible in the diary...
        o = conn.execute("SELECT * FROM orders WHERE side='sell'").fetchone()
        assert o is not None                                          # ...and in the ledger
        assert conn.execute("SELECT status FROM stops").fetchone()["status"] == "filled"
    finally:
        conn.close(); os.unlink(p)


def test_a_stop_that_has_not_been_hit_stays_resting():
    conn, p = make_db()
    try:
        buy(conn)
        assert stops.sync(conn, "paper", {"BTC/EUR": 95_000.0}) == []   # above the stop
        assert len(stops.open_stops(conn, "paper")) == 1
    finally:
        conn.close(); os.unlink(p)


def test_live_places_a_real_stop_order_and_books_its_fill(monkeypatch):
    calls = {}

    class Ex:
        def amount_to_precision(self, pair, a):
            return f"{a:.8f}"

        def price_to_precision(self, pair, p):
            return f"{p:.1f}"

        def create_order(self, pair, type_, side, amount, price=None, params=None):
            calls["order"] = (pair, type_, side, amount, price, params or {})
            return {"id": "OABC-123"}

        def fetch_order(self, oid, pair):
            calls["fetched"] = oid
            return {"status": "closed", "filled": 0.001, "average": 91_500.0}

    monkeypatch.setattr(market, "exchange", lambda: Ex())
    conn, p = make_db()
    try:
        conn.execute("UPDATE holdings SET amount=100 WHERE mode='live' AND sleeve='swing' AND asset='EUR'")
        conn.commit()
        portfolio.execute(conn, "live", "swing", 1, "buy", "BTC/EUR", 0.9, PRICES, stop_pct=10)
        pair, type_, side, amount, price, params = calls["order"]
        # ccxt's unified shape: market sell + stopLossPrice. Sending type="stop-loss" with a
        # positional price is silently wrong — Kraken rejects it ("Invalid arguments:price"),
        # which is exactly what a live backfill hit.
        assert (pair, type_, side) == ("BTC/EUR", "market", "sell")
        assert price is None
        assert params["stopLossPrice"] == 90_000.0                        # 10% under entry
        st = conn.execute("SELECT * FROM stops").fetchone()
        assert st["exchange_id"] == "OABC-123"

        fired = stops.sync(conn, "live", PRICES)     # exchange says it filled
        assert fired[0]["price"] == 91_500.0         # booked at the ACTUAL fill, not the trigger
        assert conn.execute("SELECT status FROM stops").fetchone()["status"] == "filled"
    finally:
        conn.close(); os.unlink(p)


def test_an_unplaceable_stop_does_not_undo_the_buy(monkeypatch):
    """The buy already happened and is real money on the exchange. A stop that
    will not place is a shame, not a reason to pretend the buy did not occur."""
    class Ex:
        def amount_to_precision(self, pair, a):
            return f"{a:.8f}"

        def price_to_precision(self, pair, p):
            return f"{p:.1f}"

        def create_order(self, pair, type_, side, amount, price=None, params=None):
            if (params or {}).get("stopLossPrice"):
                raise RuntimeError("exchange rejected the stop")
            return {"id": "BUY-1"}          # the buy itself goes through fine

    monkeypatch.setattr(market, "exchange", lambda: Ex())
    monkeypatch.setattr(portfolio, "_live_fill", lambda pair, side, amount, px, *a, **k: {
        "id": "BUY-1", "filled": amount, "cost": amount * px, "price": px,
        "fee_quote": amount * px * config.MAKER_FEE, "fee_base": 0.0})
    conn, p = make_db()
    try:
        conn.execute("UPDATE holdings SET amount=100 WHERE mode='live' AND sleeve='swing' AND asset='EUR'")
        conn.commit()
        out = portfolio.execute(conn, "live", "swing", 1, "buy", "BTC/EUR", 0.9, PRICES)
        assert out["stop"] is None                                   # no stop...
        assert portfolio.holdings(conn, "live", "swing").get("BTC")  # ...but the buy stands
        assert conn.execute("SELECT COUNT(*) c FROM stops").fetchone()["c"] == 0
    finally:
        conn.close(); os.unlink(p)


def test_a_fired_stop_is_claimed_before_reconcile_can_absorb_it(monkeypatch):
    """reconcile runs at 05:45, the cycle at 06:00. If reconcile absorbed the
    fill as 'drift' the sale would vanish from the diary and then be booked
    twice. sync must get there first — this pins the ordering."""
    monkeypatch.setattr(market, "tickers", lambda pairs: {"BTC/EUR": 91_000.0})
    conn, p = make_db()
    try:
        buy(conn)
        fired = stops.sync(conn, "paper", {"BTC/EUR": 91_000.0})   # claim it as a SALE first
        assert fired and conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE side='sell'").fetchone()["c"] == 1
        # reconcile now sees books that already match reality: nothing left to absorb
        again = stops.sync(conn, "paper", {"BTC/EUR": 91_000.0})
        assert again == []                                          # and never books it twice
    finally:
        conn.close(); os.unlink(p)


def test_cancel_all_clears_the_book():
    conn, p = make_db()
    try:
        buy(conn, sleeve="swing")
        buy(conn, sleeve="fortnight")
        assert stops.cancel_all(conn, "paper") == 2
        assert stops.open_stops(conn, "paper") == []
    finally:
        conn.close(); os.unlink(p)


# ---------- the partial sell: the remainder must not ride naked ----------

def test_a_partial_sell_re_rests_a_stop_over_the_remainder():
    """The brain sells fractions all the time ('SELL SOL 40%'). The sell cancels
    the sleeve's stop (the orphan guard) — so without re-resting one, the 60%
    left behind silently loses its floor until that sleeve next buys the coin."""
    conn, p = make_db()
    try:
        buy(conn)                                        # stop at 92_000 (8% under 100k)
        before = portfolio.holdings(conn, "paper", "swing")["BTC"]
        portfolio.execute(conn, "paper", "swing", 2, "sell", "BTC/EUR", 0.4, PRICES)

        left = portfolio.holdings(conn, "paper", "swing")["BTC"]
        assert 0 < left < before                         # a partial exit
        open_now = stops.open_stops(conn, "paper", "swing", "BTC/EUR")
        assert len(open_now) == 1                        # the remainder is protected again
        assert open_now[0]["stop_price"] == 92_000.0     # at the ORIGINAL floor, not a new one
        assert abs(open_now[0]["amount"] - left) < 1e-9  # covering exactly what is left
    finally:
        conn.close(); os.unlink(p)


def test_a_full_sell_leaves_no_stop_behind():
    conn, p = make_db()
    try:
        buy(conn)
        portfolio.execute(conn, "paper", "swing", 2, "sell", "BTC/EUR", 1.0, PRICES)
        assert stops.open_stops(conn, "paper", "swing") == []   # nothing left to protect
    finally:
        conn.close(); os.unlink(p)


def test_a_dust_remainder_gets_no_stop():
    """Resting an exchange order over 20 cents of coin is pointless."""
    conn, p = make_db()
    try:
        buy(conn)
        portfolio.execute(conn, "paper", "swing", 2, "sell", "BTC/EUR", 0.9995, PRICES)
        assert stops.open_stops(conn, "paper", "swing") == []
    finally:
        conn.close(); os.unlink(p)


def test_the_re_rested_stop_still_fires():
    conn, p = make_db()
    try:
        buy(conn)
        portfolio.execute(conn, "paper", "swing", 2, "sell", "BTC/EUR", 0.4, PRICES)
        fired = stops.sync(conn, "paper", {"BTC/EUR": 91_000.0})     # gaps below the floor
        assert len(fired) == 1
        assert portfolio.holdings(conn, "paper", "swing").get("BTC", 0) == 0
    finally:
        conn.close(); os.unlink(p)


# --- #67: a failed sell must not leave the position naked ---------------------

def test_a_failed_sell_re_rests_the_stop(monkeypatch):
    """execute() cancels the sleeve's stop at Kraken AND COMMITS before the sell can
    still fail. Nothing put it back, so any later failure -- a rejected order, a network
    blip -- left a REAL position with no floor, silently, until that sleeve happened to
    buy the same coin again (#67)."""
    conn, p = make_db()
    try:
        monkeypatch.setattr(config, "STOP_LOSS_ENABLED", True)
        portfolio._set(conn, "paper", "swing", "BTC", 1.0)
        portfolio._set(conn, "paper", "swing", "EUR", 0.0)
        conn.commit()
        placed = stops.place(conn, "paper", "swing", "BTC/EUR", 1.0, 100.0, 10.0)
        assert placed and len(stops.open_stops(conn, "paper", "swing", "BTC/EUR")) == 1

        # make the sell blow up AFTER the stop has been cancelled
        def boom(*a, **k):
            raise RuntimeError("kraken rejected the order")

        monkeypatch.setattr(portfolio, "_set", boom)
        monkeypatch.setattr(portfolio.market, "touch",
                            lambda pair: {"bid": 100.0, "ask": 100.0, "last": 100.0})

        with pytest.raises(RuntimeError):
            portfolio.execute(conn, "paper", "swing", 1, "sell", "BTC/EUR", 1.0,
                              {"BTC/EUR": 100.0})

        # the failure surfaced -- AND the floor is back under the position
        still = stops.open_stops(conn, "paper", "swing", "BTC/EUR")
        assert len(still) == 1, "the stop was cancelled and never re-rested -- naked position"
        assert still[0]["stop_price"] == placed["stop_price"], "re-rested at the wrong trigger"
    finally:
        conn.close(); os.unlink(p)
