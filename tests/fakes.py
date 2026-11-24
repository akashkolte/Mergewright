"""Shared test helpers. Test code only: never used to run untrusted code.

LocalSandbox runs commands as host subprocesses in the workspace, so graph and runner tests
get real pytest results on the trusted fixture repos without Docker. ScriptedSandbox returns
pre-set results and records every command, for unit tests of run_tests. The spectree-64
fixture constants, make_task and mark_valid build task directories; has_image gates the
Docker tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mergewright.oracle import VALIDATION_FILE, fingerprint
from mergewright.sandbox import Canary, ExecResult, SandboxError
from mergewright.tasks import Task, import_swe_rebench

ROW_FILE = Path(__file__).parent / "data" / "swe_rebench" / "0b01001001__spectree-64.json"
TASK_ID = "0b01001001__spectree-64"
DIGEST = "sha256:918c24cdc06282801eca6134c44d0521eceb98e395d2916c660ec92a0df75efd"
SPECTREE = "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64"


def has_image(image: str) -> bool:
    """True when the docker CLI is on PATH and the image is present locally."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    return probe.returncode == 0


def make_task(tasks_dir: Path, task_id: str) -> Task:
    """The spectree fixture row imported as a dev task under another id."""
    row = {**json.loads(ROW_FILE.read_text()), "instance_id": task_id}
    return Task.load(import_swe_rebench(row, tasks_dir, "dev", lambda image: DIGEST))


def mark_valid(task: Task, valid: bool = True) -> None:
    record = {"valid": valid, "fingerprint": fingerprint(task), "validated_at": "x"}
    (task.path / VALIDATION_FILE).write_text(json.dumps(record))


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
