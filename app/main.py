import logging

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from . import __version__, config, db, engine, market, portfolio

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOGGER = logging.getLogger(__name__)

app = FastAPI(title="Magpie", version=__version__)


@app.get("/health")
def health():
    conn = db.connect()
    try:
        last = conn.execute("SELECT at, sleeve, status FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        snap = conn.execute("SELECT SUM(total_eur) t FROM snapshots WHERE id IN "
                            "(SELECT MAX(id) FROM snapshots GROUP BY sleeve)").fetchone()
        return {
            "ok": True, "version": __version__, "mode": config.mode(),
            "halted": db.get_setting(conn, "halted") == "1",
            "gemini_configured": bool(config.GEMINI_API_KEY),
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
        return {"mode": config.mode(), "prices": prices, "overview": ov,
                "halted": db.get_setting(conn, "halted") == "1",
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
<h1>🐦‍⬛ Magpie <span class="dim" id="mode"></span></h1>
<div class="card"><div class="dim">Total</div><div class="big" id="equity">…</div></div>
<div class="row" id="sleeves"></div>
<div class="card"><div class="dim">Recent decisions</div><table id="log"></table></div>
<div class="card"><button onclick="if(confirm('Halt all trading?'))fetch('/api/halt',{method:'POST'}).then(()=>load())">⛔ HALT TRADING</button>
<span class="dim" id="halted"></span></div>
<script>
async function load(){
  const s = await (await fetch('/api/state')).json();
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
  document.getElementById('log').innerHTML = '<tr><th>when</th><th>sleeve</th><th>what</th><th>why</th></tr>' +
    s.decisions.map(d => `<tr><td class="dim">${d.at.slice(5,16)}</td><td>${d.sleeve||''}</td>` +
      `<td class="${d.status==='executed' ? d.action : d.status==='held' ? 'hold' : 'err'}">` +
      `${d.status==='held' ? 'HOLD' : (d.action||d.status).toUpperCase()}` +
      `${d.pair ? ' ' + d.pair : ''}${d.fraction ? ' ' + (d.fraction*100).toFixed(0)+'%' : ''}</td>` +
      `<td class="dim">${(d.reasoning || d.detail || '')}</td></tr>`).join('');
}
load(); setInterval(load, 30000);
</script></body></html>"""
