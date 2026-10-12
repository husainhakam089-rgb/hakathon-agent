"""The whole notebook as one Excel file the owner can open, print or keep (right-to-left sheets).

Sheets: customers' balances, suppliers' balances, every entry, withdrawals.
Undone entries are listed too, marked "ملغي", and are not in the balances.
"""

import io

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

from app import db

TYPE_WORDS = {("customer", "debt"): "دين", ("customer", "payment"): "تسديد",
              ("supplier", "debt"): "وارد", ("supplier", "payment"): "صادر"}
CURRENCY_WORDS = {"IQD": "دينار", "USD": "دولار"}
HEAD_FONT = Font(bold=True, color="FFFFFF")
HEAD_FILL = PatternFill("solid", fgColor="D97706")
AMOUNT_HEADERS = {"دينار", "دولار", "المبلغ", "بالجرار (دينار)", "چان بالجرار", "تسديدات زبائن", "دفع للموردين",
                  "سحوبات", "البيع النقدي", "بيع بالدين", "مجموع البيع", "سعر البيع", "الكلفة", "سعر الوحدة",
                  "الربح"}


def _sheet(wb, title, headers, rows, widths):
    ws = wb.create_sheet(title)
    ws.sheet_view.rightToLeft = True
    ws.append(headers)
    for cell in ws[1]:
        cell.font, cell.fill = HEAD_FONT, HEAD_FILL
    for row in rows:
        ws.append(row)
    for line in ws.iter_rows(min_row=2):
        for cell in line:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"  # text the owner typed, never a formula
    for i, w in enumerate(widths):
        ws.column_dimensions[chr(ord("A") + i)].width = w
    ws.freeze_panes = "A2"
    for col in range(1, len(headers) + 1):  # amounts with thousands separators
        if headers[col - 1] in AMOUNT_HEADERS:
            for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                cell.number_format = "#,##0"
    return ws


def _balances(conn, kind):
    rows = [r for r in db.all_balances(conn, kind) if any(r["balances"].values())]
    rows.sort(key=lambda r: (r["balances"]["IQD"], r["balances"]["USD"]), reverse=True)
    out = [[r["name"], r["balances"]["IQD"], r["balances"]["USD"], r["last_activity"] or ""] for r in rows]
    out.append(["المجموع", sum(max(r["balances"]["IQD"], 0) for r in rows),
                sum(max(r["balances"]["USD"], 0) for r in rows), ""])
    return out


def build_workbook(conn) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)

    _sheet(wb, "الزبائن (إلي)", ["الاسم", "دينار", "دولار", "آخر حركة"], _balances(conn, "customer"), [28, 16, 12, 20])
    _sheet(wb, "الموردين (عليّ)", ["الاسم", "دينار", "دولار", "آخر حركة"], _balances(conn, "supplier"), [28, 16, 12, 20])

    entries = conn.execute(
        """SELECT t.created_at, c.kind, c.name, t.type, t.amount, t.currency, t.source_message, t.note, t.undone
           FROM transactions t JOIN customers c ON c.id = t.customer_id ORDER BY t.id""").fetchall()
    _sheet(wb, "كل القيود", ["التاريخ", "الدفتر", "الاسم", "النوع", "المبلغ", "العملة", "الرسالة", "ملغي"],
           [[r["created_at"], "زبون" if r["kind"] == "customer" else "مورد", r["name"],
             TYPE_WORDS.get((r["kind"], r["type"]), r["type"]), r["amount"], CURRENCY_WORDS.get(r["currency"], r["currency"]),
             r["source_message"] or r["note"] or "", "ملغي" if r["undone"] else ""] for r in entries],
           [18, 10, 24, 10, 14, 10, 40, 8])

    wds = conn.execute("SELECT created_at, amount, currency, reason, source_message, undone FROM withdrawals ORDER BY id").fetchall()
    _sheet(wb, "السحوبات", ["التاريخ", "المبلغ", "العملة", "السبب", "الرسالة", "ملغي"],
           [[r["created_at"], r["amount"], CURRENCY_WORDS.get(r["currency"], r["currency"]), r["reason"],
             r["source_message"] or "", "ملغي" if r["undone"] else ""] for r in wds],
           [18, 14, 10, 18, 40, 8])

    closes = list(reversed(db.drawer_closes(conn, 10_000)))
    _sheet(wb, "الجرار", ["العدّة", "بالجرار (دينار)", "چان بالجرار", "تسديدات زبائن", "دفع للموردين", "سحوبات",
                          "البيع النقدي", "بيع بالدين", "مجموع البيع"],
           [[c["counted"]["at"], c["counted"]["iqd"], (c["start"] or {}).get("iqd"), c["customer_payments"]["IQD"],
             c["supplier_payments"]["IQD"], c["withdrawals"]["IQD"], c["cash_sales"].get("IQD"),
             c["credit_sales"]["IQD"], c["total_sales"].get("IQD")] for c in closes],
           [18, 14, 14, 14, 14, 12, 14, 12, 14])

    moves = conn.execute(
        """SELECT t.created_at AS at, CASE WHEN c.kind = 'customer' THEN 'وارد' ELSE 'صادر' END AS side,
                  CASE WHEN c.kind = 'customer' THEN 'تسديد من ' || c.name ELSE 'دفع لـ ' || c.name END AS what,
                  t.amount, t.currency, t.source_message AS msg
           FROM transactions t JOIN customers c ON c.id = t.customer_id
           WHERE t.type = 'payment' AND t.from_drawer = 1 AND t.undone = 0 AND t.batch_id IS NULL
           UNION ALL
           SELECT created_at, 'صادر', reason, amount, currency, source_message FROM withdrawals
           WHERE from_drawer = 1 AND undone = 0
           UNION ALL
           SELECT created_at, 'وارد', label, amount, currency, source_message FROM cash_ins WHERE undone = 0
           ORDER BY at""").fetchall()
    _sheet(wb, "الوارد والصادر", ["التاريخ", "وارد / صادر", "من وين / على شنو", "المبلغ", "العملة", "الرسالة"],
           [[r["at"], r["side"], r["what"], r["amount"], CURRENCY_WORDS.get(r["currency"], r["currency"]), r["msg"] or ""]
            for r in moves],
           [18, 12, 28, 14, 10, 40])

    items = db.all_items(conn, active_only=False)
    _sheet(wb, "المواد", ["المادة", "الوحدة", "سعر البيع", "الكلفة", "العملة"],
           [[i["name"], i["unit"] or "", i["price"], i["cost"], CURRENCY_WORDS.get(i["currency"], i["currency"])] for i in items],
           [24, 12, 14, 14, 10])
    pay = {"cash": "كاش", "debt": "دين", "electronic": "إلكتروني"}
    lines = conn.execute(
        """SELECT s.id, s.created_at, s.payment, c.name AS customer, i.name AS item, l.qty, l.unit_price, l.line_total,
                  l.unit_cost, s.currency, s.undone
           FROM sale_lines l JOIN sales s ON s.id = l.sale_id JOIN items i ON i.id = l.item_id
           LEFT JOIN customers c ON c.id = s.customer_id ORDER BY s.id, l.id""").fetchall()
    _sheet(wb, "المبيعات", ["التاريخ", "رقم البيعة", "الدفع", "الزبون", "المادة", "العدد", "سعر الوحدة", "المبلغ",
                            "الكلفة", "الربح", "ملغي"],
           [[r["created_at"], r["id"], pay[r["payment"]], r["customer"] or "", r["item"], r["qty"], r["unit_price"],
             r["line_total"], None if r["unit_cost"] is None else round(r["qty"] * r["unit_cost"]),
             None if r["unit_cost"] is None else r["line_total"] - round(r["qty"] * r["unit_cost"]),
             "ملغي" if r["undone"] else ""] for r in lines],
           [18, 10, 10, 18, 18, 8, 12, 12, 12, 12, 8])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
