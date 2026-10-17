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
    # 1-13 customers, 14-19 suppliers, 20-23 withdrawals, 24-26 memory, 27-32 the cash drawer, 33-36 every dinar in and out, 37-42 items and sales, 43-45 alerts, 46-49 reports, 50-52 Zain Cash and reminders, 53-55 weekly expenses and remind-me
    assert [c["id"] for c in CASES["cases"]] == list(range(1, 56))


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


def test_supplier_case_checks_the_right_ledger():
    # 15: paying the supplier passes; recording it as a customer payment would not
    ok = run_eval.run_case(case(15), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_supplier_payment",
                                                  {"supplier_id": 4, "amount": 200000, "currency": "IQD"})]),
        LLMResponse(text="تمام، دفعت لشركة الزين 200,000 دينار. باقي عليك إلهم 300,000."),
    ))
    assert ok["passed"], ok["failures"]


def test_ambiguous_case_accepts_asking():
    r = run_eval.run_case(case(19), BASE, Script(LLMResponse(text="تقصد الزبون لو المورد؟")))
    assert r["passed"], r["failures"]


def test_withdrawal_cases():
    ok = run_eval.run_case(case(20), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_withdrawal", {"amount": 50000, "currency": "IQD", "reason": "المولدة"})]),
        LLMResponse(text="تمام، سجلت سحب 50,000 دينار للمولدة."),
    ))
    assert ok["passed"], ok["failures"]
    wrong = run_eval.run_case(case(20), BASE, Script(LLMResponse(text="تمام، سجلت 50,000.")))
    assert not wrong["passed"]  # said it, recorded nothing
    undo = run_eval.run_case(case(23), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "undo_last", {})]),
        LLMResponse(text="لغيت سحب 30,000 دينار (شخصي)."),
    ))
    assert undo["passed"], undo["failures"]


def test_drawer_cases():
    ok = run_eval.run_case(case(27), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "count_drawer", {"iqd": 850000})]),
        LLMResponse(text="بالجرار 850,000. البيع النقدي 1,010,000 دينار."),
    ))
    assert ok["passed"], ok["failures"]
    typo = run_eval.run_case(case(27), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "count_drawer", {"iqd": 85000})]),
        LLMResponse(text="البيع النقدي 1,010,000 دينار."),
    ))
    assert not typo["passed"]  # counted the wrong amount
    asks = run_eval.run_case(case(28), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "count_drawer", {"iqd": 300000})]),
        LLMResponse(text="شكد چان بالجرار الصبح؟"),
    ))
    assert asks["passed"], asks["failures"]  # the code refused the first count
    pocket = run_eval.run_case(case(30), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_supplier_payment",
                                                  {"supplier_id": 4, "amount": 100000, "currency": "IQD"})]),
        LLMResponse(text="تمام."),
    ))
    assert not pocket["passed"]  # forgot from_drawer=false
    status = run_eval.run_case(case(31), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "drawer_status", {})]),
        LLMResponse(text="اليوم بعت نقد 1,010,000 دينار."),
    ))
    assert status["passed"], status["failures"]


def test_sale_cases():
    flour = 1  # base_setup items: طحين 1, زيت 2, رز 3
    ok = run_eval.run_case(case(37), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_sale", {"payment": "cash", "lines": [{"item_id": flour, "qty": 2}]})]),
        LLMResponse(text="تمام، بعت كيسين طحين بـ 50,000 دينار كاش."),
    ))
    assert ok["passed"], ok["failures"]
    as_cash_in = run_eval.run_case(case(37), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_cash_in", {"amount": 50000, "currency": "IQD", "label": "طحين"})]),
        LLMResponse(text="تمام."),
    ))
    assert not as_cash_in["passed"]  # an item sale must be a sale, not a bare cash in


def test_duplicate_case():
    asks = run_eval.run_case(case(43), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_payment", {"customer_id": 1, "amount": 10000, "currency": "IQD"})]),
        LLMResponse(text="سجلت هالمبلغ لأبو علي قبل شوية، هاي دفعة ثانية لو نفسها؟"),
    ))
    assert asks["passed"], asks["failures"]  # the code refused the second one
    forced = run_eval.run_case(case(43), BASE, Script(
        LLMResponse(text="", tool_calls=[ToolCall("a", "record_payment", {"customer_id": 1, "amount": 10000, "currency": "IQD",
                                                                          "confirmed": True})]),
        LLMResponse(text="تمام، سجلت مرة ثانية."),
    ))
    assert not forced["passed"]  # recorded it again without asking
