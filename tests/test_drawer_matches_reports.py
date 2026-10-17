"""17-10-2026 merchant day: at the count the drawer said "البيع النقدي 60,000، ومجموع البيع ويا
الدين 195,000" while the reports said the day's sales were 72,000 (62,000 cash + 10,000 on credit).
With recorded sales the drawer now uses the reports' figure, and shows the shortage and the
plain debts each on its own."""

from app import db, reports


def test_the_merchant_day_drawer_says_the_same_sales_as_the_reports(conn, tools):
    ali = db.add_customer(conn, "أبو علي")
    umm = db.add_customer(conn, "أم حسين")
    haider = db.add_customer(conn, "حيدر")
    rafidain = db.add_customer(conn, "شركة الرافدين", kind="supplier")
    flour = db.add_item(conn, "طحين", 25000, 21000, "كيس")
    oil = db.add_item(conn, "زيت", 4000, 3250, "قنينة")
    rice = db.add_item(conn, "رز", 2000, 1600, "كيلو")
    assert tools.execute("set_drawer_opening", {"iqd": 150000})["ok"]
    assert tools.execute("record_sale", {"payment": "cash", "lines": [{"item_id": flour, "qty": 2}, {"item_id": oil, "qty": 3}]})["ok"]
    tools.execute("record_debt", {"customer_id": ali, "amount": 35000, "currency": "IQD"})
    assert tools.execute("record_sale", {"payment": "debt", "customer_id": ali, "lines": [{"item_id": rice, "qty": 5}]})["ok"]
    tools.execute("record_debt", {"customer_id": umm, "amount": 60000, "currency": "IQD"})
    tools.execute("record_payment", {"customer_id": umm, "amount": 20000, "currency": "IQD"})
    tools.execute("record_purchase", {"supplier_id": rafidain, "amount": 500000, "currency": "IQD", "confirmed": True})
    tools.execute("record_supplier_payment", {"supplier_id": rafidain, "amount": 100000, "currency": "IQD", "from_drawer": True})
    tools.execute("record_withdrawal", {"amount": 30000, "currency": "IQD", "reason": "مولدة"})
    tools.execute("record_debt", {"customer_id": haider, "amount": 15000, "currency": "IQD"})
    tools.execute("record_payment", {"customer_id": haider, "amount": 15000, "currency": "IQD"})
    tools.execute("record_debt", {"customer_id": umm, "amount": 15000, "currency": "IQD"})

    c = tools.execute("count_drawer", {"iqd": 115000})
    assert c["expected"]["IQD"] == 117000 and c["difference"]["IQD"] == -2000  # unchanged: 2,000 short
    sold_today = reports.profit(conn, "today")["sales"]["IQD"]
    assert sold_today == 72000
    assert c["sales_recorded"]["IQD"] == sold_today == c["day_total_sales"]["IQD"]  # the same figure
    assert c["day_cash_sales"]["IQD"] == 62000  # what was sold for cash, not lowered by the shortage
    assert c["new_debts"]["IQD"] == 35000 + 60000 + 15000 + 15000  # plain debts, not sales
    m = c["meaning"]
    assert "البيع المسجل 72,000" in m and "كاش 62,000" in m and "دين 10,000" in m
    assert "ناقص 2,000" in m and "ديون جديدة على الزبائن 125,000" in m
    assert "195,000" not in m and "60,000" not in m  # the old mixed figures are gone
    # the old fields stay as they were for the export and older screens
    assert c["cash_sales"]["IQD"] == 60000 and c["total_sales"]["IQD"] == 195000


def test_a_shop_that_records_no_sales_keeps_the_old_reading(conn, tools):
    ali = db.add_customer(conn, "أبو علي")
    tools.execute("set_drawer_opening", {"iqd": 50000})
    tools.execute("record_debt", {"customer_id": ali, "amount": 75000, "currency": "IQD"})
    c = tools.execute("count_drawer", {"iqd": 150000})
    assert c["day_cash_sales"] == c["cash_sales"] == {"IQD": 100000}  # the count is the only measure
    assert c["day_total_sales"] == c["total_sales"] == {"IQD": 175000}
    assert "البيع النقدي 100,000" in c["meaning"]
