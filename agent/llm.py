"""LLM client (Groq's OpenAI-compatible chat API) + structured-output retry helper.

Uses plain `requests`: no vendor SDK, nothing that could break on a new Python version.

Env: GROQ_API_KEY, LLM_MODEL (default openai/gpt-oss-20b), LLM_BASE_URL,
     LLM_REASONING_EFFORT (low|medium|high, default low; set empty to omit).
"""
import json
import os
import time

import requests

from agent.schema import SchemaError

DEFAULT_MODEL = "openai/gpt-oss-20b"
DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"


class LLMError(Exception):
    """The model could not be reached / refused the request."""


class LLMOutputError(Exception):
    """The model kept returning output that fails schema validation."""


def _retry_after(resp):
    try:
        return float(resp.headers.get("retry-after", ""))
    except ValueError:
        return None


class GroqLLM:
    def __init__(self, api_key=None, model=None, base_url=None, reasoning_effort=None,
                 timeout=60, max_http_retries=4, sleep=time.sleep):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not set (copy .env.example to .env and fill it in)")
        self.model = model or os.environ.get("LLM_MODEL") or DEFAULT_MODEL
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.reasoning_effort = (reasoning_effort if reasoning_effort is not None
                                 else os.environ.get("LLM_REASONING_EFFORT", "low"))
        self.timeout, self.max_http_retries, self._sleep = timeout, max_http_retries, sleep
        self.calls = 0  # number of model replies requested (counted by structured())

    def complete(self, messages):
        """One chat completion in JSON mode. Retries rate limits (429) and 5xx with backoff."""
        payload = {
            "model": self.model, "messages": messages, "temperature": 0,
            "max_completion_tokens": 3000, "response_format": {"type": "json_object"},
        }
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last = "no response"
        for attempt in range(self.max_http_retries + 1):
            try:
                r = requests.post(f"{self.base_url}/chat/completions", headers=headers,
                                  data=json.dumps(payload), timeout=self.timeout)
            except requests.RequestException as e:
                last = f"network error: {e}"
                self._sleep(min(2 ** attempt, 30))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                last = f"HTTP {r.status_code}"
                self._sleep(min(_retry_after(r) or 2 ** attempt, 60))
                continue
            if r.status_code == 400 and "json_validate_failed" in r.text:
                # the model produced invalid JSON; hand it to structured() so it gets feedback
                try:
                    return r.json()["error"].get("failed_generation", "")
                except (ValueError, KeyError, AttributeError):
                    return ""
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            try:
                return r.json()["choices"][0]["message"].get("content") or ""
            except (ValueError, KeyError, IndexError) as e:
                raise LLMError(f"unexpected response shape: {r.text[:300]}") from e
        raise LLMError(f"LLM unavailable after {self.max_http_retries + 1} attempts ({last})")


def structured(llm, messages, parse, max_attempts=3):
    """Ask the model for output and validate it with `parse` (e.g. Step.from_json).

    On a SchemaError the exact problem is sent back and the model tries again, at most
    `max_attempts` times in total - then LLMOutputError. Invalid output is never accepted.
    """
    msgs = list(messages)
    problem = "no attempt made"
    for _ in range(max_attempts):
        llm.calls += 1
        raw = llm.complete(msgs)
        try:
            return parse(raw)
        except SchemaError as e:
            problem = str(e)
            msgs += [
                {"role": "assistant", "content": (raw or "")[:1500]},
                {"role": "user", "content": f"Your reply was rejected: {problem}. "
                                            "Reply again with ONLY one valid JSON object."},
            ]
    raise LLMOutputError(f"invalid model output after {max_attempts} attempts: {problem}")
