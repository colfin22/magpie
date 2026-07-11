# 🐦‍⬛ Magpie

[![Version](https://img.shields.io/github/v/release/colfin22/magpie?label=version&color=blue)](https://github.com/colfin22/magpie/releases)
[![CI](https://github.com/colfin22/magpie/actions/workflows/ci.yml/badge.svg)](https://github.com/colfin22/magpie/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

An autonomous, self-hosted crypto trading bot with an LLM for a brain. You give it
a small stake on Kraken and an API key for the LLM of your choice; it manages the
money on its own schedule, keeps a full written diary of every thought, and pushes
you a daily digest. Your only control is the halt button — by design.

> **⚠️ This is an experiment, not a product.** An LLM has no proven trading edge.
> Magpie is built for people who want to *watch an AI manage a toy stake* with
> real rails around it — not for money you can't afford to lose. Expect anything
> from slow bleed to pleasant surprise. The house always takes 0.4% a trade.

<p align="center">
  <img src="docs/screenshots/dashboard-preview.png" width="92%"
       alt="The Magpie dashboard — the dynamic alt universe, equity vs buy-and-hold, the four sleeves holding SOL/XRP/TRX/BTC, closed alt trades, vault skims, the monthly lessons note and the decision diary including a top-10 sell-floor auto-exit">
  <br><sub>Dashboard a few months in, trading the top-5 alt universe (simulated preview data — a fresh install starts humbler).</sub>
</p>

## How it thinks

Four **sleeves**, each an independent sub-portfolio with its own books, its own
mandate, and its own decision cadence:

| Sleeve | Horizon | Decides | Funded by |
|---|---|---|---|
| **swing** | ~1–3 days | every 6 h | ⅓ of the stake |
| **fortnight** | 1–2 weeks | daily | ⅓ of the stake |
| **quarter** | weeks–3 months | Mondays | ⅓ of the stake |
| **vault** | a year+ | 1st of month | **profits only** |

Each decision cycle, the engine builds a context pack — that sleeve's holdings,
market data with computed indicators (EMA 20/50/200, RSI, multi-horizon returns),
its own recent decision history, and a fee reminder — and asks its LLM for a
strict-JSON decision: buy / sell / hold, pair, fraction, confidence, reasoning.

**The validation layer is the real boundary**: only whitelisted pairs, spot only,
long only, exchange minimums enforced, balances reconciled — and any malformed,
out-of-universe or errored answer resolves to HOLD. The model's words never touch
the exchange; only validated orders do.

**The vault** starts empty. Whenever an active sleeve's value exceeds its
high-water mark, half of the *realised* profit (EUR actually banked, not paper
gains) is skimmed into the vault, and the mark ratchets up. The vault accumulates
long-term positions out of house winnings only — losing sleeves are never
refilled from it.

**It learns from itself.** Once a month it re-reads its own diary — every
decision, what it reasoned at the time, and what actually happened — and writes
itself a lessons note that is injected into all future prompts. The only memory
it carries forward is the one it earns.

**It trades like a local.** Orders go in as post-only limits at the touch
(maker fee ~0.25%) with a patient window before falling back to market
(~0.40%) — a guaranteed saving on every fill that patience can win. Decisions
see daily *and* 4-hour indicators, the live spread, and the Crypto Fear & Greed
index; the slow sleeves think with a stronger model than the fast ones.

**It keeps score against a hodler.** From its first sight of capital it tracks a
phantom buy-and-hold portfolio of the same money (topped up in lockstep) — the
dashboard and daily digest always show whether the AI is beating doing nothing,
and the monthly self-review is confronted with the number. Closed trades are
FIFO-paired into round trips (win rate, average win/loss, hold time), and a
nightly reconciliation keeps the virtual sleeve books honest against real
exchange balances.

**Top-ups:** deposit more EUR to the exchange whenever you like. The bot notices
the surplus at its next cycle, splits it equally across the three active sleeves,
and raises their high-water marks so fresh cash is never mistaken for profit.

## The brain — pick your LLM

Magpie isn't wed to one model. `LLM_PROVIDER` chooses who makes the call:

| Provider | `LLM_PROVIDER` | API key from |
|---|---|---|
| **Gemini** (Google) — default | `gemini` | [aistudio.google.com](https://aistudio.google.com/apikey) |
| **OpenAI** (ChatGPT) | `openai` | platform.openai.com |
| **Anthropic** (Claude) | `anthropic` | console.anthropic.com |
| **Perplexity** | `perplexity` | perplexity.ai → API |
| **Grok** (xAI) | `grok` | x.ai |
| **DeepSeek** | `deepseek` | platform.deepseek.com |
| **GitHub Models** (Copilot) | `github` | github.com personal access token |
| **OpenRouter** — one key, any model above and more | `openrouter` | openrouter.ai |

Each provider uses its own key — set the one for the brain you pick. The prompt
and the strict-JSON safety layer are identical whichever model answers, so a
model that formats badly simply resolves to HOLD. `LLM_MODEL` / `LLM_MODEL_DEEP`
override the per-provider default models (the deep one runs the slow sleeves and
the monthly self-review; the fast one runs the rest). Switch live from the
settings page — dropdown, key, **Test active brain**, save; no restart.

> A paid ChatGPT / Perplexity / Copilot **subscription is not an API key** —
> each needs a developer key from the provider's platform, billed per token.
> Gemini's free tier is enough to run Magpie outright.

## Safety rails (the non-negotiables)

- **The API key cannot withdraw.** Create it with query + trade permissions only.
  Worst-case compromise of the box = bad trades, never stolen funds. (The repo's
  verification snippet in `docs/` probes this.)
- **It can never deposit** — there is no payment integration. The stake you fund
  is the ceiling.
- **Spot only, long only, whitelisted pairs only** — no leverage, no derivatives,
  no liquidations.
- **Kill switch**: `POST /api/halt` (or the big red button on the dashboard)
  stops all ordering until `POST /api/resume`.
- **Total auditability**: every prompt sent to the model and every raw response
  is stored. The dashboard diary shows what it saw, what it thought, and what it
  did — for every cent that moves.

There is deliberately **no auto-stop-loss and no position cap** — the operator
takes the risk knowingly and holds the halt button. Add your own guardrails if
that's not your temperament.

## Run

**You need:** Docker, a **dedicated [Kraken](https://kraken.com) account that
holds only the money you want Magpie to manage**, a trade-only API key (see
above), and an API key for one supported LLM — **Gemini** is the default and its
[free tier](https://aistudio.google.com/apikey) is plenty to start (the bot makes
only a handful of calls a day). See [The brain](#the-brain--pick-your-llm) for the
alternatives.

> **⚠️ Use an empty Kraken account.** On its first live cycle Magpie treats the
> *entire* balance of the account as its starting stake and splits it across the
> sleeves, and its nightly reconciliation pulls any coin sitting on the account
> into its own books. So fund a **fresh account with nothing else in it** — just
> the stake you're giving Magpie. Don't point it at an account holding coins or
> cash you don't want it trading.

```
cp .env.example .env     # fill in your Kraken + LLM keys; leave TRADING_ENABLED=false
docker compose up -d --build
```

The bot starts in **paper mode**: identical code path, live market data,
simulated fills against a pretend stake. Watch the diary at `http://<host>:8000`
until you trust it, then set `TRADING_ENABLED=true` and recreate the container.
At its first live cycle it treats your entire exchange EUR balance as the opening
top-up and splits it across the sleeves.

Schedule the heartbeat (systemd timer or cron):

```
0 0,6,12,18 * * *  curl -s -X POST http://localhost:8000/api/cycle
5 18 * * *         curl -s -X POST http://localhost:8000/api/digest
45 5 * * *         curl -s -X POST http://localhost:8000/api/reconcile
30 5 1 * *         curl -s -X POST http://localhost:8000/api/review
```

The cycle endpoint is safe to call at any hour — sleeve cadences are gated
internally (fortnight only acts on the 06:00 call, quarter on Monday's, the
vault on the 1st of the month).

## Running on Windows (no coding needed)

You don't need to be technical or write any code. Magpie runs inside **Docker
Desktop** — a free app that does the hard part for you. Here's the whole thing,
step by step.

> **🖥️ Magpie needs to stay running.** It only makes decisions when the machine
> is switched on, Docker Desktop is running, and the scheduled tasks can fire. So
> install it on a computer that's on all (or most) of the time — an always-on
> desktop, a mini-PC or a home server is ideal; a laptop that sleeps or gets shut
> down will simply skip decisions until it's back on (no harm, but it can't trade
> while it's off). On whatever machine you pick, set Windows to **never sleep**
> (Settings → System → Power → Screen and sleep) and set **Docker Desktop to start
> when you sign in** (Docker Desktop → Settings → General).

### 1. Install Docker Desktop
- Go to **[docker.com/products/docker-desktop](https://www.docker.com/products/docker-desktop/)** and click **Download for Windows**.
- Run the file you downloaded, click through with the default options, and **restart your PC** if it asks you to.
- Open **Docker Desktop** from the Start menu. The first launch takes a minute — wait until the whale icon near your clock stops animating and the app says **"Engine running"**. Leave it open.

### 2. Download Magpie
- Go to **[github.com/colfin22/magpie](https://github.com/colfin22/magpie)**.
- Click the green **`<> Code`** button, then **Download ZIP**.
- Find the ZIP in your **Downloads**, right-click it → **Extract All…** → **Extract**. You'll get a folder named `magpie-master`. Move it somewhere easy to find, like your **Documents**.

### 3. Enter your keys
- Open the `magpie-master` folder and find the file **`.env.example`**.
- **Copy it** (click it, Ctrl+C, then Ctrl+V) and **rename the copy to exactly `.env`** — delete the `.example` part. *(If Windows won't let a name start with a dot, type `.env.` with a dot on the end and Windows removes it for you.)*
- **Right-click `.env` → Open with → Notepad.** Fill in:
  - your **Kraken** API key and secret (create it with *query + trade* only — **never** withdrawal),
  - one **LLM** key — a free **[Gemini key](https://aistudio.google.com/apikey)** is the easiest start,
  - your **`TIMEZONE`** (e.g. `America/New_York`),
  - leave **`TRADING_ENABLED=false`** so it starts in safe **paper mode**.
- **Save** and close Notepad.

### 4. Start it
- Open the `magpie-master` folder. Click the **address bar** at the top (where the folder path is), type **`powershell`**, and press **Enter** — a blue window opens, already pointed at the folder.
- Type this line and press **Enter**:
  ```
  docker compose up -d --build
  ```
- The first run downloads and builds everything — give it a few minutes. It's finished when the blue window shows a fresh prompt again.

### 5. Open the dashboard
- In your web browser, go to **[http://localhost:8000](http://localhost:8000)**. That's Magpie.
- It's in **paper mode** (pretend money), so you can watch it make decisions with zero risk before committing real money.

### 6. Keep it deciding automatically
Magpie only makes decisions when it's "poked" a few times a day. On Windows you set that up once with **Task Scheduler** (the built-in Windows scheduler):
- Open the Start menu, type **PowerShell**, **right-click → Run as administrator**, and click **Yes**.
- Paste these four lines (all at once is fine) and press **Enter**:
  ```
  schtasks /create /tn "Magpie cycle"     /sc daily /st 00:00 /ri 360 /du 24:00 /tr "curl.exe -X POST http://localhost:8000/api/cycle"
  schtasks /create /tn "Magpie digest"    /sc daily /st 18:05 /tr "curl.exe -X POST http://localhost:8000/api/digest"
  schtasks /create /tn "Magpie reconcile" /sc daily /st 05:45 /tr "curl.exe -X POST http://localhost:8000/api/reconcile"
  schtasks /create /tn "Magpie review"    /sc monthly /d 1 /st 05:30 /tr "curl.exe -X POST http://localhost:8000/api/review"
  ```
- That pokes the bot every 6 hours (midnight, 6am, noon, 6pm) plus a daily summary and a monthly review. Because you set your `TIMEZONE` in step 3, these line up with your own clock.

### Going live with real money
When you're happy watching the paper-mode diary, open **`.env`** in Notepad, change `TRADING_ENABLED=false` to **`TRADING_ENABLED=true`**, save, then in the blue PowerShell window run:
```
docker compose up -d --force-recreate
```
From then on it trades the real balance in your (dedicated, empty) Kraken account. Remember the [empty-account warning](#run) above.

### If something isn't working
- **Nothing loads / commands fail?** Make sure **Docker Desktop is open and shows "Engine running"** — Magpie can't run without it. To avoid this, set it to start automatically: Docker Desktop → **Settings → General → *Start Docker Desktop when you sign in***.
- **`http://localhost:8000` won't open?** Wait a minute after step 4 and refresh — the first build takes a moment to finish.
- **Do I need Python, Linux or WSL?** No. Docker Desktop sets up everything it needs behind the scenes.

## Updating on Windows

Good news: your keys (`.env`) and everything Magpie has learned and recorded (the
**`data`** folder — trading history, settings, your 2FA) are **not** part of the
download. So updating is just swapping in the newer code — your account and
history carry straight over.

1. Have a look at the **[latest release](https://github.com/colfin22/magpie/releases)** to see what's new.
2. Download the new **ZIP** the same way as before — the green **`<> Code`** button → **Download ZIP** — then right-click it → **Extract All…**.
3. Open the **freshly extracted** `magpie-master` folder, press **Ctrl+A** (select everything), then **Ctrl+C** (copy).
4. Go to your **existing** Magpie folder, press **Ctrl+V** (paste). When Windows asks, choose **"Replace the files in the destination."**
   *This overwrites the old code with the new version. It does **not** touch your `.env` or your `data` folder — the download doesn't contain either of them.*
5. Open **PowerShell** in the folder (type `powershell` in the address bar) and run:
   ```
   docker compose up -d --build
   ```
6. When it finishes, open **[http://localhost:8000](http://localhost:8000)** and press **Ctrl+Shift+R** — that forces your browser to load the new dashboard instead of a saved old one.

That's it. Your holdings, diary, settings and 2FA are all intact, and your
scheduled tasks from setup keep working unchanged.

> **⚠️ Never delete the `data` folder.** It *is* your database — trading history,
> settings and your 2FA secret. Deleting it resets Magpie to a blank install.

**Prefer the command line?** If you installed with `git clone`, updating is just
`git pull` in the folder followed by `docker compose up -d --build`.

## Uninstalling or starting fresh (Windows)

**Just pause it for a while** (keeps everything, easy to resume) — in **PowerShell**
in the Magpie folder:
```
docker compose stop      # later, bring it back with:  docker compose start
```

**Remove Magpie completely:**
1. In PowerShell in the folder, stop and delete the app and its image:
   ```
   docker compose down --rmi all
   ```
2. Delete the whole **Magpie folder** (the code, your `.env`, and the `data` database).
3. Remove the scheduled pokes — in **PowerShell as Administrator**:
   ```
   schtasks /delete /tn "Magpie cycle" /f
   schtasks /delete /tn "Magpie digest" /f
   schtasks /delete /tn "Magpie reconcile" /f
   schtasks /delete /tn "Magpie review" /f
   ```
4. If you don't use Docker for anything else, uninstall **Docker Desktop** too: Windows **Settings → Apps → Installed apps → Docker Desktop → Uninstall**.

> **💰 Your money is safe and separate.** Uninstalling Magpie does **nothing** to
> your Kraken account — your balance stays on Kraken exactly where it is. To get
> your funds back, sell or withdraw on **Kraken** directly. And once Magpie is
> gone, **delete its API key** in your Kraken account settings so nothing can use
> it again.

**Start fresh but keep using Magpie** (wipe the history, keep the app) — in the
Magpie folder:
```
docker compose down      # stop it
```
Delete just the **`data`** folder, then start it again:
```
docker compose up -d --build
```
It comes back as a brand-new install — paper mode, an empty diary, and you choose
your base currency again.

## API

- `GET /health` — liveness, mode, halt state, last decision
- `GET /api/state` — full portfolio, sleeve breakdown, decision diary, skims
- `POST /api/cycle` — run a decision tick
- `POST /api/digest` — push the daily summary
- `POST /api/review` — run the monthly self-review (writes the lessons note)
- `POST /api/reconcile` — absorb drift between the sleeve books and exchange reality
- `GET /settings` — a page to enter/change keys and Home Assistant details (secrets masked; test buttons for each integration). Web-entered settings persist and override the env.
- `POST /api/halt` / `POST /api/resume` — the only human controls
- `POST /api/topup?amount=` — paper mode only; live deposits are auto-detected

## Configuration

**Base currency.** Magpie trades against and values everything in one currency —
EUR by default, or USD, GBP, etc. It's a **one-time choice at initial setup**
(Settings → Base currency): the moment you set it — and automatically once the bot
has traded — it **locks permanently**, because switching it on a funded account
would leave your holdings and exchange balance in the wrong currency. Choose it
before funding.

**Timezone.** Set `TIMEZONE` (an IANA name like `America/New_York`, default
`Europe/Dublin`) — or pick it on the settings page — so the daily 06:00, Monday
and 1st-of-month decision slots run on your local clock. It's safe to change
anytime; align it with the schedule you set below.

Everything else is env vars — see [`.env.example`](.env.example). Notables:
`LLM_PROVIDER` (which brain; default `gemini`) and its matching API key,
`LLM_MODEL` / `LLM_MODEL_DEEP` (optional model overrides), `PAIRS` (the base
tradeable universe, default BTC/EUR + ETH/EUR), `SKIM_FRACTION` (profit share
skimmed to the vault, default 0.5),
`HA_URL`/`HA_TOKEN`/`HA_NOTIFY_SERVICE` (optional Home Assistant pushes for
trades, top-ups and the daily digest).


## Dynamic universe (optional)

With `DYNAMIC_UNIVERSE=true`, the tradeable set is your base pairs plus the
top-`DYNAMIC_TOP_N` (default 5) altcoins by market cap that trade against EUR
on Kraken — stablecoins and wrapped/staked tokens excluded. It refreshes on a
weekly timer (`POST /api/universe/refresh`) and pushes a heads-up when the set
changes.

It never strands a position, and never churns fees on a ranking reshuffle: a
held coin that slips out of the top-`DYNAMIC_TOP_N` stays sellable at the bot's
own discretion, and only once it falls past `DYNAMIC_SELL_FLOOR_N` (default 10)
is it force-sold at the weekly refresh — the band between the two is a grace
zone the model manages itself. Base pairs are never auto-sold, and sub-€1 dust
is left in place. `GET /api/universe` shows the current set.

**Pin your own coins.** Beyond the base pairs and the auto-tracked alts, add any
coin that trades against EUR on Kraken from the settings page's **Custom coins**
card (or `MANUAL_PAIRS` / `POST /api/pairs/add {symbol}`). Each is validated
against Kraken before it's saved, is always tradeable regardless of the rankings,
and — because you chose it deliberately — is **exempt from the sell floor**.


## Login (optional)

Set `DASHBOARD_PASSWORD` (env or the settings page's Security card) to require a
password for the dashboard, portfolio view and controls. It's a single-password
cookie login; the health check and the timer-triggered action endpoints stay
open (they expose no data). Leave it blank to run open on a trusted LAN or behind
your own reverse-proxy auth.

**Two-factor (TOTP).** Once a password is set, turn on 2FA from the Security card:
scan the QR into Google Authenticator / Authy / 1Password, confirm a code, and
every login then needs the 6-digit code as well as the password. It's standard
TOTP, verified at login before the session cookie is issued.

Enabling 2FA hands you **10 single-use backup codes** (shown once, stored hashed).
Any one works in the login's code field in place of your authenticator and is then
spent — the way back in if you lose your phone. Regenerate the set anytime from the
Security card (needs a current code). If you lose the authenticator *and* the codes,
the last resort clears it from the container:
`docker exec magpie sqlite3 /data/magpie.db "DELETE FROM settings WHERE key IN ('totp_enabled','totp_secret','totp_backup_codes')"`.

## Licence

MIT © 2026 [Colm Finn](https://github.com/colfin22).

---

*Built by Colm Finn. The magpie trades alone; the consequences are its keeper's.*
