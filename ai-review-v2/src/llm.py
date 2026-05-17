"""LLM client wrapper.

Handles both chat completions and embeddings against any OpenAI-compatible
endpoint. Resilient to provider quirks (response_format support varies, JSON
sometimes wrapped in code fences, transient errors).
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

from openai import APIError, APITimeoutError, OpenAI, RateLimitError
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from .config import LLMEndpoint

log = logging.getLogger(__name__)


# ============================================================
# CLIENT
# ============================================================

def make_client(endpoint: LLMEndpoint) -> OpenAI:
    return OpenAI(
        base_url=endpoint.base_url,
        api_key=endpoint.api_key,
        timeout=180.0,
        max_retries=0,  # we do our own retries with tenacity for finer control
    )


# ============================================================
# CHAT COMPLETIONS
# ============================================================

@retry(
    retry=retry_if_exception_type((APITimeoutError, RateLimitError, APIError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=60),
    reraise=True,
)
def chat(
    client: OpenAI,
    model: str,
    system: str,
    user: str,
    *,
    json_mode: bool = True,
    temperature: float = 0.2,
) -> tuple[Any, dict]:
    """Run a chat completion. Returns (parsed_result, usage_dict).

    When json_mode is True, parses the response as JSON, retrying without
    response_format if the provider rejects it.
    """
    started = time.monotonic()
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    kwargs: dict = {"model": model, "messages": messages, "temperature": temperature}

    try:
        if json_mode:
            try:
                resp = client.chat.completions.create(
                    **kwargs, response_format={"type": "json_object"}
                )
            except (APIError, TypeError):
                # Provider doesn't support response_format — fall back
                resp = client.chat.completions.create(**kwargs)
        else:
            resp = client.chat.completions.create(**kwargs)
    except Exception:
        log.exception("LLM chat call failed")
        raise

    content = (resp.choices[0].message.content or "").strip()
    elapsed_ms = int((time.monotonic() - started) * 1000)

    usage = {
        "model": model,
        "prompt_tokens": getattr(resp.usage, "prompt_tokens", None) if resp.usage else None,
        "completion_tokens": getattr(resp.usage, "completion_tokens", None) if resp.usage else None,
        "duration_ms": elapsed_ms,
    }

    result: Any = content
    if json_mode:
        result = parse_json(content)

    return result, usage


# ============================================================
# EMBEDDINGS
# ============================================================

@retry(
    retry=retry_if_exception_type((APITimeoutError, RateLimitError, APIError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=4, max=30),
    reraise=True,
)
def embed(client: OpenAI, model: str, texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts. Returns a list of vectors aligned with inputs."""
    if not texts:
        return []
    # Strip leading/trailing whitespace; some providers error on empty strings.
    cleaned = [t.strip() or " " for t in texts]
    resp = client.embeddings.create(model=model, input=cleaned)
    return [d.embedding for d in resp.data]


# ============================================================
# ROBUST JSON PARSING
# ============================================================

_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_json(s: str) -> dict:
    """Best-effort JSON extraction from possibly-noisy LLM output."""
    s = s.strip()
    if not s:
        return {}

    # Code-fenced response
    m = _JSON_FENCE_RE.match(s)
    if m:
        s = m.group(1).strip()

    # Direct parse
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {"_raw": v}
    except json.JSONDecodeError:
        pass

    # Greedy: find first balanced {...} block
    start = s.find("{")
    if start == -1:
        log.warning("No JSON object found in LLM response")
        return {}

    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    v = json.loads(s[start:i + 1])
                    return v if isinstance(v, dict) else {"_raw": v}
                except json.JSONDecodeError:
                    log.warning("Found unbalanced-looking JSON; giving up")
                    return {}
    return {}
