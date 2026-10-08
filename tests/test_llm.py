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
