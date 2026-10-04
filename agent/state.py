"""Typed agent state: the single object every node reads and writes."""
from dataclasses import dataclass, field

# Phase 3 statuses. "submitted_unverified" = the UI confirmed the submission but nothing has
# independently checked the database yet; Phase 4 adds the verifier and the "completed" status.
STATUSES = ("submitted_unverified", "failed", "needs_clarification")


@dataclass
class AgentState:
    task: str
    vendor: str | None = None
    selection_rule: str = "latest invoice"
    plan: list = field(default_factory=list)
    current_url: str = ""
    page_observation: dict = field(default_factory=dict)
    selected_invoice: dict | None = None   # {invoice_id, vendor, decision, evidence}
    invoice_data: dict | None = None       # validated + grounded extraction
    actions_taken: list = field(default_factory=list)  # {step, action, args, ok, result}
    errors: list = field(default_factory=list)
    step: int = 0
    submitted_ok: bool = False             # set ONLY by deterministic inspection of the page after submit
    next_step: object = None               # Step chosen by reason, consumed by act
    final_status: str | None = None
    final_reason: str | None = None
