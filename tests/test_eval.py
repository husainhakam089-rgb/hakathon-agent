"""The eval harness itself (offline, scripted model)."""

import importlib.util
import json
from pathlib import Path

from app.llm import LLMResponse, ToolCall

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("run_eval", ROOT / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(run_eval)

CASES = json.loads((ROOT / "eval" / "cases.json").read_text(encoding="utf-8"))
BASE = CASES["base_setup"]


def case(i):
    return next(c for c in CASES["cases"] if c["id"] == i)


class Script:
    def __init__(self, *responses):
        self.responses = list(responses)

    def complete(self, system, messages, tools):
        return self.responses.pop(0)


def test_cases_file_is_complete():
    assert [c["id"] for c in CASES["cases"]] == list(range(1, 14))


def test_correct_debt_passes():
    r = run_eval.run_case(case(1), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_debt", {"customer_id": 1, "amount": 25000, "currency": "IQD"})]),
        LLMResponse(text="تمام، سجلت على أبو علي 25,000 دينار."),
    ))
    assert r["passed"], r["failures"]


def test_wrong_amount_fails():
    r = run_eval.run_case(case(1), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_debt", {"customer_id": 1, "amount": 25, "currency": "IQD"})]),
        LLMResponse(text="تمام، سجلت 25,000"),
    ))
    assert not r["passed"] and "القيود" in r["failures"][0]


def test_recording_when_it_should_ask_fails():
    r = run_eval.run_case(case(10), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_debt", {"customer_id": 1, "amount": 3000000,
                                                                       "currency": "IQD", "confirmed": True})]),
        LLMResponse(text="متأكد؟"),
    ))
    assert not r["passed"]


def test_undo_case_sees_seeded_session_row():
    r = run_eval.run_case(case(11), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "undo_last", {})]),
        LLMResponse(text="لغيت دين أم حسين 15,000 دينار."),
    ))
    assert r["passed"], r["failures"]
