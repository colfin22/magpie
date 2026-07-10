import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from . import __version__, config, db, engine, ledger, market, portfolio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Magpie", version=__version__)


@app.middleware("http")
async def no_store(request, call_next):
    # browsers must never serve stale portfolio state (#4)
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
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
            "gemini_configured": bool(config.GEMINI_API_KEY),
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
        return {"mode": config.mode(), "prices": prices, "overview": ov,
                "halted": db.get_setting(conn, "halted") == "1",
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
 body{font-family:system-ui;margin:1.5rem;background:#12151c;color:#e6e9f0;max-width:980px}
 h1{font-size:1.4rem} .big{font-size:2rem;font-weight:700}
 .dim{color:#8b93a7} .card{background:#1a1f2b;border-radius:10px;padding:1rem;margin:.8rem 0}
 .row{display:flex;gap:.8rem;flex-wrap:wrap} .row .card{flex:1;min-width:180px;margin:0}
 .slv{font-size:1.3rem;font-weight:700} .up{color:#4cd97b}.down{color:#ff6b6b}
 table{width:100%;border-collapse:collapse;font-size:.85rem}
 td,th{padding:.35rem .5rem;text-align:left;border-bottom:1px solid #2a3140}
 .hold{color:#8b93a7}.buy{color:#4cd97b}.sell{color:#ff6b6b}.err{color:#ffb020}
 button{background:#ff6b6b;color:#000;border:0;border-radius:8px;padding:.6rem 1.2rem;font-weight:700}
</style></head><body>
<h1>🐦‍⬛ Magpie <span class="dim" id="mode"></span> <span class="dim" id="updated" style="float:right;font-size:.8rem"></span></h1>
<div class="card"><div class="dim">Total <span id="bench" style="float:right"></span></div>
<div class="big" id="equity">…</div>
<svg id="chart" viewBox="0 0 600 120" preserveAspectRatio="none"
     style="width:100%;height:120px;margin-top:.5rem"></svg></div>
<div class="row" id="sleeves"></div>
<div class="card"><div class="dim">Closed trades <span id="tstats" style="float:right"></span></div><table id="trades"></table></div>
<div class="card"><div class="dim">Recent decisions</div><table id="log"></table></div>
<div class="card"><button onclick="if(confirm('Halt all trading?'))fetch('/api/halt',{method:'POST'}).then(()=>load())">⛔ HALT TRADING</button>
<span class="dim" id="halted"></span></div>
<script>
async function load(){
  const s = await (await fetch('/api/state', {cache: 'no-store'})).json();
  document.getElementById('updated').textContent = 'updated ' + new Date().toLocaleTimeString();
  document.getElementById('mode').textContent = `(${s.mode}${s.halted ? " — HALTED" : ""})`;
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
  // benchmark line
  if (s.benchmark) {
    const edge = s.overview.total_eur - s.benchmark.hodl_eur;
    const el = document.getElementById('bench');
    el.textContent = `hodl €${s.benchmark.hodl_eur.toFixed(2)} · bot ${edge >= 0 ? '+' : ''}${edge.toFixed(2)}`;
    el.className = edge >= 0 ? 'up' : 'down';
  }
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
