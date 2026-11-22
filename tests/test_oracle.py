"""The oracle and task validation. A fake Environment hands out scripted sandboxes: no Docker."""

import json
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import pytest

from mergewright.oracle import (
    VALIDATION_FILE,
    combine_patches,
    evaluate,
    hidden_test_files,
    validate_task,
)
from mergewright.sandbox import ExecResult, SandboxError
from mergewright.tasks import Task, import_swe_rebench
from tests.fakes import ScriptedSandbox
from tests.test_tasks import DIGEST, ROW_FILE

APP = (
    "diff --git a/src/app.py b/src/app.py\n"
    "--- a/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n-a\n+b\n"
)
HIDDEN = (
    "diff --git a/tests/test_app.py b/tests/test_app.py\n"
    "--- a/tests/test_app.py\n+++ b/tests/test_app.py\n@@ -1 +1 @@\n-x\n+y\n"
)


@pytest.fixture
def task(tmp_path: Path) -> Task:
    row = json.loads(ROW_FILE.read_text())
    return Task.load(import_swe_rebench(row, tmp_path / "tasks", "dev", lambda image: DIGEST))


class FakeEnv:
    """Each start() hands out the next sandbox, or raises it if it is an exception."""

    name = "fake"
    test_command = "pytest -rA"

    def __init__(self, sandboxes: list) -> None:
        self.sandboxes = list(sandboxes)
        self.workspaces: list[Path] = []

    def prepare(self, workspace_dir: Path) -> None:
        workspace_dir.mkdir()
        self.workspaces.append(workspace_dir)

    def start(self, workspace_dir: Path, run_id: str):
        sandbox = self.sandboxes.pop(0)
        if isinstance(sandbox, Exception):
            raise sandbox
        return sandbox

    def diff(self, workspace_dir: Path, sandbox) -> str:
        return ""


class UnappliableSandbox(ScriptedSandbox):
    def reset(self, base_sha: str, patch: str) -> None:
        raise SandboxError("git apply failed")


class DyingSandbox(ScriptedSandbox):
    def exec(self, argv: list[str], timeout_s: float) -> ExecResult:
        raise SandboxError("container died")


def scripted_run(task: Task, f2p: str | None = "PASSED", p2p: str = "PASSED") -> ScriptedSandbox:
    """A sandbox whose one test run reports these outcomes (f2p=None: F2P not reported)."""
    outcomes = {**({t: f2p for t in task.f2p} if f2p else {}), **{t: p2p for t in task.p2p}}
    log = "".join(f"{outcome} {test}\n" for test, outcome in outcomes.items()) + "done in 1s\n"
    return ScriptedSandbox([ExecResult(0, log, False)])


def good_runs(task: Task) -> list[ScriptedSandbox]:
    """Three validation runs of a good task: F2P fails without the fix, all pass with it."""
    return [scripted_run(task, f2p="FAILED" if i % 2 == 0 else "PASSED") for i in range(6)]


# --- combine_patches ----------------------------------------------------------------------


def test_combine_patches_drops_candidate_edits_to_hidden_test_files():
    assert combine_patches(APP + HIDDEN, HIDDEN) == APP + HIDDEN
    assert combine_patches(HIDDEN, HIDDEN) == HIDDEN  # only the test patch's own version
    assert combine_patches("", HIDDEN) == HIDDEN
    assert hidden_test_files(HIDDEN) == ["tests/test_app.py"]


# --- evaluate -----------------------------------------------------------------------------


def test_evaluate_resolves_when_every_f2p_and_p2p_test_passes(task: Task, tmp_path: Path):
    sandbox = scripted_run(task)
    ticks = iter([10.0, 12.5])

    result = evaluate(
        task, APP, tmp_path / "work", env=FakeEnv([sandbox]), clock=lambda: next(ticks)
    )

    test_patch = task.test_patch_text()
    assert result.resolved and result.patch_applies and not result.infra_error
    assert result.tests == {t: "PASSED" for t in [*task.f2p, *task.p2p]}
    assert result.seconds == 2.5
    assert sandbox.resets == [(task.base_sha, combine_patches(APP, test_patch))]
    assert sandbox.commands[0][2].endswith("pytest -rA tests/common.py tests/test_utils.py")
    assert sandbox.closed and list((tmp_path / "work").iterdir()) == []  # workspace removed


@pytest.mark.parametrize("f2p", ["FAILED", None])
def test_evaluate_is_not_resolved_when_an_f2p_test_fails_or_is_missing(task, tmp_path, f2p):
    result = evaluate(task, APP, tmp_path, env=FakeEnv([scripted_run(task, f2p=f2p)]))

    assert not result.resolved
    assert result.f2p_failing == task.f2p and result.p2p_failing == []
    assert result.tests[task.f2p[0]] == (f2p or "missing")


def test_a_patch_that_does_not_apply_is_not_resolved_and_not_an_infra_error(task, tmp_path):
    sandbox = UnappliableSandbox([])

    result = evaluate(task, APP, tmp_path, env=FakeEnv([sandbox]))

    assert not result.resolved and not result.patch_applies and result.infra_error == ""
    assert sandbox.commands == [] and sandbox.closed


def test_infra_errors_are_retried_then_reported_and_sandboxes_closed(task, tmp_path: Path):
    dying = [DyingSandbox([]), DyingSandbox([])]
    env = FakeEnv([SandboxError("daemon down"), *dying])

    result = evaluate(task, APP, tmp_path / "work", env=env, retries=2)

    assert not result.resolved and result.infra_error == "container died"
    assert env.sandboxes == [] and len(env.workspaces) == 3  # three attempts
    assert all(s.closed for s in dying)
    assert list((tmp_path / "work").iterdir()) == []


# --- validate_task ------------------------------------------------------------------------

NOW = datetime(2026, 11, 22, 8, 30, tzinfo=UTC)


def validate(task: Task, tmp_path: Path, sandboxes: list) -> dict:
    """Three runs; each run evaluates the empty patch, then the gold patch."""
    check = partial(evaluate, env=FakeEnv(sandboxes))
    return validate_task(task, tmp_path / "work", runs=3, evaluate=check, now=lambda: NOW)


def test_a_good_task_is_valid_and_the_record_is_written(task: Task, tmp_path: Path):
    task_json = (task.path / "task.json").read_text()

    record = validate(task, tmp_path, good_runs(task))

    assert record["valid"] and record["problems"] == [] and record["flaky"] == []
    assert json.loads((task.path / VALIDATION_FILE).read_text()) == record
    assert record["validated_at"] == "2026-11-22T08:30:00Z"
    assert record["image"] == task.image and len(record["runs"]) == 3
    assert record["runs"][0]["gold"]["resolved"] and not record["runs"][0]["empty"]["resolved"]
    assert (task.path / "task.json").read_text() == task_json  # never rewritten


def test_a_task_whose_f2p_passes_at_base_is_invalid(task: Task, tmp_path: Path):
    record = validate(task, tmp_path, [scripted_run(task) for _ in range(6)])

    assert not record["valid"] and record["flaky"] == []
    assert record["problems"][0] == (
        f"run 1, empty patch: F2P {task.f2p[0]} passes without the fix"
    )


def test_a_test_whose_outcome_changes_between_runs_is_flaky(task: Task, tmp_path: Path):
    runs = good_runs(task)
    runs[2] = scripted_run(task, f2p="ERROR")  # run 2, empty patch

    record = validate(task, tmp_path, runs)

    assert not record["valid"] and record["flaky"] == task.f2p
    assert record["problems"] == [f"flaky (outcome differs between runs): {task.f2p[0]}"]
