"""Shadow arms: control strategies traded in simulation beside the live bot (#31).

The point is measurement. With one bot there is no way to tell skill from luck
or from a rising market: an arm is a rival strategy that sees exactly the same
market at exactly the same instant and trades the same books with the same
fees — so any difference in the equity curves is the difference in the
decisions, and nothing else.

An arm is just another value in the `mode` column, so the sleeves, skims,
snapshots, round trips and equity history all work on it unchanged, and
portfolio.execute() only ever reaches Kraken when mode == "live". Nothing here
can place a real order.

The rule deciders return exactly the dict advisor.validate() returns, so they
flow through the identical execute path as the LLM's decisions.

Configured by SHADOW_ARMS, comma-separated `name:kind:spec`:

    rule arms:  ema:rule:ema20,dca:rule:dca,coinflip:rule:random
    llm arms:   claude:llm:openrouter@anthropic/claude-sonnet-5

A `rule` arm's spec names a decider. An `llm` arm's spec is `provider@model`
(model optional) — it gets the IDENTICAL prompt the live brain gets, same
mandate, same market data, same validation. Only the model differs, which is
what makes it a fair bake-off: whatever the equity curves do, the prompt was
not the variable.

Empty (the default) = no arms, and not one line of the live path changes.
"""
import json
import logging
import random
from datetime import datetime, timezone

from . import advisor, config, db, ledger, portfolio, sleeves, stops

LOGGER = logging.getLogger(__name__)

PREFIX = "shadow:"
RSI_OVERBOUGHT = 70.0     # ema20 arm won't buy into froth
DCA_FRACTION = 0.2        # dca arm deploys a fifth of its remaining cash each slot
RANDOM_MIN, RANDOM_MAX = 0.2, 0.5  # coin-flip arm's position sizing


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- the deciders ----------
# Each returns a decision dict (same shape as advisor.validate) or None to hold.
# They read the same `market_data` the LLM is given, so there are no indicators
# here that the brain cannot also see.

def _rows(market_data: list[dict]) -> dict[str, dict]:
    """Daily summaries by pair, skipping any pair with no candle data."""
    return {r["pair"]: r for r in market_data
            if r.get("timeframe") == "1d" and not r.get("error")}


def _held(port: dict) -> list[str]:
    return [a for a in port["holdings"] if a != config.BASE_CURRENCY]


def _pair_of(asset: str) -> str:
    return f"{asset}/{config.BASE_CURRENCY}"


def decide_ema20(port, market_data, sleeve, rng):
    """Dumb momentum: hold what is above its 20-day EMA, sell what falls below.

    This is the bar the LLM has to clear to justify existing."""
    rows = _rows(market_data)
    for asset in _held(port):                       # exits first — protect capital
        r = rows.get(_pair_of(asset))
        if r and r.get("ema20") and r["price"] < r["ema20"]:
            return {"action": "sell", "pair": _pair_of(asset), "fraction": 1.0,
                    "confidence": 0.6,
                    "reasoning": f"price {r['price']:.2f} below EMA20 {r['ema20']:.2f}"}
    if port["holdings"].get(config.BASE_CURRENCY, 0) <= 0:
        return None
    up = [r for r in rows.values()
          if r.get("ema20") and r["price"] > r["ema20"]
          and (r.get("rsi14") or 0) < RSI_OVERBOUGHT]
    if not up:
        return None
    best = max(up, key=lambda r: r.get("return_7_candles_pct") or 0)
    return {"action": "buy", "pair": best["pair"], "fraction": 1.0, "confidence": 0.6,
            "reasoning": f"price {best['price']:.2f} above EMA20 {best['ema20']:.2f}, "
                         f"RSI {best.get('rsi14') or 0:.0f}, strongest 7-candle return"}


def decide_dca(port, market_data, sleeve, rng):
    """Buy a fixed slice of remaining cash into the first base pair. Never sells.

    The 'do nothing clever' arm. Because it always spends a FRACTION of what is
    left, its slice shrinks toward zero — so once the slice is below the exchange
    minimum it holds rather than proposing a buy that can only be rejected. Left
    unchecked it errored on every cycle forever and junked up its own diary."""
    if not config.BASE_PAIRS:
        return None
    pair = config.BASE_PAIRS[0]
    cash = port["holdings"].get(config.BASE_CURRENCY, 0)
    if cash <= 0:
        return None
    if cash * DCA_FRACTION < portfolio.min_order_eur(pair):
        return None      # slice too small to be a legal order — that is a HOLD, not an error
    return {"action": "buy", "pair": pair, "fraction": DCA_FRACTION, "confidence": 0.5,
            "reasoning": f"scheduled DCA: {DCA_FRACTION:.0%} of remaining cash into {pair}"}


def decide_random(port, market_data, sleeve, rng):
    """The null hypothesis. If the brain cannot beat this over months, that is
    the most valuable thing this project will ever tell us."""
    rows = _rows(market_data)
    if not rows:
        return None
    action = rng.choice(["buy", "sell", "hold"])
    fraction = round(rng.uniform(RANDOM_MIN, RANDOM_MAX), 3)
    if action == "buy" and port["holdings"].get(config.BASE_CURRENCY, 0) > 0:
        return {"action": "buy", "pair": rng.choice(sorted(rows)), "fraction": fraction,
                "confidence": 0.5, "reasoning": "coin flip"}
    if action == "sell" and _held(port):
        return {"action": "sell", "pair": _pair_of(rng.choice(sorted(_held(port)))),
                "fraction": fraction, "confidence": 0.5, "reasoning": "coin flip"}
    return None   # 'hold', or an impossible flip (sell with nothing, buy with no cash)


DECIDERS = {"ema20": decide_ema20, "dca": decide_dca, "random": decide_random}


# ---------- the registry ----------

def parse(spec: str) -> list[dict]:
    """`name:kind:spec` x N. Unknown kinds/deciders are dropped with a warning —
    a typo in the config must never take the live bot down with it."""
    out = []
    for chunk in (spec or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        bits = [b.strip() for b in chunk.split(":")]
        if len(bits) != 3:
            LOGGER.warning("ignoring malformed arm %r (want name:kind:spec)", chunk)
            continue
        name, kind, how = bits
        if kind == "rule":
            if how not in DECIDERS:
                LOGGER.warning("ignoring arm %r: no decider called %r", chunk, how)
                continue
            out.append({"name": name, "kind": kind, "spec": how, "mode": PREFIX + name})
        elif kind == "llm":
            provider, _, model = how.partition("@")     # model ids contain '/', so '@' splits
            if provider.lower() not in advisor.KEY_ATTR:
                LOGGER.warning("ignoring arm %r: unknown provider %r", chunk, provider)
                continue
            out.append({"name": name, "kind": kind, "spec": how, "mode": PREFIX + name,
                        "provider": provider.lower(), "model": model or None})
        else:
            LOGGER.warning("ignoring arm %r: kind must be rule or llm", chunk)
    return out


def enabled() -> list[dict]:
    return parse(config.SHADOW_ARMS)


def ensure_seeded(conn, arm: dict, primary_mode: str) -> bool:
    """An arm starts with the same STAKE as the real bot, not its current equity,
    so the curves diverge only through decisions. The vault starts empty, as
    the real one does — it is profits-only.

    Returns True if it seeded the arm just now (the caller needs to know: a
    fresh seed already copies the real bot's raised stake, so a top-up landing
    in the same breath must not then be applied to it twice).
    """
    if conn.execute("SELECT 1 FROM sleeve_meta WHERE mode=?", (arm["mode"],)).fetchone():
        return False
    for s in sleeves.ALL:
        row = conn.execute("SELECT allocated FROM sleeve_meta WHERE mode=? AND sleeve=?",
                           (primary_mode, s)).fetchone()
        allocated = (row["allocated"] if row else 0.0) if s in sleeves.ACTIVE else 0.0
        conn.execute("INSERT INTO sleeve_meta(mode, sleeve, allocated, hwm) VALUES(?,?,?,?)",
                     (arm["mode"], s, allocated, allocated))
        conn.execute("INSERT INTO holdings(mode, sleeve, asset, amount) VALUES(?,?,?,?)",
                     (arm["mode"], s, config.BASE_CURRENCY, allocated))
    db.set_setting(conn, f"arm_since_{arm['name']}", _now())
    conn.commit()
    LOGGER.info("seeded shadow arm %s from %s's stake", arm["mode"], primary_mode)
    return True


def mirror_topup(conn, amount: float, primary_mode: str) -> None:
    """Fresh cash reaches every arm too. Without this the arms fall behind the
    real bot's capital and the equity curves stop being comparable."""
    for arm in enabled():
        try:
            if ensure_seeded(conn, arm, primary_mode):
                continue   # seeded from the real bot's stake, which already includes this cash
            portfolio.apply_topup(conn, arm["mode"], amount)
        except Exception as e:  # noqa: BLE001 - an arm must never break a real top-up
            LOGGER.warning("arm %s missed the top-up: %s", arm["mode"], e)


# ---------- running ----------

def _record(conn, mode, sleeve, status, detail="", note=None, decision=None, raw=None) -> int:
    d = decision or {}
    body = raw if raw is not None else (json.dumps(d) if d else None)
    cur = conn.execute(
        "INSERT INTO decisions(at, mode, sleeve, prompt, response_raw, action, pair, fraction, "
        "confidence, reasoning, status, detail) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (_now(), mode, sleeve, note, body, d.get("action"), d.get("pair"),
         d.get("fraction"), d.get("confidence"), d.get("reasoning"), status, detail))
    conn.commit()
    return cur.lastrowid


def _valid(decision: dict) -> bool:
    """The same boundary the LLM's answers cross — a buggy rule gets no more
    trust than a hallucinating model."""
    return (decision.get("action") in ("buy", "sell")
            and decision.get("pair") in config.PAIRS
            and isinstance(decision.get("fraction"), (int, float))
            and 0 < decision["fraction"] <= 1)


DEEP_SLEEVES = ("quarter", "vault")   # rare decisions get the stronger model, as the bot does


def _ask_llm(conn, arm: dict, sleeve: str, port: dict, market_data: list[dict],
             extras: dict | None) -> tuple[dict | None, str, str | None]:
    """Run a rival brain on the IDENTICAL prompt the live bot gets.

    Returns (decision|None, prompt, raw). A dead or babbling model resolves to a
    HOLD for that arm alone — exactly as it would for the real bot."""
    from . import engine   # local import: engine imports arms

    prompt = advisor.build_prompt(
        port, market_data, engine.recent_history(conn, arm["mode"], sleeve),
        min_order=max(portfolio.min_order_eur(p) for p in config.PAIRS),
        mandate=sleeves.MANDATES[sleeve],
        lessons=db.get_setting(conn, "lessons", "") or "",
        extras=extras)
    raw = advisor.ask(prompt, deep=sleeve in DEEP_SLEEVES,
                      provider=arm["provider"], model=arm["model"])
    return advisor.validate(raw), prompt, raw


def run_sleeve(conn, arm: dict, sleeve: str, prices: dict, market_data: list[dict],
               rng: random.Random, extras: dict | None = None) -> dict:
    mode = arm["mode"]
    port = portfolio.valued(conn, mode, sleeve, prices)
    raw = None
    if arm["kind"] == "llm":
        note = f"llm:{arm['spec']}"
        try:
            decision, note, raw = _ask_llm(conn, arm, sleeve, port, market_data, extras)
        except advisor.AdvisorError as e:
            _record(conn, mode, sleeve, "error", str(e), note=note)
            return {"arm": arm["name"], "sleeve": sleeve, "status": "error", "detail": str(e)}
    else:
        note = f"rule:{arm['spec']} (no LLM)"
        decision = DECIDERS[arm["spec"]](port, market_data, sleeve, rng)
    if not decision or decision.get("action") == "hold":
        # a rival brain's HOLD carries its reasoning — that is the whole point of
        # its diary, so record the decision itself, not just the fact of holding
        _record(conn, mode, sleeve, "held", "chose to hold", note=note,
                decision=decision, raw=raw)
        return {"arm": arm["name"], "sleeve": sleeve, "status": "held",
                "reasoning": (decision or {}).get("reasoning")}
    if not _valid(decision):
        _record(conn, mode, sleeve, "invalid", "impossible decision", note=note,
                decision=decision, raw=raw)
        return {"arm": arm["name"], "sleeve": sleeve, "status": "invalid"}
    decision_id = _record(conn, mode, sleeve, "pending", note=note, decision=decision, raw=raw)
    try:
        order = portfolio.execute(conn, mode, sleeve, decision_id, decision["action"],
                                  decision["pair"], decision["fraction"], prices)
    except Exception as e:  # noqa: BLE001 - a rejected order (under the minimum, nothing
        conn.execute("UPDATE decisions SET status='error', detail=? WHERE id=?",  # to sell)
                     (str(e), decision_id))                                       # is normal
        conn.commit()
        return {"arm": arm["name"], "sleeve": sleeve, "status": "error", "detail": str(e)}
    conn.execute("UPDATE decisions SET status='executed' WHERE id=?", (decision_id,))
    conn.commit()
    return {"arm": arm["name"], "sleeve": sleeve, "status": "executed", "order": order}


def run_all(conn, primary_mode: str, due_now: list[str], prices: dict,
            market_data: list[dict], rng: random.Random | None = None,
            extras: dict | None = None) -> list[dict]:
    """Run every enabled arm over the sleeves that are due, on the SAME prices and
    market data the real bot just used — one market snapshot per cycle is what
    makes the comparison fair, so nothing here re-fetches anything.

    No arm may ever disturb the real bot: every arm is isolated, and a failure
    is logged and stepped over. Arms are also deliberately absent from the
    cycle-failure alerting — a broken shadow is not a 3am problem.
    """
    rng = rng or random.Random()
    results = []
    for arm in enabled():
        try:
            ensure_seeded(conn, arm, primary_mode)
            stops.sync(conn, arm["mode"], prices)   # simulated stops, same rules as the real bot
            for s in due_now:
                results.append(run_sleeve(conn, arm, s, prices, market_data, rng, extras))
            portfolio.skim_profits(conn, arm["mode"], prices)
            portfolio.snapshot_all(conn, arm["mode"], prices)
        except Exception as e:  # noqa: BLE001 - shadows never touch the real bot
            LOGGER.warning("shadow arm %s failed this cycle: %s", arm["mode"], e)
            results.append({"arm": arm["name"], "status": "error", "detail": str(e)})
    return results


# ---------- the leaderboard (#32) ----------

def _curve(conn, mode: str, limit: int = 400) -> list[dict]:
    rows = conn.execute(
        "SELECT substr(at,1,16) t, ROUND(SUM(total_eur),2) eur FROM snapshots "
        "WHERE mode=? GROUP BY t ORDER BY t DESC LIMIT ?", (mode, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def _entry(conn, key: str, kind: str, mode: str, prices: dict, since: str | None) -> dict:
    if not since:   # the real bot has no seed date — its record starts at its first snapshot
        row = conn.execute("SELECT MIN(at) a FROM snapshots WHERE mode=?", (mode,)).fetchone()
        since = row["a"] if row else None
    ov = portfolio.overview(conn, mode, prices)
    invested = sum(v["allocated"] or 0 for v in ov["sleeves"])
    trips = ledger.round_trips(conn, mode)
    stats = ledger.trip_stats(trips)
    trades = conn.execute("SELECT COUNT(*) c FROM orders WHERE mode=?", (mode,)).fetchone()["c"]
    equity = ov["total_eur"]
    return {"key": key, "kind": kind, "mode": mode, "equity_eur": equity,
            "invested_eur": round(invested, 2),
            "pnl_eur": round(equity - invested, 2),
            "pnl_pct": round((equity - invested) / invested * 100, 1) if invested else None,
            "trades": trades,
            "win_rate_pct": stats["win_rate_pct"] if stats else None,
            "since": since, "curve": _curve(conn, mode)}


def standings(conn, primary_mode: str, prices: dict[str, float]) -> list[dict]:
    """The real bot, every shadow arm, and the phantom hodler — ranked by equity.

    `since` is not decoration: an arm added later has a shorter and strictly
    non-comparable record, and the table must never let that pass for skill.
    """
    rows = [_entry(conn, "magpie", "bot", primary_mode, prices,
                   db.get_setting(conn, "bot_since"))]
    for arm in enabled():
        if not conn.execute("SELECT 1 FROM sleeve_meta WHERE mode=?", (arm["mode"],)).fetchone():
            continue   # configured but not seeded yet — it starts at the next cycle
        rows.append(_entry(conn, arm["name"], arm["spec"], arm["mode"], prices,
                           db.get_setting(conn, f"arm_since_{arm['name']}")))
    bench = ledger.bench_value(conn, primary_mode, prices)
    if bench:
        rows.append({"key": "hodl", "kind": "bench", "mode": None,
                     "equity_eur": bench["hodl_eur"], "invested_eur": bench["invested"],
                     "pnl_eur": round(bench["hodl_eur"] - bench["invested"], 2),
                     "pnl_pct": round((bench["hodl_eur"] - bench["invested"])
                                      / bench["invested"] * 100, 1) if bench["invested"] else None,
                     "trades": 0, "win_rate_pct": None, "since": bench["since"], "curve": []})
    return sorted(rows, key=lambda r: r["equity_eur"], reverse=True)
