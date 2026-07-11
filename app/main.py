import logging

from fastapi import Body, FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from . import __version__, advisor, auth, config, db, engine, ha, ledger, market, portfolio, universe

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Magpie", version=__version__)

# apply any web-entered settings over the env at boot (before the first cycle)
try:
    _c = db.connect()
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


@app.get("/login", response_class=HTMLResponse)
def login_page(bad: int = 0):
    err = '<p style="color:#ff6b6b">Wrong password.</p>' if bad else ''
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Magpie — login</title>
<style>body{{font-family:system-ui;background:#12151c;color:#e6e9f0;display:flex;min-height:100vh;
margin:0;align-items:center;justify-content:center}}form{{background:#1a1f2b;border:1px solid #262d3c;
border-radius:12px;padding:2rem;width:280px}}h1{{font-size:1.3rem;margin:0 0 1rem}}
input{{width:100%;box-sizing:border-box;background:#12151c;border:1px solid #2a3140;border-radius:8px;
color:#e6e9f0;padding:.6rem;font-size:1rem;margin:.5rem 0}}button{{width:100%;background:#4cd97b;color:#0a0d12;
border:0;border-radius:8px;padding:.6rem;font-weight:700;font-size:1rem;cursor:pointer;margin-top:.5rem}}</style>
</head><body><form method="post" action="/login"><h1>🐦‍⬛ Magpie</h1>{err}
<input type="text" name="username" placeholder="Username" autofocus autocomplete="username">
<input type="password" name="password" placeholder="Password" autocomplete="current-password">
<button type="submit">Sign in</button></form></body></html>"""


@app.post("/login")
def login_submit(username: str = Form(""), password: str = Form("")):
    conn = db.connect()
    try:
        if auth.check_login(conn, username, password):
            resp = RedirectResponse("/", status_code=302)
            resp.set_cookie(auth.COOKIE, auth.token(conn), httponly=True,
                            max_age=30 * 86400, samesite="lax")
            return resp
        return RedirectResponse("/login?bad=1", status_code=302)
    finally:
        conn.close()


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(auth.COOKIE)
    return resp


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


@app.post("/api/digest")
def api_digest():
    return engine.daily_digest()


@app.post("/api/review")
def api_review():
    """Monthly self-review: distil the ledger into a lessons note (see engine)."""
    return engine.self_review()


@app.post("/api/reconcile")
def api_reconcile():
    """Nightly: absorb drift between the sleeve books and exchange reality."""
    conn = db.connect()
    try:
        return ledger.reconcile(conn, config.mode(), market.tickers(config.PAIRS))
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
            return {"ok": ok, "detail": "check your phone" if ok else "HA not configured / unreachable"}
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
        return portfolio.apply_topup(conn, "paper", amount)
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
    """When the next scheduled decision cycle fires (00/06/12/18 Dublin)."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("Europe/Dublin"))
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
                "next_cycle": _next_cycle_iso(),
                "halted": db.get_setting(conn, "halted") == "1",
                "lessons": {"text": db.get_setting(conn, "lessons"),
                            "at": db.get_setting(conn, "lessons_at")},
                "benchmark": ledger.bench_value(conn, config.mode(), prices),
                "equity_curve": list(reversed(curve)),
                "trips": trips[:15], "trip_stats": ledger.trip_stats(trips),
                "decisions": decisions, "skims": skims}
    finally:
        conn.close()


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Magpie</title>
<style>
 body{font-family:system-ui;margin:1.5rem auto;padding:0 1rem;background:#12151c;color:#e6e9f0;max-width:980px}
 h1{font-size:1.4rem} .big{font-size:2rem;font-weight:700}
 .dim{color:#8b93a7} .card{background:#1a1f2b;border-radius:10px;padding:1rem;margin:.8rem 0}
 .row{display:flex;gap:.8rem;flex-wrap:wrap} .row .card{flex:1;min-width:180px;margin:0}
 .slv{font-size:1.3rem;font-weight:700} .up{color:#4cd97b}.down{color:#ff6b6b}
 table{width:100%;border-collapse:collapse;font-size:.85rem}
 td,th{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #2a3140}
 .hold{color:#8b93a7}.buy{color:#4cd97b}.sell{color:#ff6b6b}.err{color:#ffb020}
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
<div class="card"><div class="dim">Closed trades <span id="tstats" style="float:right"></span></div><table id="trades"></table></div>
<div class="card" id="lessons-card" hidden><div class="dim" id="lessons-when"></div>
<p class="dim" id="lessons-text" style="font-size:.85rem;line-height:1.55;margin:.4rem 0 0"></p></div>
<div class="card"><div class="dim">Recent decisions</div><table id="log"></table></div>
<div class="card"><button onclick="if(confirm('Halt all trading?'))fetch('/api/halt',{method:'POST'}).then(()=>load())">⛔ HALT TRADING</button>
<span class="dim" id="halted"></span></div>
<script>
async function load(){
  const s = await (await fetch('/api/state', {cache: 'no-store'})).json();
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
  document.getElementById('equity').textContent = `€${s.overview.total_eur.toFixed(2)}`;
  document.getElementById('halted').textContent = s.halted ? " halted — POST /api/resume to re-arm" : "";
  document.getElementById('sleeves').innerHTML = s.overview.sleeves.map(v => {
    const d = v.total_eur - v.allocated;
    const assets = Object.keys(v.holdings).filter(k => k !== 'EUR');
    return `<div class="card"><div class="dim">${v.sleeve}</div>` +
      `<div class="slv">€${v.total_eur.toFixed(2)}</div>` +
      `<div class="${d >= 0 ? 'up' : 'down'}">${d >= 0 ? '+' : ''}${d.toFixed(2)}</div>` +
      `<div class="dim">${assets.length ? assets.join(', ') : 'in cash'}</div></div>`;
  }).join('');
  // hero: P/L on invested + the vs-hodl sentence (#9)
  const invested = s.overview.sleeves.reduce((a, v) => a + (v.allocated || 0), 0);
  const pnl = s.overview.total_eur - invested;
  const pnlEl = document.getElementById('pnl');
  pnlEl.textContent = invested > 0
    ? `${pnl >= 0 ? '+' : '−'}€${Math.abs(pnl).toFixed(2)} (${(pnl / invested * 100).toFixed(1)}%) on €${invested.toFixed(2)} invested`
    : '';
  pnlEl.className = pnl >= 0 ? 'up' : 'down';
  const vsEl = document.getElementById('vs');
  if (s.benchmark) {
    const edge = s.overview.total_eur - s.benchmark.hodl_eur;
    vsEl.innerHTML = `vs buy-and-hold <b style="color:#e6e9f0">€${s.benchmark.hodl_eur.toFixed(2)}</b><br>` +
      `the magpie is <b class="${edge >= 0 ? 'up' : 'down'}">${edge >= 0 ? '+' : '−'}€${Math.abs(edge).toFixed(2)} ` +
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
  // equity sparkline
  const c = s.equity_curve || [];
  if (c.length > 1) {
    const vals = c.map(p => p.eur);
    const mn = Math.min(...vals), mx = Math.max(...vals), span = (mx - mn) || 1;
    const pts = vals.map((v, i) =>
      `${(i / (vals.length - 1) * 600).toFixed(1)},${(110 - (v - mn) / span * 100).toFixed(1)}`).join(' ');
    document.getElementById('chart').innerHTML =
      `<polyline points="${pts}" fill="none" stroke="#4cd97b" stroke-width="2"/>`;
  }
  // closed trades
  const ts = s.trip_stats;
  document.getElementById('tstats').textContent = ts
    ? `${ts.closed_trades} closed · ${ts.win_rate_pct}% wins · €${ts.total_pnl_eur}` : '';
  document.getElementById('trades').innerHTML = (s.trips && s.trips.length)
    ? '<tr><th>sleeve</th><th>pair</th><th>in→out</th><th>held</th><th>P/L</th></tr>' +
      s.trips.map(t => `<tr><td>${t.sleeve}</td><td>${t.pair}</td>` +
        `<td class="dim">${t.entry_price.toFixed(0)}→${t.exit_price.toFixed(0)}</td>` +
        `<td class="dim">${t.held_days}d</td>` +
        `<td class="${t.pnl_eur >= 0 ? 'up' : 'down'}">€${t.pnl_eur.toFixed(2)} (${t.pnl_pct}%)</td></tr>`).join('')
    : '<tr><td class="dim">no closed trades yet</td></tr>';
  document.getElementById('log').innerHTML = '<tr><th>when</th><th>sleeve</th><th>what</th><th>why</th></tr>' +
    s.decisions.map(d => `<tr><td class="dim">${d.at.slice(5,16)}</td><td>${d.sleeve||''}</td>` +
      `<td class="${d.status==='executed' ? d.action : d.status==='held' ? 'hold' : 'err'}">` +
      `${d.status==='held' ? 'HOLD' : (d.action||d.status).toUpperCase()}` +
      `${d.pair ? ' ' + d.pair : ''}${d.fraction ? ' ' + (d.fraction*100).toFixed(0)+'%' : ''}</td>` +
      `<td class="dim">${(d.reasoning || d.detail || '')}</td></tr>`).join('');
}
load(); setInterval(load, 30000);
</script></body></html>"""


@app.get("/settings", response_class=HTMLResponse)
def settings_page():
    return """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Magpie — settings</title>
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
</style></head><body>
<p style="margin:0 0 1rem"><a href="/" style="display:inline-block;background:#4cd97b;color:#0a0d12;
  font-weight:700;text-decoration:none;padding:.6rem 1.1rem;border-radius:9px;font-size:.95rem">←&nbsp; Back to dashboard</a></p>
<h1>🐦‍⬛ Magpie settings <span class="dim" id="mode" style="font-size:.9rem"></span></h1>
<p class="note">Secrets are stored on this machine only and
shown masked. Leave a secret field blank to keep the current value. Going <b>live</b> stays a
deliberate environment change (<code>TRADING_ENABLED</code>), never a setting here.</p>

<div class="card">
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

<div class="card">
  <p class="eyebrow">The exchange — Kraken</p>
  <p class="note">Create the key with <b>query + trade</b> permissions only — never withdrawal.</p>
  <label>API key</label><input id="KRAKEN_API_KEY" placeholder="">
  <label>Private key</label><input id="KRAKEN_API_SECRET" placeholder="">
  <div class="row"><button class="test" onclick="test('kraken')">Test Kraken</button>
    <span class="result" id="r-kraken"></span></div>
</div>

<div class="card">
  <p class="eyebrow">Notifications — Home Assistant (optional)</p>
  <label>Base URL</label><input id="HA_URL" placeholder="http://homeassistant.local:8123">
  <label>Long-lived token</label><input id="HA_TOKEN" placeholder="">
  <label>Notify service</label><input id="HA_NOTIFY_SERVICE" placeholder="notify.mobile_app_myphone">
  <div class="row"><button class="test" onclick="test('ha')">Send test push</button>
    <span class="result" id="r-ha"></span></div>
</div>

<div class="card">
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

<div class="card">
  <p class="eyebrow">Security</p>
  <label>Dashboard password</label><input id="DASHBOARD_PASSWORD" type="password" placeholder="">
  <p class="note">Set a password to require login for the dashboard and controls. Blank = keep current; clearing it (type a space then delete) leaves it unchanged — remove via the env to disable auth.</p>
</div>

<div class="row"><button class="primary" onclick="save()">Save settings</button>
  <span class="result" id="saved"></span>
  <span style="flex:1"></span><button class="test" onclick="fetch('/logout',{method:'POST'}).then(()=>location='/login')">Log out</button></div>

<script>
const SECRETS = ["GEMINI_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY","PERPLEXITY_API_KEY","GROK_API_KEY","DEEPSEEK_API_KEY","GITHUB_TOKEN","OPENROUTER_API_KEY","KRAKEN_API_KEY","KRAKEN_API_SECRET","HA_TOKEN","DASHBOARD_PASSWORD"];
const PLAIN = ["LLM_MODEL","LLM_MODEL_DEEP","GEMINI_MODEL","GEMINI_MODEL_DEEP","HA_URL","HA_NOTIFY_SERVICE","PAIRS","SKIM_FRACTION","DYNAMIC_TOP_N","DYNAMIC_SELL_FLOOR_N"];
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
load();
</script></body></html>"""
