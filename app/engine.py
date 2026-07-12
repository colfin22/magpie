"""One tick: detect top-ups -> run each due sleeve -> skim profits -> record."""
import json
import logging
import os
from datetime import datetime, timedelta, timezone

from . import advisor, arms, config, db, ha, ledger, market, portfolio, scoring, sleeves, stops

LOGGER = logging.getLogger(__name__)

# When a sleeve errors on a transient upstream failure, retry it again in a few
# minutes rather than waiting for the next 6-hourly slot (up to MAX times).
CYCLE_RETRY_MINS = int(os.getenv("CYCLE_RETRY_MINS", "10"))
CYCLE_RETRY_MAX = int(os.getenv("CYCLE_RETRY_MAX", "3"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recent_history(conn, mode: str, sleeve: str, n: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT at, action, pair, fraction, status, reasoning FROM decisions "
        "WHERE mode=? AND sleeve=? ORDER BY id DESC LIMIT ?", (mode, sleeve, n)).fetchall()
    return [dict(r) for r in rows]


def _record(conn, mode, sleeve, status, detail="", prompt=None, raw=None, decision=None) -> int:
    d = decision or {}
    cur = conn.execute(
        "INSERT INTO decisions(at, mode, sleeve, prompt, response_raw, action, pair, fraction, "
        "confidence, reasoning, status, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, sleeve, prompt, raw, d.get("action"), d.get("pair"), d.get("fraction"),
         d.get("confidence"), d.get("reasoning"), status, detail))
    conn.commit()
    return cur.lastrowid


DEEP_SLEEVES = ("quarter", "vault")  # rare decisions get the stronger model


def run_sleeve(conn, mode: str, sleeve: str, prices: dict, market_data: list[dict],
               extras: dict | None = None) -> dict:
    port = portfolio.valued(conn, mode, sleeve, prices)
    # the swing sleeve decides 6-hourly — give it 4h candles alongside the dailies
    data = list(market_data)
    if sleeve == "swing":
        data += [market.summary(conn, p, timeframe="4h") for p in config.PAIRS]
    prompt = advisor.build_prompt(
        port, data, recent_history(conn, mode, sleeve),
        min_order=max(portfolio.min_order_eur(p) for p in config.PAIRS),
        mandate=sleeves.MANDATES[sleeve],
        lessons=db.get_setting(conn, "lessons", "") or "",
        extras=extras)
    try:
        raw = advisor.ask(prompt, deep=sleeve in DEEP_SLEEVES)
    except advisor.AdvisorError as e:
        status = "no_key" if "no GEMINI_API_KEY" in str(e) else "error"
        _record(conn, mode, sleeve, status, str(e), prompt=prompt)
        return {"sleeve": sleeve, "status": status, "detail": str(e)}
    try:
        decision = advisor.validate(raw)
    except advisor.AdvisorError as e:
        _record(conn, mode, sleeve, "invalid", str(e), prompt=prompt, raw=raw)
        return {"sleeve": sleeve, "status": "invalid", "detail": str(e)}

    if decision["action"] == "hold":
        _record(conn, mode, sleeve, "held", "advisor chose to hold", prompt=prompt, raw=raw,
                decision=decision)
        return {"sleeve": sleeve, "status": "held", "reasoning": decision["reasoning"]}

    decision_id = _record(conn, mode, sleeve, "pending", prompt=prompt, raw=raw, decision=decision)
    try:
        order = portfolio.execute(conn, mode, sleeve, decision_id, decision["action"],
                                  decision["pair"], decision["fraction"], prices,
                                  stop_pct=decision.get("stop_loss_pct"))
    except Exception as e:  # noqa: BLE001 - a rejected order must never kill the loop
        conn.execute("UPDATE decisions SET status='error', detail=? WHERE id=?",
                     (str(e), decision_id))
        conn.commit()
        return {"sleeve": sleeve, "status": "error", "detail": str(e)}
    conn.execute("UPDATE decisions SET status='executed' WHERE id=?", (decision_id,))
    conn.commit()
    if mode == "live":
        ha.notify(f"Magpie [{sleeve}] traded",
                  f"{order['side'].upper()} {order['pair']}: {config.symbol()}{order['cost_eur']} "
                  f"@ {order['price']:.2f} — {decision['reasoning'][:140]}")
    return {"sleeve": sleeve, "status": "executed", "order": order,
            "reasoning": decision["reasoning"]}


def _track_cycle_outcome(conn, results: list[dict], crashed: bool) -> None:
    """Count consecutive failed cycles; push HA once when the threshold hits (#2).

    A cycle fails when it crashes or when every due sleeve errored. Held and
    executed decisions both count as the bot working."""
    failed = crashed or (results != [] and all(
        r.get("status") in ("error", "no_key", "invalid") for r in results))
    n = int(db.get_setting(conn, "consecutive_failures", "0") or 0)
    n = n + 1 if failed else 0
    db.set_setting(conn, "consecutive_failures", str(n))
    if n == config.ERROR_ALERT_AFTER:
        ha.notify("Magpie is failing silently",
                  f"{n} decision cycles in a row have failed — check the diary "
                  f"and logs. Trading is NOT halted; the bot just isn't managing.")


def run_cycle(now=None) -> dict:
    conn = db.connect()
    mode = config.mode()
    try:
        if db.get_setting(conn, "halted") == "1":
            # A halt stops the bot TRADING; it does not cancel the resting stops, so
            # one can still fire while halted — and the books must say so (#35).
            fired = []
            try:
                fired = stops.sync(conn, mode, market.tickers(config.PAIRS))
            except Exception as e:  # noqa: BLE001 - a halted cycle must still return cleanly
                LOGGER.warning("stop sync failed during halt: %s", e)
            _record(conn, mode, "", "halted", "kill switch is on")
            return {"status": "halted", "stops_fired": fired}

        # a fresh cash deposit on the exchange is split across the active sleeves
        topup = None
        try:
            topup = portfolio.detect_topup(conn, mode)
            if topup:
                ha.notify("Magpie top-up detected",
                          f"{config.symbol()}{topup['topup_eur']} new cash split three ways "
                          f"({config.symbol()}{topup['per_sleeve']} per sleeve)")
                arms.mirror_topup(conn, topup["topup_eur"], mode)
        except Exception as e:  # noqa: BLE001 - a balance blip must not stop the cycle
            LOGGER.warning("top-up detection failed: %s", e)

        due_now = [s for s in sleeves.ALL if sleeves.due(s, now)]
        prices, market_data, extras = _market_context(conn, "swing" in due_now)

        # Did a stop fire while we were away? Make the books honest BEFORE the brain
        # is asked anything — it must not reason about coins it no longer owns (#35).
        fired = stops.sync(conn, mode, prices)
        for f in fired:
            ha.notify(f"Magpie stop-loss fired [{f['sleeve']}]",
                      f"sold {f['pair']} at {f['price']:.2f} — "
                      f"{config.symbol()}{f['proceeds_eur']} back in cash")

        results = [run_sleeve(conn, mode, s, prices, market_data, extras)
                   for s in due_now]
        skims = portfolio.skim_profits(conn, mode, prices)
        ov = portfolio.snapshot_all(conn, mode, prices)
        ledger.bench_init_if_needed(conn, mode, ov["total_eur"], prices)
        # the shadow arms trade the SAME prices and market data, a beat behind the
        # real bot and entirely walled off from it (#31)
        try:
            arm_results = arms.run_all(conn, mode, due_now, prices, market_data)
        except Exception as e:  # noqa: BLE001 - shadows are never worth a failed cycle
            LOGGER.warning("shadow arms failed: %s", e)
            arm_results = []
        # NB arms are deliberately absent from _track_cycle_outcome: only the real
        # bot's health raises an alarm.
        _track_cycle_outcome(conn, results, crashed=False)
        db.set_setting(conn, "last_cycle_at", _now())
        _note_retry_state(conn, results, fresh_cycle=True)
        return {"status": "ok", "mode": mode, "topup": topup, "results": results,
                "skims": skims, "total_eur": ov["total_eur"], "arms": arm_results,
                "stops_fired": fired}
    except Exception:
        _track_cycle_outcome(conn, [], crashed=True)
        raise
    finally:
        conn.close()


def _market_context(conn, include_swing_4h: bool):
    """Refresh candles and gather the price/market/extras pack a decision needs."""
    for pair in config.PAIRS:
        market.refresh_candles(conn, pair)
        if include_swing_4h:
            market.refresh_candles(conn, pair, timeframe="4h", limit=200)
    prices = market.tickers(config.PAIRS)
    market_data = [market.summary(conn, p) for p in config.PAIRS]
    extras = {"fear_greed_index": market.fear_greed(),
              "orderbook_touch": {p: market.touch(p) for p in config.PAIRS}}
    # every one of these is optional garnish: a dead feed leaves its key absent
    # from extras (or None) and must never fail a cycle (#34)
    if config.CONTEXT_FUNDING:
        extras["perp_funding_and_open_interest"] = market.funding(config.PAIRS)
    if config.CONTEXT_DEPTH:
        extras["orderbook_depth"] = {p: market.depth(p) for p in config.PAIRS}
    news = market.headlines()
    if news:
        extras["recent_headlines"] = news
    return prices, market_data, extras


def _failed_sleeves(results: list[dict]) -> list[str]:
    """Sleeves that errored on a transient failure (worth a soon retry).

    'no_key'/'invalid' won't fix themselves, so they don't schedule a retry."""
    return [r["sleeve"] for r in results
            if r.get("status") == "error" and r.get("sleeve")]


def _note_retry_state(conn, results: list[dict], *, fresh_cycle: bool) -> None:
    """Record whether a short retry is pending. The retry itself is driven by
    the magpie-retry systemd timer (restart-proof), not an in-process timer;
    this only tracks the attempt budget and an indicative next-retry time for
    the dashboard. A fresh scheduled cycle grants a new budget."""
    if fresh_cycle:
        db.set_setting(conn, "retry_attempts", "0")
    failed = _failed_sleeves(results)
    pending = bool(failed) and int(db.get_setting(conn, "retry_attempts") or 0) < CYCLE_RETRY_MAX
    if pending:
        when = datetime.now(timezone.utc) + timedelta(minutes=CYCLE_RETRY_MINS)
        db.set_setting(conn, "retry_cycle_at", when.isoformat(timespec="seconds"))
    else:
        db.set_setting(conn, "retry_cycle_at", "")


def retry_sleeves(conn, mode: str, sleeve_names: list[str]) -> list[dict]:
    """Re-run specific sleeves off-schedule (bypasses the due() cadence gate)."""
    names = [s for s in sleeve_names if s in sleeves.ALL]
    if not names:
        return []
    prices, market_data, extras = _market_context(conn, "swing" in names)
    return [run_sleeve(conn, mode, s, prices, market_data, extras) for s in names]


def _latest_failed_sleeves(conn, mode: str) -> list[str]:
    """Sleeves whose most recent decision errored (candidates to retry now)."""
    out = []
    for s in sleeves.ALL:
        row = conn.execute(
            "SELECT status FROM decisions WHERE mode=? AND sleeve=? ORDER BY id DESC LIMIT 1",
            (mode, s)).fetchone()
        if row and row["status"] == "error":
            out.append(s)
    return out


def retry_now(force: bool = False) -> dict:
    """Retry every sleeve whose latest decision errored. Driven every few
    minutes by the magpie-retry systemd timer; a no-op when nothing failed.

    Bounded by CYCLE_RETRY_MAX consecutive attempts so a sustained outage
    doesn't retry forever (each scheduled cycle resets the budget); `force`
    (a manual retry) bypasses the cap."""
    conn = db.connect()
    mode = config.mode()
    try:
        if db.get_setting(conn, "halted") == "1":
            return {"status": "halted"}
        failed = _latest_failed_sleeves(conn, mode)
        if not failed:
            db.set_setting(conn, "retry_attempts", "0")
            db.set_setting(conn, "retry_cycle_at", "")
            return {"status": "nothing-to-retry"}
        attempts = int(db.get_setting(conn, "retry_attempts") or 0)
        if attempts >= CYCLE_RETRY_MAX and not force:
            db.set_setting(conn, "retry_cycle_at", "")  # wait for the next scheduled cycle
            return {"status": "exhausted", "attempts": attempts}
        results = retry_sleeves(conn, mode, failed)
        db.set_setting(conn, "last_cycle_at", _now())
        _track_cycle_outcome(conn, results, crashed=False)
        db.set_setting(conn, "retry_attempts", str(attempts + 1))
        _note_retry_state(conn, results, fresh_cycle=False)
        return {"status": "ok", "retried": failed, "attempt": attempts + 1,
                "results": results}
    finally:
        conn.close()


def daily_digest() -> dict:
    conn = db.connect()
    mode = config.mode()
    try:
        prices = market.tickers(config.PAIRS)
        ov = portfolio.overview(conn, mode, prices)
        first = conn.execute(
            "SELECT SUM(total_eur) s FROM snapshots WHERE mode=? AND at >= date('now') "
            "AND id IN (SELECT MIN(id) FROM snapshots WHERE mode=? AND at >= date('now') GROUP BY sleeve)",
            (mode, mode)).fetchone()
        day0 = first["s"] if first and first["s"] else ov["total_eur"]
        delta = ov["total_eur"] - day0
        trades = conn.execute("SELECT COUNT(*) c FROM orders WHERE at >= date('now')").fetchone()["c"]
        bits = []
        for v in ov["sleeves"]:
            assets = [k for k in v["holdings"] if k != config.BASE_CURRENCY]
            bits.append(f"{v['sleeve']} {config.symbol()}{v['total_eur']:.2f}"
                        + (f" ({'+'.join(assets)})" if assets else ""))
        bench = ledger.bench_value(conn, mode, prices)
        vs = ""
        if bench:
            edge = ov["total_eur"] - bench["hodl_eur"]
            vs = f" · vs hodl {config.symbol()}{bench['hodl_eur']:.2f} ({'+' if edge >= 0 else ''}{edge:.2f})"
        msg = (f"[{mode}] {config.symbol()}{ov['total_eur']:.2f} ({'+' if delta >= 0 else ''}{delta:.2f} today), "
               f"{trades} trades{vs}. " + " · ".join(bits))
        pushed = ha.notify("Magpie daily digest", msg)
        return {"pushed": pushed, "summary": msg}
    finally:
        conn.close()


REVIEW_PROMPT = """You are the portfolio manager reviewing YOUR OWN past month of
cryptocurrency trading decisions. Below is your decision log (with your reasoning
at the time), the orders that executed, and the equity progression.

Write yourself a concise lessons note (max 200 words) that will be injected into
all your future decision prompts. Focus on what your log shows actually worked
and what didn't: patterns you should repeat, mistakes you should not, market
conditions you misread. Be specific and honest — this note is the only memory
you will carry forward.

Decision log:
{decisions}

Orders:
{orders}

Equity snapshots (daily):
{equity}

How your past calls actually turned out, scored at each sleeve's horizon (this is
measured, not remembered — trust it over your recollection of your own reasoning):
{calibration}

Answer with ONLY the lessons note text, no preamble."""


def self_review() -> dict:
    """Monthly: distil the ledger into a lessons note for future prompts."""
    conn = db.connect()
    mode = config.mode()
    try:
        decisions = [dict(r) for r in conn.execute(
            "SELECT at, sleeve, action, pair, fraction, status, reasoning FROM decisions "
            "WHERE mode=? AND at >= date('now','-31 days') ORDER BY id", (mode,))]
        orders = [dict(r) for r in conn.execute(
            "SELECT at, sleeve, pair, side, price, cost, fee FROM orders "
            "WHERE mode=? AND at >= date('now','-31 days') ORDER BY id", (mode,))]
        equity = [dict(r) for r in conn.execute(
            "SELECT substr(at,1,10) day, ROUND(SUM(total_eur),2) eur FROM snapshots "
            "WHERE mode=? AND at >= date('now','-31 days') GROUP BY day, sleeve "
            "HAVING id=MAX(id)", (mode,))]
        if not decisions:
            return {"status": "skipped", "detail": "no decisions to review yet"}
        trips = ledger.round_trips(conn, mode)
        stats = ledger.trip_stats(trips)
        prices = market.tickers(config.PAIRS)
        bench = ledger.bench_value(conn, mode, prices)
        orders_extra = {"closed_round_trips": trips[:40], "stats": stats,
                        "buy_and_hold_benchmark": bench}
        prompt = REVIEW_PROMPT.format(
            decisions=json.dumps(decisions, indent=1)[:30000],
            orders=json.dumps({"orders": orders, **orders_extra}, indent=1)[:12000],
            equity=json.dumps(equity, indent=1)[:4000],
            calibration=scoring.summary_line(conn, mode))
        try:
            note = advisor.ask(prompt, deep=True).strip()[:1500]
        except advisor.AdvisorError as e:
            return {"status": "error", "detail": str(e)}
        db.set_setting(conn, "lessons", note)
        db.set_setting(conn, "lessons_at", _now())
        ha.notify("Magpie monthly self-review",
                  note[:220] + ("…" if len(note) > 220 else ""))
        return {"status": "ok", "lessons": note}
    finally:
        conn.close()
