"""One tick: detect top-ups -> run each due sleeve -> skim profits -> record."""
import json
import logging
from datetime import datetime, timezone

from . import advisor, config, db, ha, market, portfolio, sleeves

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recent_history(conn, sleeve: str, n: int = 10) -> list[dict]:
    rows = conn.execute(
        "SELECT at, action, pair, fraction, status, reasoning FROM decisions "
        "WHERE sleeve=? ORDER BY id DESC LIMIT ?", (sleeve, n)).fetchall()
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


def run_sleeve(conn, mode: str, sleeve: str, prices: dict, market_data: list[dict]) -> dict:
    port = portfolio.valued(conn, mode, sleeve, prices)
    prompt = advisor.build_prompt(
        port, market_data, recent_history(conn, sleeve),
        min_order=max(portfolio.min_order_eur(p) for p in config.PAIRS),
        mandate=sleeves.MANDATES[sleeve])
    try:
        raw = advisor.ask(prompt)
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

        for pair in config.PAIRS:
            market.refresh_candles(conn, pair)
        prices = market.tickers(config.PAIRS)
        market_data = [market.summary(conn, p) for p in config.PAIRS]

        results = [run_sleeve(conn, mode, s, prices, market_data)
                   for s in sleeves.ALL if sleeves.due(s, now)]
        skims = portfolio.skim_profits(conn, mode, prices)
        ov = portfolio.snapshot_all(conn, mode, prices)
        return {"status": "ok", "mode": mode, "topup": topup, "results": results,
                "skims": skims, "total_eur": ov["total_eur"]}
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
