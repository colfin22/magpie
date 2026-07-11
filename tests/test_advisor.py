"""Multi-provider brain dispatch. The prompt/validation layers are covered
elsewhere; here we prove ask() hits the right API shape per LLM_PROVIDER and
that model resolution + fence-stripping + the no-key guard behave."""
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
