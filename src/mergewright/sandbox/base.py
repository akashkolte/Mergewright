"""The Sandbox contract (spec Section 4.5). Repo code and tests run only through it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mergewright.sandbox.canary import Canary


class SandboxError(Exception):
    """The sandbox could not do what was asked. The message says why."""


class CanaryLeakError(SandboxError):
    """The planted canary was found where it must never be (for example, in the diff)."""


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    output: str  # stdout + stderr, last bytes only, canary redacted
    timed_out: bool
    canary_leaked: bool = False  # the canary was in the output (and was redacted)


class Sandbox(Protocol):
    canary: Canary  # planted in every sandbox (spec Section 7); callers redact with it

    def reset(self, base_sha: str, patch: str) -> None:
        """Make the working tree exactly base_sha + patch (spec Section 4.2 invariant)."""
        ...

    def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        """Run one command in the workspace. Never raises for a failing command."""
        ...

    def diff(self) -> str:
        """The working tree's changes against base_sha, new files included."""
        ...

    def close(self) -> None:
        """Destroy the sandbox. Safe to call more than once."""
        ...
