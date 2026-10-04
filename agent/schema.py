"""Structured LLM output: strict parsing + validation (stdlib only).

Every LLM reply is parsed into one of these dataclasses. Anything malformed raises
SchemaError with a precise message, which agent/llm.py feeds back to the model for a
bounded number of retries. Nothing unvalidated ever reaches the browser tools.
"""
import json
import re
from dataclasses import dataclass, field


class SchemaError(ValueError):
    """The LLM reply did not match the required structure."""


# action name -> arguments that MUST be present for it
ACTIONS = {
    "navigate": ["url"],
    "click": ["target"],
    "select_invoice": ["invoice_id"],
    "extract_invoice": ["invoice"],
    "fill_form": ["fields"],
    "submit": [],
    "finish": ["outcome", "reason"],
}
OUTCOMES = ("done", "failed", "needs_clarification")
INTENTS = ("process_invoice", "unsupported")


def parse_json_object(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    start, end = t.find("{"), t.rfind("}")
    if start == -1 or end <= start:
        raise SchemaError("no JSON object found in the reply")
    try:
        obj = json.loads(t[start:end + 1])
    except json.JSONDecodeError as e:
        raise SchemaError(f"invalid JSON: {e}") from e
    if not isinstance(obj, dict):
        raise SchemaError("reply must be a JSON object")
    return obj


def _opt_str(obj, key):
    v = obj.get(key)
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        v = str(v)
    if not isinstance(v, str):
        raise SchemaError(f"'{key}' must be a string")
    return v.strip() or None


def _str_list(obj, key, limit, width):
    v = obj.get(key, [])
    if v is None:
        v = []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        raise SchemaError(f"'{key}' must be a list of strings")
    return [str(x).strip()[:width] for x in v[:limit] if str(x).strip()]


@dataclass
class Plan:
    intent: str
    vendor: str | None
    selection_rule: str
    steps: list = field(default_factory=list)
    clarification: str | None = None

    @classmethod
    def from_json(cls, text):
        obj = parse_json_object(text)
        intent = obj.get("intent")
        if intent not in INTENTS:
            raise SchemaError(f"'intent' must be one of {list(INTENTS)}, got {intent!r}")
        return cls(
            intent=intent,
            vendor=_opt_str(obj, "vendor"),
            selection_rule=_opt_str(obj, "selection_rule") or "latest invoice",
            steps=_str_list(obj, "steps", 8, 160),
            clarification=_opt_str(obj, "clarification"),
        )


@dataclass
class Step:
    """One agent turn: a short decision + evidence (not hidden reasoning) and ONE action."""
    decision: str
    evidence: list
    action: str
    url: str | None = None
    target: str | None = None
    invoice_id: str | None = None
    invoice: dict | None = None
    fields: dict | None = None
    outcome: str | None = None
    reason: str | None = None

    @classmethod
    def from_json(cls, text):
        obj = parse_json_object(text)
        action = obj.get("action")
        if action not in ACTIONS:
            raise SchemaError(f"'action' must be one of {sorted(ACTIONS)}, got {action!r}")
        decision = _opt_str(obj, "decision")
        if not decision:
            raise SchemaError("'decision' (one short sentence) is required")
        fields = obj.get("fields")
        if fields is not None:
            if not isinstance(fields, dict) or not all(isinstance(k, str) for k in fields):
                raise SchemaError("'fields' must be an object of field name -> value")
            fields = {k: str(v) for k, v in fields.items()}
        invoice = obj.get("invoice")
        if invoice is not None and not isinstance(invoice, dict):
            raise SchemaError("'invoice' must be an object")
        step = cls(
            decision=decision[:300],
            evidence=_str_list(obj, "evidence", 5, 200),
            action=action,
            url=_opt_str(obj, "url"),
            target=_opt_str(obj, "target"),
            invoice_id=_opt_str(obj, "invoice_id"),
            invoice=invoice,
            fields=fields,
            outcome=_opt_str(obj, "outcome"),
            reason=_opt_str(obj, "reason"),
        )
        missing = [k for k in ACTIONS[action] if not getattr(step, k)]
        if missing:
            raise SchemaError(f"action '{action}' requires: {', '.join(missing)}")
        if action == "finish" and step.outcome not in OUTCOMES:
            raise SchemaError(f"'outcome' must be one of {list(OUTCOMES)}, got {step.outcome!r}")
        return step

    def args(self):
        """The action's arguments only (used for logging and repeat detection)."""
        return {k: getattr(self, k) for k in ACTIONS[self.action] if getattr(self, k)}
