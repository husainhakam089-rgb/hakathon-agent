"""Agent tools: JSON schemas for the model + their implementations.

The agent only touches the database through `DebtTools.execute`.
Every tool returns a JSON-serializable dict with an `ok` field.
"""

import json
import os
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
]


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
        # Set by the agent before each turn so every entry keeps its original message.
        self.source_message: str | None = None

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

    def _customer_or_error(self, customer_id):
        cid = _as_int(customer_id)
        customer = db.get_customer(self.conn, cid) if cid is not None else None
        if customer is None:
            return None, _error("customer_not_found", f"ما كو زبون بالرقم {customer_id}")
        return customer, None

    def _record(self, tx_type, customer_id, amount, currency="IQD", note=None, confirmed=False):
        customer, err = self._customer_or_error(customer_id)
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
            note=note, source_message=self.source_message,
        )
        self.session_tx_ids.append(tx_id)
        after = db.get_balance(self.conn, customer["id"])

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

    # ----- tools -----

    def tool_find_customer(self, query):
        matches = db.find_customers(self.conn, str(query))
        return {
            "ok": True,
            "count": len(matches),
            "matches": [
                {"id": c["id"], "name": c["name"], "balance": db.get_balance(self.conn, c["id"])}
                for c in matches
            ],
        }

    def tool_add_customer(self, name, phone=None):
        name = (name or "").strip()
        if not name:
            return _error("invalid_name", "الاسم فارغ")
        exact = [c for c in db.find_customers(self.conn, name)
                 if c["normalized_name"] == normalize(name)]
        if exact:
            return _error("customer_exists", "اكو زبون بنفس الاسم",
                          existing={"id": exact[0]["id"], "name": exact[0]["name"]})
        cid = db.add_customer(self.conn, name, phone)
        return {"ok": True, "customer_id": cid, "name": name}

    def tool_record_debt(self, customer_id, amount, currency="IQD", note=None, confirmed=False):
        return self._record("debt", customer_id, amount, currency, note, confirmed)

    def tool_record_payment(self, customer_id, amount, currency="IQD", note=None, confirmed=False):
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

    def tool_undo_last(self):
        while self.session_tx_ids:
            tx_id = self.session_tx_ids.pop()
            tx = db.get_transaction(self.conn, tx_id)
            if tx and not tx["undone"]:
                db.mark_undone(self.conn, tx_id)
                customer = db.get_customer(self.conn, tx["customer_id"])
                return {
                    "ok": True,
                    "undone": {
                        "transaction_id": tx_id,
                        "type": tx["type"],
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
