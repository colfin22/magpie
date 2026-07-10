import os

DB_PATH = os.environ.get("MAGPIE_DB", "/data/magpie.db")

# the brain
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# the slow sleeves (quarter, vault) and the monthly self-review think harder
GEMINI_MODEL_DEEP = os.environ.get("GEMINI_MODEL_DEEP", "gemini-2.5-pro")

# the exchange (live mode only — paper mode uses public market data)
KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")

# TRADING_ENABLED=true + Kraken keys = real money; anything else = paper
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "false").lower() == "true"

# the universe: Gemini may only ever trade these pairs
PAIRS = [p.strip() for p in os.environ.get("PAIRS", "BTC/EUR,ETH/EUR").split(",") if p.strip()]

START_BALANCE_EUR = float(os.environ.get("START_BALANCE_EUR", "50"))
SKIM_FRACTION = float(os.environ.get("SKIM_FRACTION", "0.5"))  # share of new profit skimmed to the vault
TAKER_FEE = float(os.environ.get("TAKER_FEE", "0.004"))   # market-order fee (fallback fills)
MAKER_FEE = float(os.environ.get("MAKER_FEE", "0.0025"))  # post-only limit fee (preferred fills)
LIMIT_FILL_WAIT_S = int(os.environ.get("LIMIT_FILL_WAIT_S", "90"))  # patience before falling back to market

STALE_AFTER_S = int(os.environ.get("STALE_AFTER_S", str(7 * 3600)))  # no cycle in 7h = unhealthy (#1)
ERROR_ALERT_AFTER = int(os.environ.get("ERROR_ALERT_AFTER", "3"))    # consecutive failed cycles -> HA push (#2)

# notifications (optional, same pattern as the other house apps)
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "")


def mode() -> str:
    return "live" if (TRADING_ENABLED and KRAKEN_API_KEY and KRAKEN_API_SECRET) else "paper"


# ---- settings editable from the web page (persist in the DB, survive restart) ----
# name -> caster ('str' | 'float' | 'csv'). TRADING_ENABLED is deliberately NOT
# here: going live stays an explicit env decision, never a stray web toggle.
EDITABLE = {
    "GEMINI_API_KEY": "str", "GEMINI_MODEL": "str", "GEMINI_MODEL_DEEP": "str",
    "KRAKEN_API_KEY": "str", "KRAKEN_API_SECRET": "str",
    "HA_URL": "str", "HA_TOKEN": "str", "HA_NOTIFY_SERVICE": "str",
    "PAIRS": "csv", "SKIM_FRACTION": "float",
}
SECRET_KEYS = {"GEMINI_API_KEY", "KRAKEN_API_KEY", "KRAKEN_API_SECRET", "HA_TOKEN"}


def _cast(key: str, raw: str):
    t = EDITABLE[key]
    if t == "csv":
        return [p.strip() for p in raw.split(",") if p.strip()]
    if t == "float":
        return float(raw)
    return raw.rstrip("/") if key == "HA_URL" else raw


def apply_overrides(conn) -> None:
    """Load cfg_* overrides from the DB onto this module. Called at startup and
    after every settings save, so web-entered keys beat the env and outlive a
    restart. A credential change invalidates the cached exchange client."""
    import sys
    mod = sys.modules[__name__]
    for key in EDITABLE:
        row = conn.execute("SELECT value FROM settings WHERE key=?", ("cfg_" + key,)).fetchone()
        if row is not None:
            try:
                setattr(mod, key, _cast(key, row[0]))
            except Exception:  # noqa: BLE001 - a bad stored value must not break boot
                pass
    try:
        from . import market
        market._exchange = None
    except Exception:  # noqa: BLE001
        pass
