"""LangGraph-style state machine, implemented in ~30 lines (no dependency).

    plan -> observe -> reason -> act -> observe -> ...  -> (agent says done) verify -> END

Conditional edges end the run as soon as a node sets a final status. The loop is bounded
by ctx.max_steps (checked in reason_node) and by the failure/repeat guards in nodes.py.
Only the verify node can set "completed". Recovery is not a separate node: AP errors come back as the
next observation and the agent adapts (bounded by MAX_RETRIES and the guards in nodes.py).
"""
from agent.nodes import Ctx, act_node, observe_node, plan_node, reason_node, verify_node
from agent.state import AgentState
from agent.trace import Trace
from tools.policy import approval_threshold

END = "__end__"

NODES = {"plan": plan_node, "observe": observe_node, "reason": reason_node, "act": act_node,
         "verify": verify_node}
EDGES = {
    "plan": lambda s: END if s.final_status else "observe",
    "observe": lambda s: END if s.final_status else "reason",
    "reason": lambda s: END if s.final_status else "act",
    "act": lambda s: "verify" if s.claimed_done else (END if s.final_status else "observe"),
    "verify": lambda s: END,
}


def run_agent(task, browser, llm, max_steps=20, trace=None, db_path=None, approver=None):
    """Run the agent on a natural-language task. Returns a structured result dict.

    db_path:  AP database the independent verifier reads (default: the mock app's DB).
    approver: callable(invoice_dict) -> bool for human approval; None = stop with "waiting_for_approval".
    """
    ctx = Ctx(browser=browser, llm=llm, trace=trace or Trace(verbose=False), max_steps=max_steps,
              db_path=db_path, approver=approver)
    state, node = AgentState(task=task), "plan"
    while node != END:
        NODES[node](state, ctx)
        node = EDGES[node](state)
    return build_result(state, ctx)


def build_result(state, ctx):
    return {
        "status": state.final_status,
        "reason": state.final_reason,
        "task": state.task,
        "vendor": state.vendor,
        "selected_invoice": state.selected_invoice,
        "invoice_data": state.invoice_data,
        "policy": {"approval_required": state.approval_required, "approval_status": state.approval_status,
                   "threshold": approval_threshold()},
        "retry_count": state.retry_count,
        "recovered": state.retry_count > 0 and state.final_status == "completed",
        "verification": state.verification_result,
        "steps": state.step,
        "browser_actions": len(getattr(ctx.browser, "actions", []) or []),
        "llm_calls": getattr(ctx.llm, "calls", None),
        "errors": state.errors,
        "actions_taken": state.actions_taken,
    }
