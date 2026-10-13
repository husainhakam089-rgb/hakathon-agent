"""Ask the agent the mall questions with the real model (LLM_PROVIDER etc. from .env).

    python eval/mall/run_questions.py                # the 80 working questions, skipping ones already done
    python eval/mall/run_questions.py --limit 15     # only the next 15 (free quota: a few a day)
    python eval/mall/run_questions.py --ids 1 7 30   # these ones again
    python eval/mall/run_questions.py --holdout      # the 20 held-out questions: run them once, at the end
    python eval/mall/run_questions.py --fresh        # forget earlier results and start again

Each question gets its own fresh copy of the mall, so nothing one question does reaches the
next. Results go to eval/mall/question_results.json after every question (a quota error midway
loses nothing). A question that failed because of the provider (quota, overload) is marked
"error", not failed, and is asked again on the next run.
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import questions  # noqa: E402
from mall import mall  # noqa: E402

from app.agent import WRITE_TOOLS, Agent  # noqa: E402
from app.llm import make_llm  # noqa: E402

RESULTS = HERE / "question_results.json"
PROVIDER_TROUBLE = ("خلصت حصة", "حصة Gemini", "زحمة", "تأخر بالرد", "ما گدرت أوصل", "خدمة Gemini")


def _has_number(reply: str, n: int) -> bool:
    text = reply.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩٬", "0123456789,"))
    plain = text.replace(",", "")
    return f"{n:,}" in text or re.search(rf"(?<!\d){n}(?!\d)", plain) is not None


def grade(q: dict, result: dict) -> tuple[str, str]:
    reply = result["reply"]
    wrote = [c["tool"] for c in result["tool_calls"] if c["tool"] in WRITE_TOOLS and c["result"].get("ok")]
    if any(t in reply for t in PROVIDER_TROUBLE) and not wrote:
        return "error", "مشكلة من مزود النموذج، تنعاد"
    if wrote:
        return "fail", f"سجل شي وهو سؤال: {wrote}"
    if any(c["tool"] == "number_check" for c in result["tool_calls"]):
        return "fail", "رقم بالرد ما طلع من الحسابات"
    if q["ask"]:
        return ("pass", "سأل") if "؟" in reply else ("fail", "جاوب بدل ما يسأل")
    # a negative balance is said as a positive figure ("مطلوب لك 30,000"): the size is what counts
    missing = [n for n in q["numbers"] if not _has_number(reply, abs(n))]
    return ("fail", f"الرقم الصحيح {', '.join(f'{n:,}' for n in missing)} مو بالرد") if missing else ("pass", "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", nargs="*", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--holdout", action="store_true")
    ap.add_argument("--fresh", action="store_true")
    args = ap.parse_args()

    _, paper, _, _ = mall()
    qs = questions.build(paper)
    done = {} if args.fresh or not RESULTS.exists() else {r["id"]: r for r in json.loads(RESULTS.read_text("utf-8"))["results"]}
    if args.ids:
        todo = [q for q in qs if q["id"] in args.ids]
    else:
        todo = [q for q in qs if q["holdout"] == args.holdout and done.get(q["id"], {}).get("status") in (None, "error")]
    if args.limit:
        todo = todo[:args.limit]
    llm = make_llm()
    for q in todo:
        _, _, conn, _ = mall()  # a fresh mall for each question
        agent = Agent(conn, llm=llm, log_path=None)
        started = time.perf_counter()
        result = agent.chat(f"q{q['id']}", q["q"])
        status, why = grade(q, result)
        done[q["id"]] = {"id": q["id"], "category": q["category"], "q": q["q"], "holdout": q["holdout"],
                         "expected": q["numbers"], "ask": q["ask"], "status": status, "why": why,
                         "reply": result["reply"], "tools": [c["tool"] for c in result["tool_calls"]],
                         "seconds": round(time.perf_counter() - started, 1), "model": getattr(llm, "model", "")}
        mark = {"pass": "✅", "fail": "❌", "error": "⏸"}[status]
        print(f"{q['id']:>3} {mark} {q['q']}  →  {result['reply'][:120]}" + (f"   ({why})" if why else ""))
        RESULTS.write_text(json.dumps({"results": sorted(done.values(), key=lambda r: r["id"])}, ensure_ascii=False,
                                      indent=2), encoding="utf-8")
        conn.close()
    graded = [r for r in done.values() if r["status"] != "error"]
    passed = sum(r["status"] == "pass" for r in graded)
    print(f"\nلحد هسه: {passed}/{len(graded)} صح، و{sum(r['status'] == 'error' for r in done.values())} تنعاد، "
          f"و{len(qs) - len(done)} بعد ما انسألت")
    return 0


if __name__ == "__main__":
    sys.exit(main())
