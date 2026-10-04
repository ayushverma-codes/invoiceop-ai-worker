"""Deterministic TEST DOUBLE for the LLM (not a real model, proves nothing about model quality).

It reads the same prompt the real model would see (STATE / CURRENT PAGE JSON) and applies a
generic policy. Its purpose is to exercise the real loop, guards, browser tools and the
observation format end-to-end without network access.
"""
import json
import re


def _line(user, prefix):
    m = re.search(rf"^{prefix}: (.*)$", user, re.M)
    return json.loads(m.group(1)) if m else None


def _step(action, decision, evidence=(), **kw):
    return json.dumps({"decision": decision, "evidence": list(evidence), "action": action, **kw})


class RuleLLM:
    calls = 0
    vendor_tail = r"[.!?]?"  # subclasses can keep the trailing period, as real models do

    def __init__(self):
        self.messages_seen = []

    def complete(self, messages):
        self.messages_seen.append(messages)
        system, user = messages[0]["content"], messages[1]["content"]  # [1] = original prompt (later ones are retry feedback)
        if "TASK INTERPRETER" in system:
            m = re.search(r"from (.+?)" + self.vendor_tail + "$", user.strip())
            vendor = m.group(1).strip() if m else None
            return json.dumps({"intent": "process_invoice" if vendor else "unsupported", "vendor": vendor,
                               "selection_rule": "latest invoice", "steps": ["find", "extract", "enter in AP"],
                               "clarification": None if vendor else "Which vendor?"})
        return self.decide(user)

    def decide(self, user):
        norm = lambda s: re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()  # a sensible model ignores punctuation
        vendor = norm(re.search(r"^VENDOR: (.*?) \|", user, re.M).group(1))
        st, page = _line(user, "STATE"), _line(user, "CURRENT PAGE")
        url = page["url"]
        tables = page.get("tables", [])
        forms = page.get("forms", [])
        if st["submitted_ok"]:
            return _step("finish", "AP confirmed the invoice", ["confirmation message on page"], outcome="done", reason="invoice recorded in AP")
        if forms and st["invoice_data"]:
            form = forms[0]["fields"]
            empty = [f["name"] for f in form if not f["value"]]
            if empty:  # fill only what is missing (all fields on a fresh form; just the dropped one after an AP error)
                return _step("fill_form", "fill the empty AP form fields from extracted data",
                             ["empty fields: " + ", ".join(empty)],
                             fields={k: v for k, v in st["invoice_data"].items() if k in empty})
            return _step("submit", "form is filled", ["all fields populated"])
        if st["invoice_data"]:
            return _step("navigate", "open the AP create form", url="/ap/create")
        kv = next((t["key_values"] for t in tables if "key_values" in t), None)
        if kv and st["selected_invoice"]:
            inv = {"invoice_id": kv["Invoice ID"], "vendor": kv["Vendor"], "amount": kv["Amount"],
                   "invoice_date": kv["Invoice date"], "due_date": kv["Due date"]}
            return _step("extract_invoice", "copy values from the detail page", ["values visible on page"], invoice=inv)
        rows = next((t["rows"] for t in tables if "rows" in t), None)
        if rows and "Vendor" in rows[0]:
            mine = [r for r in rows if vendor in norm(r["Vendor"])]
            if not mine:
                return _step("finish", "no invoices for that vendor", outcome="failed", reason="vendor has no invoices")
            if st["selected_invoice"]:
                return _step("click", "open the selected invoice", target=st["selected_invoice"])
            best = max(mine, key=lambda r: r["Invoice date"])
            return _step("select_invoice", "newest invoice date", [f"{r['Invoice']} dated {r['Invoice date']}" for r in mine],
                         invoice_id=best["Invoice"])
        return _step("navigate", "start at the invoice portal", url="/inbox")


class ScriptedLLM:
    """Replays fixed raw replies in order (for malformed-output / failure-path tests)."""
    calls = 0

    def __init__(self, replies):
        self.replies = list(replies)
        self.messages_seen = []

    def complete(self, messages):
        self.messages_seen.append(messages)
        return self.replies.pop(0)
