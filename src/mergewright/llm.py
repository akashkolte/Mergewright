"""Model access behind one small interface.

- OpenAIClient: a live model on any OpenAI-compatible chat server, through the openai package.
- BedrockConverseClient: a live model on Amazon Bedrock Runtime's Converse API (every Bedrock
  model, Anthropic's included). The agent's history stays in OpenAI chat format; the client
  translates it to Converse and back.
- live_client: picks one of the two from LiveSettings.api_style.
- ScriptedLLM: returns pre-set responses in order. Used by replay mode and by the tests.
- RecordingLLM: wraps a live client and saves its responses, so a live run can be replayed.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import httpx
import openai

from mergewright.config import LiveSettings


class LLMError(Exception):
    """The model could not be reached or returned something unusable."""


class LLMUnavailable(LLMError):
    """The endpoint failed the call after the client's retries: connection error, timeout, 408,
    429 or 5xx (spec 9.1 FAILED_INFRA, not the agent's failure). Other 4xx (a wrong key or model
    name) and unusable replies stay LLMError: they are configuration or model faults."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON text from the model; validated by the tool registry


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0

    def as_message(self) -> dict[str, Any]:
        """The assistant message in OpenAI chat format, to append to the history."""
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.arguments},
                }
                for call in self.tool_calls
            ]
        return message

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LLMResponse:
        calls = [ToolCall(**call) for call in data.get("tool_calls", [])]
        return cls(
            content=data.get("content"),
            tool_calls=calls,
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
        )


class LLMClient(Protocol):
    model: str

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        """Return the model's next message: text, tool calls, or both."""
        ...


_RETRY_STATUS = {408, 429, 500, 502, 503, 504}  # the openai SDK retries the same statuses


class OpenAIClient:
    """A live model on an OpenAI-compatible server (POST {base_url}/chat/completions).

    The openai SDK retries connection errors, timeouts, 429 and 5xx with backoff. It does not
    retry other 4xx errors, such as a wrong model name or key, because they cannot succeed.
    """

    def __init__(
        self, settings: LiveSettings, timeout_s: float, retries: int = 2, client: Any = None
    ) -> None:
        self.model = settings.model
        self.served_models: set[str] = set()  # the model IDs the server reported
        self._client = client or openai.OpenAI(
            base_url=settings.api_base,
            api_key=settings.api_key or "unused",  # some local servers need no key
            timeout=timeout_s,
            max_retries=retries,
        )

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        try:
            raw = self._client.chat.completions.create(
                model=self.model, messages=messages, tools=tools, temperature=0
            )
        except (openai.APIConnectionError, openai.APIStatusError) as exc:  # timeouts included
            status = getattr(exc, "status_code", None)
            unavailable = status is None or status in _RETRY_STATUS
            raise (LLMUnavailable if unavailable else LLMError)(
                f"model call failed: {exc}"
            ) from exc
        except openai.OpenAIError as exc:
            raise LLMError(f"model call failed: {exc}") from exc
        if not raw.choices:
            raise LLMError("model returned no choices")
        if getattr(raw, "model", None):
            self.served_models.add(raw.model)
        return _parse_completion(raw)


def _parse_completion(raw: Any) -> LLMResponse:
    message = raw.choices[0].message
    calls = [
        ToolCall(id=call.id, name=call.function.name, arguments=call.function.arguments or "{}")
        for call in (message.tool_calls or [])
    ]
    usage = getattr(raw, "usage", None)
    return LLMResponse(
        content=message.content,
        tool_calls=calls,
        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
    )


CONVERSE_MAX_TOKENS = 8192  # output cap per call; Converse needs one, and edits can be long


class BedrockConverseClient:
    """A live model on Amazon Bedrock Runtime (POST {api_base}/model/{model}/converse).

    The key is sent as a Bearer token (a Bedrock API key). Timeouts, connection errors, 408,
    429 and 5xx are retried with backoff, as the openai SDK does; other 4xx (a wrong model id,
    a refused key) are not, because they cannot succeed. Converse does not report a model ID,
    so served_models stays empty.
    """

    def __init__(
        self,
        settings: LiveSettings,
        timeout_s: float,
        retries: int = 2,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = settings.model
        self.served_models: set[str] = set()
        self._url = f"{(settings.api_base or '').rstrip('/')}/model/{quote(self.model, safe='')}"
        self._headers = {"Authorization": f"Bearer {settings.api_key or 'unused'}"}
        self._http = http or httpx.Client(timeout=timeout_s)
        self._retries = retries
        self._sleep = sleep

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        body = converse_request(messages, tools)
        for attempt in range(self._retries + 1):
            try:
                reply = self._http.post(f"{self._url}/converse", json=body, headers=self._headers)
            except httpx.HTTPError as exc:  # timeout or connection error
                error = f"model call failed: {exc!r}"
            else:
                if reply.status_code == 200:
                    return converse_response(reply.json())
                error = f"model call failed: HTTP {reply.status_code}: {reply.text[:300]}"
                if reply.status_code not in _RETRY_STATUS:
                    raise LLMError(error)
            if attempt < self._retries:
                self._sleep(2.0 * 2**attempt)
        raise LLMUnavailable(error)  # only retryable failures reach this line


def converse_request(messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict:
    """OpenAI chat messages and tool schemas -> a Converse request body.

    System messages go to `system`; tool results become user turns with toolResult blocks;
    consecutive turns of one role are merged, because Converse needs user and assistant turns
    to alternate.
    """
    system = [{"text": m["content"]} for m in messages if m["role"] == "system" and m["content"]]
    turns: list[dict[str, Any]] = []
    for message in messages:
        role, blocks = _converse_blocks(message)
        if not blocks:
            continue
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})
    body: dict[str, Any] = {
        "messages": turns,
        "inferenceConfig": {"temperature": 0, "maxTokens": CONVERSE_MAX_TOKENS},
    }
    if system:
        body["system"] = system
    if tools:
        body["toolConfig"] = {
            "tools": [
                {
                    "toolSpec": {
                        "name": tool["function"]["name"],
                        "description": tool["function"]["description"],
                        "inputSchema": {"json": tool["function"]["parameters"]},
                    }
                }
                for tool in tools
            ]
        }
    return body


def _converse_blocks(message: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    role = message["role"]
    if role == "system":
        return role, []
    if role == "tool":
        result = {"toolUseId": message["tool_call_id"], "content": [{"text": message["content"]}]}
        return "user", [{"toolResult": result}]
    blocks: list[dict[str, Any]] = [{"text": message["content"]}] if message.get("content") else []
    for call in message.get("tool_calls", []):
        blocks.append(
            {
                "toolUse": {
                    "toolUseId": call["id"],
                    "name": call["function"]["name"],
                    "input": _json_object(call["function"]["arguments"]),
                }
            }
        )
    return role, blocks


def _json_object(arguments: str) -> dict[str, Any]:
    """Converse needs an object; arguments the model sent as invalid JSON are kept as text."""
    try:
        value = json.loads(arguments or "{}")
    except json.JSONDecodeError:
        return {"invalid_json": arguments}
    return value if isinstance(value, dict) else {"value": value}


def converse_response(data: dict[str, Any]) -> LLMResponse:
    blocks = data.get("output", {}).get("message", {}).get("content", [])
    if not isinstance(blocks, list):
        raise LLMError("model returned no message")
    text = "".join(block["text"] for block in blocks if "text" in block)
    calls = [
        ToolCall(
            id=block["toolUse"]["toolUseId"],
            name=block["toolUse"]["name"],
            arguments=json.dumps(block["toolUse"].get("input", {})),
        )
        for block in blocks
        if "toolUse" in block
    ]
    usage = data.get("usage", {})
    return LLMResponse(
        content=text or None,
        tool_calls=calls,
        prompt_tokens=usage.get("inputTokens", 0),
        completion_tokens=usage.get("outputTokens", 0),
    )


def live_client(settings: LiveSettings, timeout_s: float) -> LLMClient:
    """The live client for settings.api_style. The agent sees only the LLMClient interface."""
    if settings.api_style == "bedrock-converse":
        return BedrockConverseClient(settings, timeout_s=timeout_s)
    return OpenAIClient(settings, timeout_s=timeout_s)


class ScriptedLLM:
    """Returns the given responses in order, whatever the input. Fails when they run out."""

    def __init__(self, responses: list[LLMResponse], model: str = "scripted") -> None:
        self.model = model
        self._responses = list(responses)
        self.calls = 0

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        if self.calls >= len(self._responses):
            raise LLMError("scripted responses exhausted")
        response = self._responses[self.calls]
        self.calls += 1
        return response

    @classmethod
    def from_file(cls, path: Path) -> ScriptedLLM:
        return cls(*_read_replay(path))


def _read_replay(path: Path) -> tuple[list[LLMResponse], str]:
    """Read a replay file written by RecordingLLM: the responses and the model name."""
    data = json.loads(path.read_text())
    return [LLMResponse.from_dict(r) for r in data["responses"]], data["model"]


class RecordingLLM:
    """Passes calls to a live client and saves every response to a replay file."""

    def __init__(self, inner: LLMClient, path: Path) -> None:
        self.model = inner.model
        self._inner = inner
        self._path = path
        self._responses: list[dict[str, Any]] = []

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        response = self._inner.complete(messages, tools)
        self._responses.append(asdict(response))
        payload = {"model": f"replay of {self.model}", "responses": self._responses}
        self._path.write_text(json.dumps(payload, indent=2) + "\n")
        return response
