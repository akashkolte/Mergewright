"""Environments: how a run gets its workspace and sandbox. Fake docker and sandboxes only."""

from pathlib import Path

import pytest

from mergewright.environments import (
    DEFAULT_IMAGE,
    DEFAULT_TEST_COMMAND,
    ImageRepo,
    LocalRepo,
    conda_test_command,
)
from mergewright.sandbox import SandboxError, SandboxLimits
from mergewright.sandbox.process import CommandResult
from tests.fakes import ScriptedSandbox

LIMITS = SandboxLimits(cpus=1, mem_floor_mb=256, mem_ceiling_mb=512, pids=64)


class RecordingStarter:
    def __init__(self, sandbox=None) -> None:
        self.sandbox = sandbox or ScriptedSandbox([])
        self.kwargs: dict = {}

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.sandbox


def result(code=0, output=""):
    return CommandResult(code=code, output=output, timed_out=False, truncated=False, seconds=0.1)


class FakeDocker:
    """Records docker calls; `cp` creates the destination, as docker would."""

    def __init__(self, fail: str | None = None) -> None:
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args, input=None, timeout_s=None, max_output_bytes=None):
        self.calls.append(list(args))
        if self.fail and args[0] == self.fail:
            return result(1, f"{self.fail} failed")
        if args[0] == "cp":
            Path(args[2]).mkdir(parents=True)
            (Path(args[2]) / "setup.py").write_text("")
        return result()


# --- LocalRepo ----------------------------------------------------------------------------


def test_local_repo_copies_the_repo_and_starts_the_default_image(buggy_calc: Path, tmp_path):
    starter = RecordingStarter()
    env = LocalRepo(buggy_calc, limits=LIMITS, start_sandbox=starter)
    workspace = tmp_path / "ws"

    env.prepare(workspace)
    env.start(workspace, run_id="r1")

    assert (workspace / "calc.py").read_text() == (buggy_calc / "calc.py").read_text()
    assert starter.kwargs == {
        "image": DEFAULT_IMAGE,
        "workspace": workspace,
        "run_id": "r1",
        "limits": LIMITS,
        "mount_path": "/workspace",
    }
    assert env.test_command == DEFAULT_TEST_COMMAND


def test_local_repo_diff_compares_with_the_original_folder(buggy_calc: Path, tmp_path):
    env = LocalRepo(buggy_calc, start_sandbox=RecordingStarter())
    workspace = tmp_path / "ws"
    env.prepare(workspace)
    (workspace / "calc.py").write_text("changed\n")

    diff = env.diff(workspace, ScriptedSandbox([]))

    assert "+changed" in diff and "a/calc.py" in diff


# --- ImageRepo ----------------------------------------------------------------------------


def test_image_repo_copies_the_repo_out_of_the_image(tmp_path: Path):
    docker = FakeDocker()
    env = ImageRepo("img@sha256:abc", base_sha="a" * 40, test_command="pytest", docker=docker)

    env.prepare(tmp_path / "ws")

    create, cp, rm = docker.calls
    name = create[create.index("--name") + 1]
    assert create == ["create", "--name", name, "img@sha256:abc"]
    assert cp == ["cp", f"{name}:/testbed", str(tmp_path / "ws")]
    assert rm == ["rm", "-f", name]
    assert (tmp_path / "ws" / "setup.py").exists()


def test_image_repo_removes_the_temporary_container_when_the_copy_fails(tmp_path: Path):
    docker = FakeDocker(fail="cp")
    env = ImageRepo("img", base_sha="a" * 40, test_command="pytest", docker=docker)

    with pytest.raises(SandboxError, match="cp failed"):
        env.prepare(tmp_path / "ws")
    assert docker.calls[-1][:2] == ["rm", "-f"]  # the temporary container is removed


def test_image_repo_reports_a_missing_image(tmp_path: Path):
    env = ImageRepo("img", base_sha="a" * 40, test_command="pytest", docker=FakeDocker("create"))

    with pytest.raises(SandboxError, match="create failed"):
        env.prepare(tmp_path / "ws")


def test_image_repo_mounts_at_the_repo_dir_and_resets_to_base(tmp_path: Path):
    starter = RecordingStarter()
    env = ImageRepo("img", base_sha="b" * 40, test_command="pytest", start_sandbox=starter)

    sandbox = env.start(tmp_path / "ws", run_id="r1")

    assert starter.kwargs["mount_path"] == "/testbed" and starter.kwargs["image"] == "img"
    assert sandbox.resets == [("b" * 40, "")]


def test_image_repo_diff_comes_from_the_sandbox(tmp_path: Path):
    env = ImageRepo("img", base_sha="b" * 40, test_command="pytest")

    assert env.diff(tmp_path, ScriptedSandbox([], diff="+x = 1\n")) == "+x = 1\n"


def test_conda_test_command_activates_the_env_and_continues_past_collection_errors():
    command = conda_test_command("pytest -rA -p no:cacheprovider")

    assert command.startswith("source /opt/conda/etc/profile.d/conda.sh && conda activate testbed")
    assert command.endswith("pytest -rA -p no:cacheprovider --continue-on-collection-errors")
