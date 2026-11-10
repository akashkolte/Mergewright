"""Model access behind one small interface.

- LiteLLMClient: a live model. For an OpenAI-compatible server, the model string is
  openai/<name> and api_base points at the server.
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

from mergewright.config import LiveSettings


class LLMError(Exception):
    """The model could not be reached or returned something unusable."""


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


class LiteLLMClient:
    """A live model through LiteLLM, with a few retries and exponential backoff."""

    def __init__(
        self,
        settings: LiveSettings,
        timeout_s: float,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = settings.model
        self._settings = settings
        self._timeout_s = timeout_s
        self._retries = retries
        self._sleep = sleep

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        import litellm  # imported here: slow to import, and not needed in replay mode or tests

        litellm.suppress_debug_info = True
        last_error: Exception | None = None
        for attempt in range(self._retries + 1):
            try:
                raw = litellm.completion(
                    model=self._settings.model,
                    api_base=self._settings.api_base,
                    api_key=self._settings.api_key,
                    messages=messages,
                    tools=tools,
                    temperature=0,
                    timeout=self._timeout_s,
                )
                return _parse_litellm(raw)
            except Exception as exc:  # LiteLLM maps provider errors to many exception types
                last_error = exc
                if attempt < self._retries:
                    self._sleep(2**attempt)
        raise LLMError(f"model call failed after {self._retries + 1} attempts: {last_error}")


def _parse_litellm(raw: Any) -> LLMResponse:
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
        data = json.loads(path.read_text())
        return cls([LLMResponse.from_dict(r) for r in data["responses"]], model=data["model"])


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
