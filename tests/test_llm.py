from app.llm import AnthropicLLM, ToolCall


def test_to_api_messages():
    msgs = [
        {"role": "user", "text": "سجل"},
        {"role": "assistant", "text": "أدور", "tool_calls": [ToolCall("t1", "find_customer", {"query": "علي"})]},
        {"role": "tool", "results": [{"id": "t1", "content": "{}", "is_error": False},
                                      {"id": "t2", "content": "{}", "is_error": True}]},
        {"role": "assistant", "text": "تمام", "tool_calls": []},
        {"role": "assistant", "text": "", "tool_calls": [], "raw": ["native"]},
    ]
    out = AnthropicLLM._to_api_messages(msgs)
    assert out[0] == {"role": "user", "content": "سجل"}
    assert out[1]["content"] == [
        {"type": "text", "text": "أدور"},
        {"type": "tool_use", "id": "t1", "name": "find_customer", "input": {"query": "علي"}},
    ]
    assert out[2]["role"] == "user"
    assert out[2]["content"][0] == {"type": "tool_result", "tool_use_id": "t1", "content": "{}"}
    assert out[2]["content"][1]["is_error"] is True
    assert out[3]["content"] == [{"type": "text", "text": "تمام"}]
    assert out[4]["content"] == ["native"]


# ---------- Gemini (offline: HTTP is mocked) ----------

import json

import httpx
import pytest

from app.llm import GeminiLLM, LLMError


def _gemini(responses, seen=None, retry=True):
    from google import genai

    def handler(req):
        if seen is not None:
            seen.append(json.loads(req.content))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    llm = GeminiLLM(api_key="test")
    update = {"httpx_client": httpx.Client(transport=httpx.MockTransport(handler))}
    if not retry:
        update["retry_options"] = llm.http_options.retry_options.model_copy(update={"attempts": 1})
    options = llm.http_options.model_copy(update=update)
    llm.client = genai.Client(api_key="test", http_options=options)
    return llm


def _ok(parts, finish="STOP"):
    return 200, {"candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish}]}


SCHEMAS = [
    {"name": "find_customer", "description": "d",
     "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "undo_last", "description": "d", "input_schema": {"type": "object", "properties": {}}},
]


def test_gemini_tool_call_roundtrip():
    seen = []
    llm = _gemini([
        _ok([{"functionCall": {"name": "find_customer", "args": {"query": "علي"}}, "thoughtSignature": "c2ln"}]),
        _ok([{"text": "لگيته", "thought": False}]),
    ], seen)
    msgs = [{"role": "user", "text": "دور علي"}]
    r = llm.complete("sys", msgs, SCHEMAS)
    assert [(c.name, c.input) for c in r.tool_calls] == [("find_customer", {"query": "علي"})]
    decls = seen[0]["tools"][0]["functionDeclarations"]
    has_schema = [any(k in d for k in ("parametersJsonSchema", "parameters_json_schema")) for d in decls]
    assert has_schema == [True, False]

    msgs += [{"role": "assistant", "text": "", "tool_calls": r.tool_calls, "raw": r.raw},
             {"role": "tool", "results": [{"id": r.tool_calls[0].id, "name": "find_customer",
                                           "content": '{"ok": true}', "is_error": False}]}]
    assert llm.complete("sys", msgs, SCHEMAS).text == "لگيته"
    model_turn, tool_turn = seen[1]["contents"][1:]
    assert model_turn["parts"][0]["thoughtSignature"] == "c2ln"  # replayed unchanged
    assert tool_turn["parts"][0]["functionResponse"] == {"name": "find_customer", "response": {"ok": True}}


def test_gemini_skips_thoughts():
    llm = _gemini([_ok([{"text": "أفكر...", "thought": True}, {"text": "الجواب"}])])
    assert llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS).text == "الجواب"


@pytest.mark.parametrize("status, status_text, needle", [
    (429, "RESOURCE_EXHAUSTED", "الحصة"),
    (400, "INVALID_ARGUMENT", "صار خطأ"),
    (403, "PERMISSION_DENIED", "مفتاح Gemini"),
    (404, "NOT_FOUND", "مو موجود"),
    (500, "INTERNAL", "بيها مشكلة"),
])
def test_gemini_errors(status, status_text, needle):
    llm = _gemini([(status, {"error": {"code": status, "message": "x", "status": status_text}})], retry=False)
    with pytest.raises(LLMError) as e:
        llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS)
    assert needle in e.value.user_message.replace("حصة", "الحصة")


def test_gemini_daily_quota_is_not_retried():
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}
    llm = _gemini([(429, body)])  # a retry would pop from an empty list
    with pytest.raises(LLMError) as e:
        llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS)
    assert "حصة اليوم" in e.value.user_message


def test_gemini_retries_busy_model():
    busy = (503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
    llm = _gemini([busy, _ok([{"text": "تمام"}])])
    assert llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS).text == "تمام"


def _rate_limited(delay):
    return 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                           "message": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                           "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                        "retryDelay": delay}]}}


def test_gemini_waits_out_per_minute_limit(monkeypatch):
    slept = []
    monkeypatch.setattr("app.llm.time.sleep", slept.append)
    llm = _gemini([_rate_limited("12s"), _rate_limited("5s"), _ok([{"text": "تمام"}])])
    assert llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS).text == "تمام"
    assert slept == [13.0, 6.0]


def test_gemini_long_wait_is_an_error(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda s: pytest.fail("should not wait"))
    llm = _gemini([_rate_limited("300s")])
    with pytest.raises(LLMError) as e:
        llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS)
    assert "انتظر دقيقة" in e.value.user_message


def test_gemini_blocked_is_refusal():
    llm = _gemini([(200, {"candidates": [{"finishReason": "SAFETY"}]})])
    assert llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS).refused


def test_make_llm_picks_provider(monkeypatch):
    from app.llm import AnthropicLLM, make_llm
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert isinstance(make_llm(), GeminiLLM)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    assert isinstance(make_llm(), AnthropicLLM)


def test_gemini_missing_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(LLMError):
        GeminiLLM()
