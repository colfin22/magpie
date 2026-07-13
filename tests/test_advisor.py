"""Multi-provider brain dispatch. The prompt/validation layers are covered
elsewhere; here we prove ask() hits the right API shape per LLM_PROVIDER and
that model resolution + fence-stripping + the no-key guard behave."""
import httpx
import pytest

from app import advisor, config


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeHTTP:
    """Records the last POST and returns a canned provider-shaped body."""
    def __init__(self, payload):
        self.payload = payload
        self.url = None
        self.headers = None
        self.params = None
        self.json = None

    def post(self, url, headers=None, params=None, json=None):
        self.url, self.headers, self.params, self.json = url, headers, params, json
        return FakeResp(self.payload)


ANSWER = '{"action": "hold", "confidence": 0.5, "reasoning": "test"}'


def test_openai_compat_dispatch(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(config, "LLM_MODEL", "")
    http = FakeHTTP({"choices": [{"message": {"content": ANSWER}}]})
    out = advisor.ask("prompt", http=http)
    assert out == ANSWER
    assert http.url == "https://api.openai.com/v1/chat/completions"
    assert http.headers["Authorization"] == "Bearer sk-test"
    assert http.json["model"] == "gpt-4o-mini"                 # provider default
    assert http.json["response_format"] == {"type": "json_object"}


def test_grok_and_deepseek_bases(monkeypatch):
    for prov, key, base, model in [
        ("grok", "GROK_API_KEY", "https://api.x.ai/v1", "grok-2-latest"),
        ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com", "deepseek-chat"),
        ("github", "GITHUB_TOKEN", "https://models.github.ai/inference", "openai/gpt-4o-mini"),
        ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1", "anthropic/claude-sonnet-5"),
    ]:
        monkeypatch.setattr(config, "LLM_PROVIDER", prov)
        monkeypatch.setattr(config, key, "k")
        monkeypatch.setattr(config, "LLM_MODEL", "")
        http = FakeHTTP({"choices": [{"message": {"content": ANSWER}}]})
        assert advisor.ask("p", http=http) == ANSWER
        assert http.url == base + "/chat/completions"
        assert http.json["model"] == model


def test_perplexity_omits_json_mode(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "perplexity")
    monkeypatch.setattr(config, "PERPLEXITY_API_KEY", "k")
    http = FakeHTTP({"choices": [{"message": {"content": ANSWER}}]})
    advisor.ask("p", http=http)
    assert "response_format" not in http.json               # perplexity rejects it


def test_anthropic_dispatch(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_MODEL_DEEP", "")
    http = FakeHTTP({"content": [{"text": ANSWER}]})
    out = advisor.ask("p", http=http, deep=True)
    assert out == ANSWER
    assert http.url == "https://api.anthropic.com/v1/messages"
    assert http.headers["x-api-key"] == "k"
    assert http.json["model"] == "claude-opus-4-8"            # deep default


def test_gemini_still_default(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    http = FakeHTTP({"candidates": [{"content": {"parts": [{"text": ANSWER}]}}]})
    out = advisor.ask("p", http=http)
    assert out == ANSWER
    assert "generativelanguage.googleapis.com" in http.url
    assert http.params["key"] == "k"


def test_model_override_wins(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "openai")
    monkeypatch.setattr(config, "OPENAI_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_MODEL", "gpt-4.1")
    http = FakeHTTP({"choices": [{"message": {"content": ANSWER}}]})
    advisor.ask("p", http=http)
    assert http.json["model"] == "gpt-4.1"                    # LLM_MODEL beats default


def test_strip_fences():
    assert advisor._strip_fences("```json\n" + ANSWER + "\n```") == ANSWER
    assert advisor._strip_fences("```\n" + ANSWER + "\n```") == ANSWER
    assert advisor._strip_fences(ANSWER) == ANSWER


def test_no_key_raises(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "")
    with pytest.raises(advisor.AdvisorError):
        advisor.ask("p")


def test_unknown_provider_falls_back_to_gemini(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "bogus")
    assert advisor.active_provider() == "gemini"


# --- retry / redaction (transient-failure resilience) --------------------

GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": ANSWER}]}}]}


def _status_error(code):
    req = httpx.Request("POST", "http://x")
    return httpx.HTTPStatusError("boom", request=req,
                                 response=httpx.Response(code, request=req))


class FlakyHTTP:
    """Raises `exc` for the first `fail_times` posts, then returns `payload`."""
    def __init__(self, payload, exc, fail_times):
        self.payload, self.exc, self.fail_times, self.calls = payload, exc, fail_times, 0

    def post(self, url, headers=None, params=None, json=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.exc
        return FakeResp(self.payload)


def _gemini(monkeypatch, no_sleep=True):
    monkeypatch.setattr(config, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(config, "GEMINI_API_KEY", "k")
    if no_sleep:
        monkeypatch.setattr(advisor.time, "sleep", lambda s: None)


def test_retries_transient_503_then_succeeds(monkeypatch):
    _gemini(monkeypatch)
    http = FlakyHTTP(GEMINI_OK, _status_error(503), fail_times=2)
    assert advisor.ask("p", http=http) == ANSWER
    assert http.calls == 3                      # two 503s, third OK


def test_retries_on_timeout(monkeypatch):
    _gemini(monkeypatch)
    http = FlakyHTTP(GEMINI_OK, httpx.ConnectTimeout("slow"), fail_times=1)
    assert advisor.ask("p", http=http) == ANSWER
    assert http.calls == 2


def test_auth_4xx_not_retried(monkeypatch):
    _gemini(monkeypatch)
    sleeps = []
    monkeypatch.setattr(advisor.time, "sleep", lambda s: sleeps.append(s))
    http = FlakyHTTP({}, _status_error(401), fail_times=99)
    with pytest.raises(advisor.AdvisorError):
        advisor.ask("p", http=http)
    assert http.calls == 1                      # auth error won't fix itself
    assert sleeps == []


def test_exhausted_retries_redact_api_key(monkeypatch):
    _gemini(monkeypatch)
    exc = httpx.HTTPStatusError(
        "503 for url https://x/models/gemini:generateContent?key=AQ.SECRET123",
        request=httpx.Request("POST", "http://x"),
        response=httpx.Response(503, request=httpx.Request("POST", "http://x")))
    http = FlakyHTTP({}, exc, fail_times=99)
    with pytest.raises(advisor.AdvisorError) as ei:
        advisor.ask("p", http=http)
    assert http.calls == 3                      # tried the full budget
    assert "AQ.SECRET123" not in str(ei.value)
    assert "key=REDACTED" in str(ei.value)


def test_redact_strips_api_key():
    assert advisor._redact("...?key=AQ.abc-123 more") == "...?key=REDACTED more"


# ---------- idle cash must be visible, not buried in JSON (#48) ----------

def _prompt(holdings, total, min_order=10.0, topup=None):
    port = {"sleeve": "swing", "total_eur": total, "holdings": holdings, "allocated": total}
    return advisor.build_prompt(port, [], [], min_order=min_order,
                                mandate="swing", topup=topup)


def test_idle_cash_is_named_and_quantified():
    p = _prompt({"EUR": 15.93, "TRX": {"amount": 55.0, "eur_value": 16.0}}, 31.93)
    assert "Idle cash" in p
    assert "15.93" in p
    assert "50%" in p                    # the share is the part that stings
    assert "Cash is a position too" in p


def test_a_fully_deployed_sleeve_is_not_nagged():
    p = _prompt({"EUR": 0.0, "TRX": {"amount": 110.0, "eur_value": 32.0}}, 32.0)
    assert "Idle cash" not in p


def test_cash_below_the_exchange_minimum_is_not_nagged():
    """It CANNOT be deployed — telling the brain to spend it would only provoke a
    buy the exchange must reject."""
    p = _prompt({"EUR": 3.10, "TRX": {"amount": 100.0, "eur_value": 29.0}}, 32.1, min_order=10.0)
    assert "Idle cash" not in p


def test_an_undeployed_topup_is_flagged_with_its_date():
    p = _prompt({"EUR": 15.93, "TRX": {"amount": 55.0, "eur_value": 16.0}}, 31.93,
                topup={"at": "2026-07-12T06:00:00+00:00", "amount": 47.9, "per_sleeve": 15.96})
    assert "top-up" in p
    assert "15.96" in p
    assert "2026-07-12" in p


def test_no_topup_note_when_none_is_outstanding():
    p = _prompt({"EUR": 15.93, "TRX": {"amount": 55.0, "eur_value": 16.0}}, 31.93, topup=None)
    assert "Idle cash" in p          # still nagged about the cash...
    assert "top-up" not in p         # ...but no top-up is invented
