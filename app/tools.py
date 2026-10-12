"""Agent tools: JSON schemas for the model + their implementations.

The agent only touches the database through `DebtTools.execute`.
Every tool returns a JSON-serializable dict with an `ok` field.
"""

import json
import os
import re
import sys

from app import db
from app.arabic import normalize

USD_LARGE_AMOUNT = 1000  # treated as equivalent to LARGE_AMOUNT_IQD (no FX conversion in v0)
MAX_MEMORIES = 50
MAX_MEMORY_CHARS = 200


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


TOOL_SCHEMAS = [
    {
        "name": "find_customer",
        "description": "يدور على زبون بالاسم (يتحمل اختلاف الكتابة). يرجع المطابقات مع رصيد كل واحد.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "اسم الزبون أو جزء منه"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_customer",
        "description": "يضيف زبون جديد. لا تستدعيها إلا بعد موافقة المستخدم الصريحة.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "phone": {"type": "string"},
            },
            "required": ["name"],
        },
    },
    {
        "name": "record_debt",
        "description": "يسجل دين على زبون (أخذ بضاعة بالدين). يرجع الرصيد الجديد.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
                "amount": {"type": "integer", "description": "عدد صحيح موجب، بالوحدة الكاملة (دينار أو دولار)"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
            },
            "required": ["customer_id", "amount", "currency"],
        },
    },
    {
        "name": "record_payment",
        "description": "يسجل تسديد (الزبون دفع من دينه). يرجع الرصيد الجديد.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
                "amount": {"type": "integer", "description": "عدد صحيح موجب، بالوحدة الكاملة (دينار أو دولار)"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
                "from_drawer": {"type": "boolean", "description": "false بس إذا گال إن الفلوس ما دخلت الجرار (حوالة، زين كاش، ماستر، للبيت). الافتراضي true"},
            },
            "required": ["customer_id", "amount", "currency"],
        },
    },
    {
        "name": "get_balance",
        "description": "رصيد زبون بكل عملة (موجب = عليه دين).",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "get_history",
        "description": "آخر قيود الزبون (ديون وتسديدات).",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["customer_id"],
        },
    },
    {
        "name": "list_debtors",
        "description": "المدينين مرتبين من الأكثر دين، مع المجموع الكلي لكل عملة.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    {
        "name": "undo_last",
        "description": "يلغي آخر قيد انسجل بهذه الجلسة ويرجع شنو انلغى.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "undo_batch",
        "description": "يلغي كل قيود دفعة مستوردة (من صورة أو ملف) مرة وحدة. بدون batch_id يلغي آخر دفعة.",
        "input_schema": {
            "type": "object",
            "properties": {"batch_id": {"type": "string", "description": "رقم الدفعة، اختياري"}},
        },
    },
    # ----- suppliers ledger (stage 10): companies, suppliers and sales reps the shop owes -----
    {
        "name": "find_supplier",
        "description": "يدور على مورد / شركة / مندوب بالاسم (دفتر الموردين، مو الزبائن). يرجع المطابقات مع شكد عليّ إله.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "اسم المورد أو الشركة أو المندوب"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_supplier",
        "description": "يضيف مورد / شركة / مندوب جديد. لا تستدعيها إلا بعد موافقة المستخدم الصريحة.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "phone": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "record_purchase",
        "description": "وارد: صاحب المحل أخذ بضاعة من المورد بالدين، فزاد اللي عليه للمورد. يرجع الرصيد الجديد (شكد عليّ إله).",
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_id": {"type": "integer"},
                "amount": {"type": "integer", "description": "عدد صحيح موجب، بالوحدة الكاملة"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
            },
            "required": ["supplier_id", "amount", "currency"],
        },
    },
    {
        "name": "record_supplier_payment",
        "description": "صادر: صاحب المحل دفع للمورد، فنقص اللي عليه للمورد. يرجع الرصيد الجديد.",
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_id": {"type": "integer"},
                "amount": {"type": "integer", "description": "عدد صحيح موجب، بالوحدة الكاملة"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
                "from_drawer": {"type": "boolean", "description": "false بس إذا گال إنه دفع مو من الجرار (من جيبي، من البيت، حوالة). الافتراضي true"},
            },
            "required": ["supplier_id", "amount", "currency"],
        },
    },
    {
        "name": "get_supplier_balance",
        "description": "شكد عليّ لهذا المورد بكل عملة (موجب = عليّ إله).",
        "input_schema": {
            "type": "object",
            "properties": {"supplier_id": {"type": "integer"}},
            "required": ["supplier_id"],
        },
    },
    {
        "name": "list_suppliers",
        "description": "الموردين والشركات والمندوبين اللي عليّ إلهم فلوس، من الأكثر، مع المجموع الكلي لكل عملة.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10}},
        },
    },
    # ----- withdrawals (stage 11): money taken out of the income -----
    {
        "name": "record_withdrawal",
        "description": "سحب: فلوس طلعت من دخل المحل (مولدة، إيجار، كهرباء، رواتب، شخصي...). ما إله طرف يرجّعه. يرجع مجموع اليوم والشهر.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "عدد صحيح موجب، بالوحدة الكاملة"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "reason": {"type": "string", "description": "على شنو أو منو سحبها، بكلمة أو كلمتين مثل ما گالها: مولدة، إيجار، شخصي"},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
                "from_drawer": {"type": "boolean", "description": "false بس إذا گال إنه دفعها مو من الجرار (من جيبي، من البيت، حوالة). الافتراضي true"},
            },
            "required": ["amount", "currency", "reason"],
        },
    },
    {
        "name": "list_withdrawals",
        "description": "مجموع السحوبات لفترة (today = اليوم، month = هالشهر، all = الكل)، لكل عملة ولكل سبب، وآخر السحوبات. reason اختياري: سحوبات سبب واحد بس.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "month", "all"]},
                "reason": {"type": "string"},
            },
            "required": ["period"],
        },
    },
    # ----- the cash drawer (stage 15) -----
    {
        "name": "count_drawer",
        "description": "صاحب المحل عدّ الجرار (آخر اليوم أو بأي وقت): يسجل العدّة ويرجع تقرير من آخر عدّة: "
                       "شكد چان بالجرار، شكد دخل وطلع، والبيع النقدي ومجموع البيع. iqd و usd اللي گالها بس.",
        "input_schema": {
            "type": "object",
            "properties": {
                "iqd": {"type": "integer", "description": "الدينار اللي بالجرار، عدد صحيح (0 إذا فارغ)"},
                "usd": {"type": "integer", "description": "الدولار اللي بالجرار، بس إذا ذكر دولار"},
            },
        },
    },
    {
        "name": "set_drawer_opening",
        "description": "شكد چان بالجرار ببداية اليوم (الفكة): \"الفكة الصبح 50 ألف\". يبدّل آخر عدّة.",
        "input_schema": {
            "type": "object",
            "properties": {
                "iqd": {"type": "integer"},
                "usd": {"type": "integer"},
            },
        },
    },
    {
        "name": "drawer_status",
        "description": "شكد دخل وطلع من الجرار من آخر عدّة لهسه (بدون عدّة جديدة)، ويا آخر عدّة وتقريرها.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "drawer_details",
        "description": "تفاصيل الجرار سطر سطر: الوارد (اللي دخل: الفكة، التسديدات، البيع النقدي) والصادر (اللي طلع: "
                       "الدفع للموردين والسحوبات)، ومجموع الوارد والصادر والباقي بالجرار. "
                       "which: last_close = آخر عدّة (الافتراضي)، since_last_count = من آخر عدّة لهسه (قبل ما ينعد).",
        "input_schema": {
            "type": "object",
            "properties": {"which": {"type": "string", "enum": ["last_close", "since_last_count"]}},
        },
    },
    {
        "name": "drawer_history",
        "description": "تقارير العدّات القديمة (كل يوم: بالجرار، البيع النقدي، بيع الدين، المجموع)، من الأحدث.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 7}},
        },
    },
    # ----- memory (stage 12): things the owner teaches -----
    {
        "name": "remember",
        "description": "يحفظ شي علّمه صاحب المحل حتى تتذكره بكل المحادثات: معنى كلمة (الورقة = 10,000 دينار)، لقب زبون (أبو علي نفسه علي النجار)، ملاحظة. بس لما صاحب المحل يطلب صراحة.",
        "input_schema": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "جملة قصيرة وواضحة، مثل: الورقة = 10,000 دينار"}},
            "required": ["fact"],
        },
    },
    {
        "name": "forget",
        "description": "يمسح شي من الذاكرة برقمه (من list_memories).",
        "input_schema": {
            "type": "object",
            "properties": {"memory_id": {"type": "integer"}},
            "required": ["memory_id"],
        },
    },
    {
        "name": "list_memories",
        "description": "كل اللي علّمه صاحب المحل ومحفوظ بالذاكرة، ويا أرقامها.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


# Direction checks (stage 10), on the owner's own words. When the owner talks about
# himself the message STARTS with the verb ("دفعت لأبو علي"); about a customer it starts
# with the name ("أم حسين دفعت عشرين" = she paid: Arabic "دفعت/أخذت" is also "she ...").
# So only a leading verb counts:
#   "دفعت لـ X" / "سددت لـ X" / "أعطيت X"  -> the owner paid X: never record_payment
#   "أخذت من X ..." / "أخذت بضاعة ..."      -> the owner took or bought: never record_debt
# ("أخذت من X 10 آلاف" can be a customer paying, so record_payment stays allowed.)
_LEAD = r"^\s*(?:(?:اني|إني|انا|أنا|اليوم|هسه|هسة|توني|و)\s+)*"
_OWNER_PAID = re.compile(_LEAD + r"(?:(?:دفعت|سددت|سدّدت|سلمت|سلّمت)\s*(?:لـ?(?!ي(?:\s|$))|الى|إلى)|(?:اعطيت|أعطيت)\s)")
_OWNER_TOOK = re.compile(_LEAD + r"(?:اخذت|أخذت|خذيت)\s+(?:من(?!\s*(?:عندي|عندنا))|بضاعة)")


# remember() only when the owner's own message teaches or asks to remember (stage 12):
# the model must not decide by itself what to keep.
_TEACHING = re.compile(r"تذكر|تذكّر|احفظ|احفض|خلي\s*(?:ب|في)\s*بالك|من\s*هسه|من\s*الآن|من\s*الان|اعتبر|يعني|نفسه|نفسها|"
                       r"هو\s+نفس|هي\s+نفس|لا\s*تنسى|انتبه\s+إن|انتبه\s+ان|سجل\s+ببالك|عندنا\s+.+\s+معناها")


def _error(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _drawer_meaning(report: dict) -> str:
    """The day in one plain line, so the model does not do the arithmetic itself."""
    parts = []
    words = {"IQD": "دينار", "USD": "دولار"}
    for cur, sales in report.get("cash_sales", {}).items():
        line = f"البيع النقدي {sales:,} {words[cur]}، ومجموع البيع ويا الدين {report['total_sales'][cur]:,}"
        if sales < 0:
            line += f" (بالسالب: الجرار ناقص {-sales:,} {words[cur]} أو أكو شي ما انسجل)"
        parts.append(line)
    if not parts:
        return "ما گدرت أحسب البيع: لازم عدّة بالبداية وبالنهاية لنفس العملة."
    return "، و".join(parts)


def _as_int(value):
    """Accept ints and integral floats; reject bools, strings, fractions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


class DebtTools:
    """Tool executor bound to one DB connection and one chat session."""

    def __init__(self, conn, large_amount_iqd: int | None = None):
        self.conn = conn
        self.large_amount_iqd = (
            large_amount_iqd if large_amount_iqd is not None
            else _int_env("LARGE_AMOUNT_IQD", 1_000_000)
        )
        # Entries recorded in this session, newest last, for undo_last: a transaction id
        # (int) or ("withdrawal", id).
        self.session_tx_ids: list = []
        # Set by the agent before each turn so every entry keeps its original message
        # and where it came from ("chat" typed, "voice" dictated).
        self.source_message: str | None = None
        self.source: str = "chat"
        self.session_id: str | None = None  # the conversation; set by the agent (stage 13)
        # Chat turn counter (the agent bumps it) and, per new name, the turn it first
        # came up: add_customer only works in a LATER turn, i.e. after the owner was
        # asked "أضيفه؟" and answered. Enforced here, not only in the prompt.
        self.turn = 0
        self._new_name_turn: dict[str, int] = {}

    # ----- dispatcher -----

    def execute(self, name: str, args: dict | None) -> dict:
        handler = getattr(self, f"tool_{name}", None)
        if handler is None or name not in {t["name"] for t in TOOL_SCHEMAS}:
            return _error("unknown_tool", f"أداة غير معروفة: {name}")
        try:
            return handler(**(args or {}))
        except TypeError as e:
            return _error("bad_arguments", f"مدخلات غلط: {e}")

    # ----- helpers -----

    def _party_or_error(self, party_id, kind):
        """The party, only if it is in the right ledger: a customer tool never touches a
        supplier and the other way round, whatever id the model passes."""
        pid = _as_int(party_id)
        party = db.get_customer(self.conn, pid) if pid is not None else None
        if party is None or party.get("kind", "customer") != kind:
            if kind == "supplier":
                return None, _error("supplier_not_found",
                                    f"ما كو مورد بالرقم {party_id}. دوّر بـ find_supplier.")
            return None, _error("customer_not_found", f"ما كو زبون بالرقم {party_id}")
        return party, None

    def _customer_or_error(self, customer_id):
        return self._party_or_error(customer_id, "customer")

    def _record(self, tx_type, customer_id, amount, currency="IQD", note=None, confirmed=False,
                kind="customer", from_drawer=True):
        customer, err = self._party_or_error(customer_id, kind)
        if err:
            return err

        value = _as_int(amount)
        if value is None or value <= 0:
            return _error("invalid_amount", "المبلغ لازم يكون رقم صحيح أكبر من صفر")

        currency = (currency or "IQD").upper()
        if currency not in db.CURRENCIES:
            return _error("invalid_currency", "العملة لازم تكون IQD أو USD")

        limit = self.large_amount_iqd if currency == "IQD" else USD_LARGE_AMOUNT
        if value > limit and not confirmed:
            return _error(
                "confirmation_required",
                "المبلغ كبير، لازم تأكد ويا المستخدم وبعدين تعيد الاستدعاء بـ confirmed=true",
                customer_name=customer["name"], amount=value, currency=currency, limit=limit,
            )

        before = db.get_balance(self.conn, customer["id"])
        tx_id = db.add_transaction(
            self.conn, customer["id"], tx_type, value, currency,
            note=note, source_message=self.source_message, source=self.source,
            session_id=self.session_id, from_drawer=from_drawer is not False,
        )
        self.session_tx_ids.append(tx_id)
        after = db.get_balance(self.conn, customer["id"])

        if kind == "supplier":
            result = {
                "ok": True,
                "ledger": "supplier",
                "transaction_id": tx_id,
                "type": "purchase" if tx_type == "debt" else "supplier_payment",
                "supplier_id": customer["id"],
                "supplier_name": customer["name"],
                "amount": value,
                "currency": currency,
                "balance_owed_by_me": after,
            }
        else:
            result = {
                "ok": True,
                "transaction_id": tx_id,
                "type": tx_type,
                "customer_id": customer["id"],
                "customer_name": customer["name"],
                "amount": value,
                "currency": currency,
                "balance": after,
            }
        if tx_type == "payment" and from_drawer is False:
            result["from_drawer"] = False
        if tx_type == "payment" and value > before[currency]:
            result["overpayment"] = True
            result["overpaid_by"] = value - max(before[currency], 0)
        if kind == "supplier":
            # Spell the direction out: models flip it when the balance goes negative.
            owed = after[currency]
            word = "دولار" if currency == "USD" else "دينار"
            result["meaning"] = (f"صاحب المحل عليه للمورد {owed:,} {word}" if owed > 0 else
                                 f"المورد صار هو المطلوب لصاحب المحل {-owed:,} {word}" if owed < 0 else
                                 "ما عليه شي للمورد، الحساب صفر")
        return result

    def _matches(self, query, kind):
        matches = db.find_customers(self.conn, str(query), kind)
        if not matches:
            self._new_name_turn.setdefault((kind, normalize(str(query))), self.turn)
        return [{"id": c["id"], "name": c["name"], "balance": db.get_balance(self.conn, c["id"])}
                for c in matches]

    def _add_party(self, name, phone, kind):
        label = "مورد" if kind == "supplier" else "زبون"
        name = (name or "").strip()
        if not name:
            return _error("invalid_name", "الاسم فارغ")
        exact = [c for c in db.find_customers(self.conn, name, kind)
                 if c["normalized_name"] == normalize(name)]
        if exact:
            return _error(f"{kind}_exists", f"اكو {label} بنفس الاسم",
                          existing={"id": exact[0]["id"], "name": exact[0]["name"]})
        first_seen = self._new_name_turn.setdefault((kind, normalize(name)), self.turn)
        if first_seen >= self.turn:
            return _error("ask_first",
                          f"لا تضيف هسه. اسأل صاحب المحل: 'ما عندي {label} اسمه " + name +
                          "، أضيفه؟' ولا تسجل شي، وانتظر جوابه بالرسالة الجاية.")
        pid = db.add_customer(self.conn, name, phone, kind=kind)
        return {"ok": True, f"{kind}_id": pid, "name": name}

    # ----- tools -----

    def tool_find_customer(self, query):
        matches = self._matches(query, "customer")
        return {"ok": True, "count": len(matches), "matches": matches}

    def tool_add_customer(self, name, phone=None):
        return self._add_party(name, phone, "customer")

    def tool_record_debt(self, customer_id, amount, currency="IQD", note=None, confirmed=False):
        if _OWNER_TOOK.search(self.source_message or ""):
            return _error("wrong_ledger", "صاحب المحل هو اللي أخذ (أخذت من...)، فهذا مو دين على زبون. "
                          "إذا أخذ بضاعة من مورد استعمل record_purchase، وإذا مو واضح اسأله.")
        return self._record("debt", customer_id, amount, currency, note, confirmed)

    def tool_record_payment(self, customer_id, amount, currency="IQD", note=None, confirmed=False,
                            from_drawer=True):
        if _OWNER_PAID.search(self.source_message or ""):
            return _error("wrong_ledger", "صاحب المحل هو اللي دفع (دفعت لـ...)، فهذا مو تسديد من زبون. "
                          "إذا دفع لمورد استعمل record_supplier_payment، وإذا الاسم بالدفترين اسأله "
                          "'تقصد الزبون لو المورد؟'.")
        return self._record("payment", customer_id, amount, currency, note, confirmed, from_drawer=from_drawer)

    def tool_get_balance(self, customer_id):
        customer, err = self._customer_or_error(customer_id)
        if err:
            return err
        return {"ok": True, "customer_id": customer["id"], "customer_name": customer["name"],
                "balance": db.get_balance(self.conn, customer["id"])}

    def tool_get_history(self, customer_id, limit=10):
        customer, err = self._customer_or_error(customer_id)
        if err:
            return err
        limit = max(1, min(_as_int(limit) or 10, 100))
        return {"ok": True, "customer_id": customer["id"], "customer_name": customer["name"],
                "transactions": db.get_history(self.conn, customer["id"], limit)}

    def tool_list_debtors(self, limit=10):
        limit = max(1, min(_as_int(limit) or 10, 100))
        rows = db.all_balances(self.conn)
        debtors = [r for r in rows if any(v > 0 for v in r["balances"].values())]
        debtors.sort(key=lambda r: (r["balances"]["IQD"], r["balances"]["USD"]), reverse=True)
        totals = {cur: sum(max(r["balances"][cur], 0) for r in debtors) for cur in db.CURRENCIES}
        return {"ok": True, "count": len(debtors), "debtors": debtors[:limit], "totals": totals}

    # ----- suppliers -----

    def tool_find_supplier(self, query):
        matches = self._matches(query, "supplier")
        return {"ok": True, "ledger": "supplier", "count": len(matches), "matches": matches}

    def tool_add_supplier(self, name, phone=None):
        return self._add_party(name, phone, "supplier")

    def tool_record_purchase(self, supplier_id, amount, currency="IQD", note=None, confirmed=False):
        return self._record("debt", supplier_id, amount, currency, note, confirmed, kind="supplier")

    def tool_record_supplier_payment(self, supplier_id, amount, currency="IQD", note=None, confirmed=False,
                                     from_drawer=True):
        return self._record("payment", supplier_id, amount, currency, note, confirmed, kind="supplier",
                            from_drawer=from_drawer)

    def tool_get_supplier_balance(self, supplier_id):
        supplier, err = self._party_or_error(supplier_id, "supplier")
        if err:
            return err
        return {"ok": True, "ledger": "supplier", "supplier_id": supplier["id"],
                "supplier_name": supplier["name"],
                "balance_owed_by_me": db.get_balance(self.conn, supplier["id"])}

    def tool_list_suppliers(self, limit=10):
        limit = max(1, min(_as_int(limit) or 10, 100))
        rows = [r for r in db.all_balances(self.conn, "supplier")
                if any(v > 0 for v in r["balances"].values())]
        rows.sort(key=lambda r: (r["balances"]["IQD"], r["balances"]["USD"]), reverse=True)
        totals = {cur: sum(max(r["balances"][cur], 0) for r in rows) for cur in db.CURRENCIES}
        return {"ok": True, "ledger": "supplier", "count": len(rows), "suppliers": rows[:limit],
                "total_owed_by_me": totals}

    # ----- withdrawals -----

    def tool_record_withdrawal(self, amount, currency="IQD", reason=None, note=None, confirmed=False,
                               from_drawer=True):
        reason = str(reason or "").strip()
        if not reason:
            return _error("reason_required", "اسأل صاحب المحل: سحبتهن على شنو؟ (مثل مولدة، إيجار، شخصي)")
        value = _as_int(amount)
        if value is None or value <= 0:
            return _error("invalid_amount", "المبلغ لازم يكون رقم صحيح أكبر من صفر")
        currency = (currency or "IQD").upper()
        if currency not in db.CURRENCIES:
            return _error("invalid_currency", "العملة لازم تكون IQD أو USD")
        limit = self.large_amount_iqd if currency == "IQD" else USD_LARGE_AMOUNT
        if value > limit and not confirmed:
            return _error("confirmation_required",
                          "المبلغ كبير، لازم تأكد ويا المستخدم وبعدين تعيد الاستدعاء بـ confirmed=true",
                          reason=reason, amount=value, currency=currency, limit=limit)
        wd_id = db.add_withdrawal(self.conn, value, currency, reason, note=note,
                                  source_message=self.source_message, source=self.source,
                                  session_id=self.session_id, from_drawer=from_drawer is not False)
        self.session_tx_ids.append(("withdrawal", wd_id))
        return {"ok": True, "ledger": "withdrawal", "withdrawal_id": wd_id, "amount": value,
                "currency": currency, "reason": reason, "from_drawer": from_drawer is not False,
                "today_total": db.withdrawals_summary(self.conn, "today")["totals"],
                "month_total": db.withdrawals_summary(self.conn, "month")["totals"]}

    def tool_list_withdrawals(self, period="month", reason=None):
        period = period if period in db.PERIODS else "month"
        summary = db.withdrawals_summary(self.conn, period, reason or None)
        return {"ok": True, "ledger": "withdrawal", **summary}

    # ----- the cash drawer -----

    def _drawer_amounts(self, iqd, usd):
        """(iqd, usd, error): each one a whole number >= 0 or None (not counted)."""
        values = []
        for v in (iqd, usd):
            if v is None:
                values.append(None)
                continue
            n = _as_int(v)
            if n is None or n < 0:
                return None, None, _error("invalid_amount", "المبلغ لازم يكون رقم صحيح، صفر أو أكثر")
            values.append(n)
        if values == [None, None]:
            return None, None, _error("amount_required", "اسأل صاحب المحل: شكد بالجرار؟")
        return values[0], values[1], None

    def tool_count_drawer(self, iqd=None, usd=None):
        iqd, usd, err = self._drawer_amounts(iqd, usd)
        if err:
            return err
        prev = db.last_drawer_count(self.conn)
        if prev is None:
            return _error("opening_required",
                          "هاي أول عدّة، وما أعرف شكد چان بالجرار ببداية اليوم. لا تسجل شي. اسأل صاحب المحل: "
                          "'شكد چان بالجرار الصبح (الفكة)؟' وبعد جوابه استدعي set_drawer_opening وبعدين count_drawer.",
                          counted={"iqd": iqd, "usd": usd})
        count_id = db.add_drawer_count(self.conn, "close", iqd, usd, self.source_message, self.session_id)
        self.session_tx_ids.append(("drawer", count_id))
        report = db.drawer_report(self.conn, db.get_drawer_count(self.conn, count_id))
        return {"ok": True, "ledger": "drawer", **report, "meaning": _drawer_meaning(report)}

    def tool_set_drawer_opening(self, iqd=None, usd=None):
        iqd, usd, err = self._drawer_amounts(iqd, usd)
        if err:
            return err
        count_id = db.add_drawer_count(self.conn, "open", iqd, usd, self.source_message, self.session_id)
        self.session_tx_ids.append(("drawer", count_id))
        return {"ok": True, "ledger": "drawer", "opening": {"iqd": iqd, "usd": usd}}

    def tool_drawer_status(self):
        last = db.last_drawer_count(self.conn)
        since = db.drawer_report(self.conn)
        result = {"ok": True, "ledger": "drawer", "since_last_count": since}
        if last and last["kind"] == "close":
            result["last_close"] = db.drawer_report(self.conn, last)
        if last is None:
            result["note"] = "ما كو عدّة بعد. لما يسد صاحب المحل يگول: سديت، بالجرار كذا."
        return result

    def tool_drawer_details(self, which="last_close"):
        last = db.last_drawer_count(self.conn)
        if which != "since_last_count" and last and last["kind"] == "close":
            details = db.drawer_details(self.conn, last)
        else:
            details = db.drawer_details(self.conn)
        return {"ok": True, "ledger": "drawer", **details}

    def tool_drawer_history(self, limit=7):
        limit = max(1, min(_as_int(limit) or 7, 60))
        return {"ok": True, "ledger": "drawer", "closes": db.drawer_closes(self.conn, limit)}

    # ----- memory -----

    def tool_remember(self, fact):
        fact = " ".join(str(fact or "").split())
        if not fact:
            return _error("invalid_fact", "شنو تريدني أتذكر؟")
        if len(fact) > MAX_MEMORY_CHARS:
            return _error("too_long", f"خليها أقصر من {MAX_MEMORY_CHARS} حرف")
        if not _TEACHING.search(self.source_message or ""):
            return _error("ask_first", "صاحب المحل ما طلب تتذكر شي. إذا تشوفها مفيدة، اسأله: 'أحفظها بذاكرتي؟'")
        if db.find_memory(self.conn, fact):
            return _error("already_known", "هاي محفوظة من قبل")
        if len(db.list_memories(self.conn)) >= MAX_MEMORIES:
            return _error("memory_full", f"الذاكرة مليانة ({MAX_MEMORIES}). امسح شي قديم أول.")
        mem_id = db.add_memory(self.conn, fact, self.source_message)
        return {"ok": True, "memory_id": mem_id, "fact": fact}

    def tool_forget(self, memory_id):
        mid = _as_int(memory_id)
        gone = db.forget_memory(self.conn, mid) if mid is not None else None
        if gone is None:
            return _error("memory_not_found", "ما لگيت هالشي بالذاكرة. شوف list_memories.")
        return {"ok": True, "forgot": gone}

    def tool_list_memories(self):
        memories = db.list_memories(self.conn)
        return {"ok": True, "count": len(memories), "memories": memories}

    def tool_undo_last(self):
        while self.session_tx_ids:
            entry = self.session_tx_ids.pop()
            if isinstance(entry, tuple) and entry[0] == "drawer":
                count = db.get_drawer_count(self.conn, entry[1])
                if count and not count["undone"]:
                    db.mark_drawer_count_undone(self.conn, count["id"])
                    return {"ok": True, "undone": {"ledger": "drawer", "type": "drawer_" + count["kind"],
                                                   "count_id": count["id"], "iqd": count["iqd"],
                                                   "usd": count["usd"]}}
                continue
            if isinstance(entry, tuple):  # ("withdrawal", id)
                wd = db.get_withdrawal(self.conn, entry[1])
                if wd and not wd["undone"]:
                    db.mark_withdrawal_undone(self.conn, wd["id"])
                    return {"ok": True, "undone": {"ledger": "withdrawal", "type": "withdrawal",
                                                   "withdrawal_id": wd["id"], "reason": wd["reason"],
                                                   "amount": wd["amount"], "currency": wd["currency"]},
                            "month_total": db.withdrawals_summary(self.conn, "month")["totals"]}
                continue
            tx_id = entry
            tx = db.get_transaction(self.conn, tx_id)
            if tx and not tx["undone"]:
                db.mark_undone(self.conn, tx_id)
                customer = db.get_customer(self.conn, tx["customer_id"])
                supplier = customer.get("kind") == "supplier"
                return {
                    "ok": True,
                    "undone": {
                        "transaction_id": tx_id,
                        "ledger": "supplier" if supplier else "customer",
                        "type": ({"debt": "purchase", "payment": "supplier_payment"}[tx["type"]]
                                 if supplier else tx["type"]),
                        "customer_name": customer["name"],
                        "amount": tx["amount"],
                        "currency": tx["currency"],
                    },
                    "balance": db.get_balance(self.conn, tx["customer_id"]),
                }
        return _error("nothing_to_undo", "ما كو قيد بهذه الجلسة حتى ألغيه")

    def tool_undo_batch(self, batch_id=None):
        batch_id = str(batch_id).strip() if batch_id else db.last_batch_id(self.conn)
        if not batch_id:
            return _error("nothing_to_undo", "ما كو دفعة مستوردة حتى ألغيها")
        rows = db.undo_batch(self.conn, batch_id)
        if not rows:
            return _error("nothing_to_undo", "هاي الدفعة ملغية من قبل أو مو موجودة", batch_id=batch_id)
        totals = {}
        for r in rows:
            key = f"{r['type']}_{r['currency']}"
            totals[key] = totals.get(key, 0) + r["amount"]
        names = sorted({db.get_customer(self.conn, r["customer_id"])["name"] for r in rows})
        return {"ok": True, "batch_id": batch_id, "undone_count": len(rows),
                "totals": totals, "customers": names}


def main(argv: list[str]) -> int:
    """Manual tool runner: python -m app.tools <tool_name> ['<json args>']"""
    from dotenv import load_dotenv

    load_dotenv()
    if not argv:
        print("الاستخدام: python -m app.tools <tool_name> '<json args>'")
        print("الأدوات:", ", ".join(t["name"] for t in TOOL_SCHEMAS))
        return 1
    args = json.loads(argv[1]) if len(argv) > 1 else {}
    tools = DebtTools(db.connect())
    print(json.dumps(tools.execute(argv[0], args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
