"""Docker backend for the Sandbox protocol (spec Section 7, "Sandbox (Docker)").

One long-lived container per sandbox. Guards, all set at `docker run`:
no network; a non-root user with no capabilities and no privilege escalation; CPU, memory
(floor and ceiling) and PID limits; only the workspace mounted; no host environment, only
HOME and the planted canary; a run_id label for the orphan sweeper. Each command runs under
`timeout` inside the container, with a host-side backstop that destroys the container.

Git for reset() and diff() also runs inside the container (hooks off), so repo content
never makes the host execute anything.

Stated limit: containers share the host kernel; gVisor comes in Phase 7.
"""

from __future__ import annotations

import os
import re
import secrets
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path

from mergewright.sandbox.base import CanaryLeakError, ExecResult, SandboxError
from mergewright.sandbox.canary import Canary
from mergewright.sandbox.process import CommandResult, docker_cli

Docker = Callable[..., CommandResult]  # (args, input=None, timeout_s=None, max_output_bytes=None)

SANDBOX_LABEL = "mergewright.sandbox"
RUN_LABEL = "mergewright.run_id"
CREATED_LABEL = "mergewright.created"
HOME = "/tmp"
CANARY_ENV = "GITHUB_TOKEN"
CANARY_FILE = "$HOME/.git-credentials"
KILL_GRACE_S = 2  # SIGTERM first, SIGKILL this much later
BACKSTOP_S = 15  # host-side margin over a command's own timeout
DIFF_MAX_BYTES = 4_000_000
GIT = ["git", "-c", "safe.directory=*", "-c", "core.hooksPath=/dev/null"]
_COMMIT = re.compile(r"^[0-9a-f]{7,40}$")
_PS_FORMAT = "\t".join(  # name, run id, created: one line per sandbox
    ["{{.Names}}", f'{{{{.Label "{RUN_LABEL}"}}}}', f'{{{{.Label "{CREATED_LABEL}"}}}}']
)


@dataclass(frozen=True)
class SandboxLimits:
    """Per-sandbox resources. The memory floor is reserved; the ceiling is a hard limit."""

    cpus: float = 2.0
    mem_floor_mb: int = 1024
    mem_ceiling_mb: int = 4096
    pids: int = 512

    def __post_init__(self) -> None:
        if self.cpus <= 0 or self.pids <= 0 or self.mem_floor_mb <= 0:
            raise ValueError("cpus, pids and mem_floor_mb must be positive")
        if self.mem_floor_mb > self.mem_ceiling_mb:
            raise ValueError("mem_floor_mb must be at or below mem_ceiling_mb")


class DockerSandbox:
    """A Sandbox in one Docker container. Create it with DockerSandbox.start()."""

    def __init__(self, name: str, mount_path: str, canary: Canary, docker: Docker) -> None:
        self.name = name
        self.mount_path = mount_path
        self.canary = canary
        self._docker = docker
        self._base: str | None = None
        self._closed = False

    @classmethod
    def start(
        cls,
        image: str,
        workspace: Path,
        run_id: str,
        limits: SandboxLimits | None = None,
        mount_path: str = "/workspace",
        user: str | None = None,
        docker: Docker = docker_cli,
        clock: Callable[[], float] = time.time,
        canary: Canary | None = None,
    ) -> DockerSandbox:
        """Start a container for one run, with the host workspace mounted at mount_path."""
        user = user or _default_user()
        if user.split(":")[0] in ("0", "root"):
            raise SandboxError("the sandbox never runs as root")
        limits = limits or SandboxLimits()
        canary = canary or Canary.new()
        name = f"mw-{re.sub(r'[^a-zA-Z0-9_.-]', '-', run_id)}-{secrets.token_hex(3)}"
        args = [
            "run", "-d", "--name", name, "--init",
            "--label", f"{SANDBOX_LABEL}=1",
            "--label", f"{RUN_LABEL}={run_id}",
            "--label", f"{CREATED_LABEL}={int(clock())}",
            "--network", "none",
            "--user", user,
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--cpus", f"{limits.cpus:g}",
            "--memory-reservation", f"{limits.mem_floor_mb}m",
            "--memory", f"{limits.mem_ceiling_mb}m",
            "--memory-swap", f"{limits.mem_ceiling_mb}m",  # same as --memory: no swap
            "--pids-limit", str(limits.pids),
            "-v", f"{Path(workspace).resolve()}:{mount_path}:rw",
            "-w", mount_path,
            "-e", f"HOME={HOME}",
            "-e", f"{CANARY_ENV}={canary.value}",
            "--entrypoint", "sleep",
            image, "infinity",
        ]  # fmt: skip
        started = docker(args)
        if started.code != 0:
            raise SandboxError(f"docker run failed: {started.output.strip()[-1000:]}")
        sandbox = cls(name, mount_path, canary, docker)
        planted = docker(
            ["exec", "-i", name, "sh", "-c", f'umask 077 && cat > "{CANARY_FILE}"'],
            input=f"https://x-access-token:{canary.value}@github.com\n",
        )
        if planted.code != 0:
            sandbox.close()
            raise SandboxError(f"could not plant the canary: {planted.output.strip()[-500:]}")
        return sandbox

    def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        self._require_open()
        timed = ["timeout", f"--kill-after={KILL_GRACE_S}", f"{timeout_s:g}", *argv]
        result = self._docker(
            ["exec", "-w", self.mount_path, self.name, *timed],
            timeout_s=timeout_s + KILL_GRACE_S + BACKSTOP_S,
        )
        output, leaked = self.canary.redact(result.output)
        if result.timed_out:  # the in-container timeout failed to stop it: destroy the sandbox
            self.close()
            return ExecResult(-1, output, True, result.truncated, leaked, result.seconds)
        # timeout exits 124, or 137 if it had to SIGKILL; 137 before the deadline is another
        # kill, most likely the memory ceiling.
        timed_out = result.code == 124 or (result.code == 137 and result.seconds >= timeout_s)
        return ExecResult(result.code, output, timed_out, result.truncated, leaked, result.seconds)

    def reset(self, base_sha: str, patch: str) -> None:
        if not _COMMIT.match(base_sha):
            raise SandboxError(f"base must be a commit hash, got {base_sha!r}")
        self._git(["checkout", "--force", "--detach", base_sha])
        self._git(["clean", "-ffdxq"])
        if patch:
            self._git(["apply", "--whitespace=nowarn"], input=patch)
        self._base = base_sha

    def diff(self) -> str:
        if self._base is None:
            raise SandboxError("diff() needs a reset() first, to know the base commit")
        self._git(["add", "-A"])
        result = self._git(
            ["diff", "--cached", "--binary", self._base], max_output_bytes=DIFF_MAX_BYTES
        )
        if result.truncated:
            raise SandboxError(f"diff is larger than {DIFF_MAX_BYTES} bytes")
        if self.canary.found_in(result.output):
            raise CanaryLeakError("the canary is in the diff; it must never leave the sandbox")
        return result.output

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._docker(["rm", "-f", self.name])

    def _git(
        self, args: list[str], input: str | None = None, max_output_bytes: int | None = None
    ) -> CommandResult:
        self._require_open()
        exec_args = ["exec", *(["-i"] if input is not None else []), "-w", self.mount_path]
        result = self._docker(
            [*exec_args, self.name, *GIT, *args],
            input=input,
            timeout_s=120,
            max_output_bytes=max_output_bytes,
        )
        if result.code != 0:
            output, _ = self.canary.redact(result.output)
            raise SandboxError(f"git {args[0]} failed: {output.strip()[-1000:]}")
        return result

    def _require_open(self) -> None:
        if self._closed:
            raise SandboxError(f"sandbox {self.name} is closed")


def sweep_orphans(
    max_age_s: float,
    active_run_ids: Collection[str] = (),
    docker: Docker = docker_cli,
    clock: Callable[[], float] = time.time,
) -> list[str]:
    """Remove sandboxes at least max_age_s old whose run is not active. Returns their names.

    A sandbox whose age cannot be read is kept: never delete what cannot be dated.
    """
    listing = docker(["ps", "-a", "--filter", f"label={SANDBOX_LABEL}=1", "--format", _PS_FORMAT])
    if listing.code != 0:
        raise SandboxError(f"docker ps failed: {listing.output.strip()[-500:]}")
    removed = []
    now = clock()
    for line in listing.output.splitlines():
        name, run_id, created = (line.split("\t") + ["", ""])[:3]
        if not name or run_id in active_run_ids:
            continue
        try:
            age = now - float(created)
        except ValueError:
            continue
        if age >= max_age_s:
            docker(["rm", "-f", name])
            removed.append(name)
    return removed


def _default_user() -> str:
    """The host user (so the mounted workspace stays writable), or nobody if that is root."""
    uid, gid = os.getuid(), os.getgid()
    return f"{uid}:{gid}" if uid != 0 else "65534:65534"


def copy_from_image(
    image: str,
    src_path: str,
    dest: Path,
    docker: Docker = docker_cli,
    clock: Callable[[], float] = time.time,
) -> None:
    """Copy src_path (e.g. a task image's /testbed, with its .git) out of image into dest.

    Uses a created, never-started container, which is always removed. It carries the sandbox
    labels, so sweep_orphans removes it if a crash skips the removal. dest must not exist.
    """
    if dest.exists():
        raise SandboxError(f"{dest} already exists")
    dest.parent.mkdir(parents=True, exist_ok=True)
    name = f"mw-copy-{secrets.token_hex(4)}"
    args = [
        "create", "--name", name,
        "--label", f"{SANDBOX_LABEL}=1",
        "--label", f"{RUN_LABEL}=copy",
        "--label", f"{CREATED_LABEL}={int(clock())}",
        image,
    ]  # fmt: skip
    created = docker(args)
    if created.code != 0:
        raise SandboxError(f"docker create failed for {image}: {created.output.strip()[-500:]}")
    try:
        copied = docker(["cp", f"{name}:{src_path}", str(dest)], timeout_s=600)
        if copied.code != 0:
            raise SandboxError(f"docker cp failed: {copied.output.strip()[-500:]}")
    finally:
        docker(["rm", "-f", name])
