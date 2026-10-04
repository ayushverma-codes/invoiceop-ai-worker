"""Execution trace: concise tagged lines (decisions, actions, observations, errors, evidence).
Deliberately NOT the model's chain-of-thought."""


class Trace:
    def __init__(self, verbose=True):
        self.verbose = verbose
        self.events = []  # (tag, message)

    def log(self, tag, message):
        message = " ".join(str(message).split())
        if len(message) > 260:
            message = message[:257] + "..."
        self.events.append((tag, message))
        if self.verbose:
            print(f"[{tag}] {message}", flush=True)
