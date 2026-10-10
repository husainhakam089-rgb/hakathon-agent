import json

import pytest

from app import agent as agent_mod
from app import db
from app.agent import CONFUSED_REPLY, MAX_ITERATIONS, REFUSED_REPLY, Agent
from app.llm import LLMError, LLMResponse, ToolCall


class FakeLLM:
    """Plays back scripted responses; records what it was sent."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def call(name, **args):
    return ToolCall(id=f"t_{name}", name=name, input=args)


@pytest.fixture
def make_agent(conn, tmp_path):
    def _make(*responses):
        return Agent(conn, llm=FakeLLM(*responses), log_path=tmp_path / "log.jsonl")
    return _make


def test_plain_reply(make_agent):
    a = make_agent(LLMResponse(text="هلا"))
    assert a.chat("s", "هلو") == {"reply": "هلا", "tool_calls": []}


def test_tool_loop_records_debt(make_agent, conn, tmp_path):
    cid = db.add_customer(conn, "أبو علي")
    a = make_agent(
        LLMResponse(text="", tool_calls=[call("find_customer", query="ابو علي")], raw=["raw1"]),
        LLMResponse(text="", tool_calls=[call("record_debt", customer_id=cid, amount=25000, currency="IQD")]),
        LLMResponse(text="تمام، سجلت على أبو علي 25,000 دينار. صار عليه 25,000."),
    )
    out = a.chat("s", "سجل على ابو علي 25 الف")
    assert out["reply"].startswith("تمام")
    assert [c["tool"] for c in out["tool_calls"]] == ["find_customer", "record_debt"]
    assert out["tool_calls"][1]["result"]["balance"]["IQD"] == 25000
    assert {"duration_ms", "at", "input"} <= set(out["tool_calls"][0])

    # Source message is stored for audit
    tx = db.get_history(conn, cid)[0]
    assert conn.execute("SELECT source_message FROM transactions WHERE id=?", (tx["id"],)).fetchone()[0] \
        == "سجل على ابو علي 25 الف"

    # Raw provider data is replayed inside the turn...
    second_call = a.llm.calls[1]
    assert second_call[1]["raw"] == ["raw1"]
    # ...and tool results go back as JSON
    assert json.loads(second_call[2]["results"][0]["content"])["count"] == 1

    # Log file has one line per tool call
    lines = (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(l)["tool"] for l in lines] == ["find_customer", "record_debt"]


def test_history_drops_raw_and_is_reused(make_agent):
    a = make_agent(
        LLMResponse(text="", tool_calls=[call("list_debtors")], raw=["thinking"]),
        LLMResponse(text="ما كو مدينين"),
        LLMResponse(text="زين"),
    )
    a.chat("s", "منو عليه دين؟")
    a.chat("s", "شكرًا")
    sent = a.llm.calls[2]
    assert [m["role"] for m in sent] == ["user", "assistant", "tool", "assistant", "user"]
    assert all("raw" not in m for m in sent)
    assert sent[-1]["text"] == "شكرًا"


def test_sessions_are_separate(make_agent):
    a = make_agent(LLMResponse(text="1"), LLMResponse(text="2"))
    a.chat("a", "x")
    a.chat("b", "y")
    assert len(a.llm.calls[1]) == 1


def test_history_is_trimmed(make_agent, monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_HISTORY_MESSAGES", 4)
    a = make_agent(*[LLMResponse(text=str(i)) for i in range(5)])
    for i in range(5):
        a.chat("s", f"m{i}")
    sent = a.llm.calls[-1]
    assert [m["text"] for m in sent] == ["m2", "2", "m3", "3", "m4"]


def test_max_iterations(make_agent):
    looping = [LLMResponse(text="", tool_calls=[call("list_debtors")]) for _ in range(MAX_ITERATIONS)]
    a = make_agent(*looping)
    out = a.chat("s", "؟")
    assert out["reply"] == CONFUSED_REPLY
    assert len(out["tool_calls"]) == MAX_ITERATIONS
    assert len(a.llm.calls) == MAX_ITERATIONS


def test_llm_error_is_friendly(make_agent):
    a = make_agent(LLMError("ما گدرت أتصل بالإنترنت.", "conn"), LLMResponse(text="هلا"))
    assert a.chat("s", "هلو")["reply"] == "ما گدرت أتصل بالإنترنت."
    # Session still works afterwards, with valid alternating history
    assert a.chat("s", "هلو")["reply"] == "هلا"
    assert [m["role"] for m in a.llm.calls[1]] == ["user", "assistant", "user"]


def test_error_after_write_says_what_was_saved(make_agent, conn):
    cid = db.add_customer(conn, "أبو علي")
    a = make_agent(
        LLMResponse(text="", tool_calls=[call("record_debt", customer_id=cid, amount=25000, currency="IQD")]),
        LLMError("خلصت حصة Gemini المجانية هسه، انتظر دقيقة وعيد.", "429"),
    )
    reply = a.chat("s", "سجل على ابو علي 25 الف")["reply"]
    assert reply.startswith("خلصت حصة")
    assert "سجلت على أبو علي 25,000 دينار" in reply and "لا تعيده" in reply


def test_unexpected_error_does_not_crash(make_agent):
    a = make_agent(RuntimeError("boom"))
    assert "خطأ" in a.chat("s", "هلو")["reply"]


def test_refusal(make_agent):
    a = make_agent(LLMResponse(text="", refused=True))
    assert a.chat("s", "x")["reply"] == REFUSED_REPLY


def test_tool_error_is_flagged(make_agent):
    a = make_agent(
        LLMResponse(text="", tool_calls=[call("get_balance", customer_id=999)]),
        LLMResponse(text="ما لگيته"),
    )
    a.chat("s", "x")
    assert a.llm.calls[1][-1]["results"][0]["is_error"] is True


def test_undo_works_across_turns(make_agent, conn):
    cid = db.add_customer(conn, "سعد")
    a = make_agent(
        LLMResponse(text="", tool_calls=[call("record_debt", customer_id=cid, amount=1000, currency="IQD")]),
        LLMResponse(text="تمام"),
        LLMResponse(text="", tool_calls=[call("undo_last")]),
        LLMResponse(text="لغيته"),
    )
    a.chat("s", "سجل على سعد الف")
    out = a.chat("s", "شطبها")
    assert out["tool_calls"][0]["result"]["ok"]
    assert db.get_balance(conn, cid)["IQD"] == 0


def test_new_customer_is_only_added_after_the_owner_answers(make_agent, conn):
    add = LLMResponse(text="", tool_calls=[ToolCall("t_add", "add_customer", {"name": "سعد"})])
    a = make_agent(add, LLMResponse(text="ما عندي زبون اسمه سعد، أضيفه؟"),
                   add, LLMResponse(text="ضفته."))
    first = a.chat("s", "سجل على زبون جديد اسمه سعد 30 الف")
    assert first["tool_calls"][0]["result"]["error"] == "ask_first"
    assert db.all_customers(conn) == []
    second = a.chat("s", "إي")
    assert second["tool_calls"][0]["result"]["ok"]
    assert [c["name"] for c in db.all_customers(conn)] == ["سعد"]
