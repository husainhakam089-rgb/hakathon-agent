"""Agent tools: JSON schemas for the model + their implementations.

The agent only touches the database through `DebtTools.execute`.
Every tool returns a JSON-serializable dict with an `ok` field.
"""

import json
import os
import re
import sys

from app import db, payments, reports
from app.arabic import find_matches, normalize

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
        "description": "يلغي آخر قيد انسجل بهذه الجلسة ويرجع شنو انلغى. إذا صاحب المحل ذكر اسم (\"حيدر غلط، شطبها\") "
                       "مرر name: يلغي آخر قيد لهذا الشخص بالذات، مو آخر قيد بالمحادثة.",
        "input_schema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "اسم الزبون أو المورد اللي يريد يلغي قيده، اختياري"}},
        },
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
                "quantity": {"type": "number", "description": "إذا المبلغ عدد × سعر (30 أمبير × 12,000): العدد هنا، والكود يضرب"},
                "unit_price": {"type": "integer", "description": "سعر الوحدة ويا quantity (مثلًا سعر الأمبير)"},
                "unit": {"type": "string", "description": "اسم الوحدة ويا quantity: أمبير، يوم، ساعة..."},
                "note": {"type": "string"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
                "from_drawer": {"type": "boolean", "description": "false بس إذا گال إنه دفعها مو من الجرار (من جيبي، من البيت، حوالة). الافتراضي true"},
            },
            "required": ["currency", "reason"],
        },
    },
    {
        "name": "list_withdrawals",
        "description": "مجموع السحوبات لفترة (today = اليوم، yesterday = البارحة، week = آخر 7 أيام، month = هالشهر، last_month = الشهر الماضي، all = الكل)، لكل عملة ولكل سبب، وآخر السحوبات. reason اختياري: سحوبات سبب واحد بس.",
        "input_schema": {
            "type": "object",
            "properties": {
                "period": {"type": "string", "enum": ["today", "yesterday", "week", "month", "last_month", "all"]},
                "reason": {"type": "string"},
            },
            "required": ["period"],
        },
    },
    # ----- items and sales (stage 17) -----
    {
        "name": "find_item",
        "description": "يدور مادة بالاسم (طحين، رز، زيت...). يرجع المطابقات ويا سعر البيع والكلفة والوحدة.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "اسم المادة بس، بدون العدد والوحدة"}},
            "required": ["query"],
        },
    },
    {
        "name": "add_item",
        "description": "يضيف مادة جديدة لقائمة المواد، بس لما صاحب المحل يطلب: \"ضيف مادة طحين، الكيس بـ 25 وكلفته 22\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "price": {"type": "integer", "description": "سعر البيع للوحدة، عدد صحيح"},
                "cost": {"type": "integer", "description": "كلفة الوحدة (شكد اشتراها)، إذا گالها"},
                "unit": {"type": "string", "description": "كيس، كارتون، قطعة، كيلو..."},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
            },
            "required": ["name", "price"],
        },
    },
    {
        "name": "update_item",
        "description": "يغير سعر البيع أو الكلفة أو الوحدة لمادة موجودة: \"صار سعر الطحين 26\".",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "price": {"type": "integer"},
                "cost": {"type": "integer"},
                "unit": {"type": "string"},
            },
            "required": ["item_id"],
        },
    },
    {
        "name": "record_sale",
        "description": "يسجل بيعة بمادة أو أكثر. المجموع يحسبه الكود. payment: cash (كاش، يدخل الجرار) أو debt (دين، "
                       "ينضاف لحساب الزبون وحده ويحتاج customer_id) أو electronic (زين كاش، ماستر، كي كارد: ما يدخل الجرار).",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "item_id": {"type": "integer"},
                            "qty": {"type": "number", "description": "العدد، الافتراضي 1"},
                            "unit_price": {"type": "integer", "description": "بس إذا گال سعر غير السعر المسجل للمادة"},
                        },
                        "required": ["item_id"],
                    },
                },
                "payment": {"type": "string", "enum": ["cash", "debt", "electronic"]},
                "customer_id": {"type": "integer", "description": "للدين بس: الزبون من find_customer"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
            },
            "required": ["lines", "payment"],
        },
    },
    {
        "name": "item_sales",
        "description": "شكد انباع: لكل مادة العدد والمبلغ والكلفة والربح، والمجموع حسب طريقة الدفع. "
                       "item_id اختياري (مادة وحدة: \"شكد بعنا طحين هالشهر؟\"؛ مبلغها = items[0].revenue). "
                       "period: today، yesterday، week (آخر 7 أيام)، month (هالشهر)، last_month (الشهر الماضي)، all.",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_id": {"type": "integer"},
                "period": {"type": "string", "enum": ["today", "yesterday", "week", "month", "last_month", "all"]},
            },
            "required": ["period"],
        },
    },
    # ----- the cash drawer (stage 15) -----
    {
        "name": "record_cash_in",
        "description": "وارد للجرار: فلوس دخلت مو تسديد دين، ويا منو أو شنو: \"وارد محمد 25 ألف\"، \"بعت لأبو أحمد بـ 15\"، "
                       "\"دخل 40 ألف كارت شحن\". ما يغير أي دفتر، بس يبين بالجرار من وين دخل كل فلس.",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "integer", "description": "عدد صحيح موجب، بالوحدة الكاملة"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "label": {"type": "string", "description": "من منو أو على شنو، مثل ما گالها: محمد، بيع، كارت شحن"},
                "confirmed": {"type": "boolean", "description": "true فقط بعد ما المستخدم يأكد مبلغ كبير"},
            },
            "required": ["amount", "currency", "label"],
        },
    },
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
        "description": "تفاصيل الجرار سطر سطر، كل فلس من وين ووكت: الوارد (الفكة، التسديدات، الوارد مثل محمد 25، "
                       "والزيادة) والصادر (الدفع للموردين، السحوبات مثل راتب عامل، والنقص)، ومجموع الوارد والصادر والباقي بالجرار. "
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
    # ----- reports (stage 18): all computed by code -----
    {
        "name": "profit_report",
        "description": "الربح لفترة: البيع، وكلفة البضاعة المباعة، والصرفيات (السحوبات) حسب السبب، والربح الصافي. "
                       "period: today، yesterday، week (آخر 7 أيام)، month (هالشهر)، last_month، all.",
        "input_schema": {
            "type": "object",
            "properties": {"period": {"type": "string", "enum": ["today", "yesterday", "week", "month", "last_month", "all"]}},
            "required": ["period"],
        },
    },
    {
        "name": "week_summary",
        "description": "ملخص آخر 7 أيام: البيع حسب الدفع، الربح، الصرفيات، التسديدات، الديون الجديدة، الموردين، "
                       "أكثر المواد بيعًا، أحسن يوم، وعدّات الجرار الناقصة.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cash_forecast",
        "description": "توقع الكاش للأيام الجاية (الافتراضي 30): شكد متوقع يدخل ويطلع، من معدل آخر الأسابيع ويا الصرفيات الثابتة.",
        "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 30}}},
    },
    {
        "name": "customer_statement",
        "description": "كشف حساب زبون واحد لفترة: الرصيد قبل الفترة، كل قيد بيها ويا الرصيد بعده، والرصيد بالآخر. "
                       "\"شكد حساب أبو محمد هالأسبوع؟\" = period=week.",
        "input_schema": {
            "type": "object",
            "properties": {
                "customer_id": {"type": "integer"},
                "period": {"type": "string", "enum": ["today", "yesterday", "week", "month", "last_month", "all"]},
            },
            "required": ["customer_id", "period"],
        },
    },
    # ----- reminders and alerts (stage 19) -----
    {
        "name": "get_alerts",
        "description": "شنو لازم ينتبهله صاحب المحل هسه: الجرار ما انعد بعد الساعة المحددة، صرفيات ثابتة موعدها اليوم "
                       "أو فات وما انسجلت، وزبائن عليهم دين وما سددوا من مدة.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_recurring_expense",
        "description": "صرفية ثابتة كل شهر أو كل أسبوع (إيجار، مولدة، أجرة عامل): بيومها يذكّر صاحب المحل ويسأله أسجلها؟ "
                       "بس لما صاحب المحل يطلبها: \"الإيجار 250 ألف كل شهر يوم 1\" = every=month day_of_month=1. "
                       "\"أجرة العامل 100 ألف كل أسبوع يوم الخميس\" = every=week weekday=thursday.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
                "amount": {"type": "integer"},
                "currency": {"type": "string", "enum": ["IQD", "USD"]},
                "every": {"type": "string", "enum": ["month", "week"], "description": "الافتراضي month"},
                "day_of_month": {"type": "integer", "description": "للشهري: يوم الشهر 1-31"},
                "weekday": {"type": "string", "enum": ["saturday", "sunday", "monday", "tuesday", "wednesday",
                                                       "thursday", "friday"], "description": "للأسبوعي: اليوم"},
                "from_drawer": {"type": "boolean", "description": "false إذا گال يدفعها مو من الجرار"},
            },
            "required": ["reason", "amount", "currency"],
        },
    },
    {
        "name": "list_recurring_expenses",
        "description": "الصرفيات الثابتة (الشهرية والأسبوعية) ويا أرقامها وموعدها، وأي وحدة انسجلت بموعدها الحالي.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "remove_recurring_expense",
        "description": "يشيل صرفية ثابتة من القائمة برقمها (ما يمسح السحوبات اللي انسجلت قبل).",
        "input_schema": {"type": "object", "properties": {"recurring_id": {"type": "integer"}}, "required": ["recurring_id"]},
    },
    {
        "name": "record_recurring_expense",
        "description": "يسجل صرفية ثابتة لموعدها الحالي (هالشهر أو هالأسبوع) كسحب، بس بعد ما صاحب المحل يگول اي "
                       "(\"اي سجل الإيجار\"). skip=true = مو هالمرة.",
        "input_schema": {
            "type": "object",
            "properties": {"recurring_id": {"type": "integer"}, "skip": {"type": "boolean"}},
            "required": ["recurring_id"],
        },
    },
    {
        "name": "set_reminders",
        "description": "يغير إعدادات التنبيه: drawer_hour = الساعة اللي بعدها يذكّر بعدّ الجرار (0-23)، "
                       "overdue_days = بعد كم يوم بدون تسديد يعتبر الدين متأخر.",
        "input_schema": {
            "type": "object",
            "properties": {"drawer_hour": {"type": "integer"}, "overdue_days": {"type": "integer"}},
        },
    },
    # ----- Zain Cash links and reminders (stage 22) -----
    {
        "name": "set_customer_phone",
        "description": "يحفظ رقم موبايل الزبون (حتى التذكير ينبعث له بالواتساب). \"رقم أبو علي 07701234567\".",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}, "phone": {"type": "string"}},
            "required": ["customer_id", "phone"],
        },
    },
    {
        "name": "create_payment_link",
        "description": "يسوي رابط دفع بزين كاش لزبون (بالدينار). المبلغ افتراضيًا كل اللي عليه. لما يدفع منه، "
                       "التسديد ينسجل بالدفتر وحده (إلكتروني، ما يدخل الجرار). \"سويله رابط دفع\".",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"},
                           "amount": {"type": "integer", "description": "بس إذا صاحب المحل حدد مبلغ"}},
            "required": ["customer_id"],
        },
    },
    {
        "name": "draft_reminder",
        "description": "يكتب رسالة تذكير لزبون عليه دين، بيها رصيده ورابط دفع زين كاش. الرسالة ما تنبعث: "
                       "صاحب المحل يشوفها ويدوس \"أرسل بالواتساب\". \"ذكّر أبو علي\"، \"ابعث تذكير للمتأخرين\" "
                       "(واحد واحد). note = سطر يريده صاحب المحل بالرسالة (اختياري).",
        "input_schema": {
            "type": "object",
            "properties": {"customer_id": {"type": "integer"}, "note": {"type": "string"}},
            "required": ["customer_id"],
        },
    },
    # ----- "ذكرني": anything the owner asks to be reminded of -----
    {
        "name": "remind_me",
        "description": "يحفظ تذكير لصاحب المحل ويذكّره بوقته (بالبرنامج، وبالتلگرام إذا مربوط): \"ذكرني باچر الصبح أتصل بالمندوب\"، "
                       "\"ذكرني بعد ساعتين أطلب غاز\"، \"كل جمعة ذكرني أرتب الرفوف\". الكود يحسب التاريخ: إنت بس تحدد day "
                       "(today/tomorrow/after_tomorrow/يوم بالأسبوع/date) و time بصيغة 24 ساعة (9 الصبح = 09:00، 4 العصر = 16:00، "
                       "9 بالليل = 21:00)، أو in_minutes / in_hours. بس لما صاحب المحل يطلب تذكير.",
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "شنو يذكّره، بكلامه: اتصل بالمندوب"},
                "day": {"type": "string", "enum": ["today", "tomorrow", "after_tomorrow", "saturday", "sunday", "monday",
                                                   "tuesday", "wednesday", "thursday", "friday", "date"]},
                "date": {"type": "string", "description": "YYYY-MM-DD، بس إذا day=date"},
                "time": {"type": "string", "description": "HH:MM. إذا ما گال ساعة خليه فارغ"},
                "in_minutes": {"type": "integer"},
                "in_hours": {"type": "integer"},
                "repeat": {"type": "string", "enum": ["none", "daily", "weekly", "monthly"]},
            },
            "required": ["text"],
        },
    },
    {
        "name": "list_reminders",
        "description": "تذكيرات صاحب المحل الجاية (اللي طلبها بـ ذكرني) ويا أرقامها ووقتها.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_reminder",
        "description": "يلغي تذكير برقمه (من list_reminders). \"لا تذكرني بالمندوب\".",
        "input_schema": {"type": "object", "properties": {"reminder_id": {"type": "integer"}}, "required": ["reminder_id"]},
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


# add_recurring_expense only when the owner's message says it repeats (stage 19).
_MONTHLY = re.compile(r"كل\s*شهر|شهري|شهرية|بالشهر|ثابت|ثابتة|ذكرني|ذكّرني|تذكرني|"
                      r"كل\s*(?:اسبوع|أسبوع|إسبوع|سبت|احد|أحد|اثنين|ثلاثاء|اربعاء|أربعاء|خميس|جمعة|جمعه)|"
                      r"اسبوعي|أسبوعي|اسبوعية|أسبوعية|بالاسبوع|بالأسبوع")
# remind_me only when the owner asked to be reminded
_REMIND = re.compile(r"ذكرني|ذكّرني|ذكرنى|نبهني|نبّهني|لا\s*تنساني|خلي\s*ب?بالك|ذكرهم|فكرني|فكّرني|تذكير|remind", re.I)
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}

# add_item right away only when the owner's own message asks to add an item (stage 17).
_ADD_ITEM = re.compile(r"ضيف|اضف|أضف|إضافة|اضافة|سجل\s+مادة|مادة\s+جديدة|صنف\s+جديد|نزل\s+مادة")


def _error(code: str, message: str, **extra) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def _drawer_meaning(report: dict) -> str:
    """The day in one plain line, so the model does not do the arithmetic itself."""
    parts = []
    words = {"IQD": "دينار", "USD": "دولار"}
    for cur, sales in report.get("cash_sales", {}).items():
        line = f"البيع النقدي {sales:,} {words[cur]}، ومجموع البيع ويا الدين {report['total_sales'][cur]:,}"
        diff = report["difference"][cur]
        if report["cash_ins"][cur]:
            line += (f". المفروض بالجرار {report['expected'][cur]:,}"
                     + (f"، وأكو زيادة {diff:,} (بيع ما انكتب)" if diff > 0 else
                        f"، والجرار ناقص {-diff:,} {words[cur]}" if diff < 0 else "، ومطابق بالضبط"))
        elif diff < 0:
            line += f" (الجرار ناقص {-diff:,} {words[cur]} أو أكو شي ما انسجل)"
        parts.append(line)
    if not parts:
        return "ما گدرت أحسب البيع: لازم عدّة بالبداية وبالنهاية لنفس العملة."
    return "، و".join(parts)


def _day_words(at: str) -> str:
    from datetime import date
    days = (date.today() - date.fromisoformat(at[:10])).days
    return {0: "اليوم", 1: "البارحة", 2: "قبل البارحة"}.get(days, f"قبل {days} أيام")


def _qty_str(q) -> str:
    return str(int(q)) if float(q).is_integer() else f"{q:g}"


def _as_int(value):
    """Accept ints and integral floats; reject bools, strings, fractions."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def _when_words(due, now) -> str:
    """"باچر الخميس 9:00 الصبح": the time the way the owner says it (worked out by code)."""
    from datetime import timedelta

    from app.db import WEEKDAYS_AR
    days = (due.date() - now.date()).days
    day = {0: "اليوم", 1: "باچر", 2: "عقب باچر"}.get(days)
    name = WEEKDAYS_AR[due.weekday()]
    day = f"{day} {name}" if day else (f"{name} الجاي" if days < 7 else due.strftime("%Y-%m-%d"))
    h = due.hour
    part = "بالليل" if h < 5 or h >= 18 else "الصبح" if h < 12 else "الظهر" if h < 15 else "العصر"
    twelve = h % 12 or 12
    return f"{day} الساعة {twelve}:{due.minute:02d} {part}"


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
        # Entries made in this message: two equal lines in ONE message ("دفع 10 ودفع 10") are
        # not a resent message. The agent clears it each turn.
        self._turn_entries: set[int] = set()
        # How many entries the conversation had when this message began (the agent sets it):
        # what this same message recorded is never undone by it. None = no message (direct calls).
        self.turn_start: int | None = None
        # Where customers open payment links (stage 22); main sets it from the request.
        self.base_url = ""

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

        if not confirmed:
            dup = db.recent_duplicate(self.conn, customer["id"], tx_type, value, currency)
            if dup and dup["id"] not in self._turn_entries:
                return _error(
                    "possible_duplicate",
                    f"نفس القيد ({customer['name']}، {value:,}) انسجل قبل دقايق ({dup['created_at'][11:16]}). لا تسجل. "
                    "اسأل صاحب المحل: 'سجلتها قبل شوية، هاي دفعة ثانية لو نفسها؟' وإذا گال ثانية عيد بـ confirmed=true.",
                    customer_name=customer["name"], amount=value, currency=currency, earlier_at=dup["created_at"])
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
        self._turn_entries.add(tx_id)
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
        elif tx_type == "payment":  # cash: it shows in the drawer at once (stage 15)
            result["drawer"] = "دخلت للجرار" if kind == "customer" else "طلعت من الجرار"
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

    def tool_record_withdrawal(self, amount=None, currency="IQD", reason=None, note=None, confirmed=False,
                               from_drawer=True, quantity=None, unit_price=None, unit=None):
        reason = str(reason or "").strip()
        if not reason:
            return _error("reason_required", "اسأل صاحب المحل: سحبتهن على شنو؟ (مثل مولدة، إيجار، شخصي)")
        if quantity is not None or unit_price is not None:  # "30 أمبير بـ 12 ألف": the code multiplies (stage 18)
            q, p = quantity, _as_int(unit_price)
            if isinstance(q, bool) or not isinstance(q, (int, float)) or q <= 0 or p is None or p <= 0:
                return _error("invalid_amount", "للضرب لازم العدد (quantity) وسعر الوحدة (unit_price) أرقام موجبة")
            computed = round(q * p)
            if amount is not None and _as_int(amount) != computed:
                return _error("amount_mismatch", f"{_qty_str(q)} × {p:,} = {computed:,} مو {amount}. "
                                                 "لا تحسب بنفسك: خلي amount فارغ والكود يضرب.")
            amount = computed
            detail = f"{_qty_str(q)} {unit or ''} × {p:,}".replace("  ", " ")
            note = f"{note} · {detail}" if note else detail
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
                **({"computed_from": note.split(" · ")[-1]} if quantity is not None else {}),
                "today_total": db.withdrawals_summary(self.conn, "today")["totals"],
                "month_total": db.withdrawals_summary(self.conn, "month")["totals"]}

    def tool_list_withdrawals(self, period="month", reason=None):
        period = period if period in db.PERIODS else "month"
        summary = db.withdrawals_summary(self.conn, period, reason or None)
        return {"ok": True, "ledger": "withdrawal", **summary}

    # ----- items and sales -----

    def tool_find_item(self, query):
        matches = db.find_items(self.conn, str(query or ""))
        if not matches:
            self._new_name_turn.setdefault(("item", normalize(str(query or ""))), self.turn)
        return {"ok": True, "count": len(matches),
                "matches": [{k: m[k] for k in ("id", "name", "unit", "price", "cost", "currency")} for m in matches]}

    def tool_add_item(self, name, price, cost=None, unit=None, currency="IQD"):
        name = " ".join(str(name or "").split())
        if not name:
            return _error("invalid_name", "اسم المادة فارغ")
        p, c = _as_int(price), _as_int(cost) if cost is not None else None
        if p is None or p <= 0:
            return _error("invalid_amount", "سعر البيع لازم يكون رقم صحيح أكبر من صفر")
        if cost is not None and (c is None or c < 0):
            return _error("invalid_amount", "الكلفة لازم تكون رقم صحيح")
        currency = (currency or "IQD").upper()
        if currency not in db.CURRENCIES:
            return _error("invalid_currency", "العملة لازم تكون IQD أو USD")
        exact = [m for m in db.find_items(self.conn, name) if m["normalized_name"] == normalize(name)]
        if exact:
            return _error("item_exists", "اكو مادة بنفس الاسم", existing={"id": exact[0]["id"], "name": exact[0]["name"]})
        # Same rule as a new customer: only when the owner asked to add it, or answered "yes"
        # in a later message after being asked.
        first_seen = self._new_name_turn.setdefault(("item", normalize(name)), self.turn)
        if first_seen >= self.turn and not _ADD_ITEM.search(self.source_message or ""):
            return _error("ask_first", f"لا تضيف هسه. اسأل صاحب المحل: 'ما عندي مادة اسمها {name}، أضيفها بسعر {p:,}؟' "
                                       "وانتظر جوابه بالرسالة الجاية.")
        item_id = db.add_item(self.conn, name, p, c, unit, currency)
        return {"ok": True, "item_id": item_id, "name": name, "price": p, "cost": c, "unit": unit, "currency": currency}

    def tool_update_item(self, item_id, price=None, cost=None, unit=None):
        item = db.get_item(self.conn, _as_int(item_id)) if _as_int(item_id) is not None else None
        if not item:
            return _error("item_not_found", f"ما كو مادة بالرقم {item_id}. دوّر بـ find_item.")
        p = _as_int(price) if price is not None else None
        c = _as_int(cost) if cost is not None else None
        if (price is not None and (p is None or p <= 0)) or (cost is not None and (c is None or c < 0)):
            return _error("invalid_amount", "السعر لازم يكون رقم صحيح")
        if p is None and c is None and not unit:
            return _error("nothing_to_change", "شنو أغير؟ السعر لو الكلفة؟")
        db.update_item(self.conn, item["id"], price=p, cost=c, unit=unit)
        new = db.get_item(self.conn, item["id"])
        return {"ok": True, "item_id": new["id"], "name": new["name"], "price": new["price"], "cost": new["cost"],
                "unit": new["unit"], "before": {"price": item["price"], "cost": item["cost"]}}

    def tool_record_sale(self, lines, payment="cash", customer_id=None, confirmed=False):
        if payment not in ("cash", "debt", "electronic"):
            return _error("invalid_payment", "طريقة الدفع: cash أو debt أو electronic")
        if not isinstance(lines, list) or not lines:
            return _error("no_lines", "شنو انباع؟ ما كو ولا مادة")
        checked, currency = [], None
        for raw in lines:
            raw = raw if isinstance(raw, dict) else {}
            iid = _as_int(raw.get("item_id"))
            item = db.get_item(self.conn, iid) if iid is not None else None
            if not item or not item["active"]:
                return _error("item_not_found", f"ما كو مادة بالرقم {raw.get('item_id')}. دوّر بـ find_item، "
                                                "وإذا ما موجودة اسأل صاحب المحل يضيفها وبيش.")
            qty = raw.get("qty", 1)
            if isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty <= 0 or qty > 100000:
                return _error("invalid_qty", f"العدد غلط لـ {item['name']}")
            price = item["price"] if raw.get("unit_price") is None else _as_int(raw.get("unit_price"))
            if price is None or price <= 0:
                return _error("invalid_amount", f"السعر غلط لـ {item['name']}")
            if currency and item["currency"] != currency:
                return _error("mixed_currency", "مواد بالدينار ومواد بالدولار: سجلهن ببيعتين")
            currency = item["currency"]
            checked.append({"item_id": item["id"], "qty": float(qty), "unit_price": price, "unit_cost": item["cost"],
                            "name": item["name"]})
        total = sum(round(l["qty"] * l["unit_price"]) for l in checked)
        customer = None
        if payment == "debt":
            customer, err = self._customer_or_error(customer_id)
            if err:
                return _error("customer_required", "بيعة الدين تحتاج الزبون: دوّره بـ find_customer. " + err["message"])
        limit = self.large_amount_iqd if currency == "IQD" else USD_LARGE_AMOUNT
        if total > limit and not confirmed:
            return _error("confirmation_required",
                          "المبلغ كبير، لازم تأكد ويا المستخدم وبعدين تعيد الاستدعاء بـ confirmed=true",
                          total=total, currency=currency, limit=limit)
        sale_id = db.add_sale(self.conn, payment, checked, currency, customer["id"] if customer else None,
                              self.source_message, self.source, self.session_id)
        self.session_tx_ids.append(("sale", sale_id))
        sale = db.get_sale(self.conn, sale_id)
        result = {"ok": True, "ledger": "customer" if payment == "debt" else "drawer", "sale_id": sale_id,
                  "payment": payment, "what": sale["what"], "total": sale["total"], "currency": currency,
                  "lines": [{"name": l["name"], "qty": l["qty"], "unit_price": l["unit_price"],
                             "line_total": l["line_total"]} for l in sale["lines"]]}
        if customer:
            result.update(customer_name=customer["name"], balance=db.get_balance(self.conn, customer["id"]))
        if sale["cost_total"] is not None:
            result["profit"] = sale["total"] - sale["cost_total"]
        return result

    def tool_item_sales(self, period="month", item_id=None):
        period = period if period in db.PERIODS else "month"
        iid = _as_int(item_id) if item_id is not None else None
        if item_id is not None and (iid is None or not db.get_item(self.conn, iid)):
            return _error("item_not_found", f"ما كو مادة بالرقم {item_id}. دوّر بـ find_item.")
        return {"ok": True, **db.sales_summary(self.conn, period, iid)}

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

    def tool_record_cash_in(self, amount, currency="IQD", label=None, confirmed=False):
        label = " ".join(str(label or "").split())
        if not label:
            return _error("label_required", "اسأل صاحب المحل: الوارد من منو أو على شنو؟")
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
                          label=label, amount=value, currency=currency, limit=limit)
        ci_id = db.add_cash_in(self.conn, value, currency, label, source_message=self.source_message,
                               source=self.source, session_id=self.session_id)
        self.session_tx_ids.append(("cash_in", ci_id))
        since = db.drawer_report(self.conn)
        return {"ok": True, "ledger": "drawer", "cash_in_id": ci_id, "amount": value, "currency": currency,
                "label": label, "cash_ins_since_last_count": since["cash_ins"]}

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
        closes = db.drawer_closes(self.conn, limit)
        for c in closes:  # the day in words, so the model never works out dates (found by the mall eval)
            c["day"] = _day_words(c["counted"]["at"])
        return {"ok": True, "ledger": "drawer", "closes": closes}

    # ----- reports -----

    def tool_profit_report(self, period="month"):
        period = period if period in reports.PERIODS else "month"
        return {"ok": True, **reports.profit(self.conn, period)}

    def tool_week_summary(self):
        return {"ok": True, **reports.week_summary(self.conn)}

    def tool_cash_forecast(self, days=30):
        d = _as_int(days) or 30
        if not 1 <= d <= 365:
            return _error("invalid_days", "عدد الأيام من 1 لـ 365")
        result = reports.cash_forecast(self.conn, d)
        if not result.pop("ok"):
            return _error("not_enough_data", result["reason"])
        return {"ok": True, **result}

    def tool_customer_statement(self, customer_id, period="month"):
        customer, err = self._customer_or_error(customer_id)
        if err:
            return err
        period = period if period in reports.PERIODS else "month"
        st = reports.customer_statement(self.conn, customer["id"], period)
        st["lines"] = st["lines"][-40:]  # the model needs the recent ones; the page shows them all
        st["page"] = f"/statement/{customer['id']}?period={period}"
        return {"ok": True, **st}

    # ----- Zain Cash links and reminders (stage 22) -----

    def tool_set_customer_phone(self, customer_id, phone):
        customer, err = self._customer_or_error(customer_id)
        if err:
            return err
        p = payments.normalize_phone(str(phone or ""))
        if not p:
            return _error("invalid_phone", "الرقم لازم يكون موبايل عراقي، مثل 07701234567")
        db.set_phone(self.conn, customer["id"], p)
        return {"ok": True, "customer_id": customer["id"], "customer_name": customer["name"], "phone": p}

    def _link(self, customer, amount=None):
        """A pending link for this customer, for all they owe unless an amount is given. An older
        pending link is cancelled so the customer cannot pay the same debt twice."""
        owed = db.get_balance(self.conn, customer["id"])["IQD"]
        value = _as_int(amount) if amount is not None else owed
        if owed <= 0 and amount is None:
            return None, _error("nothing_owed", f"{customer['name']} ما عليه شي بالدينار هسه", balance=owed)
        if value is None or value < payments.MIN_AMOUNT:
            return None, _error("invalid_amount", f"المبلغ لازم يكون {payments.MIN_AMOUNT} دينار أو أكثر")
        self.conn.execute("UPDATE payment_links SET status = 'cancelled' WHERE customer_id = ? AND status = 'pending'",
                          (customer["id"],))
        link = db.add_payment_link(self.conn, customer["id"], value, "IQD", payments.provider_name())
        url = payments.pay_url(link["token"], payments.base_url(self.base_url))
        return {"link_id": link["id"], "url": url, "amount": value, "balance": owed,
                "provider": "زين كاش" if link["provider"] == "zaincash" else "زين كاش (وضع تجربة: ما تتحرك فلوس حقيقية)"}, None

    def tool_create_payment_link(self, customer_id, amount=None):
        customer, err = self._customer_or_error(customer_id)
        if err:
            return err
        link, err = self._link(customer, amount)
        if err:
            return err
        return {"ok": True, "customer_id": customer["id"], "customer_name": customer["name"], **link,
                "meaning": "لما الزبون يدفع من الرابط، التسديد ينسجل وحده ويطلعلك تنبيه"}

    def tool_draft_reminder(self, customer_id, note=None):
        customer, err = self._customer_or_error(customer_id)
        if err:
            return err
        link, err = self._link(customer)
        if err:
            return err
        shop = db.get_setting(self.conn, "shop_name")
        text = payments.reminder_text(customer["name"], link["amount"], link["url"], shop, note)
        phone = db.get_customer(self.conn, customer["id"]).get("phone")
        last = db.last_reminder(self.conn, customer["id"])
        out = {"ok": True, "customer_id": customer["id"], "customer_name": customer["name"], "text": text,
               "phone": phone, "whatsapp": payments.whatsapp_url(phone, text), **link,
               "last_reminded": last["created_at"] if last else None,
               "meaning": "الرسالة جاهزة وما انبعثت. صاحب المحل يدوس أرسل بالواتساب تحت الرد"}
        if not phone:
            out["no_phone"] = "ما عندي رقمه: الواتساب راح يسألك لمن تبعثها، أو گلي رقمه وأحفظه"
        return out

    # ----- "ذكرني" -----

    def tool_remind_me(self, text, day=None, date=None, time=None, in_minutes=None, in_hours=None, repeat="none"):
        from datetime import datetime, timedelta
        text = " ".join(str(text or "").split())
        if not text:
            return _error("text_required", "بشنو أذكّرك؟")
        if not _REMIND.search(self.source_message or ""):
            return _error("ask_first", "صاحب المحل ما طلب تذكير. اسأله: 'أذكّرك بيها؟ إيمتى؟'")
        repeat = repeat if repeat in ("none", "daily", "weekly", "monthly") else "none"
        now = datetime.now().replace(second=0, microsecond=0)
        mins, hrs = _as_int(in_minutes), _as_int(in_hours)
        if mins or hrs:
            due = now + timedelta(minutes=mins or 0, hours=hrs or 0)
        else:
            hh, mm = 9, 0  # no hour said: 9 in the morning
            if time:
                m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*", str(time))
                if not m or int(m.group(1)) > 23 or int(m.group(2) or 0) > 59:
                    return _error("invalid_time", "الساعة لازم تكون مثل 09:00 أو 21:30")
                hh, mm = int(m.group(1)), int(m.group(2) or 0)
            elif day in (None, "today"):
                return _error("time_required", "اليوم بأي ساعة أذكّرك؟")
            if day in (None, "today"):
                d = now.date()
            elif day == "tomorrow":
                d = now.date() + timedelta(days=1)
            elif day == "after_tomorrow":
                d = now.date() + timedelta(days=2)
            elif day in _WEEKDAYS:
                d = now.date() + timedelta(days=(_WEEKDAYS[day] - now.weekday()) % 7)
                if datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm) <= now:
                    d += timedelta(days=7)  # "الخميس" said on Thursday evening: next Thursday
            elif day == "date":
                try:
                    d = datetime.strptime(str(date), "%Y-%m-%d").date()
                except ValueError:
                    return _error("invalid_date", "التاريخ لازم يكون مثل 2026-10-22")
            else:
                return _error("invalid_day", "إيمتى أذكّرك؟ (اليوم، باچر، الخميس، بعد ساعة…)")
            due = datetime.combine(d, datetime.min.time()).replace(hour=hh, minute=mm)
        if due <= now:
            return _error("time_passed", f"هالوقت فات ({due:%Y-%m-%d %H:%M}). إيمتى أذكّرك؟")
        rid = db.add_owner_reminder(self.conn, text, due, repeat, self.source_message)
        return {"ok": True, "reminder_id": rid, "text": text, "due_at": due.strftime("%Y-%m-%d %H:%M"),
                "when_words": _when_words(due, now), "repeat": repeat,
                "meaning": "انحفظ. بوقته يطلعله تنبيه بالبرنامج (وبالتلگرام إذا مربوط). گوله الوقت بـ when_words"}

    def tool_list_reminders(self):
        from datetime import datetime
        now = datetime.now()
        rows = [{"id": r["id"], "text": r["text"], "due_at": r["due_at"][:16], "repeat": r["repeat"],
                 "when_words": _when_words(datetime.fromisoformat(r["due_at"]), now)}
                for r in db.list_owner_reminders(self.conn)]
        return {"ok": True, "count": len(rows), "reminders": rows}

    def tool_cancel_reminder(self, reminder_id):
        r = db.get_owner_reminder(self.conn, _as_int(reminder_id)) if _as_int(reminder_id) is not None else None
        if not r or r["done"]:
            return _error("reminder_not_found", "ما لگيت هالتذكير. شوف list_reminders.")
        db.cancel_owner_reminder(self.conn, r["id"])
        return {"ok": True, "cancelled": {"id": r["id"], "text": r["text"]}}

    # ----- reminders and alerts -----

    def tool_get_alerts(self):
        a = db.alerts(self.conn)
        return {"ok": True, **a, "overdue": a["overdue"][:10], "overdue_count": len(a["overdue"])}

    def tool_add_recurring_expense(self, reason, amount, currency="IQD", day_of_month=1, from_drawer=True,
                                   every="month", weekday=None):
        reason = " ".join(str(reason or "").split())
        value, day = _as_int(amount), _as_int(day_of_month if day_of_month is not None else 1)
        every = "week" if str(every or "").lower() == "week" else "month"
        dow = _WEEKDAYS.get(str(weekday or "").lower())
        if not reason:
            return _error("reason_required", "على شنو هالصرفية؟")
        if value is None or value <= 0:
            return _error("invalid_amount", "المبلغ لازم يكون رقم صحيح أكبر من صفر")
        if every == "week" and dow is None:
            return _error("weekday_required", "أي يوم بالأسبوع؟ (مثلًا الخميس)")
        if every == "month" and (day is None or not 1 <= day <= 31):
            return _error("invalid_day", "يوم الشهر لازم يكون من 1 لـ 31")
        currency = (currency or "IQD").upper()
        if currency not in db.CURRENCIES:
            return _error("invalid_currency", "العملة لازم تكون IQD أو USD")
        if not _MONTHLY.search(self.source_message or ""):
            return _error("ask_first", "صاحب المحل ما طلب صرفية ثابتة. اسأله: 'أخليها صرفية ثابتة كل شهر وأذكّرك بيها؟'")
        rec_id = db.add_recurring(self.conn, reason, value, currency, day, from_drawer is not False,
                                  period=every, day_of_week=dow)
        rec = db.get_recurring(self.conn, rec_id)
        return {"ok": True, "recurring_id": rec_id, "reason": reason, "amount": value, "currency": currency,
                "every": every, "when": rec["label"],
                "due_now": any(r["id"] == rec_id for r in db.recurring_due(self.conn))}

    def tool_list_recurring_expenses(self):
        rows = [{**{k: r[k] for k in ("id", "reason", "amount", "currency", "period")}, "when": r["label"],
                 "done_now": db.recurring_done_now(r)} for r in db.list_recurring(self.conn)]
        return {"ok": True, "count": len(rows), "recurring": rows}

    def tool_remove_recurring_expense(self, recurring_id):
        rec = db.get_recurring(self.conn, _as_int(recurring_id)) if _as_int(recurring_id) is not None else None
        if not rec or not rec["active"]:
            return _error("recurring_not_found", "ما لگيت هالصرفية. شوف list_recurring_expenses.")
        db.remove_recurring(self.conn, rec["id"])
        return {"ok": True, "removed": {"id": rec["id"], "reason": rec["reason"], "amount": rec["amount"]}}

    def tool_record_recurring_expense(self, recurring_id, skip=False):
        from datetime import datetime
        rec = db.get_recurring(self.conn, _as_int(recurring_id)) if _as_int(recurring_id) is not None else None
        if not rec or not rec["active"]:
            return _error("recurring_not_found", "ما لگيت هالصرفية. شوف list_recurring_expenses.")
        if db.recurring_done_now(rec, datetime.now()):
            return _error("already_done", f"{rec['reason']} انسجلت (أو انتركت) {rec['period_words']} من قبل.")
        if skip:
            db.mark_recurring_done(self.conn, rec["id"])
            return {"ok": True, "skipped": True, "reason": rec["reason"], "period_words": rec["period_words"]}
        result = self.tool_record_withdrawal(rec["amount"], rec["currency"], rec["reason"],
                                             note="صرفية ثابتة", confirmed=True, from_drawer=bool(rec["from_drawer"]))
        if result.get("ok"):
            db.mark_recurring_done(self.conn, rec["id"])
            result["recurring_id"] = rec["id"]
        return result

    def tool_set_reminders(self, drawer_hour=None, overdue_days=None):
        changed = {}
        if drawer_hour is not None:
            h = _as_int(drawer_hour)
            if h is None or not 0 <= h <= 23:
                return _error("invalid_hour", "الساعة لازم تكون من 0 لـ 23 (مثلًا 21 = 9 بالليل)")
            db.set_setting(self.conn, "drawer_hour", h)
            changed["drawer_hour"] = h
        if overdue_days is not None:
            d = _as_int(overdue_days)
            if d is None or not 1 <= d <= 365:
                return _error("invalid_days", "عدد الأيام لازم يكون من 1 لـ 365")
            db.set_setting(self.conn, "overdue_days", d)
            changed["overdue_days"] = d
        if not changed:
            return _error("nothing_to_change", "شنو أغير؟ ساعة تذكير الجرار لو أيام الدين المتأخر؟")
        return {"ok": True, **changed}

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

    def tool_undo_last(self, name=None):
        if name is not None and str(name).strip():
            return self._undo_named(str(name).strip())
        while self.session_tx_ids:
            if self.turn_start is not None and len(self.session_tx_ids) > self.turn_start:
                return self._same_message_error()
            out = self._undo_entry(self.session_tx_ids.pop())
            if out:
                return out
        return _error("nothing_to_undo", "ما كو قيد بهذه الجلسة حتى ألغيه")

    def _same_message_error(self):
        return _error("undo_same_message",
                      "آخر قيد انسجل بنفس هالرسالة، فما ينلغى بيها. لا تسجل وتلغي بنفس الرسالة: گول لصاحب المحل "
                      "شنو سجلت بالضبط، واسأله شنو يريد يلغي.")

    def _entry_party(self, entry):
        """The customer or supplier an entry belongs to (None for the drawer, expenses and cash sales)."""
        if isinstance(entry, tuple):
            if entry[0] != "sale":
                return None
            sale = db.get_sale(self.conn, entry[1])
            return db.get_customer(self.conn, sale["customer_id"]) if sale and sale["customer_id"] else None
        tx = db.get_transaction(self.conn, entry)
        return db.get_customer(self.conn, tx["customer_id"]) if tx else None

    def _undo_named(self, name):
        """"حيدر غلط، شطبها": the last entry of THAT person in this conversation, not just the last entry."""
        last = len(self.session_tx_ids) if self.turn_start is None else min(self.turn_start, len(self.session_tx_ids))
        parties = {}
        for i in range(last - 1, -1, -1):
            party = self._entry_party(self.session_tx_ids[i])
            if party and party["id"] not in parties:
                parties[party["id"]] = (party, i)
        matches = find_matches(name, [p for p, _ in parties.values()])
        if not matches:
            return _error("nothing_to_undo", f"ما كو قيد لـ {name} بهالمحادثة حتى ألغيه. لا تلغي غيره: گول هذا لصاحب المحل.",
                          name=name)
        exact = [m for m in matches if m["normalized_name"] == normalize(name)]
        if len(exact) != 1 and len(matches) > 1:
            return _error("ambiguous_name", "أكثر من شخص بهالاسم سجلتلهم بهالمحادثة. اسأل صاحب المحل يا واحد.",
                          candidates=[m["name"] for m in matches])
        party = (exact or matches)[0]
        _, i = parties[party["id"]]
        entry = self.session_tx_ids.pop(i)
        return self._undo_entry(entry) or _error("nothing_to_undo", f"قيد {party['name']} ملغي من قبل.")

    def _undo_entry(self, entry) -> dict | None:
        """Undo one recorded entry; None when it was already undone."""
        if isinstance(entry, tuple) and entry[0] == "sale":
            sale = db.get_sale(self.conn, entry[1])
            if sale and not sale["undone"]:
                db.mark_sale_undone(self.conn, sale["id"])
                undone = {"ledger": "customer" if sale["payment"] == "debt" else "drawer", "type": "sale",
                          "sale_id": sale["id"], "what": sale["what"], "payment": sale["payment"],
                          "amount": sale["total"], "currency": sale["currency"]}
                out = {"ok": True, "undone": undone}
                if sale["customer_id"]:
                    undone["customer_name"] = db.get_customer(self.conn, sale["customer_id"])["name"]
                    out["balance"] = db.get_balance(self.conn, sale["customer_id"])
                return out
            return None
        if isinstance(entry, tuple) and entry[0] == "cash_in":
            ci = db.get_cash_in(self.conn, entry[1])
            if ci and not ci["undone"]:
                db.mark_cash_in_undone(self.conn, ci["id"])
                return {"ok": True, "undone": {"ledger": "drawer", "type": "cash_in", "cash_in_id": ci["id"],
                                               "label": ci["label"], "amount": ci["amount"],
                                               "currency": ci["currency"]}}
            return None
        if isinstance(entry, tuple) and entry[0] == "drawer":
            count = db.get_drawer_count(self.conn, entry[1])
            if count and not count["undone"]:
                db.mark_drawer_count_undone(self.conn, count["id"])
                return {"ok": True, "undone": {"ledger": "drawer", "type": "drawer_" + count["kind"],
                                               "count_id": count["id"], "iqd": count["iqd"],
                                               "usd": count["usd"]}}
            return None
        if isinstance(entry, tuple):  # ("withdrawal", id)
            wd = db.get_withdrawal(self.conn, entry[1])
            if wd and not wd["undone"]:
                db.mark_withdrawal_undone(self.conn, wd["id"])
                return {"ok": True, "undone": {"ledger": "withdrawal", "type": "withdrawal",
                                               "withdrawal_id": wd["id"], "reason": wd["reason"],
                                               "amount": wd["amount"], "currency": wd["currency"]},
                        "month_total": db.withdrawals_summary(self.conn, "month")["totals"]}
            return None
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
        return None

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
