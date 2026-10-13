"""Stage 20: a made-up mall notebook for 3 months, and an independent "paper" calculator.

- generate(): the mall's days as a list of events (sales, payments, purchases, withdrawals,
  cash in, drawer counts), from a fixed seed, so every run gets the same notebook. The last
  day is today (so "هالشهر" and "اليوم" mean what they say when the eval runs).
- build_db(conn, events): writes them through the app's own functions, with their dates.
- Paper: answers worked out from the events alone, without any app code. When the app's
  answer and the paper's differ, one of them is wrong, and the difference is written down.

The mall (from the submission): food, cleaning, toys and stationery; debts on a notebook; cash,
debt and Zain Cash sales; a generator paid by the ampere; rent and two salaries a month.
"""

import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DAYS = 90
SEED = 2026

# name, unit, price, cost (None = the owner never wrote the cost)
ITEMS = [
    ("طحين", "كيس", 25000, 22000), ("رز", "كيس", 30000, 26000), ("سكر", "كيس", 20000, 17500),
    ("زيت", "قنينة", 4000, 3000), ("شاي", "علبة", 6000, 4500), ("معجون طماطة", "علبة", 1500, 1100),
    ("حليب", "كارتون", 9000, 7500), ("عدس", "كيلو", 2500, 1900), ("بيض", "طبقة", 6000, 5000),
    ("مناديل", "باكيت", 2000, 1400), ("صابون", "قطعة", 1000, None), ("مسحوق غسيل", "كيس", 7000, 5500),
    ("كلور", "قنينة", 1500, 1000), ("شامبو", "قنينة", 5000, 3800), ("معطر", "قنينة", 3500, None),
    ("كرة قدم", "قطعة", 10000, 7000), ("دمية", "قطعة", 15000, 10000), ("سيارة لعبة", "قطعة", 12000, 8500),
    ("دفتر", "قطعة", 1000, 600), ("قلم", "قطعة", 500, 250), ("حقيبة مدرسية", "قطعة", 25000, 18000),
    ("ألوان", "علبة", 3000, 2000), ("بسكت", "قطعة", 500, 350), ("مشروب غازي", "قنينة", 1000, 750),
]
CUSTOMERS = ["أبو علي", "علي النجار", "أم حسين", "أبو حسين", "الحجي كريم", "أم زينب", "أبو مصطفى", "مصطفى الحداد",
             "أبو حيدر", "أم مريم", "كرار", "سجاد", "أبو أحمد", "أم علي", "الأستاذ سعد", "أبو جاسم", "حيدر الكهربائي",
             "أم نور"]
SUPPLIERS = ["شركة الزين", "مندوب البيبسي", "شركة الريم للمنظفات", "مكتبة النور", "معمل ألعاب بغداد"]
# what each supplier sells (purchases are charged to them)
SUPPLIES = {"شركة الزين": (300000, 1500000), "مندوب البيبسي": (50000, 200000), "شركة الريم للمنظفات": (100000, 600000),
            "مكتبة النور": (80000, 400000), "معمل ألعاب بغداد": (150000, 700000)}
GENERATOR_AMPS, AMP_PRICE = 30, 12000          # 360,000 a month, on the 1st
RENT, SALARY = 500000, 400000                   # rent on the 1st, two salaries on the 28th
LATE_PAYERS = ["أبو جاسم", "أم نور", "الأستاذ سعد"]  # take on debt early, then never pay (overdue)
SHORT_DAYS = (17, 38, 61, 80)                   # planted drawer shortages (day index)
SHORT_AMOUNTS = (25000, 40000, 15000, 50000)


def day_date(day: int, today: date | None = None) -> date:
    """Day index -> date: the last day (DAYS - 1) is today."""
    return (today or date.today()) - timedelta(days=DAYS - 1 - day)


def generate(seed: int = SEED, today: date | None = None) -> list[dict]:
    rnd = random.Random(seed)
    events = []

    def add(day, hh, mm, kind, **kw):
        events.append({"day": day, "time": f"{hh:02d}:{mm:02d}:00", "kind": kind, **kw})

    add(0, 8, 30, "open", iqd=100000)
    item_names = [i[0] for i in ITEMS]
    weights = [6, 4, 4, 8, 6, 6, 5, 4, 5, 6, 8, 4, 5, 3, 3, 2, 1, 1, 6, 8, 1, 3, 9, 9]
    for day in range(DAYS):
        d = day_date(day, today)
        is_today = day == DAYS - 1
        last_hour = 12 if is_today else 21  # today is not over: only the morning
        weekend = d.weekday() == 4  # Friday is busier
        # fixed monthly expenses
        if d.day == 1:
            add(day, 9, 0, "withdrawal", reason="مولدة", amount=GENERATOR_AMPS * AMP_PRICE, recurring=True,
                note=f"{GENERATOR_AMPS} أمبير × {AMP_PRICE:,}")
            add(day, 9, 5, "withdrawal", reason="إيجار", amount=RENT, recurring=True)
        if d.day == 28:
            add(day, 20, 0, "withdrawal", reason="رواتب", amount=2 * SALARY, recurring=True)
        n_sales = rnd.randint(18, 30) + (10 if weekend else 0)
        if is_today:
            n_sales = 6
        for _ in range(n_sales):
            hh, mm = rnd.randint(9, last_hour - 1), rnd.randint(0, 59)
            lines = {}
            for _ in range(rnd.choice([1, 1, 1, 2, 2, 3])):
                name = rnd.choices(item_names, weights)[0]
                lines[name] = lines.get(name, 0) + rnd.choice([1, 1, 1, 2, 2, 3])
            pay = rnd.choices(["cash", "debt", "electronic"], [70, 15, 15])[0]
            customer = None
            if pay == "debt":
                pool = CUSTOMERS if day < 20 else [c for c in CUSTOMERS if c not in LATE_PAYERS]
                customer = rnd.choice(pool)
            add(day, hh, mm, "sale", lines=lines, payment=pay, customer=customer)
        # customers paying back (the late payers never do)
        for _ in range(rnd.randint(0, 4) if not is_today else 1):
            who = rnd.choice([c for c in CUSTOMERS if c not in LATE_PAYERS])
            add(day, rnd.randint(9, last_hour - 1), rnd.randint(0, 59), "payment", customer=who,
                amount=rnd.choice([5000, 10000, 15000, 20000, 25000, 50000]))
        # an old-style notebook debt written by hand (no items) now and then
        if rnd.random() < 0.25 and not is_today:
            add(day, rnd.randint(9, 20), rnd.randint(0, 59), "debt", customer=rnd.choice(CUSTOMERS),
                amount=rnd.choice([10000, 15000, 25000, 40000]))
        # suppliers: goods on credit every few days, paid in cash from the drawer
        if day % 4 == 1 and not is_today:
            sup = SUPPLIERS[(day // 4) % len(SUPPLIERS)]
            lo, hi = SUPPLIES[sup]
            add(day, 10, rnd.randint(0, 59), "purchase", supplier=sup, amount=rnd.randrange(lo, hi, 50000))
        if day % 4 == 3 and not is_today:
            sup = SUPPLIERS[(day // 4) % len(SUPPLIERS)]
            add(day, 13, rnd.randint(0, 59), "supplier_payment", supplier=sup,
                amount=rnd.choice([100000, 150000, 200000, 250000]))
        # small daily expenses
        add(day, 14, rnd.randint(0, 59), "withdrawal", reason="أكل", amount=rnd.choice([5000, 7000, 10000]))
        if rnd.random() < 0.3:
            add(day, 11, rnd.randint(0, 59), "withdrawal", reason="نقل", amount=rnd.choice([10000, 15000, 25000]))
        if rnd.random() < 0.15:
            add(day, 16, rnd.randint(0, 59), "cash_in", label="كارت شحن", amount=rnd.choice([10000, 20000, 30000]))
        if is_today:
            continue
        # closing: the count, then the owner takes money home (it counts on the next day)
        if day in SHORT_DAYS:
            extra = -SHORT_AMOUNTS[SHORT_DAYS.index(day)]
        else:
            extra = rnd.choice([0, 0, 0, 1000, 2000, 5000, 10000])  # small sales nobody wrote down
        add(day, 22, 0, "close", extra=extra)  # the counted amount is worked out in build order
        add(day, 22, 10, "withdrawal", reason="البيت", amount=None, take_home=True)
    events.sort(key=lambda e: (e["day"], e["time"]))
    return events


# ---------- the paper calculator: from the events only, no app code ----------

PRICE = {name: price for name, _, price, _ in ITEMS}
COST = {name: cost for name, _, _, cost in ITEMS}


class Paper:
    """Walks the events once and keeps every figure a question can ask about."""

    def __init__(self, events: list[dict], today: date | None = None):
        self.today = today or date.today()
        self.events = events
        self.customer = {c: 0 for c in CUSTOMERS}
        self.supplier = {s: 0 for s in SUPPLIERS}
        self.drawer = 0
        self.closes = []  # (day, counted, expected, cash_sales_total)
        self._walk()

    def date(self, e):
        return day_date(e["day"], self.today)

    def sale_total(self, e):
        return sum(PRICE[n] * q for n, q in e["lines"].items())

    def _walk(self):
        since = {"payments": 0, "cash_ins": 0, "sales_cash": 0, "out": 0}
        start = None
        for e in self.events:
            k = e["kind"]
            if k == "open":
                self.drawer = e["iqd"]
                start = e["iqd"]
            elif k == "sale":
                total = self.sale_total(e)
                e["total"] = total
                if e["payment"] == "cash":
                    self.drawer += total
                    since["sales_cash"] += total
                elif e["payment"] == "debt":
                    self.customer[e["customer"]] += total
            elif k == "payment":
                self.customer[e["customer"]] -= e["amount"]
                self.drawer += e["amount"]
                since["payments"] += e["amount"]
            elif k == "debt":
                self.customer[e["customer"]] += e["amount"]
            elif k == "purchase":
                self.supplier[e["supplier"]] += e["amount"]
            elif k == "supplier_payment":
                self.supplier[e["supplier"]] -= e["amount"]
                self.drawer -= e["amount"]
                since["out"] += e["amount"]
            elif k == "withdrawal":
                if e.get("take_home"):
                    keep = 150000  # leaves change for the morning
                    e["amount"] = max(self.drawer - keep, 0) or 1000
                self.drawer -= e["amount"]
                since["out"] += e["amount"]
            elif k == "cash_in":
                self.drawer += e["amount"]
                since["cash_ins"] += e["amount"]
            elif k == "close":
                expected = self.drawer
                counted = expected + e["extra"]
                e["counted"] = counted
                cash_sales = counted - start - since["payments"] + since["out"]
                self.closes.append({"day": e["day"], "counted": counted, "expected": expected,
                                    "difference": counted - expected, "cash_sales": cash_sales})
                self.drawer = counted
                start = counted
                since = {"payments": 0, "cash_ins": 0, "sales_cash": 0, "out": 0}

    # ----- periods, the same definitions the shop owner means -----
    def in_period(self, e, period):
        d, t = self.date(e), self.today
        if period == "today":
            return d == t
        if period == "yesterday":
            return d == t - timedelta(days=1)
        if period == "week":
            return t - timedelta(days=6) <= d <= t
        if period == "month":
            return d.year == t.year and d.month == t.month
        if period == "last_month":
            first = t.replace(day=1)
            prev = (first - timedelta(days=1)).replace(day=1)
            return prev <= d < first
        return True

    def item_sales(self, item, period):
        qty = revenue = 0
        for e in self.events:
            if e["kind"] == "sale" and item in e["lines"] and self.in_period(e, period):
                qty += e["lines"][item]
                revenue += PRICE[item] * e["lines"][item]
        return qty, revenue

    def sales_total(self, period, payment=None):
        return sum(e["total"] for e in self.events if e["kind"] == "sale" and self.in_period(e, period)
                   and (payment is None or e["payment"] == payment))

    def withdrawals(self, period, reason=None):
        return sum(e["amount"] for e in self.events if e["kind"] == "withdrawal" and self.in_period(e, period)
                   and (reason is None or e["reason"] == reason))

    def profit(self, period):
        """Sales with a known cost minus their cost, minus the shop's expenses (as the app defines it)."""
        gross = 0
        for e in self.events:
            if e["kind"] == "sale" and self.in_period(e, period):
                for n, q in e["lines"].items():
                    if COST[n] is not None:
                        gross += (PRICE[n] - COST[n]) * q
        return gross - self.withdrawals(period) + self.withdrawals(period, "البيت")  # taking money home is no expense

    def statement(self, customer, period):
        debts = paid = 0
        for e in self.events:
            if e.get("customer") != customer or not self.in_period(e, period):
                continue
            if e["kind"] == "sale" and e["payment"] == "debt":
                debts += e["total"]
            elif e["kind"] == "debt":
                debts += e["amount"]
            elif e["kind"] == "payment":
                paid += e["amount"]
        return debts, paid, self.customer[customer]

    def close_on(self, day):
        return next((c for c in self.closes if c["day"] == day), None)


# ---------- writing the events into the app's database ----------

def build_db(conn, events: list[dict], today: date | None = None) -> dict:
    """Through the app's own functions, then each row is dated. Events must be in time order
    (ids follow time, as in a real shop). Returns name -> id maps."""
    from app import db

    ids = {"customer": {}, "supplier": {}, "item": {}}
    for c in CUSTOMERS:
        ids["customer"][c] = db.add_customer(conn, c)
    for s in SUPPLIERS:
        ids["supplier"][s] = db.add_customer(conn, s, kind="supplier")
    for name, unit, price, cost in ITEMS:
        ids["item"][name] = db.add_item(conn, name, price, cost, unit)
    db.add_recurring(conn, "مولدة", GENERATOR_AMPS * AMP_PRICE, "IQD", 1)
    db.add_recurring(conn, "إيجار", RENT, "IQD", 1)
    db.add_recurring(conn, "رواتب", 2 * SALARY, "IQD", 28)

    def stamp(table, row_id, e):
        at = f"{day_date(e['day'], today).isoformat()} {e['time']}"
        conn.execute(f"UPDATE {table} SET created_at = ? WHERE id = ?", (at, row_id))

    for e in events:
        k = e["kind"]
        if k == "sale":
            lines = [{"item_id": ids["item"][n], "qty": q, "unit_price": PRICE[n], "unit_cost": COST[n]}
                     for n, q in e["lines"].items()]
            cust = ids["customer"][e["customer"]] if e["customer"] else None
            sid = db.add_sale(conn, e["payment"], lines, customer_id=cust, source="screen")
            stamp("sales", sid, e)
            debt_tx = conn.execute("SELECT debt_tx_id FROM sales WHERE id = ?", (sid,)).fetchone()[0]
            if debt_tx:
                stamp("transactions", debt_tx, e)
        elif k in ("payment", "debt"):
            tid = db.add_transaction(conn, ids["customer"][e["customer"]], k, e["amount"])
            stamp("transactions", tid, e)
        elif k == "purchase":
            stamp("transactions", db.add_transaction(conn, ids["supplier"][e["supplier"]], "debt", e["amount"]), e)
        elif k == "supplier_payment":
            stamp("transactions", db.add_transaction(conn, ids["supplier"][e["supplier"]], "payment", e["amount"]), e)
        elif k == "withdrawal":
            wid = db.add_withdrawal(conn, e["amount"], "IQD", e["reason"],
                                    note="صرفية ثابتة" if e.get("recurring") else e.get("note"))
            stamp("withdrawals", wid, e)
        elif k == "cash_in":
            stamp("cash_ins", db.add_cash_in(conn, e["amount"], "IQD", e["label"]), e)
        elif k in ("open", "close"):
            cid = db.add_drawer_count(conn, k, e["iqd"] if k == "open" else e["counted"], None)
            stamp("drawer_counts", cid, e)
    # the fixed expenses of this month that already happened are done
    month = (today or date.today()).strftime("%Y-%m")
    for e in events:
        if e["kind"] == "withdrawal" and e.get("recurring") and day_date(e["day"], today).strftime("%Y-%m") == month:
            conn.execute("UPDATE recurring_expenses SET done_month = ? WHERE reason = ?", (month, e["reason"]))
    conn.commit()
    return ids


def mall(today: date | None = None):
    """(events, paper, conn) for one fresh mall in memory."""
    from app import db

    events = generate(today=today)
    paper = Paper(events, today)  # fills in counted amounts and take-home sums
    conn = db.connect(":memory:")
    ids = build_db(conn, events, today)
    return events, paper, conn, ids


if __name__ == "__main__":
    events, paper, conn, ids = mall()
    kinds = {}
    for e in events:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    print(f"{len(events)} حدث خلال {DAYS} يوم:", kinds)
    print("الدين على الزبائن:", sum(v for v in paper.customer.values() if v > 0))
    print("عليّ للموردين:", sum(v for v in paper.supplier.values() if v > 0))
    print("بيع هالشهر:", paper.sales_total("month"), "ربح هالشهر:", paper.profit("month"))
    print("generated at", datetime.now().isoformat(timespec="seconds"))
