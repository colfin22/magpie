import logging

from fastapi import Body, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import (__version__, advisor, arms, auth, config, db, engine, ha, ledger, market,
               portfolio, scoring, stops, universe)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Magpie", version=__version__)

# apply any web-entered settings over the env at boot (before the first cycle)
try:
    _c = db.connect()
    config.autolock_currency(_c)   # an install with trade history keeps its currency
    config.apply_overrides(_c)
    _c.close()
except Exception as _e:  # noqa: BLE001 - never block startup on settings
    LOGGER.warning("settings override load failed: %s", _e)


@app.middleware("http")
async def no_store(request, call_next):
    # browsers must never serve stale portfolio state (#4)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.middleware("http")
async def auth_gate(request, call_next):
    """Gate everything behind a password when one is set (#14). Health is public;
    localhost (the timers) is always allowed."""
    path = request.url.path
    if path not in auth.PUBLIC_PATHS:
        conn = db.connect()
        try:
            authed = auth.is_authed(request, conn)
        finally:
            conn.close()
        if not authed:
            wants_html = request.method == "GET" and "text/html" in request.headers.get("accept", "")
            if wants_html:
                return RedirectResponse("/login", status_code=302)
            return Response(status_code=401, content="authentication required")
    return await call_next(request)


# The 🐦‍⬛ mark on the accent-green rounded square — reads on light *and* dark tab
# bars (a bare dark emoji vanishes on a dark tab). Self-contained, no external req.
FAVICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
               '<rect width="100" height="100" rx="22" fill="#4cd97b"/>'
               '<text x="50" y="55" font-size="60" text-anchor="middle" '
               'dominant-baseline="central">\U0001f426‍⬛</text></svg>')
FAVICON_LINK = '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'


@app.get("/favicon.svg")
def favicon_svg():
    return Response(FAVICON_SVG, media_type="image/svg+xml",
                   headers={"Cache-Control": "max-age=86400"})


@app.get("/favicon.ico")
def favicon_ico():
    return RedirectResponse("/favicon.svg", status_code=301)


@app.get("/login", response_class=HTMLResponse)
def login_page(bad: int = 0):
    err = ''
    if bad == 2:
        err = '<p style="color:#ff6b6b">Enter your 2FA code (or a backup code).</p>'
    elif bad:
        err = '<p style="color:#ff6b6b">Wrong details.</p>'
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Magpie — login</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>body{{font-family:system-ui;background:#12151c;color:#e6e9f0;display:flex;min-height:100vh;
margin:0;align-items:center;justify-content:center}}form{{background:#1a1f2b;border:1px solid #262d3c;
border-radius:12px;padding:2rem;width:280px}}h1{{font-size:1.3rem;margin:0 0 1rem}}
input{{width:100%;box-sizing:border-box;background:#12151c;border:1px solid #2a3140;border-radius:8px;
color:#e6e9f0;padding:.6rem;font-size:1rem;margin:.5rem 0}}button{{width:100%;background:#4cd97b;color:#0a0d12;
border:0;border-radius:8px;padding:.6rem;font-weight:700;font-size:1rem;cursor:pointer;margin-top:.5rem}}</style>
</head><body><form method="post" action="/login"><h1>🐦‍⬛ Magpie</h1>{err}
<input type="text" name="username" placeholder="Username" autofocus autocomplete="username">
<input type="password" name="password" placeholder="Password" autocomplete="current-password">
<input type="text" name="otp" inputmode="numeric" autocomplete="one-time-code" placeholder="2FA code (if enabled)">
<button type="submit">Sign in</button></form></body></html>"""


@app.post("/login")
def login_submit(username: str = Form(""), password: str = Form(""), otp: str = Form("")):
    conn = db.connect()
    try:
        if not auth.check_login(conn, username, password):
            return RedirectResponse("/login?bad=1", status_code=302)
        if auth.totp_is_enabled(conn) and not (
                auth.check_totp(conn, otp) or auth.consume_backup_code(conn, otp)):
            return RedirectResponse("/login?bad=2", status_code=302)  # password ok, code missing/wrong
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie(auth.COOKIE, auth.token(conn), httponly=True,
                        max_age=30 * 86400, samesite="lax")
        return resp
    finally:
        conn.close()


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/api/2fa")
def api_2fa_status():
    conn = db.connect()
    try:
        return {"enabled": auth.totp_is_enabled(conn), "password_set": auth.enabled(),
                "backup_remaining": auth.backup_codes_remaining(conn)}
    finally:
        conn.close()


@app.post("/api/2fa/setup")
def api_2fa_setup():
    """Mint a fresh secret and return the QR + manual code. Not active until
    /api/2fa/enable confirms a code. Needs the first factor (a password) set."""
    conn = db.connect()
    try:
        if not auth.enabled():
            return Response(status_code=400, content="set a dashboard password first")
        secret = auth.new_totp_secret(conn)
        uri = auth.totp_uri(conn, secret)
        return {"secret": secret, "uri": uri, "qr_svg": auth.totp_qr_svg(uri)}
    finally:
        conn.close()


@app.post("/api/2fa/enable")
def api_2fa_enable(body: dict = Body(...)):
    conn = db.connect()
    try:
        if auth.enable_totp(conn, str(body.get("code", ""))):
            codes = auth.generate_backup_codes(conn)   # shown once
            return {"enabled": True, "backup_codes": codes}
        return Response(status_code=400, content="that code didn't verify — check the time on your phone and try the current one")
    finally:
        conn.close()


@app.post("/api/2fa/backup")
def api_2fa_backup(body: dict = Body(...)):
    """Regenerate the backup codes (invalidates the old set). Needs a current
    authenticator code so a hijacked session can't silently mint new ones."""
    conn = db.connect()
    try:
        if not auth.totp_is_enabled(conn):
            return Response(status_code=400, content="enable 2FA first")
        if not auth.check_totp(conn, str(body.get("code", ""))):
            return Response(status_code=400, content="enter a current authenticator code to regenerate")
        return {"backup_codes": auth.generate_backup_codes(conn)}
    finally:
        conn.close()


@app.post("/api/2fa/disable")
def api_2fa_disable(body: dict = Body(...)):
    conn = db.connect()
    try:
        if not auth.totp_is_enabled(conn):
            return {"enabled": False}
        if not auth.check_totp(conn, str(body.get("code", ""))):
            return Response(status_code=400, content="enter a current code to turn 2FA off")
        auth.disable_totp(conn)
        return {"enabled": False}
    finally:
        conn.close()


@app.get("/health")
def health():
    conn = db.connect()
    try:
        last = conn.execute("SELECT at, sleeve, status FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        snap = conn.execute("SELECT SUM(total_eur) t FROM snapshots WHERE id IN "
                            "(SELECT MAX(id) FROM snapshots GROUP BY sleeve)").fetchone()
        from datetime import datetime, timezone
        last_cycle = db.get_setting(conn, "last_cycle_at")
        stale = True
        if last_cycle:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last_cycle)).total_seconds()
            stale = age > config.STALE_AFTER_S
        failures = int(db.get_setting(conn, "consecutive_failures", "0") or 0)
        return {
            "ok": True, "version": __version__, "mode": config.mode(),
            "halted": db.get_setting(conn, "halted") == "1",
            "llm_provider": advisor.active_provider(),
            "brain_configured": advisor.brain_configured(),
            "last_cycle_at": last_cycle,
            "stale": stale,                    # no cycle within STALE_AFTER_S (#1)
            "consecutive_failures": failures,  # cycles failing in a row (#2)
            "healthy": not stale and failures < config.ERROR_ALERT_AFTER,
            "last_decision": dict(last) if last else None,
            "last_equity_eur": round(snap["t"], 2) if snap and snap["t"] else None,
        }
    finally:
        conn.close()


@app.post("/api/cycle")
def api_cycle():
    """One tick — invoked by the systemd timer (and manually)."""
    return engine.run_cycle()


@app.post("/api/cycle/retry")
def api_cycle_retry(force: bool = False):
    """Retry now every sleeve whose latest decision errored (off-schedule).

    Called every few minutes by the magpie-retry systemd timer; `force=true`
    bypasses the consecutive-attempt cap for a manual retry."""
    return engine.retry_now(force=force)


@app.post("/api/digest")
def api_digest():
    return engine.daily_digest()


@app.post("/api/review")
def api_review():
    """Monthly self-review: distil the ledger into a lessons note (see engine)."""
    return engine.self_review()


@app.post("/api/reconcile")
def api_reconcile():
    """Nightly: absorb drift between the sleeve books and exchange reality, and
    mark any decision whose horizon has now elapsed (#33)."""
    conn = db.connect()
    try:
        prices = market.tickers(config.PAIRS)
        # Order matters: claim any fired stop as a SALE before reconcile gets a look
        # at the drift, or the sale is silently absorbed and then double-booked by
        # the next cycle's sync (#35).
        fired = stops.sync(conn, config.mode(), prices)
        out = ledger.reconcile(conn, config.mode(), prices)
        return {**out, "stops_fired": fired, "scoring": scoring.grade(conn)}
    finally:
        conn.close()


@app.get("/api/universe")
def api_universe():
    """The current tradeable universe: base + dynamic top-alt set."""
    conn = db.connect()
    try:
        return universe.current(conn)
    finally:
        conn.close()


@app.post("/api/universe/refresh")
def api_universe_refresh():
    """Re-detect the top-N altcoins and update the universe (weekly timer + manual)."""
    conn = db.connect()
    try:
        return universe.refresh(conn)
    finally:
        conn.close()


def _save_manual_pairs(conn, pairs: list[str]) -> None:
    db.set_setting(conn, "cfg_MANUAL_PAIRS", ",".join(pairs))
    config.apply_overrides(conn)   # re-merge the effective universe now


@app.get("/api/currency")
def api_currency():
    conn = db.connect()
    try:
        return {"currency": config.BASE_CURRENCY, "symbol": config.symbol(),
                "locked": config.currency_locked(conn),
                "supported": list(config.SUPPORTED_CURRENCIES)}
    finally:
        conn.close()


@app.post("/api/currency/set")
def api_currency_set(body: dict = Body(...)):
    """Commit the base currency — a ONE-TIME choice at initial setup. Refused once
    locked (or once the bot has traded). Validates Kraken lists the base pairs."""
    conn = db.connect()
    try:
        if config.currency_locked(conn):
            return Response(status_code=400, content=f"base currency is already set to {config.BASE_CURRENCY} and locked")
        ccy = str(body.get("currency", "")).strip().upper()
        if ccy not in config.SUPPORTED_CURRENCIES:
            return Response(status_code=400,
                            content=f"unsupported — pick one of {', '.join(config.SUPPORTED_CURRENCIES)}")
        try:
            for coin in ("BTC", "ETH"):
                universe.validate_tradeable(f"{coin}/{ccy}")
        except ValueError as e:
            return Response(status_code=400, content=f"Kraken can't trade the base pairs in {ccy}: {e}")
        db.set_setting(conn, "base_currency", ccy)
        db.set_setting(conn, "cfg_PAIRS", f"BTC/{ccy},ETH/{ccy}")  # default base pairs in the new currency
        config.apply_overrides(conn)
        return {"currency": config.BASE_CURRENCY, "symbol": config.symbol(), "locked": True}
    finally:
        conn.close()


@app.get("/api/pairs")
def api_pairs():
    """The manually-pinned coins plus the full effective universe."""
    conn = db.connect()
    try:
        return {"manual": list(config.MANUAL_PAIRS), "base": list(config.BASE_PAIRS),
                "effective": list(config.PAIRS)}
    finally:
        conn.close()


@app.post("/api/pairs/add")
def api_pairs_add(body: dict = Body(...)):
    """Pin any Kraken EUR spot coin into the universe (validated before saving)."""
    conn = db.connect()
    try:
        try:
            pair = universe.resolve_pair(str(body.get("symbol", "")))
            universe.validate_tradeable(pair)
        except ValueError as e:
            return Response(status_code=400, content=str(e))
        pairs = list(config.MANUAL_PAIRS)
        if pair not in pairs and pair not in config.BASE_PAIRS:
            pairs.append(pair)
            _save_manual_pairs(conn, pairs)
        return {"manual": list(config.MANUAL_PAIRS), "effective": list(config.PAIRS), "added": pair}
    finally:
        conn.close()


@app.post("/api/pairs/remove")
def api_pairs_remove(body: dict = Body(...)):
    """Unpin a manual coin. A position still held stays sellable via the books."""
    conn = db.connect()
    try:
        pair = str(body.get("pair", "")).strip().upper()
        pairs = [p for p in config.MANUAL_PAIRS if p != pair]
        _save_manual_pairs(conn, pairs)
        return {"manual": list(config.MANUAL_PAIRS), "effective": list(config.PAIRS), "removed": pair}
    finally:
        conn.close()


def _mask(value: str) -> str:
    if not value:
        return ""
    return "•••• " + value[-4:] if len(value) > 4 else "••••"


@app.get("/api/settings")
def api_settings_get():
    """Current editable settings — secrets returned masked, never in full."""
    out = {"mode": config.mode()}
    for key in config.EDITABLE:
        val = getattr(config, key)
        if key in config.SECRET_KEYS:
            out[key] = {"set": bool(val), "hint": _mask(val)}
        elif key == "PAIRS":
            out[key] = ", ".join(config.BASE_PAIRS)  # the field edits the base, not the effective set
        else:
            out[key] = val
    return out


@app.post("/api/settings")
def api_settings_set(body: dict = Body(...)):
    """Save settings. A blank secret field means 'leave unchanged'; a blank
    non-secret clears to empty. Going live stays an env decision — not here."""
    conn = db.connect()
    try:
        changed = []
        for key, raw in body.items():
            if key not in config.EDITABLE:
                continue
            raw = "" if raw is None else str(raw).strip()
            if key in config.SECRET_KEYS and raw == "":
                continue  # unchanged
            try:
                config._cast(key, raw)  # validate before persisting
            except Exception as e:  # noqa: BLE001
                return Response(status_code=400, content=f"{key}: {e}")
            db.set_setting(conn, "cfg_" + key, raw)
            changed.append(key)
        config.apply_overrides(conn)
        return {"saved": changed, "mode": config.mode()}
    finally:
        conn.close()


@app.post("/api/settings/test")
def api_settings_test(target: str):
    """Probe a configured integration and report a human-readable result."""
    try:
        if target in ("brain", "gemini"):
            raw = advisor.ask('Reply with exactly {"ok": true}')
            return {"ok": '"ok"' in raw or "ok" in raw.lower(),
                    "detail": f"[{advisor.active_provider()}] " + raw[:110]}
        if target == "kraken":
            bal = market.exchange().fetch_balance()
            eur = (bal.get("total") or {}).get("EUR")
            # confirm the key cannot withdraw (the safety guarantee)
            can_withdraw = True
            try:
                market.exchange().private_post_withdrawmethods({"asset": "XBT"})
            except Exception:  # noqa: BLE001 - denial is what we want
                can_withdraw = False
            return {"ok": True, "detail": f"balance readable, EUR {eur}",
                    "withdrawal_blocked": not can_withdraw}
        if target == "ha":
            ok = ha.notify("Magpie test", "Settings page test — notifications are working.")
            return {"ok": ok, "detail": "sent — check your channels" if ok
                    else "no channel configured or reachable"}
        return Response(status_code=400, content="target must be brain|kraken|ha")
    except Exception as e:  # noqa: BLE001 - surface the failure to the page
        return {"ok": False, "detail": str(e)[:200]}


@app.post("/api/topup")
def api_topup(amount: float = 0):
    """Paper-mode only: simulate a cash deposit (live deposits are auto-detected)."""
    if config.mode() != "paper":
        return Response(status_code=400, content="live mode detects deposits automatically")
    if amount <= 0:
        return Response(status_code=400, content="?amount= must be positive")
    conn = db.connect()
    try:
        result = portfolio.apply_topup(conn, "paper", amount)
        arms.mirror_topup(conn, amount, "paper")   # arms must stay capital-comparable
        return result
    finally:
        conn.close()


@app.post("/api/halt")
def api_halt():
    conn = db.connect()
    try:
        db.set_setting(conn, "halted", "1")
        return {"halted": True, "note": "no further orders until /api/resume"}
    finally:
        conn.close()


@app.post("/api/resume")
def api_resume():
    conn = db.connect()
    try:
        db.set_setting(conn, "halted", "0")
        return {"halted": False}
    finally:
        conn.close()


def _next_cycle_iso() -> str:
    """When the next scheduled decision cycle fires (00/06/12/18 local time)."""
    from datetime import datetime, timedelta
    now = datetime.now(config.tz())
    todays = [now.replace(hour=h, minute=0, second=0, microsecond=0) for h in (0, 6, 12, 18)]
    future = [t for t in todays if t > now] or [todays[0] + timedelta(days=1)]
    return min(future).isoformat()


@app.get("/api/state")
def api_state():
    conn = db.connect()
    try:
        prices = market.tickers(config.PAIRS)
        ov = portfolio.overview(conn, config.mode(), prices)
        decisions = [dict(r) for r in conn.execute(
            "SELECT at, mode, sleeve, action, pair, fraction, confidence, reasoning, status, detail "
            "FROM decisions ORDER BY id DESC LIMIT 30")]
        skims = [dict(r) for r in conn.execute(
            "SELECT at, sleeve, amount FROM skims ORDER BY id DESC LIMIT 10")]
        curve = [dict(r) for r in conn.execute(
            "SELECT substr(at,1,16) t, ROUND(SUM(total_eur),2) eur FROM snapshots "
            "WHERE mode=? GROUP BY t ORDER BY t DESC LIMIT 400", (config.mode(),))]
        trips = ledger.round_trips(conn, config.mode())
        return {"mode": config.mode(), "version": __version__, "prices": prices, "overview": ov,
                "ccy": config.symbol(), "ccy_code": config.BASE_CURRENCY,
                "next_cycle": _next_cycle_iso(),
                "retry_cycle_at": db.get_setting(conn, "retry_cycle_at") or None,
                "halted": db.get_setting(conn, "halted") == "1",
                "lessons": {"text": db.get_setting(conn, "lessons"),
                            "at": db.get_setting(conn, "lessons_at")},
                "benchmark": ledger.bench_value(conn, config.mode(), prices),
                "standings": arms.standings(conn, config.mode(), prices),
                "calibration": scoring.calibration(conn, config.mode()),
                "stops": stops.open_stops(conn, config.mode()),
                "equity_curve": list(reversed(curve)),
                "trips": trips[:15], "trip_stats": ledger.trip_stats(trips),
                "decisions": decisions, "skims": skims}
    finally:
        conn.close()


@app.get("/api/stops")
def api_stops():
    """Resting stop-losses. In live mode these are real orders sitting at Kraken."""
    conn = db.connect()
    try:
        return {"enabled": config.STOP_LOSS_ENABLED,
                "open": stops.open_stops(conn, config.mode()),
                "note": "a HALT does not cancel resting stops — they are protective, and "
                        "cancelling them would leave positions naked while trading is paused. "
                        "POST /api/stops/cancel to clear them deliberately."}
    finally:
        conn.close()


@app.post("/api/stops/cancel")
def api_stops_cancel():
    """Deliberately clear every resting stop (they are NOT cleared by a halt)."""
    conn = db.connect()
    try:
        return {"cancelled": stops.cancel_all(conn, config.mode())}
    finally:
        conn.close()


@app.get("/api/arms")
def api_arms():
    """The leaderboard: the real bot, every shadow arm, and the phantom hodler."""
    conn = db.connect()
    try:
        return {"standings": arms.standings(conn, config.mode(), market.tickers(config.PAIRS)),
                "note": "shadow arms are simulated: fills assume the maker limit fills, "
                        "so they run mildly optimistic against a live bot that pays slippage"}
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Magpie</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
 body{font-family:system-ui;margin:1.5rem auto;padding:0 1rem;background:#12151c;color:#e6e9f0;max-width:980px}
 h1{font-size:1.4rem} .big{font-size:2rem;font-weight:700}
 .dim{color:#8b93a7} .card{background:#1a1f2b;border-radius:10px;padding:1rem;margin:.8rem 0}
 .row{display:flex;gap:.8rem;flex-wrap:wrap} .row .card{flex:1;min-width:180px;margin:0}
 .slv{font-size:1.3rem;font-weight:700} .up{color:#4cd97b}.down{color:#ff6b6b}
 table{width:100%;border-collapse:collapse;font-size:.85rem}
 td,th{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #2a3140}
 .hold{color:#8b93a7}.buy{color:#4cd97b}.sell{color:#ff6b6b}.err{color:#ffb020}
 tr.me td{background:#20283a;font-weight:700}
 .dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:.45rem}
 .bar td{color:#8b93a7;font-style:italic}
 button{background:#ff6b6b;color:#000;border:0;border-radius:8px;padding:.6rem 1.2rem;font-weight:700}
</style></head><body>
<h1>🐦‍⬛ Magpie <span class="dim" id="mode"></span> <span class="dim" id="ver" style="font-size:.8rem"></span>
<span style="float:right;font-size:.8rem"><a href="/settings" style="color:#8b93a7;text-decoration:none">⚙ settings</a>
<a href="#" onclick="fetch('/logout',{method:'POST'}).then(()=>location='/login');return false" style="color:#8b93a7;text-decoration:none;margin-left:12px">⎋ log out</a>
<span class="dim" id="updated" style="margin-left:12px"></span></span></h1>
<div class="card"><div class="dim">Total portfolio <span id="nextcheck" style="float:right"></span></div>
<div class="big" id="equity">…</div>
<div id="pnl" style="font-weight:600"></div>
<div id="vs" class="dim" style="margin-top:.6rem;padding-top:.6rem;border-top:1px solid #2a3140;line-height:1.5"></div>
<svg id="chart" viewBox="0 0 600 120" preserveAspectRatio="none"
     style="width:100%;height:120px;margin-top:.5rem"></svg></div>
<div class="row" id="sleeves"></div>
<div class="card" id="board-card" hidden><div class="dim">Leaderboard
<span class="dim" id="board-note" style="float:right;font-size:.75rem"></span></div>
<table id="board"></table>
<p class="dim" style="font-size:.72rem;margin:.6rem 0 0;line-height:1.45">
Shadow arms trade the same market in simulation — same sleeves, same fees, no real orders.
Fills assume the maker limit fills, so they run mildly optimistic vs the live bot's slippage.
Arms with a shorter record are not yet comparable — mind the "since" column.</p></div>
<div class="card" id="stops-card" hidden><div class="dim">Resting stop-losses
<span class="dim" id="stops-note" style="float:right;font-size:.75rem"></span></div>
<table id="stops"></table>
<p class="dim" style="font-size:.72rem;margin:.6rem 0 0;line-height:1.45">
These sit <b>at the exchange</b>, so they protect the position even when the bot is
offline — between cycles, through an outage, while you sleep. A HALT does not cancel
them (that would leave positions naked); clear them deliberately with POST /api/stops/cancel.</p></div>
<div class="card" id="cal-card" hidden><div class="dim">Was it right?
<span class="dim" id="cal-head" style="float:right"></span></div>
<table id="cal"></table>
<p class="dim" style="font-size:.72rem;margin:.6rem 0 0;line-height:1.45">
Every buy/sell is a falsifiable claim about direction, graded at its sleeve's horizon
(swing 3d, fortnight 10d, quarter 90d) against the price that actually happened. Holds
make no claim and are not graded. The question is not only whether it beats 50%, but
whether its <b>confident</b> calls land better than its unsure ones — if they don't,
the confidence is decoration.</p></div>
<div class="card"><div class="dim">Closed trades <span id="tstats" style="float:right"></span></div><table id="trades"></table></div>
<div class="card" id="lessons-card" hidden><div class="dim" id="lessons-when"></div>
<p class="dim" id="lessons-text" style="font-size:.85rem;line-height:1.55;margin:.4rem 0 0"></p></div>
<div class="card"><div class="dim">Recent decisions</div><table id="log"></table></div>
<div class="card"><button onclick="if(confirm('Halt all trading?'))fetch('/api/halt',{method:'POST'}).then(()=>load())">⛔ HALT TRADING</button>
<span class="dim" id="halted"></span></div>
<script>
async function load(){
  const s = await (await fetch('/api/state', {cache: 'no-store'})).json();
  const CCY = s.ccy || '€', CCODE = s.ccy_code || 'EUR';
  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  if (s.version) document.getElementById('ver').textContent = 'v' + s.version;
  if (s.next_cycle) {
    const n = new Date(s.next_cycle), mins = Math.max(0, Math.round((n - Date.now()) / 60000));
    document.getElementById('nextcheck').textContent =
      `next decision ${n.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})}` +
      ` · in ${Math.floor(mins / 60)}h ${mins % 60}m`;
  }
  const modeEl = document.getElementById('mode');
  modeEl.textContent = `(${s.mode}${s.halted ? " — HALTED" : ""})`;
  modeEl.className = s.halted ? 'down' : (s.mode === 'live' ? 'up' : 'dim');
  document.getElementById('equity').textContent = `${CCY}${s.overview.total_eur.toFixed(2)}`;
  document.getElementById('halted').textContent = s.halted ? " halted — POST /api/resume to re-arm" : "";
  document.getElementById('sleeves').innerHTML = s.overview.sleeves.map(v => {
    const d = v.total_eur - v.allocated;
    const assets = Object.keys(v.holdings).filter(k => k !== CCODE);
    return `<div class="card"><div class="dim">${v.sleeve}</div>` +
      `<div class="slv">${CCY}${v.total_eur.toFixed(2)}</div>` +
      `<div class="${d >= 0 ? 'up' : 'down'}">${d >= 0 ? '+' : ''}${d.toFixed(2)}</div>` +
      `<div class="dim">${assets.length ? assets.join(', ') : 'in cash'}</div></div>`;
  }).join('');
  // hero: P/L on invested + the vs-hodl sentence (#9)
  const invested = s.overview.sleeves.reduce((a, v) => a + (v.allocated || 0), 0);
  const pnl = s.overview.total_eur - invested;
  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = invested > 0
    ? `${pnl >= 0 ? '+' : '−'}${CCY}${Math.abs(pnl).toFixed(2)} (${(pnl / invested * 100).toFixed(1)}%) on ${CCY}${invested.toFixed(2)} invested`
    : '';
  pnlEl.className = pnl >= 0 ? 'up' : 'down';
  const vsEl = document.getElementById('vs');
  if (s.benchmark) {
    const edge = s.overview.total_eur - s.benchmark.hodl_eur;
    vsEl.innerHTML = `vs buy-and-hold <b style="color:#e6e9f0">${CCY}${s.benchmark.hodl_eur.toFixed(2)}</b><br>` +
      `the magpie is <b class="${edge >= 0 ? 'up' : 'down'}">${edge >= 0 ? '+' : '−'}${CCY}${Math.abs(edge).toFixed(2)} ` +
      `${edge >= 0 ? 'ahead of' : 'behind'} doing nothing</b>`;
  } else { vsEl.textContent = ''; }
  // lessons note (appears after the first monthly self-review)
  const lc = document.getElementById('lessons-card');
  if (s.lessons && s.lessons.text) {
    lc.hidden = false;
    document.getElementById('lessons-when').textContent =
      'Lessons note · self-review ' + (s.lessons.at || '').slice(0, 10);
    document.getElementById('lessons-text').textContent = '"' + s.lessons.text + '"';
  } else { lc.hidden = true; }
  // equity chart — the bot plus every shadow arm, on one shared scale (#32).
  // x is mapped by TIME, not by index: arms started on different days and have
  // curves of different lengths, so index-mapping would silently lie.
  const COLOURS = {magpie: '#4cd97b', hodl: '#ffb020'};
  const PALETTE = ['#6aa9ff', '#c792ea', '#ff8fa3', '#5ad1c5'];
  const board = s.standings || [];
  let ci = 0;
  board.forEach(r => { if (!COLOURS[r.key]) COLOURS[r.key] = PALETTE[ci++ % PALETTE.length]; });
  const series = board.filter(r => (r.curve || []).length > 1)
    .map(r => ({key: r.key, pts: r.curve.map(p => ({x: Date.parse(p.t + 'Z'), y: p.eur}))}));
  const legacy = s.equity_curve || [];
  if (!series.length && legacy.length > 1) {
    series.push({key: 'magpie', pts: legacy.map(p => ({x: Date.parse(p.t + 'Z'), y: p.eur}))});
  }
  if (series.length) {
    const all = series.flatMap(sr => sr.pts);
    const x0 = Math.min(...all.map(p => p.x)), x1 = Math.max(...all.map(p => p.x));
    const y0 = Math.min(...all.map(p => p.y)), y1 = Math.max(...all.map(p => p.y));
    const xs = (x1 - x0) || 1, ys = (y1 - y0) || 1;
    document.getElementById('chart').innerHTML = series.map(sr => {
      const pts = sr.pts.map(p =>
        `${((p.x - x0) / xs * 600).toFixed(1)},${(110 - (p.y - y0) / ys * 100).toFixed(1)}`).join(' ');
      const me = sr.key === 'magpie';
      return `<polyline points="${pts}" fill="none" stroke="${COLOURS[sr.key] || '#8b93a7'}" ` +
        `stroke-width="${me ? 2.5 : 1.3}" opacity="${me ? 1 : 0.75}"/>`;
    }).join('');
  }
  // leaderboard (#32) — the bot vs the control arms vs doing nothing
  const bc = document.getElementById('board-card');
  if (board.length > 1) {
    bc.hidden = false;
    document.getElementById('board-note').textContent =
      board.some(r => r.key === 'coinflip' || r.kind === 'random') ? 'beat the coin flip' : '';
    const BARS = {hodl: 'doing nothing', random: 'chance'};
    document.getElementById('board').innerHTML =
      '<tr><th></th><th>equity</th><th>P/L</th><th>trades</th><th>wins</th><th>since</th></tr>' +
      board.map(r => {
        const me = r.key === 'magpie';
        const bar = BARS[r.key] || BARS[r.kind];
        const pl = r.pnl_eur;
        return `<tr class="${me ? 'me' : (bar ? 'bar' : '')}">` +
          `<td><span class="dot" style="background:${COLOURS[r.key] || '#8b93a7'}"></span>` +
          `${me ? 'magpie (the brain)' : r.key}` +
          `${bar ? ` <span class="dim">— ${bar}</span>` : ''}</td>` +
          `<td>${CCY}${r.equity_eur.toFixed(2)}</td>` +
          `<td class="${pl >= 0 ? 'up' : 'down'}">${pl >= 0 ? '+' : '−'}${CCY}${Math.abs(pl).toFixed(2)}` +
          `${r.pnl_pct === null ? '' : ` (${r.pnl_pct}%)`}</td>` +
          `<td class="dim">${r.trades}</td>` +
          `<td class="dim">${r.win_rate_pct === null ? '—' : r.win_rate_pct + '%'}</td>` +
          `<td class="dim">${(r.since || '').slice(0, 10)}</td></tr>`;
      }).join('');
  } else { bc.hidden = true; }
  // resting stops — real orders at the exchange, so show them plainly (#35)
  const st = s.stops || [];
  const sc = document.getElementById('stops-card');
  if (st.length) {
    sc.hidden = false;
    document.getElementById('stops-note').textContent = `${st.length} resting at the exchange`;
    document.getElementById('stops').innerHTML =
      '<tr><th>sleeve</th><th>pair</th><th>stop</th><th>below entry</th><th>placed</th></tr>' +
      st.map(x => `<tr><td>${x.sleeve}</td><td>${x.pair}</td>` +
        `<td>${CCY}${x.stop_price.toFixed(2)}</td>` +
        `<td class="down">−${x.pct.toFixed(1)}%</td>` +
        `<td class="dim">${(x.placed_at || '').slice(0, 10)}</td></tr>`).join('');
  } else { sc.hidden = true; }
  // calibration: is the brain right, and does its confidence mean anything? (#33)
  const cal = s.calibration;
  const cc = document.getElementById('cal-card');
  if (cal && cal.graded) {
    cc.hidden = false;
    const edge = cal.hit_rate_pct - 50;
    document.getElementById('cal-head').innerHTML =
      `<b class="${edge >= 0 ? 'up' : 'down'}">${cal.hit_rate_pct}% correct</b> ` +
      `<span class="dim">of ${cal.graded} graded · coin flip = 50%</span>`;
    document.getElementById('cal').innerHTML =
      '<tr><th>stated confidence</th><th>calls</th><th>hit rate</th><th>avg move</th></tr>' +
      cal.buckets.map(b => `<tr><td>${b.bucket}</td><td class="dim">${b.n}</td>` +
        `<td class="${b.hit_rate_pct >= 50 ? 'up' : 'down'}">${b.hit_rate_pct}%</td>` +
        `<td class="dim">${b.avg_move_pct > 0 ? '+' : ''}${b.avg_move_pct}%</td></tr>`).join('');
  } else { cc.hidden = true; }
  // closed trades
  const ts = s.trip_stats;
  document.getElementById('tstats').textContent = ts
    ? `${ts.closed_trades} closed · ${ts.win_rate_pct}% wins · ${CCY}${ts.total_pnl_eur}` : '';
  document.getElementById('trades').innerHTML = (s.trips && s.trips.length)
    ? '<tr><th>sleeve</th><th>pair</th><th>in→out</th><th>held</th><th>P/L</th></tr>' +
      s.trips.map(t => `<tr><td>${t.sleeve}</td><td>${t.pair}</td>` +
        `<td class="dim">${t.entry_price.toFixed(0)}→${t.exit_price.toFixed(0)}</td>` +
        `<td class="dim">${t.held_days}d</td>` +
        `<td class="${t.pnl_eur >= 0 ? 'up' : 'down'}">${CCY}${t.pnl_eur.toFixed(2)} (${t.pnl_pct}%)</td></tr>`).join('')
    : '<tr><td class="dim">no closed trades yet</td></tr>';
  // a transient upstream failure (LLM overload etc.) never surfaces the raw
  // error (which can carry an API key) — it shows as a calm 'retrying' note.
  // A short retry (minutes) is preferred over waiting for the next 6h slot.
  const soon = s.retry_cycle_at ? new Date(s.retry_cycle_at) : null;
  const nc = s.next_cycle ? new Date(s.next_cycle) : null;
  const when = soon ? soon : nc;
  let retryWhen = 'retrying shortly';
  if (when) {
    const mins = Math.max(0, Math.round((when - Date.now()) / 60000));
    const inTxt = mins < 60 ? `in ${mins} min` : `in ${Math.floor(mins / 60)}h ${mins % 60}m`;
    retryWhen = soon ? `retrying ${inTxt}` : `retrying at next decision · ${inTxt}`;
  }
  const FAIL = new Set(['error', 'invalid', 'no_key']);
  document.getElementById('log').innerHTML = '<tr><th>when</th><th>sleeve</th><th>what</th><th>why</th></tr>' +
    s.decisions.map(d => {
      const failed = FAIL.has(d.status);
      const cls = d.status==='executed' ? d.action : d.status==='held' ? 'hold' : failed ? 'dim' : 'err';
      const what = failed ? '⏳ RETRY' : (d.status==='held' ? 'HOLD' : (d.action||d.status).toUpperCase()) +
        (d.pair ? ' ' + d.pair : '') + (d.fraction ? ' ' + (d.fraction*100).toFixed(0)+'%' : '');
      const why = failed ? `temporary hiccup — ${retryWhen}` : (d.reasoning || d.detail || '');
      return `<tr><td class="dim">${d.at.slice(5,16)}</td><td>${d.sleeve||''}</td>` +
        `<td class="${cls}">${what}</td><td class="dim">${why}</td></tr>`;
    }).join('');
}
load(); setInterval(load, 30000);
</script></body></html>"""


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Magpie — settings</title>
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<style>
 body{font-family:system-ui;margin:1.5rem auto;padding:0 1rem;max-width:640px;
   background:#12151c;color:#e6e9f0}
 h1{font-size:1.3rem} a{color:#8b93a7}
 .dim{color:#8b93a7} .up{color:#4cd97b} .down{color:#ff6b6b}
 .card{background:#1a1f2b;border:1px solid #262d3c;border-radius:12px;padding:1rem 1.2rem;margin:.9rem 0}
 .eyebrow{font-size:.68rem;letter-spacing:.1em;text-transform:uppercase;color:#8b93a7;margin:0 0 .8rem}
 label{display:block;font-size:.8rem;color:#8b93a7;margin:.7rem 0 .2rem}
 input{width:100%;box-sizing:border-box;background:#12151c;border:1px solid #2a3140;
   border-radius:8px;color:#e6e9f0;padding:.55rem .7rem;font-size:.9rem;font-family:inherit}
 input:focus{outline:2px solid #4cd97b55;border-color:#4cd97b}
 .row{display:flex;gap:.6rem;align-items:center;margin-top:.9rem;flex-wrap:wrap}
 button{background:#2a3140;color:#e6e9f0;border:1px solid #3a4356;border-radius:8px;
   padding:.5rem 1rem;font-weight:600;cursor:pointer;font-size:.85rem}
 button.primary{background:#4cd97b;color:#0a0d12;border:0}
 button.test{background:transparent;color:#8b93a7}
 .result{font-size:.8rem;margin-left:.3rem}
 .note{font-size:.78rem;color:#5a6377;line-height:1.5}
 .tabs{display:flex;gap:.4rem;flex-wrap:wrap;margin:.2rem 0 1rem;position:sticky;top:0;background:#12151c;padding:.45rem 0;z-index:2}
 .tab-btn{background:#1a1f2b;color:#8b93a7;border:1px solid #262d3c;border-radius:999px;padding:.4rem .95rem;font-size:.82rem;font-weight:600;cursor:pointer}
 .tab-btn.active{background:#4cd97b;color:#0a0d12;border-color:#4cd97b}
 .card[hidden]{display:none}
</style></head><body>
<p style="margin:0 0 1rem"><a href="/" style="display:inline-block;background:#4cd97b;color:#0a0d12;
  font-weight:700;text-decoration:none;padding:.6rem 1.1rem;border-radius:9px;font-size:.95rem">←&nbsp; Back to dashboard</a></p>
<h1>🐦‍⬛ Magpie settings <span class="dim" id="mode" style="font-size:.9rem"></span></h1>
<p class="note">Secrets are stored on this machine only and
shown masked. Leave a secret field blank to keep the current value. Going <b>live</b> stays a
deliberate environment change (<code>TRADING_ENABLED</code>), never a setting here.</p>

<div class="tabs">
  <button type="button" class="tab-btn" data-tab="brain" onclick="setTab('brain')">Brain</button>
  <button type="button" class="tab-btn" data-tab="exchange" onclick="setTab('exchange')">Exchange</button>
  <button type="button" class="tab-btn" data-tab="trading" onclick="setTab('trading')">Trading</button>
  <button type="button" class="tab-btn" data-tab="notify" onclick="setTab('notify')">Notifications</button>
  <button type="button" class="tab-btn" data-tab="security" onclick="setTab('security')">Security</button>
</div>

<div class="card" data-tab="brain">
  <p class="eyebrow">The brain — LLM provider</p>
  <label>Provider (the active decision-maker)</label>
  <select id="LLM_PROVIDER">
    <option value="gemini">Gemini (Google)</option>
    <option value="openai">OpenAI (ChatGPT)</option>
    <option value="anthropic">Anthropic (Claude)</option>
    <option value="perplexity">Perplexity</option>
    <option value="grok">Grok (xAI)</option>
    <option value="deepseek">DeepSeek</option>
    <option value="github">GitHub Models (Copilot)</option>
    <option value="openrouter">OpenRouter (catch-all)</option>
  </select>
  <label>Model override — frequent decisions <span class="note">(blank = provider default)</span></label><input id="LLM_MODEL" placeholder="blank = default">
  <label>Model override — deep (slow sleeves + review)</label><input id="LLM_MODEL_DEEP" placeholder="blank = default">
  <div class="row"><button class="test" onclick="test('brain')">Test active brain</button>
    <span class="result" id="r-brain"></span></div>
  <p class="note" style="margin-top:1rem">Each provider uses its own key below — set the one for your chosen provider. A paid ChatGPT/Perplexity/Copilot <em>subscription</em> is not an API key; get a developer key from the provider's platform.</p>
  <label>Gemini API key</label><input id="GEMINI_API_KEY" placeholder="">
  <label>OpenAI API key</label><input id="OPENAI_API_KEY" placeholder="">
  <label>Anthropic (Claude) API key</label><input id="ANTHROPIC_API_KEY" placeholder="">
  <label>Perplexity API key</label><input id="PERPLEXITY_API_KEY" placeholder="">
  <label>Grok (xAI) API key</label><input id="GROK_API_KEY" placeholder="">
  <label>DeepSeek API key</label><input id="DEEPSEEK_API_KEY" placeholder="">
  <label>GitHub token (GitHub Models)</label><input id="GITHUB_TOKEN" placeholder="">
  <label>OpenRouter API key</label><input id="OPENROUTER_API_KEY" placeholder="">
  <label style="margin-top:.8rem">Gemini model (frequent) — legacy override</label><input id="GEMINI_MODEL">
  <label>Gemini deep model — legacy override</label><input id="GEMINI_MODEL_DEEP">
</div>

<div class="card" data-tab="exchange">
  <p class="eyebrow">The exchange — Kraken</p>
  <p class="note">Create the key with <b>query + trade</b> permissions only — never withdrawal.</p>
  <label>API key</label><input id="KRAKEN_API_KEY" placeholder="">
  <label>Private key</label><input id="KRAKEN_API_SECRET" placeholder="">
  <div class="row"><button class="test" onclick="test('kraken')">Test Kraken</button>
    <span class="result" id="r-kraken"></span></div>
</div>

<div class="card" data-tab="notify">
  <p class="eyebrow">Notifications</p>
  <p class="note">Fill in any channels you want — every alert (trades, top-ups, daily digest, errors, reviews) is sent to <b>all</b> configured channels. Leave the rest blank.</p>
  <label>Home Assistant — base URL</label><input id="HA_URL" placeholder="http://homeassistant.local:8123">
  <label>Home Assistant — long-lived token</label><input id="HA_TOKEN" placeholder="">
  <label>Home Assistant — notify service</label><input id="HA_NOTIFY_SERVICE" placeholder="notify.mobile_app_myphone">
  <label style="margin-top:1rem">Pushover — app token</label><input id="PUSHOVER_TOKEN" placeholder="">
  <label>Pushover — user / group key</label><input id="PUSHOVER_USER" placeholder="">
  <label style="margin-top:.6rem">Pushbullet — access token</label><input id="PUSHBULLET_TOKEN" placeholder="">
  <label style="margin-top:.6rem">Discord — webhook URL</label><input id="DISCORD_WEBHOOK_URL" placeholder="https://discord.com/api/webhooks/…">
  <label style="margin-top:.6rem">Telegram — bot token</label><input id="TELEGRAM_BOT_TOKEN" placeholder="">
  <label>Telegram — chat ID</label><input id="TELEGRAM_CHAT_ID" placeholder="">
  <label style="margin-top:.6rem">ntfy — topic</label><input id="NTFY_TOPIC" placeholder="my-magpie-alerts">
  <label>ntfy — server</label><input id="NTFY_SERVER" placeholder="https://ntfy.sh">
  <div class="row"><button class="test" onclick="test('ha')">Send test to all channels</button>
    <span class="result" id="r-ha"></span></div>
</div>

<div class="card" data-tab="trading">
  <p class="eyebrow">Base currency</p>
  <div id="ccy-locked" hidden><p style="margin:.2rem 0">Trading and valuing everything in <b id="ccy-cur"></b>.</p><p class="note">Chosen at initial setup and <b>locked</b> — it can't be changed once the bot has traded (safe: your holdings and exchange balance are in this currency).</p></div>
  <div id="ccy-choose" hidden>
    <p class="note">The currency Magpie trades against and values everything in. <b>This is permanent</b> — it locks the moment you set it (and automatically once the bot has traded). Choose it before funding the account.</p>
    <label>Currency</label>
    <select id="ccy-select"></select>
    <div class="row"><button class="test" onclick="currencySet()">Set permanently</button>
      <span class="result" id="r-ccy"></span></div>
  </div>
</div>

<div class="card" data-tab="trading">
  <p class="eyebrow">Location</p>
  <label>Timezone</label>
  <input id="TIMEZONE" placeholder="Europe/Dublin">
  <p class="note">Your IANA timezone — e.g. <code>America/New_York</code>, <code>Europe/London</code>, <code>Australia/Sydney</code>. Sets the clock the daily 06:00, Monday and 1st-of-month decision slots run on; match it to the schedule you set. Safe to change anytime.</p>
</div>

<div class="card" data-tab="trading">
  <p class="eyebrow">Strategy</p>
  <label>Base pairs — always tradeable (comma-separated)</label><input id="PAIRS" placeholder="BTC/EUR, ETH/EUR">
  <label>Profit skim to vault (0–1)</label><input id="SKIM_FRACTION">
  <label style="margin-top:1rem"><input type="checkbox" id="DYNAMIC_UNIVERSE_ENABLED" style="width:auto;margin-right:.5rem">
    Auto-track the top altcoins by market cap</label>
  <label>How many top altcoins to include</label><input id="DYNAMIC_TOP_N" placeholder="5">
  <label>Auto-sell a held coin once it drops past rank</label><input id="DYNAMIC_SELL_FLOOR_N" placeholder="10">
  <p class="note">A held alt that falls out of the top set stays sellable at the bot's discretion until it drops past this rank, then it's force-sold at the weekly refresh. Set equal to the number above to sell the instant a coin leaves the buy set.</p>
  <div class="row"><button class="test" onclick="refreshUniverse()">Refresh universe now</button>
    <span class="result" id="r-universe"></span></div>
</div>

<div class="card" data-tab="trading">
  <p class="eyebrow">Custom coins</p>
  <p class="note">Pin any coin that trades against EUR on Kraken — it's always tradeable and, unlike the auto-tracked alts, is never force-sold by the sell floor. Type a symbol (e.g. <code>ADA</code>) or a full pair (<code>LINK/EUR</code>).</p>
  <div class="row" style="align-items:stretch">
    <input id="pair-add" placeholder="ADA" style="flex:1" onkeydown="if(event.key==='Enter')pairAdd()">
    <button class="test" onclick="pairAdd()">Add coin</button>
  </div>
  <span class="result" id="r-pair"></span>
  <div id="pair-list" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px"></div>
</div>

<div class="card" data-tab="security">
  <p class="eyebrow">Security</p>
  <label>Dashboard password</label><input id="DASHBOARD_PASSWORD" type="password" placeholder="">
  <p class="note">Set a password to require login for the dashboard and controls. Blank = keep current; clearing it (type a space then delete) leaves it unchanged — remove via the env to disable auth.</p>

  <p style="margin-top:1.2rem"><b>Two-factor authentication (TOTP)</b> <span id="tfa-status" class="note"></span></p>
  <div id="tfa-nopw" class="note" hidden>Set and save a dashboard password first — 2FA sits on top of it.</div>
  <div id="tfa-off" hidden>
    <button class="test" onclick="tfaSetup()">Set up 2FA</button>
  </div>
  <div id="tfa-setup" hidden>
    <p class="note">Scan with Google Authenticator, Authy or 1Password, then enter a current code to confirm.</p>
    <div id="tfa-qr" style="background:#fff;display:inline-block;padding:8px;border-radius:8px;max-width:200px"></div>
    <p class="note">Or enter this key manually: <code id="tfa-secret"></code></p>
    <input id="tfa-code" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code">
    <div class="row"><button class="test" onclick="tfaEnable()">Confirm &amp; enable</button>
      <span class="result" id="tfa-r"></span></div>
  </div>
  <div id="tfa-codes" hidden style="margin-top:.8rem;border:1px solid #4cd97b;border-radius:8px;padding:.8rem">
    <p class="note" style="margin:0 0 .4rem"><b>⚠ Save these backup codes now — shown only once.</b> Each works once in place of your authenticator if you lose your phone.</p>
    <pre id="tfa-codes-list" style="margin:.4rem 0;font-size:1rem;line-height:1.7;user-select:all"></pre>
    <button class="test" onclick="document.getElementById('tfa-codes').hidden=true">I've saved them</button>
  </div>
  <div id="tfa-on" hidden>
    <p class="note">2FA is on. Backup codes left: <b id="tfa-remaining">–</b>.</p>
    <label>Regenerate backup codes (needs a current code)</label>
    <input id="tfa-rcode" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code">
    <div class="row"><button class="test" onclick="tfaRegen()">Regenerate backup codes</button>
      <span class="result" id="tfa-rr"></span></div>
    <label style="margin-top:.8rem">Disable 2FA (needs a current code)</label>
    <input id="tfa-dcode" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code">
    <div class="row"><button class="test" onclick="tfaDisable()">Disable 2FA</button>
      <span class="result" id="tfa-dr"></span></div>
  </div>
  <p class="note" style="margin-top:.6rem">Lost your authenticator? Clear it from the container:
    <code>docker exec magpie sqlite3 /data/magpie.db "DELETE FROM settings WHERE key IN ('totp_enabled','totp_secret')"</code></p>
</div>

<div class="row"><button class="primary" onclick="save()">Save settings</button>
  <span class="result" id="saved"></span>
  <span style="flex:1"></span><button class="test" onclick="fetch('/logout',{method:'POST'}).then(()=>location='/login')">Log out</button></div>

<script>
const SECRETS = ["GEMINI_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","PERPLEXITY_API_KEY","GROK_API_KEY","DEEPSEEK_API_KEY","GITHUB_TOKEN","OPENROUTER_API_KEY","KRAKEN_API_KEY","KRAKEN_API_SECRET","HA_TOKEN","PUSHOVER_TOKEN","PUSHOVER_USER","PUSHBULLET_TOKEN","DISCORD_WEBHOOK_URL","TELEGRAM_BOT_TOKEN","DASHBOARD_PASSWORD"];
const PLAIN = ["LLM_MODEL","LLM_MODEL_DEEP","GEMINI_MODEL","GEMINI_MODEL_DEEP","HA_URL","HA_NOTIFY_SERVICE","TELEGRAM_CHAT_ID","NTFY_TOPIC","NTFY_SERVER","PAIRS","SKIM_FRACTION","DYNAMIC_TOP_N","DYNAMIC_SELL_FLOOR_N","TIMEZONE"];
function setTab(name){
  document.querySelectorAll('.card[data-tab]').forEach(c => c.hidden = c.dataset.tab !== name);
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
}
async function load(){
  const s = await (await fetch('/api/settings',{cache:'no-store'})).json();
  document.getElementById('mode').textContent = '('+s.mode+')';
  PLAIN.forEach(k => { if (s[k] !== undefined) document.getElementById(k).value = s[k]; });
  if (s.LLM_PROVIDER) document.getElementById('LLM_PROVIDER').value = s.LLM_PROVIDER;
  document.getElementById('DYNAMIC_UNIVERSE_ENABLED').checked = !!s.DYNAMIC_UNIVERSE_ENABLED;
  SECRETS.forEach(k => {
    const el = document.getElementById(k);
    el.placeholder = s[k] && s[k].set ? (s[k].hint + " — set, blank to keep") : "not set";
  });
  tfaLoad();
  pairsLoad();
  currencyLoad();
}
async function currencyLoad(){
  const s = await (await fetch('/api/currency',{cache:'no-store'})).json();
  document.getElementById('ccy-locked').hidden = !s.locked;
  document.getElementById('ccy-choose').hidden = s.locked;
  document.getElementById('ccy-cur').textContent = s.currency + ' (' + s.symbol.trim() + ')';
  if (!s.locked){
    const sel = document.getElementById('ccy-select'); sel.innerHTML='';
    s.supported.forEach(c => { const o=document.createElement('option'); o.value=c; o.textContent=c;
      if(c===s.currency) o.selected=true; sel.appendChild(o); });
  }
}
async function currencySet(){
  const el = document.getElementById('r-ccy');
  const ccy = document.getElementById('ccy-select').value;
  if (!confirm('Set the base currency to '+ccy+' permanently? This is locked and cannot be changed later.')) return;
  el.className='result dim'; el.textContent='checking Kraken…';
  const r = await fetch('/api/currency/set',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({currency:ccy})});
  if (r.ok){ el.className='result up'; el.textContent='✓ locked to '+ccy; load(); }
  else { el.className='result down'; el.textContent='✗ '+(await r.text()); }
}
async function pairsLoad(){
  const s = await (await fetch('/api/pairs',{cache:'no-store'})).json();
  const box = document.getElementById('pair-list'); box.innerHTML='';
  (s.manual||[]).forEach(p => {
    const c = document.createElement('span');
    c.style.cssText='font-size:.9em;font-weight:700;padding:4px 6px 4px 11px;border-radius:9px;background:#1a2030;border:1px solid #2a3140;display:inline-flex;align-items:center;gap:8px';
    c.innerHTML = p + ' <a href="#" title="remove" style="color:#8b93a7;text-decoration:none;font-weight:800">×</a>';
    c.querySelector('a').onclick = (e)=>{ e.preventDefault(); pairRemove(p); };
    box.appendChild(c);
  });
  if (!(s.manual||[]).length) box.innerHTML='<span class="note">No custom coins pinned.</span>';
}
async function pairAdd(){
  const el = document.getElementById('r-pair'); const inp = document.getElementById('pair-add');
  if (!inp.value.trim()) return;
  el.className='result dim'; el.textContent='checking Kraken…';
  const r = await fetch('/api/pairs/add',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({symbol:inp.value})});
  if (r.ok){ const j=await r.json(); el.className='result up'; el.textContent='✓ added '+j.added; inp.value=''; pairsLoad(); }
  else { el.className='result down'; el.textContent='✗ '+(await r.text()); }
}
async function pairRemove(pair){
  const el = document.getElementById('r-pair'); el.className='result dim'; el.textContent='…';
  const r = await fetch('/api/pairs/remove',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({pair})});
  if (r.ok){ el.className='result up'; el.textContent='✓ removed '+pair; pairsLoad(); }
  else { el.className='result down'; el.textContent='✗ '+(await r.text()); }
}
async function tfaLoad(){
  const s = await (await fetch('/api/2fa',{cache:'no-store'})).json();
  const show = (id,on) => document.getElementById(id).hidden = !on;
  document.getElementById('tfa-status').textContent = s.enabled ? '· on ✓' : '· off';
  show('tfa-nopw', !s.password_set);
  show('tfa-off', s.password_set && !s.enabled);
  show('tfa-on', s.password_set && s.enabled);
  show('tfa-setup', false);
  document.getElementById('tfa-remaining').textContent = s.backup_remaining ?? '–';
}
function showBackupCodes(codes){
  document.getElementById('tfa-codes-list').textContent = (codes||[]).join('\\n');
  document.getElementById('tfa-codes').hidden = false;
}
async function tfaRegen(){
  const el = document.getElementById('tfa-rr'); el.className='result dim'; el.textContent='…';
  const r = await fetch('/api/2fa/backup',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:document.getElementById('tfa-rcode').value})});
  if (r.ok){ el.className='result up'; el.textContent='✓ new set below'; showBackupCodes((await r.json()).backup_codes); tfaLoad(); }
  else { el.className='result down'; el.textContent='✗ '+(await r.text()); }
}
async function tfaSetup(){
  const r = await (await fetch('/api/2fa/setup',{method:'POST'})).json();
  document.getElementById('tfa-qr').innerHTML = r.qr_svg;
  document.getElementById('tfa-secret').textContent = r.secret;
  document.getElementById('tfa-off').hidden = true;
  document.getElementById('tfa-setup').hidden = false;
}
async function tfaEnable(){
  const el = document.getElementById('tfa-r'); el.className='result dim'; el.textContent='checking…';
  const r = await fetch('/api/2fa/enable',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:document.getElementById('tfa-code').value})});
  if (r.ok){ el.className='result up'; el.textContent='✓ 2FA on'; showBackupCodes((await r.json()).backup_codes); tfaLoad(); }
  else { el.className='result down'; el.textContent='✗ '+(await r.text()); }
}
async function tfaDisable(){
  const el = document.getElementById('tfa-dr'); el.className='result dim'; el.textContent='checking…';
  const r = await fetch('/api/2fa/disable',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:document.getElementById('tfa-dcode').value})});
  if (r.ok){ el.className='result up'; el.textContent='✓ 2FA off'; tfaLoad(); }
  else { el.className='result down'; el.textContent='✗ '+(await r.text()); }
}
async function refreshUniverse(){
  const el = document.getElementById('r-universe'); el.className='result dim'; el.textContent='refreshing…';
  const r = await (await fetch('/api/universe/refresh',{method:'POST'})).json();
  el.className = 'result ' + (r.status==='ok' ? 'up' : 'down');
  el.textContent = r.status==='ok' ? ('✓ now: ' + (r.effective||[]).join(', ')) : ('✗ ' + (r.detail||r.status));
}
async function save(){
  const body = {};
  PLAIN.forEach(k => body[k] = document.getElementById(k).value);
  body.LLM_PROVIDER = document.getElementById('LLM_PROVIDER').value;
  body.DYNAMIC_UNIVERSE_ENABLED = document.getElementById('DYNAMIC_UNIVERSE_ENABLED').checked;
  SECRETS.forEach(k => { const v = document.getElementById(k).value.trim(); if (v) body[k] = v; });
  const r = await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)});
  const el = document.getElementById('saved');
  if (r.ok){ const j = await r.json(); el.className='result up';
    el.textContent = '✓ saved '+j.saved.length+' field(s) · mode '+j.mode;
    SECRETS.forEach(k => document.getElementById(k).value='');
    load();
  } else { el.className='result down'; el.textContent = '✗ '+(await r.text()); }
}
async function test(t){
  const el = document.getElementById('r-'+t); el.className='result dim'; el.textContent='testing…';
  const r = await (await fetch('/api/settings/test?target='+t,{method:'POST'})).json();
  el.className = 'result '+(r.ok?'up':'down');
  let msg = (r.ok?'✓ ':'✗ ')+(r.detail||'');
  if (t==='kraken' && r.ok) msg += r.withdrawal_blocked ? ' · withdrawal blocked ✓' : ' · ⚠ KEY CAN WITHDRAW';
  el.textContent = msg;
}
setTab('brain');
load();
</script></body></html>"""
