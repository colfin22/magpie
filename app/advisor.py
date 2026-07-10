"""The brain: Gemini as an autonomous portfolio manager.

The engine builds a context pack, Gemini answers in strict JSON, and the
validation here is the boundary between the model's words and the exchange:
malformed, out-of-universe or impossible answers all resolve to HOLD.
"""
import json
import logging

import httpx

from . import config

LOGGER = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """You are the autonomous manager of ONE strategy sleeve of a small
cryptocurrency spot portfolio on Kraken. You decide at scheduled intervals; between
decisions the sleeve is untouched. Your objective is to grow this sleeve's EUR value.
There is no human oversight of individual decisions — be deliberate, and remember
every trade costs ~{fee_pct:.2f}% in fees each way. HOLD is a perfectly good decision.

{mandate}
{lessons}
Rules you must follow:
- You may only trade these pairs: {pairs}
- Spot only, long only: you buy with EUR you have, you sell assets you hold.
- fraction is the share of your EUR balance (buy) or of the asset position (sell), 0.0-1.0.
- Buys below the exchange minimum (~€{min_order:.0f}) will be rejected — don't bother.

Current portfolio:
{portfolio}

Market data:
{market}

Market context:
{extras}

Your recent decisions and trades (most recent first):
{history}

Answer with ONLY a JSON object, no other text:
{{"action": "buy" | "sell" | "hold", "pair": "<pair or null>",
  "fraction": <0.0-1.0 or null>, "confidence": <0.0-1.0>,
  "reasoning": "<one or two sentences>"}}"""


class AdvisorError(RuntimeError):
    pass


def build_prompt(portfolio: dict, market_data: list[dict], history: list[dict],
                 min_order: float, mandate: str = "", lessons: str = "",
                 extras: dict | None = None) -> str:
    lessons_block = ""
    if lessons:
        lessons_block = ("\nLessons from your own past performance (a monthly "
                         "self-review — weigh them):\n" + lessons + "\n")
    return PROMPT.format(
        mandate=mandate,
        lessons=lessons_block,
        fee_pct=config.MAKER_FEE * 100,
        pairs=", ".join(config.PAIRS),
        min_order=min_order,
        portfolio=json.dumps(portfolio, indent=1),
        market=json.dumps(market_data, indent=1),
        extras=json.dumps(extras, indent=1) if extras else "(none)",
        history=json.dumps(history, indent=1) if history else "(none yet)")


def ask(prompt: str, http: httpx.Client | None = None, model: str | None = None) -> str:
    """Raw model call. Returns the text of the first candidate."""
    if not config.GEMINI_API_KEY:
        raise AdvisorError("no GEMINI_API_KEY configured")
    own = http is None
    http = http or httpx.Client(timeout=120)
    try:
        r = http.post(
            API.format(model=model or config.GEMINI_MODEL),
            params={"key": config.GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.2,
                    "responseMimeType": "application/json",
                },
            })
        r.raise_for_status()
        body = r.json()
        return body["candidates"][0]["content"]["parts"][0]["text"]
    except (httpx.HTTPError, KeyError, IndexError) as e:
        raise AdvisorError(f"gemini call failed: {e}") from e
    finally:
        if own:
            http.close()


def validate(raw: str) -> dict:
    """Parse + validate the model's answer. Raises AdvisorError -> HOLD."""
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise AdvisorError(f"unparseable response: {e}") from e
    action = str(d.get("action", "")).lower()
    if action not in ("buy", "sell", "hold"):
        raise AdvisorError(f"invalid action {d.get('action')!r}")
    out = {"action": action, "pair": None, "fraction": None,
           "confidence": float(d.get("confidence") or 0),
           "reasoning": str(d.get("reasoning", ""))[:500]}
    if action in ("buy", "sell"):
        pair = d.get("pair")
        if pair not in config.PAIRS:
            raise AdvisorError(f"pair {pair!r} not in allowed universe {config.PAIRS}")
        try:
            fraction = float(d.get("fraction"))
        except (TypeError, ValueError) as e:
            raise AdvisorError(f"bad fraction {d.get('fraction')!r}") from e
        if not 0 < fraction <= 1:
            raise AdvisorError(f"fraction {fraction} outside (0, 1]")
        out["pair"] = pair
        out["fraction"] = fraction
    return out
