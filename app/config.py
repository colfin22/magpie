import os

DB_PATH = os.environ.get("MAGPIE_DB", "/data/magpie.db")

# the brain
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

# the exchange (live mode only — paper mode uses public market data)
KRAKEN_API_KEY = os.environ.get("KRAKEN_API_KEY", "")
KRAKEN_API_SECRET = os.environ.get("KRAKEN_API_SECRET", "")

# TRADING_ENABLED=true + Kraken keys = real money; anything else = paper
TRADING_ENABLED = os.environ.get("TRADING_ENABLED", "false").lower() == "true"

# the universe: Gemini may only ever trade these pairs
PAIRS = [p.strip() for p in os.environ.get("PAIRS", "BTC/EUR,ETH/EUR").split(",") if p.strip()]

START_BALANCE_EUR = float(os.environ.get("START_BALANCE_EUR", "50"))
SKIM_FRACTION = float(os.environ.get("SKIM_FRACTION", "0.5"))  # share of new profit skimmed to the vault
TAKER_FEE = float(os.environ.get("TAKER_FEE", "0.004"))  # Kraken taker, mirrored in paper fills

# notifications (optional, same pattern as the other house apps)
HA_URL = os.environ.get("HA_URL", "").rstrip("/")
HA_TOKEN = os.environ.get("HA_TOKEN", "")
HA_NOTIFY_SERVICE = os.environ.get("HA_NOTIFY_SERVICE", "")


def mode() -> str:
    return "live" if (TRADING_ENABLED and KRAKEN_API_KEY and KRAKEN_API_SECRET) else "paper"
