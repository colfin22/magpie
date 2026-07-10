"""One tick: detect top-ups -> run each due sleeve -> skim profits -> record."""
import json
import logging
from datetime import datetime, timezone

from . import advisor, config, db, ha, market, portfolio, sleeves

LOGGER = logging.getLogger(__name__)


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
        raw = advisor.ask(prompt, model=config.GEMINI_MODEL_DEEP
                          if sleeve in DEEP_SLEEVES else None)
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
                                  decision["pair"], decision["fraction"], prices)
    except Exception as e:  # noqa: BLE001 - a rejected order must never kill the loop
        conn.execute("UPDATE decisions SET status='error', detail=? WHERE id=?",
                     (str(e), decision_id))
        conn.commit()
        return {"sleeve": sleeve, "status": "error", "detail": str(e)}
    conn.execute("UPDATE decisions SET status='executed' WHERE id=?", (decision_id,))
    conn.commit()
    if mode == "live":
        ha.notify(f"Magpie [{sleeve}] traded",
                  f"{order['side'].upper()} {order['pair']}: €{order['cost_eur']} "
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
            _record(conn, mode, "", "halted", "kill switch is on")
            return {"status": "halted"}

        # a fresh cash deposit on the exchange is split across the active sleeves
        topup = None
        try:
            topup = portfolio.detect_topup(conn, mode)
            if topup:
                ha.notify("Magpie top-up detected",
                          f"€{topup['topup_eur']} new cash split three ways "
                          f"(€{topup['per_sleeve']} per sleeve)")
        except Exception as e:  # noqa: BLE001 - a balance blip must not stop the cycle
            LOGGER.warning("top-up detection failed: %s", e)

        due_now = [s for s in sleeves.ALL if sleeves.due(s, now)]
        for pair in config.PAIRS:
            market.refresh_candles(conn, pair)
            if "swing" in due_now:
                market.refresh_candles(conn, pair, timeframe="4h", limit=200)
        prices = market.tickers(config.PAIRS)
        market_data = [market.summary(conn, p) for p in config.PAIRS]
        extras = {"fear_greed_index": market.fear_greed(),
                  "orderbook_touch": {p: market.touch(p) for p in config.PAIRS}}

        results = [run_sleeve(conn, mode, s, prices, market_data, extras)
                   for s in due_now]
        skims = portfolio.skim_profits(conn, mode, prices)
        ov = portfolio.snapshot_all(conn, mode, prices)
        _track_cycle_outcome(conn, results, crashed=False)
        db.set_setting(conn, "last_cycle_at", _now())
        return {"status": "ok", "mode": mode, "topup": topup, "results": results,
                "skims": skims, "total_eur": ov["total_eur"]}
    except Exception:
        _track_cycle_outcome(conn, [], crashed=True)
        raise
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
            assets = [k for k in v["holdings"] if k != "EUR"]
            bits.append(f"{v['sleeve']} €{v['total_eur']:.2f}"
                        + (f" ({'+'.join(assets)})" if assets else ""))
        msg = (f"[{mode}] €{ov['total_eur']:.2f} ({'+' if delta >= 0 else ''}{delta:.2f} today), "
               f"{trades} trades. " + " · ".join(bits))
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
        prompt = REVIEW_PROMPT.format(
            decisions=json.dumps(decisions, indent=1)[:30000],
            orders=json.dumps(orders, indent=1)[:8000],
            equity=json.dumps(equity, indent=1)[:4000])
        try:
            note = advisor.ask(prompt, model=config.GEMINI_MODEL_DEEP).strip()[:1500]
        except advisor.AdvisorError as e:
            return {"status": "error", "detail": str(e)}
        db.set_setting(conn, "lessons", note)
        db.set_setting(conn, "lessons_at", _now())
        ha.notify("Magpie monthly self-review",
                  note[:220] + ("…" if len(note) > 220 else ""))
        return {"status": "ok", "lessons": note}
    finally:
        conn.close()
