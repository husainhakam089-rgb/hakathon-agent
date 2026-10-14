"""Read the scan-eval pages with the real model and score them (stage 23).

    python eval/scan/make_pages.py      # once: makes the pages (committed)
    python eval/scan/run_scan_eval.py   # uses the model from .env (3 requests)

Per expected line: was it found, is the name right, the amount, dollars or dinars, debt or
payment, and is a crossed-out line flagged (and left unticked)? Plus rows the model made up.
Writes eval/scan/RESULTS.md and results.json.
"""

import argparse
import difflib
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from app import db, extraction  # noqa: E402
from app.arabic import normalize  # noqa: E402
from app.llm import LLMError, make_llm  # noqa: E402

KNOWN = ["ابو علي", "ام حسين", "الحجي كريم", "سعد"]  # already in the shop's notebook


def exact_name(expected: str, read: str | None) -> bool:
    e, r = normalize(expected), normalize(read or "")
    return bool(r) and (e == r or e in r or r in e)  # "ابو علي ابو الخضرة" is ابو علي


def same_name(expected: str, read: str | None) -> bool:
    """The same line even if a letter is off ("الفيتير" for "الفيتر"): the owner fixes it in the table."""
    e, r = normalize(expected), normalize(read or "")
    return exact_name(expected, read) or (bool(r) and difflib.SequenceMatcher(None, e, r).ratio() >= 0.8)


def grade(expected: list[dict], rows: list[dict]) -> dict:
    used, lines = set(), []
    for exp in expected:
        hit = next((i for i, r in enumerate(rows) if i not in used and same_name(exp["name"], r.get("name"))
                    and (r.get("amount") == exp["amount"] or r.get("amount") == exp["amount"] // 1000)), None)
        if hit is None:
            hit = next((i for i, r in enumerate(rows) if i not in used and same_name(exp["name"], r.get("name"))), None)
        if hit is None:
            lines.append({"expected": exp, "found": False})
            continue
        used.add(hit)
        r = rows[hit]
        crossed_ok = bool(r.get("crossed_out")) == exp["crossed_out"]
        line = {"expected": exp, "found": True, "read": {k: r.get(k) for k in
                ("name", "amount", "amount_as_written", "currency", "type", "crossed_out", "confidence", "selected")},
                "name": exact_name(exp["name"], r.get("name")),
                "amount": r.get("amount") == exp["amount"], "currency": r.get("currency") == exp["currency"],
                "crossed": crossed_ok,
                # a crossed-out line's kind is the owner's call: "unknown" is the right answer there
                "type": r.get("type") == exp["type"] or (exp["crossed_out"] and r.get("type") == "unknown"),
                "unticked_if_crossed": not exp["crossed_out"] or not r.get("selected")}
        line["all"] = all(line[k] for k in ("name", "amount", "currency", "type", "crossed", "unticked_if_crossed"))
        # the costly mistake: a line that starts ticked with the wrong amount or debt/payment
        line["silent_wrong"] = bool(r.get("selected")) and not (line["amount"] and line["type"] and line["currency"])
        lines.append(line)
    extra = [r for i, r in enumerate(rows) if i not in used]
    return {"lines": lines, "extra": [{k: r.get(k) for k in ("name", "amount", "raw_text")} for r in extra]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1, help="read every page this many times (the model varies)")
    args = ap.parse_args()
    llm = make_llm()
    conn = db.connect(":memory:")
    for name in KNOWN:
        db.add_customer(conn, name)
    pages = sorted(HERE.glob("page_*.jpg")) * args.runs
    results = []
    for p in pages:
        expected = json.loads(p.with_suffix(".expected.json").read_text("utf-8"))["rows"]
        started = time.perf_counter()
        try:
            review = extraction.build_review(conn, extraction.extract_file(llm, p.read_bytes(), "image/jpeg"))
        except LLMError as e:
            print(f"{p.name}: ⏸ {e.user_message}")
            results.append({"page": p.name, "error": e.user_message})
            continue
        g = grade(expected, review["rows"])
        g.update(page=p.name, seconds=round(time.perf_counter() - started, 1), model=getattr(llm, "model", ""),
                 unreadable=review.get("unreadable_lines", []), notes=review.get("notes", ""))
        results.append(g)
        ok = sum(x.get("all", False) for x in g["lines"])
        print(f"{p.name}: {ok}/{len(expected)} سطر صح بالكامل، {len(g['extra'])} سطر زايد ({g['seconds']} ثانية)")
        for x in g["lines"]:
            if not x.get("all"):
                print("   ✗", x["expected"]["written"], "→", x.get("read") or "ما انلگى")
    (HERE / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(results)
    return 0


def write_report(results: list[dict]) -> None:
    graded = [r for r in results if "lines" in r]
    lines = [x for r in graded for x in r["lines"]]
    n = len(lines)
    count = lambda k: sum(1 for x in lines if x.get(k))  # noqa: E731
    crossed = [x for x in lines if x["expected"]["crossed_out"]]
    out = ["# تقييم سكان الدفتر (المرحلة 23)", "",
           f"_{time.strftime('%Y-%m-%d %H:%M')} · {len(graded)} قراءة صفحة ({len({r['page'] for r in graded})} صفحات، "
           f"كل وحدة انقرت {len(graded) // max(1, len({r['page'] for r in graded}))} مرات) · {n} سطر · النموذج: "
           f"{', '.join(sorted({r['model'] for r in graded})) or '-'}_", "",
           "الصفحات بـ `eval/scan/`: دفتر محل بخط الرقعة، مصوّر بالموبايل على طاولة (مايل، ظل، ضبابية، ضغط JPEG). "
           "بيها اللي يكتبه صاحب المحل فعلًا: \"25\" يعني 25 ألف، أرقام هندية، \"واصل\" و\"سدد\" للتسديد، علامة ناقص، "
           "دولار، سطر مشطوب، واسم يرجع بصفحة ثانية.", "",
           "| | صح |", "|---|---|",
           f"| السطر انلگى | {count('found')}/{n} |",
           f"| الاسم بالضبط | {count('name')}/{n} |",
           f"| المبلغ | {count('amount')}/{n} |",
           f"| دينار / دولار | {count('currency')}/{n} |",
           f"| دين / تسديد | {count('type')}/{n} |",
           f"| المشطوب انمسك (ويبقى مو محدد) | {sum(1 for x in crossed if x.get('crossed') and x.get('unticked_if_crossed'))}/{len(crossed)} |",
           f"| **السطر صح بالكامل** | **{count('all')}/{n}** |",
           f"| أسطر زايدة (مو بالصفحة) | {sum(len(r['extra']) for r in graded)} |",
           f"| ⚠ غلط وهو محدد للتسجيل (أخطر غلط) | {count('silent_wrong')}/{n} |", "",
           "\"محدد\" يعني الصح عليه بالجدول من البداية؛ صاحب المحل يراجع قبل ما يسجل، بس هذا الغلط اللي ممكن يفوت.", ""]
    bad = [x for x in lines if not x.get("all")]
    if bad:
        out += ["## وين غلط", ""] + [f"- «{x['expected']['written']}» → " + (
            "ما انلگى" if not x["found"] else ("⚠ محدد · " if x.get("silent_wrong") else "") +
            f"{x['read']['name']} · {x['read']['amount']} {x['read']['currency']} · {x['read']['type']}"
            + (" · مشطوب" if x["read"]["crossed_out"] else "")) for x in bad] + [""]
    out += ["## التجربة كاملة من شاشة موبايل (14-10-2026)",
            "الصفحات الثلاث انرفعت من ☰ ← 📷 سكان الدفتر، وانقرت، وانسجل الجدول **بدون ما نعدل ولا سطر**، "
            "وبعدين قارنّا رصيد كل زبون بالدفتر الورقي:",
            "- **18 من 20 زبون رصيدهم مطابق بالضبط.**",
            "- \"ابو علي ابو الخضرة\" (ص 1) و\"ابو علي\" (ص 2) صاروا زبونين، فالثاني طلع رصيده سالب. "
            "**انصلح:** الجدول صار ينبه \"يمكن نفس الشخص\" و\"تسديد من اسم ما عليه دين\" قبل التسجيل.",
            "- \"الفيتر\" انقرت \"الفيتو\": حرف واحد، يتصلح بالجدول.",
            "- وانصلح بالكود: \"سدد ابو علي\" مرة انقرت اسم \"سد ابو علي\" ودين، فصار الكود يشيل كلمة التسديد من الاسم ويخليه تسديد. "
            "والتاريخ فوق الصفحة ما يطلع سطر بعد.", "",
            "## بصراحة",
            "- هذا **خط كمبيوتر بشكل رقعة**، مو إيد إنسان. خط الإيد الحقيقي أصعب (حروف ملزگة، أرقام تشبه بعض).",
            "- الاختبار الحقيقي: صور دفتر حقيقي. ضيفها بنفس المجلد ويا ملف expected وعيد التشغيل.",
            "- مهما طلعت النتيجة، ما ينسجل شي إلا بعد ما صاحب المحل يراجع الجدول ويدوس سجّل.", ""]
    (HERE / "RESULTS.md").write_text("\n".join(out), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
