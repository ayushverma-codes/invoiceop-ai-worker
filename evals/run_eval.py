"""Evaluation harness: runs the agent on fixed scenarios and records ACTUAL results.

  python -m evals.run_eval --mode double   # deterministic LLM double: checks the loop/guards/verifier ONLY
  python -m evals.run_eval --mode live     # real model (needs GROQ_API_KEY): the numbers that matter

Each scenario gets its own mock server + temp DB + real headless Chromium. Nothing is hand-written:
metrics are computed from the result dicts the agent returns, and written to evals/results_<mode>.json.
"""
import argparse
import json
import pathlib
import sys
import time

from agent.graph import run_agent
from tests.helpers import ap_rows, running_app
from tools.browser import Browser

OUT = pathlib.Path(__file__).parent

# name, task, fault, approver ("approve" | "deny" | None), expected final status, tags
SCENARIOS = [
    ("latest_acme", "Process the latest invoice from Acme Corp.", "", None, "completed", set()),
    ("latest_globex", "Process the latest invoice from Globex Inc.", "", None, "completed", set()),
    ("submission_failure_recovery", "Process the latest invoice from Acme Corp.", "invoice_date_once", None,
     "completed", {"recovery"}),
    ("high_value_approved", "Process the latest invoice from Initech LLC.", "", "approve", "completed",
     {"approval"}),
    ("high_value_denied", "Process the latest invoice from Initech LLC.", "", "deny", "failed", {"approval"}),
    ("high_value_no_approver", "Process the latest invoice from Initech LLC.", "", None, "waiting_for_approval",
     {"approval"}),
    ("unknown_vendor", "Process the latest invoice from Umbrella Corp.", "", None, "failed", set()),
    ("ambiguous_task", "Process the latest invoice.", "", None, "needs_clarification", set()),
    ("ap_never_accepts", "Process the latest invoice from Acme Corp.", "invoice_date_always", None, "failed",
     {"retries"}),
    ("ui_lies_wrong_amount", "Process the latest invoice from Acme Corp.", "store_wrong_amount", None, "failed",
     {"verifier"}),
]


def make_llm(mode):
    if mode == "double":
        from tests.fake_llm import RuleLLM
        return RuleLLM()
    from dotenv import load_dotenv
    load_dotenv()
    from agent.llm import GroqLLM
    return GroqLLM()


def run_scenario(mode, name, task, fault, approver, expected, tags):
    asked = []
    def approve(inv):
        asked.append(inv.get("invoice_id"))
        return approver == "approve"
    t0 = time.time()
    with running_app(fault) as app, Browser(base_url=app["url"]) as b:
        r = run_agent(task, b, make_llm(mode), db_path=app["db"], approver=approve if approver else None)
        rows = ap_rows(app["db"])
    r.pop("actions_taken", None)
    return {"scenario": name, "task": task, "fault": fault or None, "tags": sorted(tags), "expected": expected,
            "actual": r["status"], "ok": r["status"] == expected, "reason": r["reason"],
            "selected": (r["selected_invoice"] or {}).get("invoice_id"),
            "retry_count": r["retry_count"], "recovered": r["recovered"],
            "human_escalations": len(asked) or int(r["status"] == "waiting_for_approval"),
            "verification_ran": r["verification"] is not None,
            "verification_passed": bool(r["verification"] and r["verification"]["passed"]),
            "failed_checks": [k for k, v in (r["verification"] or {}).get("checks", {}).items() if v != "PASS"],
            "ap_rows_written": len(rows), "steps": r["steps"], "browser_actions": r["browser_actions"],
            "llm_calls": r["llm_calls"], "seconds": round(time.time() - t0, 1)}


def rate(num, den):
    return f"{num}/{den}" if den else "n/a"


def summarize(rows):
    rec = [r for r in rows if "recovery" in r["tags"]]
    ver = [r for r in rows if r["verification_ran"]]
    return {
        "scenarios": len(rows),
        "task_success_rate (actual status == expected status)": rate(sum(r["ok"] for r in rows), len(rows)),
        "recovery_success_rate (injected AP error -> completed)": rate(sum(r["recovered"] for r in rec), len(rec)),
        "verification_success_rate (runs reaching verifier whose checks all passed)":
            rate(sum(r["verification_passed"] for r in ver), len(ver)),
        "false_completions (completed but DB wrong, must be 0)":
            sum(1 for r in rows if r["actual"] == "completed" and not r["verification_passed"]),
        "human_escalations": sum(r["human_escalations"] for r in rows),
        "total_retries": sum(r["retry_count"] for r in rows),
        "avg_browser_actions": round(sum(r["browser_actions"] for r in rows) / len(rows), 1),
        "avg_llm_calls": round(sum(r["llm_calls"] or 0 for r in rows) / len(rows), 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["double", "live"], default="double")
    ap.add_argument("--only", help="run a single scenario by name")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    rows = []
    for sc in SCENARIOS:
        if args.only and sc[0] != args.only:
            continue
        r = run_scenario(args.mode, *sc)
        rows.append(r)
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['scenario']:<30} expected={r['expected']:<20} actual={r['actual']:<20}"
              f" retries={r['retry_count']} llm={r['llm_calls']} actions={r['browser_actions']} {r['seconds']}s", flush=True)
    summary = summarize(rows)
    print("\n" + json.dumps(summary, indent=2, ensure_ascii=False))
    label = ("DETERMINISTIC LLM DOUBLE: validates loop, guards, verifier. Says nothing about model quality."
             if args.mode == "double" else "LIVE MODEL run")
    (OUT / f"results_{args.mode}.json").write_text(
        json.dumps({"mode": args.mode, "label": label, "summary": summary, "scenarios": rows}, indent=2,
                   ensure_ascii=False), encoding="utf-8")
    sys.exit(0 if all(r["ok"] for r in rows) else 1)


if __name__ == "__main__":
    main()
