"""Sandbox unit tests: canary, bounded command runner, docker arguments, sweeper. No Docker."""

import sys
from pathlib import Path

import pytest

from mergewright.sandbox import (
    Canary,
    CanaryLeakError,
    DockerSandbox,
    SandboxError,
    SandboxLimits,
    sweep_orphans,
)
from mergewright.sandbox.process import CommandResult, run_command

# --- Canary -------------------------------------------------------------------------------


def test_each_canary_is_unique_and_looks_like_a_token():
    first, second = Canary.new(), Canary.new()

    assert first.value != second.value
    assert first.value.startswith("ghp_mwcanary")


def test_canary_redact_replaces_every_copy_and_reports_the_leak():
    canary = Canary("ghp_mwcanaryabc")

    text, leaked = canary.redact("token=ghp_mwcanaryabc and again ghp_mwcanaryabc")

    assert leaked and canary.value not in text
    assert text == f"token={Canary.REDACTED} and again {Canary.REDACTED}"
    assert canary.redact("clean output") == ("clean output", False)


# --- Bounded command runner ------------------------------------------------------------------


def test_run_command_keeps_only_the_output_tail():
    script = "import sys; sys.stdout.write('a' * 5000 + 'END')"

    result = run_command([sys.executable, "-c", script], max_output_bytes=100)

    assert result.code == 0 and result.truncated
    assert result.output.endswith("END") and len(result.output) == 100


def test_run_command_kills_the_process_on_timeout():
    result = run_command([sys.executable, "-c", "import time; time.sleep(30)"], timeout_s=0.5)

    assert result.timed_out and result.seconds < 5


def test_run_command_passes_input_and_merges_stderr():
    script = "import sys; print(sys.stdin.read().upper()); print('err', file=sys.stderr)"

    result = run_command([sys.executable, "-c", script], input="patch")

    assert "PATCH" in result.output and "err" in result.output


# --- Docker sandbox, with a fake docker ------------------------------------------------------


class FakeDocker:
    """Records docker CLI calls; answers from a list of (match, CommandResult)."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, input=None, timeout_s=None, max_output_bytes=None):
        self.calls.append((list(args), input))
        command = " ".join(args)
        for match, result in self.answers:
            if match in command:
                return result
        return CommandResult(code=0, output="", timed_out=False, truncated=False, seconds=0.1)

    def commands(self) -> list[str]:
        return [" ".join(args) for args, _ in self.calls]


def ok(output="", code=0, seconds=0.1, timed_out=False):
    return CommandResult(
        code=code, output=output, timed_out=timed_out, truncated=False, seconds=seconds
    )


CANARY = Canary("ghp_mwcanaryfixed")
LIMITS = SandboxLimits(cpus=1.5, mem_floor_mb=512, mem_ceiling_mb=2048, pids=128)


def start(docker, tmp_path: Path, **kwargs) -> DockerSandbox:
    options = {"limits": LIMITS, "user": "501:20", "canary": CANARY, "clock": lambda: 1000.0}
    return DockerSandbox.start(
        "img@sha256:abc", tmp_path, run_id="run1", docker=docker, **{**options, **kwargs}
    )


def test_start_applies_every_guard_to_docker_run(tmp_path: Path):
    docker = FakeDocker()

    start(docker, tmp_path)

    run_args = docker.calls[0][0]
    command = " ".join(run_args)
    for guard in (
        "--network none",
        "--user 501:20",
        "--cap-drop ALL",
        "--security-opt no-new-privileges",
        "--cpus 1.5",
        "--memory-reservation 512m",
        "--memory 2048m",
        "--memory-swap 2048m",
        "--pids-limit 128",
        "--init",
        "--label mergewright.sandbox=1",
        "--label mergewright.run_id=run1",
        "--label mergewright.created=1000",
    ):
        assert guard in command
    assert run_args.count("-v") == 1
    assert f"{tmp_path.resolve()}:/workspace:rw" in run_args
    env = [run_args[i + 1] for i, arg in enumerate(run_args) if arg == "-e"]
    assert env == ["HOME=/tmp", f"GITHUB_TOKEN={CANARY.value}"]  # nothing from the host


def test_start_plants_the_canary_file_through_stdin(tmp_path: Path):
    docker = FakeDocker()

    start(docker, tmp_path)

    args, stdin = docker.calls[1]
    assert args[0] == "exec" and ".git-credentials" in " ".join(args)
    assert CANARY.value not in " ".join(args)  # not on the command line
    assert CANARY.value in stdin


@pytest.mark.parametrize("user", ["0:0", "0", "root", "root:root"])
def test_start_refuses_to_run_as_root(tmp_path: Path, user: str):
    docker = FakeDocker()

    with pytest.raises(SandboxError, match="root"):
        start(docker, tmp_path, user=user)
    assert docker.calls == []


def test_limits_need_a_floor_at_or_below_the_ceiling():
    with pytest.raises(ValueError, match="floor"):
        SandboxLimits(mem_floor_mb=4096, mem_ceiling_mb=1024)


def test_start_fails_cleanly_when_docker_run_fails(tmp_path: Path):
    docker = FakeDocker([("run -d", ok("no such image", code=125))])

    with pytest.raises(SandboxError, match="no such image"):
        start(docker, tmp_path)


def test_exec_runs_under_timeout_in_the_workspace(tmp_path: Path):
    docker = FakeDocker([("echo hi", ok("hi\n"))])
    sandbox = start(docker, tmp_path)

    result = sandbox.exec(["echo", "hi"], timeout_s=7)

    args = docker.calls[-1][0]
    assert args[:3] == ["exec", "-w", "/workspace"]
    assert args[4:] == ["timeout", "--kill-after=2", "7", "echo", "hi"]
    assert result.exit_code == 0 and result.output == "hi\n" and not result.timed_out


@pytest.mark.parametrize(
    ("code", "seconds", "timed_out"),
    [(124, 7.0, True), (137, 9.0, True), (137, 0.5, False)],  # 137 early = killed, e.g. OOM
)
def test_exec_tells_a_timeout_from_another_kill(tmp_path: Path, code, seconds, timed_out):
    docker = FakeDocker([("sleep 60", ok(code=code, seconds=seconds))])

    result = start(docker, tmp_path).exec(["sleep", "60"], timeout_s=7)

    assert result.timed_out is timed_out and result.exit_code == code


def test_exec_backstop_kills_the_container_and_retires_the_sandbox(tmp_path: Path):
    docker = FakeDocker([("sleep 60", ok(code=-9, seconds=30, timed_out=True))])
    sandbox = start(docker, tmp_path)

    result = sandbox.exec(["sleep", "60"], timeout_s=7)

    assert result.timed_out
    assert any(c.startswith("rm -f") for c in docker.commands())
    with pytest.raises(SandboxError, match="closed"):
        sandbox.exec(["true"], timeout_s=1)


def test_exec_redacts_the_canary_and_flags_the_leak(tmp_path: Path):
    docker = FakeDocker([("printenv", ok(f"{CANARY.value}\n"))])

    result = start(docker, tmp_path).exec(["printenv", "GITHUB_TOKEN"], timeout_s=5)

    assert result.canary_leaked and CANARY.value not in result.output


def test_reset_rejects_a_base_that_is_not_a_commit_hash(tmp_path: Path):
    docker = FakeDocker()
    sandbox = start(docker, tmp_path)
    calls_before = len(docker.calls)

    with pytest.raises(SandboxError, match="commit"):
        sandbox.reset("--upload-pack=evil", "")
    assert len(docker.calls) == calls_before


def test_reset_checks_out_cleans_and_applies_the_patch(tmp_path: Path):
    docker = FakeDocker()
    sandbox = start(docker, tmp_path)

    sandbox.reset("a" * 40, "diff --git a/x b/x\n")

    git = [(args, stdin) for args, stdin in docker.calls if "git" in args]
    steps = [" ".join(args[args.index("git") :]) for args, _ in git]
    assert "checkout --force --detach " + "a" * 40 in steps[0]
    assert steps[1].endswith("clean -ffdq")  # not -x: ignored build output stays
    assert "apply" in steps[2] and git[2][1] == "diff --git a/x b/x\n"
    assert all("core.hooksPath=/dev/null" in step for step in steps)


def test_reset_raises_when_the_patch_does_not_apply(tmp_path: Path):
    docker = FakeDocker([("apply", ok("error: patch failed", code=1))])

    with pytest.raises(SandboxError, match="patch failed"):
        start(docker, tmp_path).reset("a" * 40, "bad patch")


def test_diff_needs_a_reset_first(tmp_path: Path):
    with pytest.raises(SandboxError, match="reset"):
        start(FakeDocker(), tmp_path).diff()


def test_diff_refuses_to_return_a_patch_that_contains_the_canary(tmp_path: Path):
    docker = FakeDocker([("diff --cached", ok(f"+token = '{CANARY.value}'\n"))])
    sandbox = start(docker, tmp_path)
    sandbox.reset("a" * 40, "")

    with pytest.raises(CanaryLeakError):
        sandbox.diff()


def test_close_is_idempotent(tmp_path: Path):
    docker = FakeDocker()
    sandbox = start(docker, tmp_path)

    sandbox.close()
    sandbox.close()

    assert sum(c.startswith("rm -f") for c in docker.commands()) == 1


# --- Orphan sweeper ----------------------------------------------------------------------


def test_sweep_removes_old_sandboxes_of_inactive_runs_only():
    listing = "\n".join(
        [
            "mw-old-dead\tdead\t100",  # old, run not active: removed
            "mw-old-live\tlive\t100",  # old, but its run is active: kept
            "mw-new-dead\tdead2\t950",  # too young: kept
            "mw-bad-label\tx\tnot-a-number",  # age unknown: kept (never delete what we can't date)
        ]
    )
    docker = FakeDocker([("ps -a", ok(listing))])

    removed = sweep_orphans(
        max_age_s=600, active_run_ids={"live"}, docker=docker, clock=lambda: 1000.0
    )

    assert removed == ["mw-old-dead"]
    assert docker.commands()[1:] == ["rm -f mw-old-dead"]
