"""Where a run's code comes from and where its tests run.

An Environment fills the host workspace, starts the sandbox with the workspace mounted, and
produces the final diff. Two kinds:

- LocalRepo: a repo folder on the host (e.g. examples/buggy_calc), run in DEFAULT_IMAGE. With a
  base_sha (local eval tasks), the sandbox commits the copy deterministically and checks that
  the commit is base_sha, so the oracle can reset to it as it does for an image.
- ImageRepo: a task image (SWE-rebench). The repo is copied out of the image, with its .git,
  and mounted back at the same path, so the image's installed environment applies.

The file tools always work on the host workspace (spec Section 3, "File tools vs execution");
every command, tests included, runs in the sandbox.
"""

from __future__ import annotations

import difflib
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from mergewright.sandbox import (
    DockerSandbox,
    Sandbox,
    SandboxError,
    SandboxLimits,
    copy_from_image,
)
from mergewright.sandbox.docker import GIT, Docker
from mergewright.sandbox.process import docker_cli
from mergewright.tools import DEFAULT_TEST_COMMAND, SKIP_DIRS

DEFAULT_IMAGE = "mergewright-python:1"  # docker/python/Dockerfile
CONDA_ACTIVATE = "source /opt/conda/etc/profile.d/conda.sh && conda activate testbed"
# A fixed author, committer and date: the same files always give the same base commit.
BASE_COMMIT_DATE = "2026-01-01T00:00:00Z"
_BASE_COMMIT = [
    "env", f"GIT_AUTHOR_DATE={BASE_COMMIT_DATE}", f"GIT_COMMITTER_DATE={BASE_COMMIT_DATE}",
    *GIT, "-c", "user.name=mergewright", "-c", "user.email=mergewright@localhost",
    "-c", "commit.gpgsign=false", "commit", "-q", "--no-verify", "-m", "base",
]  # fmt: skip

StartSandbox = Callable[..., Sandbox]  # DockerSandbox.start's keyword interface


class Environment(Protocol):
    name: str
    test_command: str

    def prepare(self, workspace_dir: Path) -> None:
        """Fill a new, empty workspace."""
        ...

    def start(self, workspace_dir: Path, run_id: str) -> Sandbox:
        """Start a sandbox with the workspace mounted. The caller closes it."""
        ...

    def diff(self, workspace_dir: Path, sandbox: Sandbox) -> str:
        """The run's changes. Call before closing the sandbox."""
        ...


@dataclass(frozen=True)
class LocalRepo:
    path: Path
    image: str = DEFAULT_IMAGE
    test_command: str = DEFAULT_TEST_COMMAND
    limits: SandboxLimits | None = None
    start_sandbox: StartSandbox = DockerSandbox.start
    base_sha: str | None = None  # set: the copy is committed in the sandbox and must match

    @property
    def name(self) -> str:
        return str(self.path)

    def prepare(self, workspace_dir: Path) -> None:
        copy_workspace(self.path, workspace_dir)

    def start(self, workspace_dir: Path, run_id: str) -> Sandbox:
        sandbox = self.start_sandbox(
            image=self.image,
            workspace=workspace_dir,
            run_id=run_id,
            limits=self.limits,
            mount_path="/workspace",
        )
        if self.base_sha is None:
            return sandbox
        try:
            actual = commit_workspace(sandbox)
            if actual != self.base_sha:
                raise SandboxError(
                    f"{self.path} commits as {actual}, but the task's base_sha is {self.base_sha}"
                )
            sandbox.reset(self.base_sha, "")
        except BaseException:
            sandbox.close()
            raise
        return sandbox

    def diff(self, workspace_dir: Path, sandbox: Sandbox) -> str:
        if self.base_sha is not None:
            return sandbox.diff()
        return workspace_diff(self.path, workspace_dir)


@dataclass(frozen=True)
class ImageRepo:
    image: str  # by digest for evals
    base_sha: str
    test_command: str  # the exact command to run in the image, e.g. conda_test_command(...)
    repo_dir: str = "/testbed"
    limits: SandboxLimits | None = None
    start_sandbox: StartSandbox = DockerSandbox.start
    docker: Docker = docker_cli

    @property
    def name(self) -> str:
        return self.image

    def prepare(self, workspace_dir: Path) -> None:
        copy_from_image(self.image, self.repo_dir, workspace_dir, self.docker)

    def start(self, workspace_dir: Path, run_id: str) -> Sandbox:
        sandbox = self.start_sandbox(
            image=self.image,
            workspace=workspace_dir,
            run_id=run_id,
            limits=self.limits,
            mount_path=self.repo_dir,
        )
        try:
            sandbox.reset(self.base_sha, "")  # the tree is exactly base_sha, and diff() knows it
        except BaseException:
            sandbox.close()
            raise
        return sandbox

    def diff(self, workspace_dir: Path, sandbox: Sandbox) -> str:
        return sandbox.diff()


def conda_test_command(test_cmd: str) -> str:
    """A SWE-rebench task's test command, run in the image's `testbed` conda env."""
    return f"{CONDA_ACTIVATE} && {test_cmd} --continue-on-collection-errors"


def commit_workspace(sandbox: Sandbox) -> str:
    """Make the sandbox's workspace a git repo with one deterministic commit; return its SHA.

    Git runs in the sandbox with hooks off, like reset() and diff().
    """
    steps = {
        "init": [*GIT, "init", "-q"],
        "add": [*GIT, "add", "-A"],
        "commit": _BASE_COMMIT,
        "rev-parse": [*GIT, "rev-parse", "HEAD"],
    }
    output = ""
    for step, argv in steps.items():
        result = sandbox.exec(argv, timeout_s=120)
        output = result.output.strip()
        if result.exit_code != 0 or result.timed_out:
            raise SandboxError(f"git {step} failed: {output[-500:]}")
    return output


def copy_workspace(repo: Path, workspace_dir: Path) -> None:
    """Copy a host repo into a fresh workspace for one run. The agent edits only the copy."""
    # symlinks=True copies links as links. The default copies the target's content, which would
    # bring files from outside the repo into the workspace, past the tools' path guard.
    shutil.copytree(repo, workspace_dir, symlinks=True, ignore=shutil.ignore_patterns(*SKIP_DIRS))


def workspace_diff(original: Path, changed: Path) -> str:
    """Unified diff of every text file that differs between the two trees."""

    def files(root: Path) -> set[str]:
        return {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)
        }

    chunks: list[str] = []
    for rel in sorted(files(original) | files(changed)):
        before = _lines(original / rel)
        after = _lines(changed / rel)
        if before != after:
            chunks.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
    return "".join(chunks)


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
