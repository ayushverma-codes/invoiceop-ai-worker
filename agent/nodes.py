"""Graph nodes: plan, observe, reason, act. Each takes (state, ctx), mutates state.

Split of responsibility:
  reason -> the LLM picks ONE structured action (decision + evidence + action).
  act    -> deterministic code validates (guards) and executes it with the browser tools.
The LLM can never touch the browser, the database, or the final status directly.
"""
import json
import os
from dataclasses import dataclass

from agent.llm import LLMError, LLMOutputError, structured
from agent.prompts import AGENT_SYSTEM, PLANNER_SYSTEM
from agent.schema import Plan, Step
from tools.browser import SELECTOR_PREFIXES, BrowserError
from tools.invoice import FIELDS, InvoiceData, InvoiceError, find_invoice_table, rows_for_vendor
from tools.policy import approval_threshold, requires_approval
from tools.verification import verify_submission

MAX_RETRIES = int(os.environ.get("MAX_RETRIES", "3"))  # AP rejections tolerated before giving up
MAX_CONSECUTIVE_FAILURES = 3
MAX_IDENTICAL_ACTIONS = 3
HISTORY_LINES = 14


class GuardError(Exception):
    """A deterministic safety/validation guard rejected the LLM's action."""


@dataclass
class Ctx:
    browser: object
    llm: object
    trace: object
    max_steps: int = 20
    db_path: str | None = None   # AP database the independent verifier reads
    approver: object = None      # callable(invoice) -> bool; None = pause with waiting_for_approval


def _fail(state, ctx, status, reason):
    state.final_status, state.final_reason = status, reason
    ctx.trace.log("ERROR" if status == "failed" else "RESULT", reason)


# --- observation formatting -------------------------------------------------
def compact_observation(obs):
    """Shrink inspect_page() output to what the LLM needs (keeps token use low)."""
    out = {"url": obs.get("url"), "title": obs.get("title")}
    tables = []
    for t in obs.get("tables", []):
        if t.get("headers"):
            rows = []
            for r in t["rows"]:
                cells = dict(r["cells"])
                link = r["links"][0]["text"] if r.get("links") else None
                if link and link not in cells.values():
                    cells["_link"] = link
                rows.append(cells)
            tables.append({"rows": rows})
        else:
            tables.append({"key_values": t.get("key_values", {})})
    if tables:
        out["tables"] = tables
    forms = [{"fields": [{"name": f["name"], "label": f["label"], "value": f["value"]} for f in fm["fields"]],
              "buttons": fm["buttons"]} for fm in obs.get("forms", [])]
    if forms:
        out["forms"] = forms
    if obs.get("messages"):
        out["messages"] = obs["messages"]
    out["nav_links"] = [l["text"] for l in obs.get("links", []) if l.get("text")]
    if not tables and not forms:
        out["text"] = obs.get("text", "")[:500]
    return out


def _dumps(x):
    return json.dumps(x, separators=(",", ":"), ensure_ascii=False)


def _observation_summary(obs):
    parts = [obs.get("url", "")]
    for t in obs.get("tables", []):
        parts.append(f"table({len(t['rows'])} rows)" if t.get("headers") else f"details({len(t.get('key_values', {}))} fields)")
    if obs.get("forms"):
        parts.append("form(" + ",".join(f["name"] for f in obs["forms"][0]["fields"]) + ")")
    for m in obs.get("messages", []):
        parts.append(f"{m['kind']}: {m['text']}")
    return " | ".join(parts)


# --- nodes ---------------------------------------------------------------------
def plan_node(state, ctx):
    ctx.trace.log("TASK", state.task)
    try:
        plan = structured(ctx.llm, [{"role": "system", "content": PLANNER_SYSTEM},
                                    {"role": "user", "content": state.task}], Plan.from_json)
    except (LLMError, LLMOutputError) as e:
        return _fail(state, ctx, "failed", f"could not interpret the task: {e}")
    if plan.intent != "process_invoice" or not plan.vendor:
        return _fail(state, ctx, "needs_clarification",
                     plan.clarification or "Which vendor's invoice should I process?")
    state.vendor, state.selection_rule, state.plan = plan.vendor, plan.selection_rule, plan.steps
    ctx.trace.log("PLAN", f"vendor={plan.vendor!r}; rule={plan.selection_rule!r}; " + " -> ".join(plan.steps))


def observe_node(state, ctx):
    try:
        state.page_observation = ctx.browser.inspect_page()
    except Exception as e:  # browser died / page crashed
        return _fail(state, ctx, "failed", f"could not observe the page: {e}")
    state.current_url = state.page_observation.get("url", "")
    ctx.trace.log("OBSERVE", _observation_summary(state.page_observation))


def _history(state):
    lines = []
    for a in state.actions_taken[-HISTORY_LINES:]:
        args = _dumps(a["args"])[:160]
        lines.append(f"{a['step']}. {a['action']} {args} -> {'OK' if a['ok'] else 'REJECTED/ERROR'}: {a['result'][:220]}")
    return "\n".join(lines) or "(none yet)"


def _state_facts(state, ctx):
    return {
        "selected_invoice": state.selected_invoice["invoice_id"] if state.selected_invoice else None,
        "invoice_data": state.invoice_data,
        "submitted_ok": state.submitted_ok,
        "steps_used": state.step, "steps_max": ctx.max_steps,
    }


def reason_node(state, ctx):
    if state.step >= ctx.max_steps:
        return _fail(state, ctx, "failed", f"step budget of {ctx.max_steps} exhausted without finishing")
    state.step += 1
    user = (
        f"TASK: {state.task}\n"
        f"VENDOR: {state.vendor} | SELECTION RULE: {state.selection_rule}\n"
        f"PLAN: {'; '.join(state.plan)}\n"
        f"STATE: {_dumps(_state_facts(state, ctx))}\n"
        f"HISTORY:\n{_history(state)}\n"
        f"CURRENT PAGE: {_dumps(compact_observation(state.page_observation))}\n"
        "Reply with the next step as one JSON object."
    )
    try:
        step = structured(ctx.llm, [{"role": "system", "content": AGENT_SYSTEM},
                                    {"role": "user", "content": user}], Step.from_json)
    except (LLMError, LLMOutputError) as e:
        return _fail(state, ctx, "failed", f"reasoning step failed: {e}")
    ctx.trace.log("DECISION", step.decision)
    for ev in step.evidence:
        ctx.trace.log("EVIDENCE", ev)
    # runaway guard: the same action three times in a row means the agent is stuck
    recent = [(a["action"], _dumps(a["args"])) for a in state.actions_taken[-(MAX_IDENTICAL_ACTIONS - 1):]]
    key = (step.action, _dumps(step.args()))
    if len(recent) == MAX_IDENTICAL_ACTIONS - 1 and all(r == key for r in recent):
        return _fail(state, ctx, "failed", f"agent repeated the same action {MAX_IDENTICAL_ACTIONS} times: {step.action}")
    state.next_step = step


# --- act: guards + execution ------------------------------------------------------
def _x_navigate(state, ctx, s):
    if s.url.lower().startswith(("http://", "https://")) and not s.url.startswith(ctx.browser.base_url):
        raise GuardError(f"navigation outside the company intranet is not allowed: {s.url}")
    r = ctx.browser.navigate(s.url)
    return f"opened {r['url']} (HTTP {r['status']})"


def _x_click(state, ctx, s):
    # A form button/selector could submit the form and bypass the submit action (and its policy gate).
    if state.page_observation.get("forms"):
        buttons = [b for fm in state.page_observation["forms"] for b in fm["buttons"]]
        if s.target in buttons or s.target.startswith(SELECTOR_PREFIXES):
            raise GuardError("form buttons/selectors cannot be clicked directly; use fill_form, then the submit action")
    return f"clicked {s.target!r}, now at {ctx.browser.click(s.target)['url']}"


def _x_select_invoice(state, ctx, s):
    """Guard: the chosen invoice must be on the observed list AND belong to the requested vendor."""
    try:
        table = find_invoice_table(state.page_observation.get("tables", []))
        rows = rows_for_vendor(table, state.vendor)
    except InvoiceError as e:
        raise GuardError(str(e)) from e
    id_col, vendor_col = table["headers"][0], next(h for h in table["headers"] if "vendor" in h.lower())
    match = next((r for r in rows if r["cells"][id_col] == s.invoice_id), None)
    if match is None:
        ids = [r["cells"][id_col] for r in rows]
        raise GuardError(f"{s.invoice_id!r} is not one of the requested vendor's invoices on this page: {ids}")
    state.selected_invoice = {"invoice_id": s.invoice_id, "vendor": match["cells"][vendor_col],
                              "decision": s.decision, "evidence": s.evidence}
    ctx.trace.log("EVIDENCE", f"selected {s.invoice_id} ({match['cells'][vendor_col]}) from {len(rows)} candidate(s)")
    return f"selected invoice {s.invoice_id}"


def _x_extract_invoice(state, ctx, s):
    """Guard: extraction must pass schema validation AND every value must be visible on the page."""
    if not state.selected_invoice:
        raise GuardError("select_invoice must be done before extract_invoice")
    raw = {f: str(s.invoice.get(f, "")).strip() for f in FIELDS}
    raw["amount"] = raw["amount"].replace(",", "")
    try:
        data = InvoiceData.from_key_values(raw).to_dict()
    except InvoiceError as e:
        raise GuardError(f"extraction invalid: {e}") from e
    if data["invoice_id"] != state.selected_invoice["invoice_id"]:
        raise GuardError(f"extracted invoice_id {data['invoice_id']} differs from selected {state.selected_invoice['invoice_id']}")
    page = _dumps(state.page_observation).replace(",", "")
    ungrounded = [f for f in FIELDS if str(data[f]).replace(",", "") not in page]
    if ungrounded:
        raise GuardError(f"values not found on the current page (hallucinated or wrong page?): {ungrounded}")
    state.invoice_data = data
    ctx.trace.log("EXTRACT", _dumps(data))
    return "invoice data extracted and validated against the page"


def _x_fill_form(state, ctx, s):
    available = [f["name"] for fm in state.page_observation.get("forms", []) for f in fm["fields"]]
    unknown = [n for n in s.fields if n not in available]
    if unknown:  # fail fast instead of waiting for a browser timeout
        where = f"form fields on this page: {available}" if available else "this page has no form"
        raise GuardError(f"cannot fill {unknown}: {where}")
    if state.invoice_data:
        for name, value in s.fields.items():
            if name in state.invoice_data and str(value) != str(state.invoice_data[name]):
                raise GuardError(f"value for {name!r} ({value!r}) differs from extracted data ({state.invoice_data[name]!r})")
    for name, value in s.fields.items():
        ctx.browser.fill(name, value)
    return f"filled {', '.join(s.fields)}"


def _unfixed_rejection(state):
    """True if AP rejected the last submit and the agent has changed nothing since."""
    for a in reversed(state.actions_taken):
        if not a["ok"]:
            continue
        if a["action"] in ("fill_form", "navigate"):
            return False
        if a["action"] == "submit":
            return a["result"].startswith("AP rejected")
    return False


def _x_submit(state, ctx, s):
    if not state.invoice_data:
        raise GuardError("extract_invoice must be done before submit")
    if _unfixed_rejection(state):
        raise GuardError("AP rejected the last submission and nothing has changed since; use fill_form to fix "
                         "the cause (see the error message and the form's current values), then submit again")
    # --- deterministic policy gate: runs BEFORE anything is written to AP ---
    if requires_approval(state.invoice_data):
        state.approval_required = True
        if state.approval_status in (None, "pending"):
            ctx.trace.log("POLICY", f"amount {state.invoice_data['amount']} exceeds threshold "
                                    f"{approval_threshold()}: human approval required")
            if ctx.approver is None:
                state.approval_status = "pending"
                state.final_status = "waiting_for_approval"
                state.final_reason = "high-value invoice: paused before submission, awaiting human approval"
                return "paused before submission: waiting for human approval"
            state.approval_status = "approved" if ctx.approver(dict(state.invoice_data)) else "denied"
            ctx.trace.log("APPROVAL", state.approval_status)
        if state.approval_status == "denied":
            state.final_status, state.final_reason = "failed", "human denied approval; nothing was submitted"
            return "not submitted: approval denied"
    state.submitted_ok = False
    r = ctx.browser.submit()
    after = ctx.browser.inspect_page()
    errors = [m["text"] for m in after["messages"] if m["kind"] == "error"]
    oks = [m["text"] for m in after["messages"] if m["kind"] == "ok"]
    inv_id = state.invoice_data["invoice_id"]
    if errors:
        state.errors.extend(errors)
        state.retry_count += 1
        ctx.trace.log("RECOVERY", f"AP rejected the submission (rejection {state.retry_count}, "
                                  f"max retries {MAX_RETRIES}): {errors}")
        if state.retry_count > MAX_RETRIES:
            state.final_status = "failed"
            state.final_reason = f"AP kept rejecting the submission after {MAX_RETRIES} retries: {errors}"
        return f"AP rejected the submission (HTTP {r['status']}): {errors}"
    if any(inv_id in text for text in oks):
        state.submitted_ok = True
        return f"AP confirmed (HTTP {r['status']}): {oks[0]}"
    return f"submitted (HTTP {r['status']}) but the page showed no confirmation"


def _x_finish(state, ctx, s):
    if s.outcome == "done":
        if not state.submitted_ok:
            raise GuardError("finish 'done' rejected: no AP submission has been confirmed by the page")
        # not final yet: the independent verifier (verify_node) decides whether this claim is true
        state.claimed_done, state.final_reason = True, s.reason
    else:
        state.final_status, state.final_reason = s.outcome, s.reason  # "failed" | "needs_clarification"
    return f"finished: {s.outcome}"


EXECUTORS = {
    "navigate": _x_navigate, "click": _x_click, "select_invoice": _x_select_invoice,
    "extract_invoice": _x_extract_invoice, "fill_form": _x_fill_form, "submit": _x_submit,
    "finish": _x_finish,
}


def act_node(state, ctx):
    s = state.next_step
    state.next_step = None
    try:
        result, ok = EXECUTORS[s.action](state, ctx, s), True
    except (GuardError, BrowserError) as e:
        result, ok = str(e), False
        state.errors.append(result)
        ctx.trace.log("ERROR", f"{s.action} rejected: {result}")
    except Exception as e:  # unexpected tool failure: record it, let the bounded loop continue
        result, ok = f"unexpected {type(e).__name__}: {e}", False
        state.errors.append(result)
        ctx.trace.log("ERROR", f"{s.action}: {result}")
    state.actions_taken.append({"step": state.step, "action": s.action, "args": s.args(), "ok": ok, "result": result})
    if ok:
        ctx.trace.log("ACTION", f"{s.action} {_dumps(s.args())} -> {result}")
    if state.final_status:
        ctx.trace.log("RESULT", f"{state.final_status}: {state.final_reason}")
    elif len(state.actions_taken) >= MAX_CONSECUTIVE_FAILURES and not any(
            a["ok"] for a in state.actions_taken[-MAX_CONSECUTIVE_FAILURES:]):
        _fail(state, ctx, "failed", f"{MAX_CONSECUTIVE_FAILURES} consecutive failed actions; giving up")


def verify_node(state, ctx):
    """Independent check of the real database. The agent's own claim is never evidence."""
    ctx.trace.log("VERIFY", "agent claims done; checking the AP database independently")
    result = verify_submission(ctx.db_path, state.invoice_data, state.approval_status)
    state.verification_result = result
    for name, outcome in result["checks"].items():
        extra = f" ({result['details'][name]})" if outcome == "FAIL" else ""
        ctx.trace.log("VERIFY", f"{name}={outcome}{extra}")
    if result["passed"]:
        state.final_status = "completed"
        state.final_reason = f"verified in database: {state.final_reason}"
    else:
        failed = [n for n, o in result["checks"].items() if o == "FAIL"]
        state.final_status = "failed"
        state.final_reason = f"verification failed ({', '.join(failed)}): the UI reported success but the stored record is not correct"
    ctx.trace.log("RESULT", f"{state.final_status}: {state.final_reason}")
