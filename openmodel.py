"""An open-weights model behind exactly the same gate: the slice of the Anthropic client the agent loop uses,
`client.beta.messages.create(...)`, over a standard chat completions API with tools (`/chat/completions`).

The loop, the prompt, the tool schemas, the verifier and the 80% rule are untouched; only the wire format is
translated. Request fields with no equivalent are dropped (see docs/open-model.md):
- `thinking` and `output_config.effort`: the model reasons by its own default. On GLM-5.3 the provider's
  `reasoning_effort` of medium or high switched its reasoning off altogether, so effort is not mapped.
- `betas` and `fallbacks`: server-side fallbacks are a Claude feature (only Opus gets them anyway).
- `strict` on each tool: the same JSON schemas are sent, without the strict flag.
- `is_error` on a tool result: the result text already says `error:`.
The model's reasoning comes back as thinking blocks and goes back to it with its tool calls, as Claude's does.
"""
from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

KEY_ENV = "FIREWORKS_API_KEY"
BASE_URL = "https://api.fireworks.ai/inference/v1"
GLM = "accounts/fireworks/models/glm-5p3"             # GLM-5.3: 743B mixture of experts
GPT_OSS = "accounts/fireworks/models/gpt-oss-120b"     # gpt-oss-120b: 116B mixture of experts, the smaller one
# serverless list prices per million tokens (input, output); cached input ($0.26 and $0.015) is priced as uncached,
# like the Claude runs, so these costs are an upper bound
USD_PER_MTOK = {GLM: (1.40, 4.40), GPT_OSS: (0.15, 0.60)}
RETRY_STATUS = {408, 409, 425, 429, 500, 502, 503, 504}
ATTEMPTS = 6
BACKOFF_S = 2.0           # then 4, 8, 16, 32
TIMEOUT_S = 300.0         # a reasoning model with 16K output tokens can take minutes
STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "content_filter": "refusal"}


def _get(block, key: str, default=None):
    """A content block's field, whether it is a dict (the loop's own messages) or a block from a response."""
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def _assistant(blocks: list) -> dict:
    text = "".join(_get(b, "text", "") for b in blocks if _get(b, "type") == "text")
    reasoning = "".join(_get(b, "thinking", "") for b in blocks if _get(b, "type") == "thinking")
    calls = [{"id": _get(b, "id"), "type": "function",
              # what the model sent, exactly, even when it was not valid JSON
              "function": {"name": _get(b, "name"), "arguments": _get(b, "arguments") or json.dumps(_get(b, "input"))}}
             for b in blocks if _get(b, "type") == "tool_use"]
    return {"role": "assistant", "content": text or None,
            **({"reasoning_content": reasoning} if reasoning else {}), **({"tool_calls": calls} if calls else {})}


def _turn(message: dict) -> list[dict]:
    content = message["content"]
    if isinstance(content, str):
        return [{"role": message["role"], "content": content}]
    if message["role"] == "assistant":
        return [_assistant(content)]
    # a user turn of tool results, then perhaps a note: each result is its own tool message, the note follows them
    results = [{"role": "tool", "tool_call_id": _get(b, "tool_use_id"), "content": _get(b, "content")}
               for b in content if _get(b, "type") == "tool_result"]
    note = "\n\n".join(_get(b, "text") for b in content if _get(b, "type") == "text")
    return [*results, *([{"role": "user", "content": note}] if note else [])]


def to_chat(*, model: str, max_tokens: int, system: str, tools: list[dict], messages: list[dict], **dropped) -> dict:
    """The Anthropic-style request as a chat completions body; `dropped` are the Claude-only fields."""
    return {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system}, *(m for message in messages for m in _turn(message))],
        "tools": [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                    "parameters": t["input_schema"]}} for t in tools],
    }


def _text(content) -> str:
    if isinstance(content, list):  # the API allows content parts
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def _tool_use(call: dict, index: int) -> NS:
    fn = call.get("function") or {}
    raw = fn.get("arguments")
    raw = raw if isinstance(raw, str) else json.dumps(raw)
    try:
        args = json.loads(raw)
    except json.JSONDecodeError:
        args = raw  # not an object: the loop answers with its usual error result
    return NS(type="tool_use", id=call.get("id") or f"call_{index}", name=fn.get("name") or "", input=args,
              arguments=raw)


def from_chat(body: dict) -> NS:
    """A chat completion as the response the loop reads: content blocks, stop_reason and usage."""
    choice = body["choices"][0]
    message = choice.get("message") or {}
    uses = [_tool_use(c, i) for i, c in enumerate(message.get("tool_calls") or [])]
    reasoning, text = message.get("reasoning_content") or "", _text(message.get("content"))
    content = [*([NS(type="thinking", thinking=reasoning)] if reasoning else []),
               *([NS(type="text", text=text)] if text else []), *uses]
    usage = body.get("usage") or {}
    return NS(content=content,
              stop_reason="tool_use" if uses else STOP_REASONS.get(choice.get("finish_reason"), "end_turn"),
              usage=NS(input_tokens=usage.get("prompt_tokens") or 0, output_tokens=usage.get("completion_tokens") or 0))


class APIError(Exception):
    def __init__(self, status: int, body: bytes):
        super().__init__(f"chat completions answered {status}: {body[:300].decode(errors='replace')}")
        self.status = status


def urllib_post(url: str, data: bytes, headers: dict, timeout: float) -> tuple[int, bytes]:
    """The stdlib transport: (status, body) for any HTTP answer; a dropped connection or timeout raises OSError."""
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class Client:
    """Stands in for `anthropic.Anthropic()` in `investigate.investigate(inc, client, tools, model)`. The stdlib
    only: app.py imports bench, and the deploy installs requirements.txt alone."""

    def __init__(self, api_key: str | None = None, base_url: str = BASE_URL, transport=urllib_post, sleep=time.sleep):
        key = api_key or os.environ.get(KEY_ENV)
        if not key:
            raise RuntimeError(f"{KEY_ENV} is not set")
        self._url = f"{base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self._transport, self._sleep = transport, sleep
        self.beta = NS(messages=NS(create=self.create))

    def create(self, **request) -> NS:
        return from_chat(self._post(to_chat(**request)))

    def _post(self, body: dict) -> dict:
        """POST with retries on rate limits, server errors and dropped connections; anything else raises at once."""
        data = json.dumps(body).encode()
        for attempt in range(ATTEMPTS):
            last = attempt == ATTEMPTS - 1
            try:
                status, answer = self._transport(self._url, data, self._headers, TIMEOUT_S)
            except (OSError, http.client.HTTPException):
                if last:
                    raise
            else:
                if status == 200:
                    return json.loads(answer)
                if status not in RETRY_STATUS or last:
                    raise APIError(status, answer)
            self._sleep(BACKOFF_S * 2**attempt)
        raise AssertionError("unreachable")
