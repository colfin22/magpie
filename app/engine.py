"""One decision cycle: market -> context -> Gemini -> validate -> execute -> record."""
import json
import logging
from datetime import datetime, timezone

from . import advisor, config, db, ha, market, portfolio

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recent_history(conn, n: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT at, action, pair, fraction, status, reasoning FROM decisions "
        "ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    return [dict(r) for r in rows]


def _record(conn, mode, status, detail="", prompt=None, raw=None, decision=None) -> int:
    d = decision or {}
    cur = conn.execute(
        "INSERT INTO decisions(at, mode, prompt, response_raw, action, pair, fraction, "
        "confidence, reasoning, status, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, prompt, raw, d.get("action"), d.get("pair"), d.get("fraction"),
         d.get("confidence"), d.get("reasoning"), status, detail))
    conn.commit()
    return cur.lastrowid


def run_cycle() -> dict:
    conn = db.connect()
    mode = config.mode()
    try:
        if db.get_setting(conn, "halted") == "1":
            _record(conn, mode, "halted", "kill switch is on")
            return {"status": "halted"}

        # market refresh (public endpoints — fine in either mode)
        for pair in config.PAIRS:
            market.refresh_candles(conn, pair)
        prices = market.tickers(config.PAIRS)
        market_data = [market.summary(conn, p) for p in config.PAIRS]
        port = portfolio.valued(conn, mode, prices)

        prompt = advisor.build_prompt(
            port, market_data, recent_history(conn),
            min_order=max(portfolio.min_order_eur(p) for p in config.PAIRS))
        try:
            raw = advisor.ask(prompt)
        except advisor.AdvisorError as e:
            status = "no_key" if "no GEMINI_API_KEY" in str(e) else "error"
            _record(conn, mode, status, str(e), prompt=prompt)
            portfolio.snapshot(conn, mode, prices)
            return {"status": status, "detail": str(e)}

        try:
            decision = advisor.validate(raw)
        except advisor.AdvisorError as e:
            _record(conn, mode, "invalid", str(e), prompt=prompt, raw=raw)
            portfolio.snapshot(conn, mode, prices)
            return {"status": "invalid", "detail": str(e)}

        if decision["action"] == "hold":
            _record(conn, mode, "held", "advisor chose to hold", prompt=prompt, raw=raw,
                    decision=decision)
            snap = portfolio.snapshot(conn, mode, prices)
            return {"status": "held", "reasoning": decision["reasoning"],
                    "total_eur": snap["total_eur"]}

        decision_id = _record(conn, mode, "pending", prompt=prompt, raw=raw, decision=decision)
        try:
            order = portfolio.execute(conn, mode, decision_id, decision["action"],
                                      decision["pair"], decision["fraction"], prices)
        except Exception as e:  # noqa: BLE001 - a rejected order must never kill the loop
            conn.execute("UPDATE decisions SET status='error', detail=? WHERE id=?",
                         (str(e), decision_id))
            conn.commit()
            portfolio.snapshot(conn, mode, prices)
            return {"status": "error", "detail": str(e)}

        conn.execute("UPDATE decisions SET status='executed' WHERE id=?", (decision_id,))
        conn.commit()
        snap = portfolio.snapshot(conn, mode, prices)
        if mode == "live":
            ha.notify("Magpie traded",
                      f"{order['side'].upper()} {order['pair']}: €{order['cost_eur']} "
                      f"@ {order['price']:.2f} — {decision['reasoning'][:140]} "
                      f"(portfolio €{snap['total_eur']})")
        return {"status": "executed", "order": order, "total_eur": snap["total_eur"],
                "reasoning": decision["reasoning"]}
    finally:
        conn.close()


def daily_digest() -> dict:
    """Evening summary push: equity, day movement, decisions taken."""
    conn = db.connect()
    mode = config.mode()
    try:
        prices = market.tickers(config.PAIRS)
        snap = portfolio.valued(conn, mode, prices)
        first = conn.execute(
            "SELECT total_eur FROM snapshots WHERE mode=? AND at >= date('now') ORDER BY id LIMIT 1",
            (mode,)).fetchone()
        day0 = first["total_eur"] if first else snap["total_eur"]
        n = conn.execute("SELECT COUNT(*) c FROM decisions WHERE at >= date('now')").fetchone()["c"]
        trades = conn.execute("SELECT COUNT(*) c FROM orders WHERE at >= date('now')").fetchone()["c"]
        delta = snap["total_eur"] - day0
        msg = (f"[{mode}] €{snap['total_eur']:.2f} ({'+' if delta >= 0 else ''}{delta:.2f} today) — "
               f"{n} decisions, {trades} trades. Holdings: "
               + ", ".join(f"{k}" for k in snap["holdings"]))
        pushed = ha.notify("Magpie daily digest", msg)
        return {"pushed": pushed, "summary": msg}
    finally:
        conn.close()
