"""A made-up shop for showing the program online (DEMO_DATA=1).

The competition asks for fake data: this fills an EMPTY notebook with a small Iraqi grocery
(customers and their debts, suppliers, items, a week of sales, expenses, fixed expenses and the
owner's reminders), dated back from today so the reports, the week's chart and the alerts all
have something to show. A notebook that already has anything in it is never touched, so the
owner's real notebook on his own computer is safe even if DEMO_DATA is set by mistake.
"""

import random
from datetime import datetime, timedelta

from app import db

CUSTOMERS = [  # name, phone: the street's regulars
    ("أبو علي", "07701234567"), ("أم حسين", None), ("الحجي كريم", "07801112233"), ("أبو جاسم", None),
    ("حيدر الكاسب", None), ("أم زينب", "07731239876"), ("مصطفى الحلاق", None), ("أبو مريم", None),
    ("سجاد ابن الجيران", None), ("أم محمد", "07714567321"), ("الأستاذ رعد", "07905556677"), ("أبو سيف السايق", None),
]
SUPPLIERS = ["شركة الرافدين للمواد الغذائية", "مندوب المشروبات الغازية", "معمل ألبان الريف", "مخبز الحي"]
ITEMS = [  # name, price, cost, unit: neighbourhood prices in dinars
    ("طحين صفر", 27000, 23500, "كيس"), ("رز عنبر", 2500, 2000, "كيلو"), ("رز بسمتي", 2000, 1600, "كيلو"),
    ("زيت دوار الشمس", 3500, 2900, "لتر"), ("شكر", 1250, 1000, "كيلو"), ("شاي سيلاني", 6000, 4750, "نص كيلو"),
    ("مشروب غازي", 500, 375, "قنينة"), ("ماء", 250, 160, "قنينة"), ("لبن", 1000, 750, "علبة"),
    ("بيض", 6000, 5250, "طبقة"), ("معجون طماطة", 1250, 1000, "علبة"), ("عدس", 2000, 1600, "كيلو"),
    ("صمون", 1000, 750, "10 صمونات"), ("جبن", 3000, 2400, "علبة"), ("معكرونة", 1000, 750, "كيس"),
    ("صابون", 750, 500, "قطعة"), ("مسحوق غسيل", 4500, 3700, "كيس"), ("مناديل", 1500, 1100, "باكيت"),
]
# what a basket usually looks like: an item and how many, weighted towards the everyday things
BASKET = [("صمون", 1, 9), ("صمون", 2, 4), ("ماء", 6, 4), ("مشروب غازي", 2, 5), ("لبن", 2, 5), ("بيض", 1, 3),
          ("شكر", 2, 3), ("رز عنبر", 3, 2), ("رز بسمتي", 5, 2), ("زيت دوار الشمس", 1, 3), ("شاي سيلاني", 1, 2),
          ("معجون طماطة", 2, 3), ("جبن", 1, 2), ("معكرونة", 2, 2), ("عدس", 1, 1), ("صابون", 2, 1),
          ("مسحوق غسيل", 1, 1), ("مناديل", 1, 2), ("طحين صفر", 1, 1)]
DAYS = 14  # two weeks of sales behind today
# customer debts and payments written by hand (old debts, salary-day payments): (days ago, name, kind, amount)
LEDGER = [
    (58, "الحجي كريم", "debt", 175000), (47, "الحجي كريم", "payment", 50000), (44, "أبو جاسم", "debt", 85000),
    (41, "أبو جاسم", "debt", 60000), (38, "أبو علي", "debt", 45000), (35, "أبو علي", "debt", 32500),
    (25, "أم حسين", "debt", 27750), (17, "أم حسين", "payment", 20000), (9, "مصطفى الحلاق", "debt", 18500),
    (1, "مصطفى الحلاق", "payment", 10000),
]
# regulars who take on credit: each buys on debt now and then (the code picks the days)
ON_CREDIT = ["أم حسين", "أم زينب", "حيدر الكاسب", "أبو مريم", "سجاد ابن الجيران", "أم محمد", "الأستاذ رعد",
             "أبو سيف السايق"]
SUPPLIER_LEDGER = [  # (days ago, supplier, "debt" = goods came / "payment" = paid them, amount)
    (24, "شركة الرافدين للمواد الغذائية", "debt", 1850000), (24, "شركة الرافدين للمواد الغذائية", "payment", 1000000),
    (10, "شركة الرافدين للمواد الغذائية", "debt", 960000), (10, "شركة الرافدين للمواد الغذائية", "payment", 500000),
    (8, "مندوب المشروبات الغازية", "debt", 312000), (8, "مندوب المشروبات الغازية", "payment", 312000),
    (6, "معمل ألبان الريف", "debt", 145000), (3, "معمل ألبان الريف", "payment", 100000),
    (2, "مخبز الحي", "debt", 84000), (1, "مندوب المشروبات الغازية", "debt", 187500),
]
WITHDRAWALS = [  # (days ago, reason, amount)
    (13, "مولدة", 45000), (12, "كهرباء وطنية", 22000), (11, "أجرة العامل", 75000), (9, "نقل بضاعة", 15000),
    (7, "كيس نايلون وأكياس", 12000), (6, "مولدة", 45000), (4, "أجرة العامل", 75000), (3, "تصليح الثلاجة", 35000),
    (2, "غدا العامل", 6000), (1, "نقل بضاعة", 10000),
]
MEMORIES = ["أبو جاسم ما أنطيه دين جديد قبل ما يسدد القديم.",
            "الحجي كريم يسدد أول كل شهر لما يستلم راتبه.",
            "المحل يفتح 8 الصبح ويسد 11 بالليل."]


def _at(conn, table: str, row_id: int, when: datetime) -> None:
    conn.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (when.strftime("%Y-%m-%d %H:%M:%S"), row_id))


def is_empty(conn) -> bool:
    return not any(conn.execute(f"SELECT 1 FROM {t} LIMIT 1").fetchone()
                   for t in ("customers", "items", "transactions", "sales", "withdrawals"))


def seed_if_empty(conn, now: datetime | None = None) -> bool:
    """Fill an empty notebook with the made-up shop. Returns False (and does nothing) otherwise."""
    if not is_empty(conn):
        return False
    now = now or datetime.now()
    day = lambda ago, hour: (now - timedelta(days=ago)).replace(hour=hour, minute=15, second=0, microsecond=0)  # noqa: E731
    people = {name: db.add_customer(conn, name, phone, commit=False) for name, phone in CUSTOMERS}
    people |= {name: db.add_customer(conn, name, kind="supplier", commit=False) for name in SUPPLIERS}
    items = {name: db.add_item(conn, name, price, cost, unit) for name, price, cost, unit in ITEMS}
    by_name = {name: (price, cost) for name, price, cost, _ in ITEMS}

    rng = random.Random(17)  # the same shop every time it is made
    weights = [w for _, _, w in BASKET]

    def basket():
        lines = {}
        for _ in range(rng.choice([1, 1, 2, 2, 3, 4])):
            name, qty, _w = rng.choices(BASKET, weights)[0]
            lines[name] = lines.get(name, 0) + qty
        return list(lines.items())

    def day_of_sales(ago):
        """A day's baskets: busier on Thursday and Friday evenings, quieter on Sunday."""
        busy = {3: 1.3, 4: 1.25, 6: 0.8}.get((now - timedelta(days=ago)).weekday(), 1.0)
        hours = [8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 19, 19, 20, 20, 21, 21, 22]
        out = []
        for _ in range(int(rng.randint(26, 38) * busy)):
            hour = rng.choice(hours)
            when = day(ago, hour).replace(minute=rng.randint(0, 59))
            if ago == 0 and when > now - timedelta(minutes=10):
                continue
            pick = rng.random()
            payment, who = ("debt", rng.choice(ON_CREDIT)) if pick < 0.09 else ("electronic", None) if pick < 0.13 \
                else ("cash", None)
            out.append((when, payment, who, basket()))
        return sorted(out)

    def sale(when, payment, customer, lines):
        sid = db.add_sale(conn, payment, [{"item_id": items[n], "qty": q, "unit_price": by_name[n][0],
                                           "unit_cost": by_name[n][1]} for n, q in lines],
                          customer_id=people.get(customer), source="screen")
        _at(conn, "sales", sid, when)
        tx = conn.execute("SELECT debt_tx_id FROM sales WHERE id = ?", (sid,)).fetchone()[0]
        if tx:
            _at(conn, "transactions", tx, when)

    # what happened before today, then this morning's float, then today
    for ago, name, kind, amount in sorted(LEDGER + SUPPLIER_LEDGER, reverse=True):
        _at(conn, "transactions", db.add_transaction(conn, people[name], kind, amount, commit=False),
            day(ago, 11).replace(minute=rng.randint(0, 59)))
    for ago, reason, amount in WITHDRAWALS:
        _at(conn, "withdrawals", db.add_withdrawal(conn, amount, "IQD", reason), day(ago, 13))
    for ago in range(DAYS, 0, -1):
        for args in day_of_sales(ago):
            sale(*args)
    conn.commit()
    # those who take on credit pay part of it back (some more, some less), from what they really owe
    for name in ON_CREDIT:
        owed = db.get_balance(conn, people[name])["IQD"]
        paid = int(owed * rng.choice([0.4, 0.5, 0.6, 0.75, 1.0]) // 1000 * 1000)
        if paid > 0:
            _at(conn, "transactions", db.add_transaction(conn, people[name], "payment", paid, commit=False),
                day(rng.randint(1, 4), 18).replace(minute=rng.randint(0, 59)))
    conn.commit()
    opening = db.add_drawer_count(conn, "open", 150000, None)
    _at(conn, "drawer_counts", opening, min(day(0, 8).replace(minute=0), now - timedelta(minutes=20)))
    conn.commit()
    for args in day_of_sales(0):
        sale(*args)

    db.add_recurring(conn, "إيجار المحل", 400000, "IQD", 1)
    db.add_recurring(conn, "أجرة العامل", 75000, "IQD", 1, period="week", day_of_week=3)  # every Thursday
    db.add_owner_reminder(conn, "اتصل بمندوب المشروبات يجيب 3 صناديق", now + timedelta(hours=2))
    db.add_owner_reminder(conn, "سدد الباقي لمعمل ألبان الريف (45 ألف)",
                          (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0))
    for text in MEMORIES:
        db.add_memory(conn, text)
    db.set_setting(conn, "shop_name", "أسواق الحي (بيانات تجريبية)")
    conn.commit()
    return True
