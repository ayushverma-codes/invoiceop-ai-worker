"""LangGraph-style state machine, implemented in ~30 lines (no dependency).

    plan -> observe -> reason -> act -> observe -> ...  -> END

Conditional edges end the run as soon as a node sets a final status. The loop is bounded
by ctx.max_steps (checked in reason_node) and by the failure/repeat guards in nodes.py.
Verification and recovery nodes are added in Phase 4.
"""
from agent.nodes import Ctx, act_node, observe_node, plan_node, reason_node
from agent.state import AgentState
from agent.trace import Trace

END = "__end__"

NODES = {"plan": plan_node, "observe": observe_node, "reason": reason_node, "act": act_node}
EDGES = {
    "plan": lambda s: END if s.final_status else "observe",
    "observe": lambda s: END if s.final_status else "reason",
    "reason": lambda s: END if s.final_status else "act",
    "act": lambda s: END if s.final_status else "observe",
}


def run_agent(task, browser, llm, max_steps=20, trace=None):
    """Run the agent on a natural-language task. Returns a structured result dict."""
    ctx = Ctx(browser=browser, llm=llm, trace=trace or Trace(verbose=False), max_steps=max_steps)
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
        "steps": state.step,
        "browser_actions": len(getattr(ctx.browser, "actions", []) or []),
        "llm_calls": getattr(ctx.llm, "calls", None),
        "errors": state.errors,
        "actions_taken": state.actions_taken,
    }
