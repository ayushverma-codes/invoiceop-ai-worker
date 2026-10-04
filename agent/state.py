"""Typed agent state: the single object every node reads and writes."""
from dataclasses import dataclass, field

# "completed" is only ever set by the independent verifier (tools/verification.py), never by the LLM.
STATUSES = ("completed", "failed", "waiting_for_approval", "needs_clarification")


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
    retry_count: int = 0                   # AP rejections so far (bounded by MAX_RETRIES)
    approval_required: bool = False        # set by the deterministic policy engine
    approval_status: str | None = None     # None | "pending" | "approved" | "denied"
    claimed_done: bool = False             # the agent said "done"; the verifier decides if that is true
    verification_result: dict | None = None
    next_step: object = None               # Step chosen by reason, consumed by act
    final_status: str | None = None
    final_reason: str | None = None
