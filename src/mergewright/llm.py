"""Model access behind one small interface.

- OpenAIClient: a live model on any OpenAI-compatible chat server, through the openai package.
- ScriptedLLM: returns pre-set responses in order. Used by replay mode and by the tests.
- RecordingLLM: wraps a live client and saves its responses, so a live run can be replayed.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import openai

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


class TurnReplayLLM:
    """Returns response n for the n-th assistant turn in the messages.

    The reply is a pure function of the input. A retried call, or the same call on a restarted
    worker, gets the same reply. ScriptedLLM counts calls in memory, so it cannot do that.
    """

    def __init__(self, responses: list[LLMResponse], model: str = "scripted") -> None:
        self.model = model
        self._responses = list(responses)

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> LLMResponse:
        turn = sum(1 for message in messages if message.get("role") == "assistant")
        if turn >= len(self._responses):
            raise LLMError("scripted responses exhausted")
        return self._responses[turn]

    @classmethod
    def from_file(cls, path: Path) -> TurnReplayLLM:
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
