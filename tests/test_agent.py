"""Phase 3 tests: schema, LLM client, and the agent loop through a REAL browser + server.

LLM behaviour is replaced by deterministic doubles (tests/fake_llm.py), so these tests verify the
loop, guards, bounded behaviour and generalisation - NOT the quality of the real model. Run the
live model with: python run.py agent "Process the latest invoice from <vendor>."
"""
import json
import pathlib
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from agent.graph import run_agent
from agent.llm import GroqLLM, LLMError, LLMOutputError, structured
from agent.schema import Plan, SchemaError, Step
from agent.trace import Trace
from tests.fake_llm import RuleLLM, ScriptedLLM, _step
from tests.helpers import ROOT, ap_rows, running_app
from tools.browser import Browser

ACME = ("AC-2026-104", "Acme Corp", 125000, "2026-10-02", "2026-10-30", "submitted")


class StubBrowser:
    base_url = "http://127.0.0.1:5000"
    actions = []


# --- schema ----------------------------------------------------------------------------
class SchemaTests(unittest.TestCase):
    def test_valid_step_and_fenced_json(self):
        s = Step.from_json('```json\n{"decision":"d","action":"navigate","url":"/inbox"}\n```')
        self.assertEqual((s.action, s.url), ("navigate", "/inbox"))

    def test_rejections_are_specific(self):
        for raw, msg in [
            ("not json at all", "no JSON object"),
            ('{"decision":"d","action":"teleport"}', "'action' must be one of"),
            ('{"decision":"d","action":"click"}', "requires: target"),
            ('{"action":"submit"}', "'decision'"),
            ('{"decision":"d","action":"finish","outcome":"maybe","reason":"r"}', "'outcome' must be"),
            ('{"decision":"d","action":"fill_form","fields":["a"]}', "'fields' must be an object"),
        ]:
            with self.assertRaisesRegex(SchemaError, msg):
                Step.from_json(raw)

    def test_plan(self):
        p = Plan.from_json('{"intent":"process_invoice","vendor":"X","steps":["a"]}')
        self.assertEqual((p.vendor, p.selection_rule), ("X", "latest invoice"))
        with self.assertRaises(SchemaError):
            Plan.from_json('{"intent":"chat"}')


# --- LLM client against a local fake OpenAI-compatible server ------------------------------
class _Handler(BaseHTTPRequestHandler):
    script = []   # list of (status, headers, body) consumed per request
    seen = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).seen.append((self.path, self.headers.get("Authorization"), body))
        status, headers, payload = type(self).script.pop(0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, *a):
        pass


def ok(content):
    return (200, {}, {"choices": [{"message": {"content": content}}]})


class LLMClientTests(unittest.TestCase):
    def setUp(self):
        _Handler.script, _Handler.seen = [], []
        self.srv = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.sleeps = []
        self.llm = GroqLLM(api_key="k", model="m", base_url=f"http://127.0.0.1:{self.srv.server_port}/v1",
                           sleep=self.sleeps.append)

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_request_shape_and_success(self):
        _Handler.script = [ok('{"a":1}')]
        self.assertEqual(self.llm.complete([{"role": "user", "content": "hi"}]), '{"a":1}')
        path, auth, body = _Handler.seen[0]
        self.assertEqual((path, auth), ("/v1/chat/completions", "Bearer k"))
        self.assertEqual((body["model"], body["temperature"], body["response_format"]), ("m", 0, {"type": "json_object"}))

    def test_rate_limit_is_retried_honouring_retry_after(self):
        _Handler.script = [(429, {"retry-after": "7"}, {"error": {}}), ok("{}")]
        self.assertEqual(self.llm.complete([]), "{}")
        self.assertEqual(self.sleeps, [7.0])

    def test_persistent_failure_is_bounded(self):
        _Handler.script = [(503, {}, {})] * 5
        with self.assertRaisesRegex(LLMError, "unavailable after 5 attempts"):
            self.llm.complete([])
        self.assertEqual(len(_Handler.seen), 5)

    def test_hard_error_not_retried(self):
        _Handler.script = [(401, {}, {"error": "bad key"})]
        with self.assertRaisesRegex(LLMError, "HTTP 401"):
            self.llm.complete([])
        self.assertEqual(len(_Handler.seen), 1)

    def test_json_validate_failed_is_surfaced_for_feedback(self):
        _Handler.script = [(400, {}, {"error": {"code": "json_validate_failed", "failed_generation": "{broken"}})]
        self.assertEqual(self.llm.complete([]), "{broken")

    def test_missing_api_key(self):
        import os
        old = os.environ.pop("GROQ_API_KEY", None)
        try:
            with self.assertRaisesRegex(LLMError, "GROQ_API_KEY"):
                GroqLLM()
        finally:
            if old:
                os.environ["GROQ_API_KEY"] = old

    def test_structured_retries_with_feedback_then_succeeds(self):
        llm = ScriptedLLM(["garbage", '{"decision":"d","action":"submit"}'])
        step = structured(llm, [{"role": "user", "content": "x"}], Step.from_json)
        self.assertEqual((step.action, llm.calls), ("submit", 2))
        self.assertIn("rejected", llm.messages_seen[1][-1]["content"])

    def test_structured_gives_up_after_max_attempts(self):
        llm = ScriptedLLM(["bad"] * 3)
        with self.assertRaises(LLMOutputError):
            structured(llm, [], Step.from_json)
        self.assertEqual(llm.calls, 3)


# --- agent loop: real browser, real server ------------------------------------------------
class AgentLoopTests(unittest.TestCase):
    def run_task(self, llm, task, fault="", **kw):
        with running_app(fault) as app, Browser(base_url=app["url"]) as b:
            result = run_agent(task, b, llm, db_path=app["db"], **kw)
            return result, ap_rows(app["db"]), b

    def test_latest_acme_invoice(self):
        llm = RuleLLM()
        r, rows, b = self.run_task(llm, "Process the latest invoice from Acme Corp.")
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["selected_invoice"]["invoice_id"], "AC-2026-104")
        self.assertEqual(rows, [ACME])
        self.assertTrue(any("AC-2026-104" in e for e in r["selected_invoice"]["evidence"]))
        self.assertEqual(r["llm_calls"], llm.calls)
        self.assertGreater(r["llm_calls"], 5)
        # Scenario A: normal invoice -> autonomous, verified success, no approval, no retries
        self.assertTrue(r["verification"]["passed"])
        self.assertTrue(all(v == "PASS" for v in r["verification"]["checks"].values()))
        self.assertEqual((r["retry_count"], r["policy"]["approval_required"]), (0, False))
        # the agent opened only the selected invoice, never the older ones
        visited = [a["result"] for a in r["actions_taken"] if a["action"] in ("navigate", "click")]
        self.assertTrue(any("/invoice/AC-2026-104" in v for v in visited))
        self.assertFalse(any("AC-2026-103" in v or "AC-2026-101" in v for v in visited))

    def test_same_agent_other_vendor_different_data_only(self):
        r, rows, _ = self.run_task(RuleLLM(), "Process the latest invoice from Globex Inc.")
        self.assertEqual(r["status"], "completed")
        self.assertEqual([row[0] for row in rows], ["GX-2026-221"])

    def test_vendor_copied_with_trailing_period_still_matches(self):
        """Regression (found in the live run): the real model returns 'Globex Inc.' for 'Globex Inc'."""
        class KeepsPeriod(RuleLLM):
            vendor_tail = ""

        llm = KeepsPeriod()
        r, rows, _ = self.run_task(llm, "Process the latest invoice from Globex Inc.")
        self.assertEqual(r["vendor"], "Globex Inc.")
        self.assertEqual(r["status"], "completed")
        self.assertEqual([row[0] for row in rows], ["GX-2026-221"])

    def test_unknown_vendor_controlled_failure_nothing_written(self):
        r, rows, _ = self.run_task(RuleLLM(), "Process the latest invoice from Umbrella Corp.")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(rows, [])

    def test_missing_vendor_asks_for_clarification_without_browsing(self):
        llm = ScriptedLLM(['{"intent":"unsupported","vendor":null,"clarification":"Which vendor?"}'])
        r = run_agent("Process the latest invoice", StubBrowser(), llm)
        self.assertEqual((r["status"], r["reason"]), ("needs_clarification", "Which vendor?"))
        self.assertEqual(r["steps"], 0)

    def test_hallucinated_extraction_is_rejected_then_agent_continues(self):
        class Liar(RuleLLM):
            lied = False

            def decide(self, user):
                out = super().decide(user)
                if '"action": "extract_invoice"' in out and not self.lied:
                    self.lied = True
                    return out.replace('"amount": "125000"', '"amount": 999999')
                return out

        r, rows, _ = self.run_task(Liar(), "Process the latest invoice from Acme Corp.")
        self.assertEqual(r["status"], "completed")
        self.assertTrue(any("not found on the current page" in e for e in r["errors"]))
        self.assertEqual(rows, [ACME])  # DB holds the real amount, not the hallucinated one

    def test_fill_value_that_differs_from_extraction_is_blocked(self):
        class Sloppy(RuleLLM):
            tampered = False

            def decide(self, user):
                out = super().decide(user)
                if '"action": "fill_form"' in out and not self.tampered:
                    self.tampered = True
                    d = json.loads(out)
                    d["fields"]["amount"] = "1"
                    return json.dumps(d)
                return out

        r, rows, _ = self.run_task(Sloppy(), "Process the latest invoice from Acme Corp.")
        self.assertTrue(any("differs from extracted data" in e for e in r["errors"]))
        self.assertEqual(rows, [ACME])

    def test_fill_on_page_without_that_form_fails_fast(self):
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}',
                           _step("fill_form", "fill", fields={"invoice_id": "X"}),
                           _step("finish", "stop", outcome="failed", reason="r")])
        with running_app() as app, Browser(base_url=app["url"]) as b:
            r = run_agent("Process the latest invoice from V", b, llm)
        self.assertTrue(any("this page has no form" in e for e in r["errors"]))
        self.assertEqual(r["status"], "failed")

    def test_finish_done_without_confirmed_submission_is_rejected(self):
        llm = ScriptedLLM([
            '{"intent":"process_invoice","vendor":"V","steps":[]}',
            _step("finish", "all done", outcome="done", reason="trust me"),
            _step("finish", "giving up", outcome="failed", reason="could not do it"),
        ])
        with running_app() as app, Browser(base_url=app["url"]) as b:
            r = run_agent("Process the latest invoice from V", b, llm)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(any("finish 'done' rejected" in e for e in r["errors"]))

    def test_navigation_outside_intranet_is_blocked(self):
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}']
                          + [_step("navigate", "go", url="https://example.com")] * 3)

        class B(StubBrowser):
            def inspect_page(self):
                return {"url": "about:blank", "title": "", "tables": [], "forms": [], "messages": [], "links": [], "text": ""}

        r = run_agent("Process the latest invoice from V", B(), llm)
        self.assertEqual(r["status"], "failed")
        self.assertTrue(all("outside the company intranet" in e for e in r["errors"]))

    def test_stuck_agent_is_stopped_not_looped(self):
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}']
                          + [_step("navigate", "again", url="/inbox")] * 30)
        with running_app() as app, Browser(base_url=app["url"]) as b:
            r = run_agent("Process the latest invoice from V", b, llm, max_steps=20)
        self.assertEqual(r["status"], "failed")
        self.assertLess(r["steps"], 20)
        self.assertIn("repeated the same action", r["reason"])

    def test_step_budget_is_enforced(self):
        urls = ["/inbox", "/ap", "/ap/create", "/inbox", "/ap", "/ap/create"]
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}']
                          + [_step("navigate", "wander", url=u) for u in urls])
        with running_app() as app, Browser(base_url=app["url"]) as b:
            r = run_agent("Process the latest invoice from V", b, llm, max_steps=4)
        self.assertEqual((r["status"], r["steps"]), ("failed", 4))
        self.assertIn("step budget", r["reason"])

    def test_malformed_llm_output_is_recovered_inside_the_loop(self):
        class Flaky(RuleLLM):
            n = 0

            def complete(self, messages):
                Flaky.n += 1
                if Flaky.n == 3:
                    return "```oops not json"
                return super().complete(messages)

        r, rows, _ = self.run_task(Flaky(), "Process the latest invoice from Acme Corp.")
        self.assertEqual((r["status"], rows), ("completed", [ACME]))

    def test_persistently_malformed_output_fails_cleanly(self):
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}'] + ["nope"] * 3)
        with running_app() as app, Browser(base_url=app["url"]) as b:
            r = run_agent("Process the latest invoice from V", b, llm)
        self.assertEqual(r["status"], "failed")
        self.assertIn("invalid model output", r["reason"])


# --- Phase 4: recovery, approval policy, independent verification -------------------------------
class ReliabilityTests(unittest.TestCase):
    def run_task(self, llm, task, fault="", approver=None, **kw):
        with running_app(fault) as app, Browser(base_url=app["url"]) as b:
            trace = Trace(verbose=False)
            result = run_agent(task, b, llm, db_path=app["db"], approver=approver, trace=trace, **kw)
            return result, ap_rows(app["db"]), trace

    # Scenario B: AP rejects the first submission -> agent reads the error, fixes the cause, retries
    def test_ap_rejection_is_recovered_from_the_observed_error(self):
        r, rows, trace = self.run_task(RuleLLM(), "Process the latest invoice from Acme Corp.", fault="invoice_date_once")
        self.assertEqual(r["status"], "completed")
        self.assertEqual((r["retry_count"], r["recovered"]), (1, True))
        self.assertIn("Invoice date is required.", r["errors"])
        self.assertEqual(rows, [ACME])
        subs = [a for a in r["actions_taken"] if a["action"] == "submit"]
        self.assertEqual([a["result"].startswith("AP rejected") for a in subs], [True, False])
        fixes = [a for a in r["actions_taken"] if a["action"] == "fill_form"][-1]
        self.assertEqual(list(fixes["args"]["fields"]), ["invoice_date"])  # changed only what the error pointed at
        self.assertTrue(any(tag == "RECOVERY" for tag, _ in trace.events))

    def test_blind_resubmit_after_rejection_is_blocked(self):
        class Impatient(RuleLLM):
            tried = False

            def decide(self, user):
                if not self.tried and "Invoice date is required." in user:
                    self.tried = True
                    return _step("submit", "just try again")
                return super().decide(user)

        r, rows, _ = self.run_task(Impatient(), "Process the latest invoice from Acme Corp.", fault="invoice_date_once")
        self.assertTrue(any("nothing has changed since" in e for e in r["errors"]))
        self.assertEqual((r["status"], rows), ("completed", [ACME]))

    def test_retries_are_bounded_when_ap_never_accepts(self):
        r, rows, _ = self.run_task(RuleLLM(), "Process the latest invoice from Acme Corp.", fault="invoice_date_always")
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["retry_count"], 4)  # first rejection + 3 retries, then stop
        self.assertIn("kept rejecting", r["reason"])
        self.assertEqual(rows, [])
        self.assertLess(r["steps"], 20)

    # Scenario C: high-value invoice -> human approval before anything is written
    def test_high_value_invoice_asks_human_and_proceeds_when_approved(self):
        asked = []
        def approver(inv):
            asked.append(inv)
            return True
        r, rows, trace = self.run_task(RuleLLM(), "Process the latest invoice from Initech LLC.", approver=approver)
        self.assertEqual(r["status"], "completed")
        self.assertEqual([a["invoice_id"] for a in asked], ["IN-2026-310"])
        self.assertEqual(r["policy"], {"approval_required": True, "approval_status": "approved", "threshold": 200000})
        self.assertEqual(rows[0][:3], ("IN-2026-310", "Initech LLC", 500000))
        self.assertEqual(r["verification"]["checks"]["approval_respected"], "PASS")

    def test_high_value_invoice_denied_writes_nothing(self):
        r, rows, _ = self.run_task(RuleLLM(), "Process the latest invoice from Initech LLC.", approver=lambda inv: False)
        self.assertEqual(r["status"], "failed")
        self.assertIn("denied", r["reason"])
        self.assertEqual(rows, [])

    def test_high_value_invoice_without_approver_pauses_before_submission(self):
        r, rows, _ = self.run_task(RuleLLM(), "Process the latest invoice from Initech LLC.")
        self.assertEqual(r["status"], "waiting_for_approval")
        self.assertEqual(r["policy"]["approval_status"], "pending")
        self.assertEqual(rows, [])

    def test_below_threshold_never_asks(self):
        def boom(inv):
            raise AssertionError("approver must not be called")
        r, rows, _ = self.run_task(RuleLLM(), "Process the latest invoice from Globex Inc.", approver=boom)
        self.assertEqual(r["status"], "completed")

    def test_llm_cannot_bypass_policy_by_clicking_the_form_button(self):
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}',
                           _step("navigate", "open form", url="/ap/create"),
                           _step("click", "press submit", target="Submit invoice"),
                           _step("click", "press submit via selector", target="#submit-btn"),
                           _step("finish", "stop", outcome="failed", reason="r")])
        r, rows, _ = self.run_task(llm, "Process the latest invoice from V")
        self.assertEqual(sum("cannot be clicked directly" in e for e in r["errors"]), 2)
        self.assertEqual(rows, [])

    def test_submit_requires_extracted_data(self):
        llm = ScriptedLLM(['{"intent":"process_invoice","vendor":"V","steps":[]}',
                           _step("navigate", "open form", url="/ap/create"),
                           _step("submit", "submit empty"),
                           _step("finish", "stop", outcome="failed", reason="r")])
        r, rows, _ = self.run_task(llm, "Process the latest invoice from V")
        self.assertTrue(any("extract_invoice must be done before submit" in e for e in r["errors"]))
        self.assertEqual(rows, [])

    # Scenario D: the UI says success but the stored record is wrong -> NOT reported as success
    def test_verification_failure_is_not_reported_as_success(self):
        r, rows, trace = self.run_task(RuleLLM(), "Process the latest invoice from Acme Corp.", fault="store_wrong_amount")
        self.assertEqual(r["status"], "failed")
        self.assertIn("amount_matches", r["reason"])
        self.assertEqual(r["verification"]["checks"]["amount_matches"], "FAIL")
        self.assertEqual(r["verification"]["checks"]["vendor_matches"], "PASS")
        self.assertEqual(rows[0][2], 125001)  # the UI had confirmed success; the DB disagrees
        self.assertFalse(r["recovered"])

    def test_completed_only_comes_from_verifier_not_from_llm_claim(self):
        """Agent says done, AP page confirmed, but the verifier finds no record -> failed."""
        import tempfile, os
        from mock_app.database import SCHEMA
        import sqlite3, contextlib
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            empty_db = os.path.join(tmp, "other.db")
            with contextlib.closing(sqlite3.connect(empty_db)) as c:
                c.executescript(SCHEMA)
            with running_app() as app, Browser(base_url=app["url"]) as b:
                r = run_agent("Process the latest invoice from Acme Corp.", b, RuleLLM(), db_path=empty_db)
        self.assertEqual(r["status"], "failed")
        self.assertEqual(r["verification"]["checks"]["invoice_exists"], "FAIL")


class VerifierAndPolicyUnitTests(unittest.TestCase):
    INV = {"invoice_id": "T-1", "vendor": "V Co", "amount": 100, "invoice_date": "2026-01-01", "due_date": "2026-02-01"}

    def make_db(self, tmp, ap=None, source=True):
        import contextlib, os, sqlite3
        from mock_app.database import SCHEMA
        path = os.path.join(tmp, "v.db")
        with contextlib.closing(sqlite3.connect(path)) as c:
            c.executescript(SCHEMA)
            if source:
                c.execute("INSERT INTO inbox_invoices VALUES ('T-1','V Co',100,'2026-01-01','2026-02-01','x')")
            if ap:
                c.execute("INSERT INTO invoices VALUES (?,?,?,?,?,?)", ap)
            c.commit()
        return path

    def verify(self, **kw):
        import tempfile
        from tools.verification import verify_submission
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            path = self.make_db(tmp, **{k: v for k, v in kw.items() if k in ("ap", "source")})
            return verify_submission(kw.get("path", path), kw.get("inv", self.INV), kw.get("approval"))

    GOOD = ("T-1", "V Co", 100, "2026-01-01", "2026-02-01", "submitted")

    def test_all_pass(self):
        v = self.verify(ap=self.GOOD)
        self.assertTrue(v["passed"])
        self.assertEqual(set(v["checks"].values()), {"PASS"})

    def test_missing_record(self):
        v = self.verify(ap=None)
        self.assertFalse(v["passed"])
        self.assertEqual(v["checks"]["invoice_exists"], "FAIL")

    def test_each_field_mismatch_is_caught(self):
        for idx, check in ((1, "vendor_matches"), (2, "amount_matches"), (3, "invoice_date_matches"), (4, "due_date_matches")):
            row = list(self.GOOD)
            row[idx] = "999" if idx != 2 else 999
            v = self.verify(ap=tuple(row))
            self.assertEqual(v["checks"][check], "FAIL", check)
            self.assertFalse(v["passed"])

    def test_wrong_status(self):
        v = self.verify(ap=self.GOOD[:5] + ("pending",))
        self.assertEqual(v["checks"]["status_confirmed"], "FAIL")

    def test_extraction_that_differs_from_source_is_caught(self):
        bad = dict(self.INV, amount=101)
        v = self.verify(ap=("T-1", "V Co", 101, "2026-01-01", "2026-02-01", "submitted"), inv=bad)
        self.assertEqual(v["checks"]["amount_matches"], "PASS")   # AP == what the agent entered...
        self.assertEqual(v["checks"]["matches_source_record"], "FAIL")  # ...but that was wrong vs the source

    def test_unreadable_database_fails_cleanly(self):
        from tools.verification import verify_submission
        v = verify_submission("/nonexistent/dir/x.db", self.INV)
        self.assertFalse(v["passed"])
        self.assertEqual(v["checks"]["database_readable"], "FAIL")

    def test_approval_respected_check(self):
        big = dict(self.INV, amount=250000)
        row = ("T-1", "V Co", 250000, "2026-01-01", "2026-02-01", "submitted")
        self.assertEqual(self.verify(ap=row, inv=big, approval=None)["checks"]["approval_respected"], "FAIL")
        self.assertEqual(self.verify(ap=row, inv=big, approval="approved")["checks"]["approval_respected"], "PASS")

    def test_policy_threshold_is_strictly_greater_than(self):
        from tools.policy import requires_approval
        self.assertFalse(requires_approval({"amount": 200000}))
        self.assertTrue(requires_approval({"amount": 200001}))

    def test_threshold_is_configurable_by_env(self):
        import os
        from tools.policy import requires_approval
        os.environ["APPROVAL_THRESHOLD"] = "1000"
        try:
            self.assertTrue(requires_approval({"amount": 1001}))
        finally:
            del os.environ["APPROVAL_THRESHOLD"]


class VendorMatchTests(unittest.TestCase):
    TABLE = {"headers": ["Invoice", "Vendor", "Invoice date"], "rows": [
        {"cells": {"Invoice": "A-1", "Vendor": "Alpha Corp", "Invoice date": "2026-01-01"}, "links": []},
        {"cells": {"Invoice": "B-1", "Vendor": "Beta Inc", "Invoice date": "2026-01-02"}, "links": []},
        {"cells": {"Invoice": "B-2", "Vendor": "Beta Inc", "Invoice date": "2026-02-02"}, "links": []}]}

    def test_matching_ignores_case_punctuation_spacing(self):
        from tools.invoice import rows_for_vendor
        for q in ("Beta Inc", "Beta Inc.", "  beta   INC ", "beta"):
            self.assertEqual(len(rows_for_vendor(self.TABLE, q)), 2, q)

    def test_unknown_vendor_error_lists_vendors_on_page(self):
        from tools.invoice import VendorNotFound, rows_for_vendor
        with self.assertRaisesRegex(VendorNotFound, "vendors on this page: \\['Alpha Corp', 'Beta Inc'\\]"):
            rows_for_vendor(self.TABLE, "Gamma")

    def test_empty_query_matches_nothing(self):
        from tools.invoice import VendorNotFound, rows_for_vendor
        with self.assertRaises(VendorNotFound):
            rows_for_vendor(self.TABLE, " . ")


class GuardTests(unittest.TestCase):
    def test_no_vendor_specific_code_or_prompts_in_agent_or_tools(self):
        pattern = re.compile(r"acme|globex|initech", re.IGNORECASE)
        for folder in ("agent", "tools"):
            for path in (ROOT / folder).rglob("*.py"):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")), f"vendor name in {path.name}")


if __name__ == "__main__":
    unittest.main()
