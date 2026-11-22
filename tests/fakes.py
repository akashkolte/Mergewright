"""Test doubles for the Sandbox protocol. Test code only: never used to run untrusted code.

LocalSandbox runs commands as host subprocesses in the workspace, so graph and runner tests
get real pytest results on the trusted fixture repos without Docker. ScriptedSandbox returns
pre-set results and records every command, for unit tests of run_tests.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from mergewright.sandbox import Canary, ExecResult, SandboxError


class LocalSandbox:
    def __init__(self, root: Path, canary: Canary | None = None) -> None:
        self.root = root
        self.canary = canary or Canary.new()
        self.closed = False
        self.commands: list[list[str]] = []

    def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        if self.closed:
            raise SandboxError("sandbox is closed")
        self.commands.append(list(argv))
        # The venv's python first, so `python -m pytest` finds pytest; no other host env.
        env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(self.root)}
        begin = time.monotonic()
        try:
            proc = subprocess.run(
                argv, cwd=self.root, env=env, capture_output=True, text=True, timeout=timeout_s
            )
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", True, seconds=timeout_s)
        output, leaked = self.canary.redact(proc.stdout + proc.stderr)
        return ExecResult(
            proc.returncode, output, False, canary_leaked=leaked, seconds=time.monotonic() - begin
        )

    def reset(self, base_sha: str, patch: str) -> None:
        raise NotImplementedError("LocalSandbox does not manage git state")

    def diff(self) -> str:
        raise NotImplementedError("LocalSandbox does not manage git state")

    def close(self) -> None:
        self.closed = True


class ScriptedSandbox:
    """Answers each exec with the next pre-set result; records commands and resets."""

    def __init__(self, results: list[ExecResult], diff: str = "") -> None:
        self.canary = Canary.new()
        self.results = list(results)
        self.commands: list[list[str]] = []
        self.resets: list[tuple[str, str]] = []
        self._diff = diff
        self.closed = False

    def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        self.commands.append(list(argv))
        return self.results.pop(0)

    def reset(self, base_sha: str, patch: str) -> None:
        self.resets.append((base_sha, patch))

    def diff(self) -> str:
        return self._diff

    def close(self) -> None:
        self.closed = True


def local_start(**kwargs) -> LocalSandbox:
    """Stands in for DockerSandbox.start in environments: a LocalSandbox on the workspace."""
    return LocalSandbox(Path(kwargs["workspace"]))
