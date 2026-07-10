"""The four sleeves: three active horizons + a profits-only vault.

Each sleeve is a self-contained sub-portfolio with its own books, its own
mandate text in the prompt, and its own decision cadence. The vault starts
empty and is funded exclusively by profit skims from the other three.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Dublin")

ACTIVE = ["swing", "fortnight", "quarter"]  # split the starting stake equally
VAULT = "vault"
ALL = ACTIVE + [VAULT]

MANDATES = {
    "swing": (
        "Your mandate: SHORT swing trades with a target holding period of roughly "
        "1 to 3 days. You decide every 6 hours. A full round trip costs ~0.8% in "
        "fees, so only trade moves you expect to clear that hurdle comfortably. "
        "You are not a scalper — if nothing clears the bar, HOLD."),
    "fortnight": (
        "Your mandate: swing positions held for roughly ONE TO TWO WEEKS. You "
        "decide once a day. Ride medium-term momentum and trend; don't react to "
        "single-day noise."),
    "quarter": (
        "Your mandate: position trades held for WEEKS TO ~3 MONTHS. You decide "
        "once a week (Mondays). Think in market regimes and major trends; act "
        "rarely and with conviction."),
    "vault": (
        "Your mandate: this is the VAULT — a long-term store funded only by "
        "profits skimmed from the other strategies. Horizon is a YEAR OR MORE. "
        "Long-only accumulation: buy quality (BTC/ETH) at sensible moments and "
        "then sit on it. Selling is for exceptional circumstances only. Holding "
        "EUR while waiting for a good entry (or until you have enough to meet "
        "the exchange minimum) is perfectly fine."),
}


def due(sleeve: str, now: datetime | None = None) -> bool:
    """Is this sleeve's decision slot at the given (Dublin) time?

    Timers fire at 00/06/12/18 IST; the hour gates the slower sleeves.
    """
    n = (now or datetime.now(TZ)).astimezone(TZ)
    if sleeve == "swing":
        return True
    if sleeve == "fortnight":
        return n.hour == 6
    if sleeve == "quarter":
        return n.weekday() == 0 and n.hour == 6
    if sleeve == "vault":
        return n.day == 1 and n.hour == 6
    return False
