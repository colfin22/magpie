import os

DB_PATH = os.environ.get("MAGPIE_DB", "/data/magpie.db")

# the brain — pluggable LLM provider. The prompt + JSON-validation layer are
# provider-agnostic; only advisor.ask() branches on this. Each provider uses its
# OWN api key below; empty model overrides fall back to the provider's defaults.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
VALID_PROVIDERS = ("gemini", "openai", "anthropic", "perplexity",
                   "grok", "deepseek", "github", "openrouter")
LLM_MODEL = os.environ.get("LLM_MODEL", "")            # blank = provider default (frequent)
LLM_MODEL_DEEP = os.environ.get("LLM_MODEL_DEEP", "")  # blank = provider default (slow sleeves + review)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# the slow sleeves (quarter, vault) and the monthly self-review think harder
# Defaults to the FAST model on purpose. The old default was `gemini-2.5-pro`, which is
# RETIRED — it 404s with "no longer available to new users", and billing does not fix it.
# A fresh install therefore failed every quarter and vault decision and safe-HELD forever:
# a bot that looks thoughtful and is dead on half its sleeves (#58). Flash exists, is on
# the free tier, and works out of the box. Point this at a stronger model if you want one —
# and prefer a tracking alias (`gemini-pro-latest`) over a pinned id, which will be retired.
GEMINI_MODEL_DEEP = os.environ.get("GEMINI_MODEL_DEEP", "gemini-2.5-flash")

# alternative brains — one key each; the active one is chosen by LLM_PROVIDER
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")          # ChatGPT (platform.openai.com)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")    # Claude (api.anthropic.com)
PERPLEXITY_API_KEY = os.environ.get("PERPLEXITY_API_KEY", "")  # Perplexity (api.perplexity.ai)
GROK_API_KEY = os.environ.get("GROK_API_KEY", "")              # xAI Grok (api.x.ai)
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")      # DeepSeek (api.deepseek.com)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")              # GitHub Models / Copilot (models.github.ai)
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")  # catch-all router (openrouter.ai)

# the exchange (live mode only — paper mode uses public market data)
KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")

# TRADING_ENABLED=true + Kraken keys = real money; anything else = paper
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "false").lower() == "true"

# the base/quote currency: the coin every pair trades against, the cash asset in
# the books, and the money everything is valued and displayed in. Chosen ONCE at
# initial setup then LOCKED (a `base_currency` row in settings is the source of
# truth; the env is only the pre-lock default). Never change it on a funded account.
BASE_CURRENCY = os.environ.get("BASE_CURRENCY", "EUR").upper()
SUPPORTED_CURRENCIES = ("EUR", "USD", "GBP", "AUD", "CAD", "CHF", "JPY", "NZD")
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "AUD": "A$",
                    "CAD": "C$", "CHF": "Fr ", "JPY": "¥", "NZD": "NZ$"}


def symbol() -> str:
    """The display symbol for the active base currency (e.g. € / $ / £)."""
    import sys
    mod = sys.modules[__name__]
    return mod.CURRENCY_SYMBOLS.get(mod.BASE_CURRENCY, mod.BASE_CURRENCY + " ")


def currency_locked(conn) -> bool:
    """True once the base currency has been committed (chosen at initial setup)."""
    r = conn.execute("SELECT value FROM settings WHERE key='base_currency'").fetchone()
    return bool(r and r[0])


def autolock_currency(conn) -> None:
    """Lock an install to its current currency the moment it has any trade history —
    so an already-running bot (e.g. the live EUR one) can never have it changed."""
    try:
        has_history = bool(conn.execute("SELECT 1 FROM orders LIMIT 1").fetchone())
    except Exception:  # noqa: BLE001 - fresh db without the table yet
        has_history = False
    if has_history and not currency_locked(conn):
        import sys
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('base_currency', ?)",
                     (sys.modules[__name__].BASE_CURRENCY,))
        conn.commit()


# the universe: BASE_PAIRS are always tradeable; the dynamic top-alt set (when
# enabled) is layered on top. PAIRS is the EFFECTIVE universe everything reads.
_pairs_env = os.environ.get("PAIRS", "").strip()
BASE_PAIRS = ([p.strip() for p in _pairs_env.split(",") if p.strip()]
              or [f"BTC/{BASE_CURRENCY}", f"ETH/{BASE_CURRENCY}"])
# manually pinned coins — always tradeable, exempt from the dynamic sell floor
MANUAL_PAIRS = [p.strip() for p in os.environ.get("MANUAL_PAIRS", "").split(",") if p.strip()]
PAIRS = list(BASE_PAIRS)
DYNAMIC_UNIVERSE_ENABLED = os.environ.get("DYNAMIC_UNIVERSE", "false").lower() == "true"
DYNAMIC_TOP_N = int(os.environ.get("DYNAMIC_TOP_N", "5"))
# a held alt that falls out of the top-N stays sellable at the advisor's discretion
# (grace band N+1..FLOOR) but is force-sold once it drops past this rank. Must be
# >= DYNAMIC_TOP_N; set equal to it to sell the instant a coin leaves the buy set.
DYNAMIC_SELL_FLOOR_N = int(os.environ.get("DYNAMIC_SELL_FLOOR_N", "10"))

# location / timezone (IANA name) — the clock the daily 06:00, Monday and 1st-of-
# month decision slots run on. Safe to change (no money implication); drives
# sleeves.due(). Default keeps existing installs on Irish time.
TIMEZONE = os.environ.get("TIMEZONE", "Europe/Dublin")


def tz():
    import sys
    from zoneinfo import ZoneInfo
    return ZoneInfo(sys.modules[__name__].TIMEZONE)


START_BALANCE_EUR = float(os.environ.get("START_BALANCE_EUR", "50"))
SKIM_FRACTION = float(os.environ.get("SKIM_FRACTION", "0.5"))  # share of new profit skimmed to the vault
# Kraken's BOTTOM volume tier — what a small portfolio actually pays. ccxt's static
# metadata claims 0.40/0.25, which describes a mid-tier account and is half the real
# rate; sizing against it leaves too little cash for the fee (#85). These are only the
# fallback: market.fees() asks the exchange for the live schedule.
TAKER_FEE = float(os.environ.get("TAKER_FEE", "0.008"))   # market-order fee (fallback fills)
MAKER_FEE = float(os.environ.get("MAKER_FEE", "0.004"))   # post-only limit fee (preferred fills)
LIMIT_FILL_WAIT_S = int(os.environ.get("LIMIT_FILL_WAIT_S", "90"))  # patience before falling back to market

# backups of the ledger. The DB is the audit trail — every prompt, decision, order
# and fill. VACUUM INTO is SQLite's own online-backup path: safe against a live
# writer, unlike copying a WAL-mode file out from under itself (#41).
BACKUP_DIR = os.environ.get("BACKUP_DIR", "/data/backups")
BACKUP_KEEP = int(os.environ.get("BACKUP_KEEP", "7"))

# the DEEP brain (quarter, vault, monthly review). These calls are rare but want the
# strongest model — and a free-tier key often has NO quota for the big model at all
# (Gemini's free tier gives none for 2.5-pro), which fails those sleeves forever while
# the cheap sleeves carry on fine. Point them at a provider that will actually serve
# them; empty = use LLM_PROVIDER, exactly as before.
DEEP_PROVIDER = os.environ.get("DEEP_PROVIDER", "")
DEEP_MODEL = os.environ.get("DEEP_MODEL", "")

# richer decision context (#34). Funding + open interest come from Kraken's PUBLIC
# futures book — read-only sentiment, the bot stays spot-only and never trades a perp.
# News is OFF unless you give it a feed: headlines are the one source that can make an
# LLM's decisions worse, so treat it as an experiment (A/B it with a shadow arm).
CONTEXT_FUNDING = os.environ.get("CONTEXT_FUNDING", "true").lower() == "true"
CONTEXT_DEPTH = os.environ.get("CONTEXT_DEPTH", "true").lower() == "true"
NEWS_RSS_URL = os.environ.get("NEWS_RSS_URL", "")

# exchange-side stop-losses (#35). OFF by default. The brain proposes a distance,
# these clamps decide what is sane. A stop lives AT Kraken so it still works when
# the bot does not (outage, crash, the 6h gap between cycles).
STOP_LOSS_ENABLED = os.environ.get("STOP_LOSS_ENABLED", "false").lower() == "true"
STOP_LOSS_PCT = float(os.environ.get("STOP_LOSS_PCT", "8"))          # default distance below entry
STOP_LOSS_MIN_PCT = float(os.environ.get("STOP_LOSS_MIN_PCT", "2"))  # tighter than this = stopped out by noise
STOP_LOSS_MAX_PCT = float(os.environ.get("STOP_LOSS_MAX_PCT", "30"))

# shadow arms: rival strategies traded in simulation for comparison (#31).
# `name:kind:spec`, comma-separated, e.g. "ema:rule:ema20,coinflip:rule:random".
# Empty = off, and the live path is untouched.
SHADOW_ARMS = os.environ.get("SHADOW_ARMS", "")

STALE_AFTER_S = int(os.environ.get("STALE_AFTER_S", str(7 * 3600)))  # no cycle in 7h = unhealthy (#1)
ERROR_ALERT_AFTER = int(os.environ.get("ERROR_ALERT_AFTER", "3"))    # consecutive failed cycles -> HA push (#2)

# notifications (optional, same pattern as the other house apps)
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "")
HA_NOTIFY_CLICK_URL = os.environ.get("HA_NOTIFY_CLICK_URL", "")  # tap-to-open URL, reused by every channel

# extra notification channels — each fires only when its config is set; notify()
# fans a message out to ALL of them. HA above is one of them.
PUSHOVER_TOKEN = os.environ.get("PUSHOVER_TOKEN", "")       # pushover.net app token
PUSHOVER_USER = os.environ.get("PUSHOVER_USER", "")         # pushover user/group key
PUSHBULLET_TOKEN = os.environ.get("PUSHBULLET_TOKEN", "")   # pushbullet access token
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")  # a channel webhook URL
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")              # ntfy topic name
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

# login: set a password to gate the UI + control endpoints. Empty = no auth
# (fine behind your own reverse-proxy auth). Localhost (the timers) is exempt.
DASHBOARD_USER = os.environ.get("DASHBOARD_USER", "")          # optional username; blank = password only
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")


def mode() -> str:
    return "live" if (TRADING_ENABLED and KRAKEN_API_KEY and KRAKEN_API_SECRET) else "paper"


# ---- settings editable from the web page (persist in the DB, survive restart) ----
# name -> caster ('str' | 'float' | 'csv'). TRADING_ENABLED is deliberately NOT
# here: going live stays an explicit env decision, never a stray web toggle.
EDITABLE = {
    "LLM_PROVIDER": "str", "LLM_MODEL": "str", "LLM_MODEL_DEEP": "str",
    "GEMINI_API_KEY": "str", "GEMINI_MODEL": "str", "GEMINI_MODEL_DEEP": "str",
    "OPENAI_API_KEY": "str", "ANTHROPIC_API_KEY": "str", "PERPLEXITY_API_KEY": "str",
    "GROK_API_KEY": "str", "DEEPSEEK_API_KEY": "str", "GITHUB_TOKEN": "str",
    "OPENROUTER_API_KEY": "str",
    "KRAKEN_API_KEY": "str", "KRAKEN_API_SECRET": "str",
    "HA_URL": "str", "HA_TOKEN": "str", "HA_NOTIFY_SERVICE": "str",
    "PUSHOVER_TOKEN": "str", "PUSHOVER_USER": "str", "PUSHBULLET_TOKEN": "str",
    "DISCORD_WEBHOOK_URL": "str", "TELEGRAM_BOT_TOKEN": "str", "TELEGRAM_CHAT_ID": "str",
    "NTFY_TOPIC": "str", "NTFY_SERVER": "str",
    "PAIRS": "csv", "MANUAL_PAIRS": "csv", "SKIM_FRACTION": "float", "TIMEZONE": "str",
    "DYNAMIC_UNIVERSE_ENABLED": "bool", "DYNAMIC_TOP_N": "int",
    "DYNAMIC_SELL_FLOOR_N": "int",
    "SHADOW_ARMS": "str",
    "CONTEXT_FUNDING": "bool", "CONTEXT_DEPTH": "bool", "NEWS_RSS_URL": "str",
    "DEEP_PROVIDER": "str", "DEEP_MODEL": "str",
    "STOP_LOSS_ENABLED": "bool", "STOP_LOSS_PCT": "float",
    "STOP_LOSS_MIN_PCT": "float", "STOP_LOSS_MAX_PCT": "float",
    "DASHBOARD_USER": "str", "DASHBOARD_PASSWORD": "str",
}
SECRET_KEYS = {"GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
               "PERPLEXITY_API_KEY", "GROK_API_KEY", "DEEPSEEK_API_KEY",
               "GITHUB_TOKEN", "OPENROUTER_API_KEY",
               "KRAKEN_API_KEY", "KRAKEN_API_SECRET", "HA_TOKEN",
               "PUSHOVER_TOKEN", "PUSHOVER_USER", "PUSHBULLET_TOKEN",
               "DISCORD_WEBHOOK_URL", "TELEGRAM_BOT_TOKEN",
               "DASHBOARD_PASSWORD"}


def _cast(key: str, raw: str):
    t = EDITABLE[key]
    if t == "csv":
        return [p.strip() for p in raw.split(",") if p.strip()]
    if t == "float":
        return float(raw)
    if t == "int":
        return int(raw)
    if t == "bool":
        return str(raw).strip().lower() in ("true", "1", "yes", "on")
    if key == "TIMEZONE":
        from zoneinfo import ZoneInfo
        tzn = raw.strip() or "Europe/Dublin"
        try:
            ZoneInfo(tzn)
        except Exception as e:  # noqa: BLE001
            raise ValueError(f"unknown timezone {tzn!r} — use an IANA name like Europe/Dublin") from e
        return tzn
    if key == "LLM_PROVIDER":
        p = raw.strip().lower()
        if p and p not in VALID_PROVIDERS:
            raise ValueError(f"unknown provider {p!r} (pick one of {', '.join(VALID_PROVIDERS)})")
        return p or "gemini"
    return raw.rstrip("/") if key == "HA_URL" else raw


def apply_universe(conn) -> None:
    """PAIRS (effective) = base pairs + manually pinned coins + the stored
    dynamic top-alt set (if on)."""
    import json
    import sys
    mod = sys.modules[__name__]
    dyn = []
    if mod.DYNAMIC_UNIVERSE_ENABLED:
        row = conn.execute("SELECT value FROM settings WHERE key='dynamic_pairs'").fetchone()
        if row:
            try:
                dyn = json.loads(row[0])
            except Exception:  # noqa: BLE001
                dyn = []
    mod.PAIRS = list(dict.fromkeys(list(mod.BASE_PAIRS) + list(mod.MANUAL_PAIRS) + dyn))


def apply_overrides(conn) -> None:
    """Load cfg_* overrides from the DB onto this module. Called at startup and
    after every settings save, so web-entered settings beat the env and outlive
    a restart. A credential change invalidates the cached exchange client."""
    import sys
    mod = sys.modules[__name__]
    for key in EDITABLE:
        row = conn.execute("SELECT value FROM settings WHERE key=?", ("cfg_" + key,)).fetchone()
        if row is not None:
            try:
                val = _cast(key, row[0])
                if key == "PAIRS":       # the editable field is the BASE, not the effective set
                    mod.BASE_PAIRS = val
                else:
                    setattr(mod, key, val)
            except Exception:  # noqa: BLE001 - a bad stored value must not break boot
                pass
    # the locked base currency (set once at initial setup) beats the env default
    row = conn.execute("SELECT value FROM settings WHERE key='base_currency'").fetchone()
    if row and row[0]:
        mod.BASE_CURRENCY = row[0].strip().upper()
    apply_universe(conn)                 # recompute the effective universe
    try:
        from . import market
        market._exchange = None
    except Exception:  # noqa: BLE001
        pass
