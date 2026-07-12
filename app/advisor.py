"""The brain: a pluggable LLM as an autonomous portfolio manager.

The engine builds a context pack, the model answers in strict JSON, and the
validation here is the boundary between the model's words and the exchange:
malformed, out-of-universe or impossible answers all resolve to HOLD.

The provider is chosen by config.LLM_PROVIDER; build_prompt() and validate()
are provider-agnostic, so only ask() branches. Most providers speak the
OpenAI-compatible chat/completions shape; Gemini and Anthropic are special.
"""
import json
import logging
import re
import time

import httpx

from . import config

LOGGER = logging.getLogger(__name__)
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# OpenAI-compatible providers: name -> (base_url, default_model, deep_model, json_mode)
# json_mode toggles the response_format={"type":"json_object"} hint (some
# providers reject it; the HOLD-on-malformed guard is the safety net either way).
OPENAI_COMPAT = {
    "openai":     ("https://api.openai.com/v1",     "gpt-4o-mini",  "gpt-4o",             True),
    "perplexity": ("https://api.perplexity.ai",     "sonar",        "sonar-pro",          False),
    "grok":       ("https://api.x.ai/v1",           "grok-2-latest", "grok-2-latest",     True),
    "deepseek":   ("https://api.deepseek.com",      "deepseek-chat", "deepseek-reasoner", True),
    "github":     ("https://models.github.ai/inference", "openai/gpt-4o-mini", "openai/gpt-4o", True),
    "openrouter": ("https://openrouter.ai/api/v1",  "anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5", True),
}
ANTHROPIC_DEFAULTS = ("claude-sonnet-5", "claude-opus-4-8")
KEY_ATTR = {
    "gemini": "GEMINI_API_KEY", "openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
    "perplexity": "PERPLEXITY_API_KEY", "grok": "GROK_API_KEY", "deepseek": "DEEPSEEK_API_KEY",
    "github": "GITHUB_TOKEN", "openrouter": "OPENROUTER_API_KEY",
}

PROMPT = """You are the autonomous manager of ONE strategy sleeve of a small
cryptocurrency spot portfolio on Kraken. You decide at scheduled intervals; between
decisions the sleeve is untouched. Your objective is to grow this sleeve's {ccy} value.
There is no human oversight of individual decisions — be deliberate, and remember
every trade costs ~{fee_pct:.2f}% in fees each way. HOLD is a perfectly good decision.

{mandate}
{lessons}
Rules you must follow:
- You may only trade these pairs: {pairs}
- Spot only, long only: you buy with {ccy} you have, you sell assets you hold.
- fraction is the share of your {ccy} balance (buy) or of the asset position (sell), 0.0-1.0.
- Buys below the exchange minimum (~{sym}{min_order:.0f}) will be rejected — don't bother.

Current portfolio:
{portfolio}

Market data:
{market}

Market context:
{extras}

Your recent decisions and trades (most recent first):
{history}

{stops}
Answer with ONLY a JSON object, no other text:
{{"action": "buy" | "sell" | "hold", "pair": "<pair or null>",
  "fraction": <0.0-1.0 or null>, "confidence": <0.0-1.0>,{stop_field}
  "reasoning": "<one or two sentences>"}}"""

STOP_BLOCK = """
Stop-losses are ON. When you BUY you may add "stop_loss_pct": how far below your entry
a protective sell should rest AT THE EXCHANGE, so it protects the position even if this
bot is offline. It is clamped to {lo:.0f}-{hi:.0f}% (default {dflt:.0f}%). Set it from the
asset's own volatility: too tight and ordinary noise stops you out for a certain loss."""


class AdvisorError(RuntimeError):
    pass


def build_prompt(portfolio: dict, market_data: list[dict], history: list[dict],
                 min_order: float, mandate: str = "", lessons: str = "",
                 extras: dict | None = None) -> str:
    stops_block = stop_field = ""
    if config.STOP_LOSS_ENABLED:   # unmentioned, and so unchanged, when off
        stops_block = STOP_BLOCK.format(lo=config.STOP_LOSS_MIN_PCT, hi=config.STOP_LOSS_MAX_PCT,
                                        dflt=config.STOP_LOSS_PCT)
        stop_field = '\n  "stop_loss_pct": <number or null>,'
    lessons_block = ""
    if lessons:
        lessons_block = ("\nLessons from your own past performance (a monthly "
                         "self-review — weigh them):\n" + lessons + "\n")
    return PROMPT.format(
        mandate=mandate,
        lessons=lessons_block,
        fee_pct=config.MAKER_FEE * 100,
        ccy=config.BASE_CURRENCY, sym=config.symbol(),
        pairs=", ".join(config.PAIRS),
        min_order=min_order,
        portfolio=json.dumps(portfolio, indent=1),
        market=json.dumps(market_data, indent=1),
        extras=json.dumps(extras, indent=1) if extras else "(none)",
        stops=stops_block, stop_field=stop_field,
        history=json.dumps(history, indent=1) if history else "(none yet)")


def active_provider() -> str:
    p = (config.LLM_PROVIDER or "gemini").lower()
    return p if p in KEY_ATTR else "gemini"


def key_for(provider: str) -> str:
    return getattr(config, KEY_ATTR.get(provider, "GEMINI_API_KEY"), "") or ""


def brain_configured() -> bool:
    return bool(key_for(active_provider()))


def _default_models(provider: str) -> tuple[str, str]:
    if provider == "gemini":
        return config.GEMINI_MODEL, config.GEMINI_MODEL_DEEP
    if provider == "anthropic":
        return ANTHROPIC_DEFAULTS
    if provider in OPENAI_COMPAT:
        _, m, md, _ = OPENAI_COMPAT[provider]
        return m, md
    return config.GEMINI_MODEL, config.GEMINI_MODEL_DEEP


def _resolve_model(provider: str, deep: bool, override: str | None) -> str:
    if override:
        return override
    reg, deep_m = _default_models(provider)
    if deep:
        return config.LLM_MODEL_DEEP or deep_m
    return config.LLM_MODEL or reg


def _strip_fences(text: str) -> str:
    """Peel a ```json … ``` (or bare ```) wrapper some models add around JSON."""
    t = text.strip()
    m = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    return m.group(1).strip() if m else t


def _call_gemini(key: str, model: str, prompt: str, http: httpx.Client) -> str:
    r = http.post(
        GEMINI_API.format(model=model), params={"key": key},
        json={"contents": [{"parts": [{"text": prompt}]}],
              "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"}})
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def _call_anthropic(key: str, model: str, prompt: str, http: httpx.Client) -> str:
    r = http.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={"model": model, "max_tokens": 1024, "temperature": 0.2,
              "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def _call_openai_compat(base: str, key: str, model: str, prompt: str,
                        http: httpx.Client, json_mode: bool) -> str:
    body = {"model": model, "temperature": 0.2,
            "messages": [{"role": "user", "content": prompt}]}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = http.post(base + "/chat/completions",
                  headers={"Authorization": f"Bearer {key}"}, json=body)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


RETRY_ATTEMPTS = 3       # total tries before a transient failure gives up
RETRY_BACKOFF_S = 1.0    # base backoff; grows 1s, 2s, … between tries

# A Gemini call carries the API key as a ?key= query param, so raise_for_status'
# error string leaks it. Scrub it before anything reaches the ledger or the page.
_SECRET_RE = re.compile(r"(key=)[\w.\-]+", re.I)


def _redact(msg: str) -> str:
    return _SECRET_RE.sub(r"\1REDACTED", msg)


def _retryable(e: Exception) -> bool:
    """Transient failures worth another attempt: provider overload / rate-limit
    (HTTP 5xx or 429) and network/timeout blips. Auth (4xx) and malformed-shape
    errors (KeyError/IndexError) are not — they won't fix themselves."""
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        return code == 429 or code >= 500
    return isinstance(e, httpx.TransportError)  # covers timeouts + connect/read


def _call(provider: str, key: str, chosen: str, prompt: str, http: httpx.Client) -> str:
    if provider == "gemini":
        return _call_gemini(key, chosen, prompt, http)
    if provider == "anthropic":
        return _call_anthropic(key, chosen, prompt, http)
    base, _, _, json_mode = OPENAI_COMPAT[provider]
    return _call_openai_compat(base, key, chosen, prompt, http, json_mode)


def ask(prompt: str, http: httpx.Client | None = None,
        model: str | None = None, deep: bool = False) -> str:
    """Raw model call against the configured provider. Returns the JSON text.

    Transient upstream failures (503 overload, 429, timeouts) are retried with
    exponential backoff; only a sustained outage surfaces as an AdvisorError
    (which the engine turns into a safe HOLD)."""
    provider = active_provider()
    key = key_for(provider)
    if not key:
        raise AdvisorError(f"no API key configured for provider {provider!r}")
    chosen = _resolve_model(provider, deep, model)
    own = http is None
    http = http or httpx.Client(timeout=120)
    try:
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return _strip_fences(_call(provider, key, chosen, prompt, http))
            except (httpx.HTTPError, KeyError, IndexError) as e:
                if attempt < RETRY_ATTEMPTS and _retryable(e):
                    wait = RETRY_BACKOFF_S * (2 ** (attempt - 1))
                    LOGGER.warning("%s call failed (%s); retry %d/%d in %.0fs",
                                   provider, _redact(str(e)), attempt,
                                   RETRY_ATTEMPTS - 1, wait)
                    time.sleep(wait)
                    continue
                raise AdvisorError(f"{provider} call failed: {_redact(str(e))}") from e
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
        # optional: the model's proposed stop distance. Junk here is ignored rather
        # than fatal (stops.clamp_pct falls back to the configured default) — an
        # otherwise good decision should not be thrown away over a garnish field.
        try:
            pct = d.get("stop_loss_pct")
            out["stop_loss_pct"] = float(pct) if pct is not None else None
        except (TypeError, ValueError):
            out["stop_loss_pct"] = None
    return out
