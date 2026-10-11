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


def _error(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


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
        # Transactions recorded in this session, for undo_last.
        self.session_tx_ids: list[int] = []
        # Set by the agent before each turn so every entry keeps its original message
        # and where it came from ("chat" typed, "voice" dictated).
        self.source_message: str | None = None
        self.source: str = "chat"
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
                kind="customer"):
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
        if tx_type == "payment" and value > before[currency]:
            result["overpayment"] = True
            result["overpaid_by"] = value - max(before[currency], 0)
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

    def tool_record_payment(self, customer_id, amount, currency="IQD", note=None, confirmed=False):
        if _OWNER_PAID.search(self.source_message or ""):
            return _error("wrong_ledger", "صاحب المحل هو اللي دفع (دفعت لـ...)، فهذا مو تسديد من زبون. "
                          "إذا دفع لمورد استعمل record_supplier_payment، وإذا الاسم بالدفترين اسأله "
                          "'تقصد الزبون لو المورد؟'.")
        return self._record("payment", customer_id, amount, currency, note, confirmed)

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

    def tool_record_supplier_payment(self, supplier_id, amount, currency="IQD", note=None, confirmed=False):
        return self._record("payment", supplier_id, amount, currency, note, confirmed, kind="supplier")

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

    def tool_undo_last(self):
        while self.session_tx_ids:
            tx_id = self.session_tx_ids.pop()
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
