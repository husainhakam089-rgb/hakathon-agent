"""Stage 20: 100 questions in Iraqi dialect about the mall, each with its answer worked out on
paper (eval/mall/mall.py, no app code). 20 of them are held out: they are not run while the
system is being worked on, only once at the end (run_questions.py --holdout).

A numeric answer passes when the expected figure is in the reply (with or without commas).
An unclear question passes when the agent asks back ("؟") instead of answering. Any question
that makes the agent write something fails: these are all questions, nothing is recorded.
"""

from mall import CUSTOMERS, DAYS, ITEMS, SUPPLIERS

PERIOD_Q = {"month": "هالشهر", "week": "هالأسبوع", "last_month": "الشهر الماضي", "today": "اليوم",
            "yesterday": "البارحة"}


def build(paper) -> list[dict]:
    qs = []

    def add(category, q, numbers=None, ask=False):
        qs.append({"category": category, "q": q, "numbers": numbers or [], "ask": ask})

    owing = [c for c in CUSTOMERS if paper.customer[c] > 0]
    forms = ["شكد على {}؟", "شكد باقي على {}؟", "{} شكد عليه؟", "حساب {} شكد صار؟"]
    for i, c in enumerate(owing[:18]):
        add("رصيد زبون", forms[i % len(forms)].format(c), [paper.customer[c]])
    total = sum(v for v in paper.customer.values() if v > 0)
    top = max(CUSTOMERS, key=lambda c: paper.customer[c])
    add("مجموع الديون", "شكد مجموع الديون على الزبائن؟", [total])
    add("مجموع الديون", "منو أكثر واحد عليه دين؟", [paper.customer[top]])
    add("مجموع الديون", "شكد إلي على الناس كلهم؟", [total])
    add("مجموع الديون", "أكثر زبون مطلوب شكد عليه؟", [paper.customer[top]])

    for s in SUPPLIERS:
        add("الموردين", f"شكد عليّ لـ {s}؟", [paper.supplier[s]])
    owed = sum(v for v in paper.supplier.values() if v > 0)
    add("الموردين", "شكد مطلوب مني للشركات كلها؟", [owed])
    add("الموردين", "شكد عليّ للموردين؟", [owed])

    for name, unit, *_ in ITEMS[:8]:
        qty, revenue = paper.item_sales(name, "month")
        add("بيع مادة", f"شكد بعنا {name} هالشهر؟", [revenue])
        qty, revenue = paper.item_sales(name, "week")
        add("بيع مادة", f"شكد بعنا {name} هالأسبوع؟", [revenue])
    for name, unit, *_ in ITEMS[8:13]:
        qty, _ = paper.item_sales(name, "month")
        add("عدد مادة", f"كم {unit} {name} انباع هالشهر؟", [qty])

    add("مجموع البيع", "شكد بعنا هالأسبوع؟", [paper.sales_total("week")])
    add("مجموع البيع", "شكد بعنا هالشهر؟", [paper.sales_total("month")])
    add("مجموع البيع", "شكد بعنا الشهر الماضي؟", [paper.sales_total("last_month")])
    add("مجموع البيع", "شكد بعنا زين كاش وماستر هالشهر؟", [paper.sales_total("month", "electronic")])
    add("مجموع البيع", "شكد بعنا دين هالأسبوع؟", [paper.sales_total("week", "debt")])
    add("مجموع البيع", "شكد بعنا كاش الشهر الماضي؟", [paper.sales_total("last_month", "cash")])

    for period in ("month", "last_month", "week", "yesterday", "today"):
        add("الربح", f"شكد ربحت {PERIOD_Q[period]}؟", [abs(paper.profit(period))])
    add("الربح", "شكد الربح الصافي للشهر الماضي؟", [abs(paper.profit("last_month"))])

    for reason, period in [("مولدة", "month"), ("مولدة", "last_month"), ("إيجار", "month"), ("رواتب", "last_month"),
                           ("أكل", "month"), ("أكل", "week"), ("نقل", "month"), ("نقل", "last_month")]:
        add("الصرفيات", f"شكد صرفنا على ال{reason.removeprefix('ال')} {PERIOD_Q[period]}؟"
            if reason not in ("رواتب", "إيجار") else f"شكد دفعنا {reason} {PERIOD_Q[period]}؟",
            [paper.withdrawals(period, reason)])
    add("الصرفيات", "شكد سحبت للبيت هالشهر؟", [paper.withdrawals("month", "البيت")])
    add("الصرفيات", "شكد سحبت للبيت هالأسبوع؟", [paper.withdrawals("week", "البيت")])

    for c in owing[:8]:
        debts, paid, now = paper.statement(c, "week")
        add("كشف حساب", f"شكد حساب {c} هالأسبوع؟", [now])

    for c in owing[8:13]:
        add("كشف حساب", f"كشف حساب {c} هالشهر، شكد صار عليه؟", [paper.customer[c]])
    for name, unit, *_ in ITEMS[13:17]:
        add("بيع مادة", f"شكد بعنا {name} الشهر الماضي؟", [paper.item_sales(name, "last_month")[1]])

    y = DAYS - 2  # yesterday
    close = paper.close_on(y)
    add("الجرار", "شكد بعنا نقد البارحة حسب الجرار؟", [close["cash_sales"]])
    add("الجرار", "شكد عديت بالجرار البارحة؟", [close["counted"]])
    if close["difference"]:
        add("الجرار", "البارحة الجرار چان بيه زيادة لو نقص؟ شكد؟", [abs(close["difference"])])
    else:
        add("الجرار", "شكد چان المفروض بالجرار البارحة؟", [close["expected"]])
    day_before = paper.close_on(y - 1)
    add("الجرار", "شكد چان بالجرار قبل البارحة آخر الليل؟", [day_before["counted"]])
    add("الجرار", "شكد بعنا نقد قبل البارحة؟", [day_before["cash_sales"]])

    for q in ["شكد عليه؟", "شكد طلعت؟", "سجل عليه 25", "شكد على حسين؟", "شكد على علي؟", "شكد دفع؟",
              "شكد عليّ للشركة؟", "بعت كيس", "سجل على أبو 10 آلاف", "شكد باقي عليها؟"]:
        add("سؤال غامض", q, ask=True)

    for i, q in enumerate(qs, 1):
        q["id"] = i
        q["holdout"] = i % 5 == 0  # 20 questions kept aside until the end
    return qs
