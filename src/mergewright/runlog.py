"""The run log: one JSON line per event in runs/<run_id>/run.jsonl, plus a short console line."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_FIELD_CHARS = 2000


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_FIELD_CHARS:
        return value[:MAX_FIELD_CHARS] + "...[clipped]"
    return value


class RunLog:
    def __init__(self, path: Path, echo: Callable[[str], None] | None = print) -> None:
        self.path = path
        self._echo = echo
        # Set once the sandbox exists: every string in every later event passes through it
        # (spec Section 8: the canary never reaches the logs).
        self.redact: Callable[[str], str] | None = None
        path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, kind: str, **fields: Any) -> None:
        if self.redact:
            fields = {key: _apply(self.redact, value) for key, value in fields.items()}
        record = {"ts": datetime.now(UTC).isoformat(), "event": kind}
        record.update({key: _clip(value) for key, value in fields.items()})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        if self._echo:
            self._echo(_summary(kind, fields))

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.path.read_text().splitlines()]


def _apply(redact: Callable[[str], str], value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [_apply(redact, item) for item in value]
    if isinstance(value, dict):
        return {key: _apply(redact, item) for key, item in value.items()}
    return value


def _summary(kind: str, fields: dict[str, Any]) -> str:
    if kind == "tool_call":
        status = "ok" if fields.get("ok") else "error"
        return f"[tool] {fields.get('tool')}({fields.get('args')}) -> {status}"
    if kind == "llm_call":
        return f"[llm]  step {fields.get('step')}: {fields.get('tool_calls') or 'final answer'}"
    if kind == "test_result":
        return (
            f"[test] attempt {fields.get('attempt')}: passed={fields.get('num_passed')} "
            f"failed={fields.get('num_failed')}"
        )
    details = " ".join(f"{k}={v}" for k, v in fields.items() if k not in {"issue", "diff"})
    return f"[run]  {kind} {details}".rstrip()
