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


def _gemini(responses, seen=None, retry=True, models=None, urls=None):
    from google import genai

    def handler(req):
        if seen is not None:
            seen.append(json.loads(req.content))
        if urls is not None:
            urls.append(str(req.url))
        status, body = responses.pop(0)
        return httpx.Response(status, json=body)

    llm = GeminiLLM(api_key="test")
    llm.models = models or [llm.model]  # no fallback models unless a test asks for them
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


def test_gemini_thinking_level(monkeypatch):
    seen = []
    monkeypatch.setenv("GEMINI_THINKING", "minimal")
    llm = _gemini([_ok([{"text": "x"}])], seen)
    llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS)
    config = seen[0]["generationConfig"]["thinkingConfig"]
    assert list(config.values()) == ["MINIMAL"]  # key spelling differs between SDK versions


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


def _per_day():
    return 429, {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                           "message": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}}


def test_gemini_switches_model_when_daily_quota_runs_out():
    urls = []
    llm = _gemini([_per_day(), _ok([{"text": "تمام"}]), _ok([{"text": "ثاني"}])],
                  models=["m-a", "m-b"], urls=urls)
    llm.model = "m-a"
    msgs = [{"role": "user", "text": "x"}]
    assert llm.complete("sys", msgs, SCHEMAS).text == "تمام"
    assert llm.complete("sys", msgs, SCHEMAS).text == "ثاني"  # stays on m-b, no retry of m-a
    assert [u.split("/models/")[1].split(":")[0] for u in urls] == ["m-a", "m-b", "m-b"]


def test_gemini_no_switch_mid_turn():
    llm = _gemini([_ok([{"functionCall": {"name": "undo_last", "args": {}}}]), _per_day()],
                  models=["m-a", "m-b"])
    llm.model = "m-a"
    msgs = [{"role": "user", "text": "x"}]
    r = llm.complete("sys", msgs, SCHEMAS)
    msgs += [{"role": "assistant", "text": "", "tool_calls": r.tool_calls, "raw": r.raw},
             {"role": "tool", "results": [{"id": r.tool_calls[0].id, "name": "undo_last",
                                           "content": "{}", "is_error": False}]}]
    with pytest.raises(LLMError) as e:  # a fallback call would pop from an empty list
        llm.complete("sys", msgs, SCHEMAS)
    assert "عيد رسالتك" in e.value.user_message


def test_gemini_all_models_out_of_quota():
    llm = _gemini([_per_day(), _per_day()], models=["m-a", "m-b"])
    llm.model = "m-a"
    for _ in range(2):  # second time: known exhausted, no request at all
        with pytest.raises(LLMError) as e:
            llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS)
        assert "لكل نماذج" in e.value.user_message


def test_gemini_switches_instead_of_waiting_at_turn_start(monkeypatch):
    monkeypatch.setattr("app.llm.time.sleep", lambda s: pytest.fail("should switch, not wait"))
    llm = _gemini([_rate_limited("30s"), _ok([{"text": "تمام"}])], models=["m-a", "m-b"])
    llm.model = "m-a"
    assert llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS).text == "تمام"
    assert llm.model == "m-b"


def test_gemini_skips_overloaded_model():
    busy = (503, {"error": {"code": 503, "message": "high demand", "status": "UNAVAILABLE"}})
    llm = _gemini([busy, _ok([{"text": "تمام"}])], retry=False, models=["m-a", "m-b"])
    llm.model = "m-a"
    assert llm.complete("sys", [{"role": "user", "text": "x"}], SCHEMAS).text == "تمام"
    assert llm.model == "m-b"


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


# ---------- extraction calls (stage 6) ----------

EXTRACT_TOOL = {"name": "submit_extraction", "description": "d",
                "input_schema": {"type": "object", "properties": {"rows": {"type": "array"}}}}


def test_gemini_extract_forces_the_tool_and_sends_the_image():
    seen = []
    llm = _gemini([_ok([{"functionCall": {"name": "submit_extraction", "args": {"rows": [{"name": "علي"}]}}}])], seen)
    out = llm.extract("اقرا", "الصفحة", b"\xff\xd8\xffJPEG", "image/jpeg", EXTRACT_TOOL)
    assert out == {"rows": [{"name": "علي"}]}
    req = seen[0]
    fcc = req["toolConfig"]["functionCallingConfig"]
    assert fcc["mode"] == "ANY" and fcc["allowedFunctionNames"] == ["submit_extraction"]
    assert [d["name"] for d in req["tools"][0]["functionDeclarations"]] == ["submit_extraction"]
    parts = req["contents"][0]["parts"]
    inline = parts[0]["inlineData"]
    assert (inline.get("mimeType") or inline.get("mime_type")) == "image/jpeg"  # spelling varies by SDK
    assert parts[1]["text"] == "الصفحة"


def test_gemini_extract_without_tool_call_is_an_error():
    llm = _gemini([_ok([{"text": "ما گدرت"}])])
    with pytest.raises(LLMError):
        llm.extract("s", "t", b"%PDF", "application/pdf", EXTRACT_TOOL)


def _anthropic(responses, seen):
    import anthropic

    from app.llm import AnthropicLLM

    # The anthropic SDK ships its own httpx fork (httpx2); older versions use httpx.
    try:
        import httpx2 as hx
    except ImportError:
        hx = httpx

    def handler(req):
        seen.append(json.loads(req.content))
        return hx.Response(200, json=responses.pop(0))

    llm = AnthropicLLM(model="claude-test", api_key="k")
    llm.client = anthropic.Anthropic(api_key="k", http_client=hx.Client(transport=hx.MockTransport(handler)))
    return llm


def _claude_message(content, stop="tool_use"):
    return {"id": "m", "type": "message", "role": "assistant", "model": "claude-test", "content": content,
            "stop_reason": stop, "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}}


def test_anthropic_extract_forces_the_tool():
    seen = []
    llm = _anthropic([_claude_message([{"type": "tool_use", "id": "t", "name": "submit_extraction",
                                        "input": {"rows": []}}])], seen)
    assert llm.extract("اقرا", "الصفحة", b"%PDF-1.4", "application/pdf", EXTRACT_TOOL) == {"rows": []}
    req = seen[0]
    assert req["tool_choice"] == {"type": "tool", "name": "submit_extraction"}
    block = req["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"


def test_anthropic_complete_still_works_after_refactor():
    seen = []
    llm = _anthropic([_claude_message([{"type": "text", "text": "هلا"}], stop="end_turn")], seen)
    assert llm.complete("s", [{"role": "user", "text": "هلو"}], SCHEMAS).text == "هلا"
