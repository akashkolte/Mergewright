"""Shared test helpers. Test code only: never used to run untrusted code.

LocalSandbox runs commands as host subprocesses in the workspace, so graph and runner tests
get real pytest results on the trusted fixture repos without Docker; its reset() and diff() run
git on the host as DockerSandbox does in the container. ScriptedSandbox returns
pre-set results and records every command, for unit tests of run_tests. The spectree-64
fixture constants, make_task and mark_valid build task directories; has_image gates the
Docker tests.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from mergewright.oracle import VALIDATION_FILE, fingerprint
from mergewright.sandbox import Canary, CanaryLeakError, ExecResult, SandboxError
from mergewright.sandbox.docker import GIT
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
        self._base: str | None = None
        # The venv's python first, so `python -m pytest` finds pytest; no other host env, so the
        # host's git config (e.g. diff.noprefix) does not change the diff either.
        self._env = {"PATH": f"{Path(sys.executable).parent}:/usr/bin:/bin", "HOME": str(root)}

    def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        if self.closed:
            raise SandboxError("sandbox is closed")
        self.commands.append(list(argv))
        try:
            proc = subprocess.run(
                argv,
                cwd=self.root,
                env=self._env,
                capture_output=True,
                text=True,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", True)
        output, leaked = self.canary.redact(proc.stdout + proc.stderr)
        return ExecResult(proc.returncode, output, False, canary_leaked=leaked)

    def reset(self, base_sha: str, patch: str) -> None:
        self._git("checkout", "--force", "--detach", base_sha)
        self._git("clean", "-ffdq")
        if patch:
            self._git("apply", "--whitespace=nowarn", input=patch)
        self._base = base_sha

    def diff(self) -> str:
        if self._base is None:
            raise SandboxError("diff() needs a reset() first, to know the base commit")
        self._git("add", "-A")
        diff = self._git("diff", "--cached", "--binary", self._base)
        if self.canary.found_in(diff):
            raise CanaryLeakError("the canary is in the diff; it must never leave the sandbox")
        return diff

    def _git(self, *args: str, input: str | None = None) -> str:
        proc = subprocess.run(
            [*GIT, *args], cwd=self.root, env=self._env, input=input, capture_output=True, text=True
        )
        if proc.returncode != 0:
            raise SandboxError(f"git {args[0]} failed: {proc.stderr.strip()[-1000:]}")
        return proc.stdout

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
        if self.canary.found_in(self._diff):
            raise CanaryLeakError("the canary is in the diff; it must never leave the sandbox")
        return self._diff

    def close(self) -> None:
        self.closed = True


def local_start(**kwargs) -> LocalSandbox:
    """Stands in for DockerSandbox.start in environments: a LocalSandbox on the workspace."""
    return LocalSandbox(Path(kwargs["workspace"]))
