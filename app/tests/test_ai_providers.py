"""Providers against a mocked urllib: request body shape + response parsing."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

APP_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_DIR))

from plannerboard.ai.anthropic_provider import API_VERSION, JSON_TOOL_NAME, AnthropicProvider  # noqa: E402
from plannerboard.ai.base import NullProvider  # noqa: E402
from plannerboard.ai.factory import PROVIDER_CATALOG, make_provider  # noqa: E402
from plannerboard.ai.ollama_provider import OllamaProvider  # noqa: E402
from plannerboard.ai.openai_provider import OpenAIProvider  # noqa: E402


class _Resp:
    def __init__(self, payload: dict, status: int = 200):
        self._b = json.dumps(payload).encode()
        self.status = status

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Recorder:
    """Stands in for urllib.request.urlopen. `script` is a list of responses
    or exceptions, consumed in order."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append((req, timeout))
        nxt = self.script.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _body(req) -> dict:
    return json.loads(req.data.decode())


SCHEMA = {"type": "object", "properties": {"classification": {"type": "string"}}, "required": ["classification"]}


# -- Anthropic ---------------------------------------------------------------

def test_anthropic_request_shape_and_tool_parse():
    rec = Recorder([_Resp({
        "model": "claude-sonnet-4-5-20250929",
        "content": [{"type": "tool_use", "name": JSON_TOOL_NAME, "input": {"classification": "request"}}],
        "usage": {"input_tokens": 120, "output_tokens": 15},
    })])
    p = AnthropicProvider("sk-test", opener=rec)
    res = p.complete("sys text", "user text", max_tokens=100, temperature=0.1, json_schema=SCHEMA)
    req, timeout = rec.requests[0]
    assert timeout == 60
    assert req.get_header("X-api-key") == "sk-test"
    assert req.get_header("Anthropic-version") == API_VERSION
    body = _body(req)
    assert body["model"] == "claude-sonnet-4-5"
    assert body["system"] == "sys text"
    assert body["messages"] == [{"role": "user", "content": "user text"}]
    assert body["max_tokens"] == 100 and body["temperature"] == 0.1
    assert body["tools"][0]["input_schema"] == SCHEMA
    assert body["tool_choice"] == {"type": "tool", "name": JSON_TOOL_NAME}
    assert res.ok and res.parsed == {"classification": "request"}
    assert res.input_tokens == 120 and res.output_tokens == 15
    assert res.model.startswith("claude-sonnet-4-5")
    assert res.cost_usd > 0 and res.cost_estimated


def test_anthropic_text_mode_and_array_schema_unwrap():
    rec = Recorder([
        _Resp({"content": [{"type": "text", "text": "hello there"}], "usage": {"input_tokens": 5, "output_tokens": 2}}),
        _Resp({"content": [{"type": "tool_use", "name": JSON_TOOL_NAME, "input": {"result": ["a", "b"]}}],
               "usage": {"input_tokens": 5, "output_tokens": 2}}),
    ])
    p = AnthropicProvider("k", opener=rec)
    r1 = p.complete("s", "u")
    assert r1.text == "hello there" and r1.parsed is None
    assert "tools" not in _body(rec.requests[0][0])
    r2 = p.complete("s", "u", json_schema={"type": "array", "items": {"type": "string"}})
    assert _body(rec.requests[1][0])["tools"][0]["input_schema"]["type"] == "object"
    assert r2.parsed == ["a", "b"]


def test_anthropic_error_mapping():
    err401 = HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(b'{"error":{"message":"invalid x-api-key"}}'))
    err429 = HTTPError("u", 429, "Too Many", {}, io.BytesIO(b'{"error":{"message":"rate"}}'))
    rec = Recorder([err401, err429, URLError("dns failed"), TimeoutError()])
    p = AnthropicProvider("k", opener=rec)
    r = p.complete("s", "u")
    assert r.error.startswith("auth:") and "invalid x-api-key" in r.error
    assert p.complete("s", "u").error.startswith("rate_limit:")
    r = p.complete("s", "u")
    assert r.error.startswith("network:") and not r.ok
    r = p.complete("s", "u")
    assert r.error.startswith("timeout:")
    assert not AnthropicProvider("").available()
    assert AnthropicProvider("").complete("s", "u").error.startswith("auth:")


# -- OpenAI compatible -----------------------------------------------------

def test_openai_schema_mode_request_and_parse():
    rec = Recorder([_Resp({
        "model": "gpt-4o-mini-2024",
        "choices": [{"message": {"role": "assistant", "content": '{"classification": "approval"}'}}],
        "usage": {"prompt_tokens": 80, "completion_tokens": 10},
    })])
    p = OpenAIProvider("sk-o", model="gpt-4o-mini", opener=rec)
    res = p.complete("sys", "usr", max_tokens=50, json_schema=SCHEMA)
    req, _ = rec.requests[0]
    assert req.full_url == "https://api.openai.com/v1/chat/completions"
    assert req.get_header("Authorization") == "Bearer sk-o"
    body = _body(req)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["messages"][1]["content"] == "usr"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert res.ok and res.parsed == {"classification": "approval"}
    assert res.input_tokens == 80 and res.output_tokens == 10 and res.cost_usd > 0


def test_openai_base_url_presets_and_prompt_fallback_on_400():
    bad = HTTPError("u", 400, "Bad", {}, io.BytesIO(b'{"error":{"message":"response_format is not supported"}}'))
    good = _Resp({"choices": [{"message": {"content": "Sure! ```json\n{\"classification\":\"vendor\"}\n```"}}],
                  "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    rec = Recorder([bad, good])
    p = OpenAIProvider("k", model="llama-3.3-70b-versatile", base_url="groq", opener=rec)
    assert p.base_url == "https://api.groq.com/openai/v1"
    res = p.complete("s", "u", json_schema=SCHEMA)
    assert len(rec.requests) == 2
    second = _body(rec.requests[1][0])
    assert "response_format" not in second
    assert "Respond with ONLY a JSON" in second["messages"][-1]["content"]
    assert res.ok and res.parsed == {"classification": "vendor"}
    assert p.json_mode == "prompt"


def test_openai_local_server_needs_no_key_and_costs_zero():
    rec = Recorder([_Resp({"model": "qwen", "choices": [{"message": {"content": "hi"}}],
                           "usage": {"prompt_tokens": 3, "completion_tokens": 1}})])
    p = OpenAIProvider("", base_url="lmstudio", opener=rec)
    assert p.available()
    res = p.complete("s", "u")
    assert res.ok and res.model == "local/qwen" and res.cost_usd == 0 and res.cost_estimated
    assert "Authorization" not in {k.lower(): v for k, v in rec.requests[0][0].header_items()} and \
        rec.requests[0][0].get_header("Authorization") is None


# -- Ollama ------------------------------------------------------------------

def test_ollama_think_flag_schema_and_404_fallback():
    err404 = HTTPError("u", 404, "not found", {}, io.BytesIO(b'{"error":"model not found"}'))
    rec = Recorder([err404, _Resp({"response": '{"classification":"junk"}', "prompt_eval_count": 40, "eval_count": 6})])
    p = OllamaProvider(model="qwen3.5:4b", fallback_model="llama3.2:3b", opener=rec)
    res = p.complete("sys", "usr", max_tokens=77, temperature=0.0, json_schema=SCHEMA)
    first = _body(rec.requests[0][0])
    assert first["model"] == "qwen3.5:4b" and first["think"] is False and first["stream"] is False
    assert first["system"] == "sys" and first["prompt"] == "usr"
    assert first["options"] == {"temperature": 0.0, "num_predict": 77}
    assert first["format"] == SCHEMA
    assert rec.requests[0][0].full_url == "http://localhost:11434/api/generate"
    second = _body(rec.requests[1][0])
    assert second["model"] == "llama3.2:3b" and "think" not in second
    assert res.ok and res.parsed == {"classification": "junk"}
    assert res.model == "ollama/llama3.2:3b" and res.input_tokens == 40 and res.cost_usd == 0


def test_ollama_available_probe():
    assert OllamaProvider(opener=Recorder([_Resp({"models": []})])).available()
    assert not OllamaProvider(opener=Recorder([URLError("refused")])).available()


# -- factory -----------------------------------------------------------------

def test_factory_routes_and_null_provider():
    cfg = {"ai": {"provider": "none"}}
    assert isinstance(make_provider(cfg, lambda k: ""), NullProvider)
    assert isinstance(make_provider({"ai": {"provider": "anthropic"}}, lambda k: ""), NullProvider)
    p = make_provider({"ai": {"provider": "anthropic", "model": "claude-haiku-4-5"}}, lambda k: "sk-1")
    assert isinstance(p, AnthropicProvider) and p.model == "claude-haiku-4-5"
    p = make_provider({"ai": {"provider": "openai", "base_url": "together"}}, lambda k: "tk")
    assert isinstance(p, OpenAIProvider) and "together" in p.base_url
    p = make_provider({"ai": {"provider": "ollama", "base_url": "http://box:11434"}}, lambda k: "")
    assert isinstance(p, OllamaProvider) and p.host == "http://box:11434"
    p = make_provider({"ai": {"provider": "anthropic"}}, lambda k: "", overrides={"api_key": "over"})
    assert isinstance(p, AnthropicProvider) and p.api_key == "over"
    assert {c["id"] for c in PROVIDER_CATALOG} == {"emailboss", "none", "anthropic", "openai", "ollama"}
    assert NullProvider().complete("s", "u").ok is False
