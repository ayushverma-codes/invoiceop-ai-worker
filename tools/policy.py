"""Deterministic business policy. The LLM has no say in any of this.

Rule: an invoice whose amount is strictly greater than the threshold needs human approval
before it may be submitted to AP. Threshold: APPROVAL_THRESHOLD env var (default 200000).
"""
import os

DEFAULT_THRESHOLD = 200000


def approval_threshold():
    return int(os.environ.get("APPROVAL_THRESHOLD", DEFAULT_THRESHOLD))


def requires_approval(invoice):
    return int(invoice["amount"]) > approval_threshold()


def cli_approver(invoice):
    """Ask a human at the terminal. Returns True only on an explicit yes; EOF/anything else = deny."""
    print("\nHuman approval required.")
    print(f"Invoice: {invoice['invoice_id']}")
    print(f"Vendor:  {invoice['vendor']}")
    print(f"Amount:  \u20b9{invoice['amount']} (threshold \u20b9{approval_threshold()})")
    try:
        answer = input("Approve? [y/n] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
