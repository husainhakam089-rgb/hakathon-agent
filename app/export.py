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
                  "سحوبات", "البيع النقدي", "بيع بالدين", "مجموع البيع"}


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

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
