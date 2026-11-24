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
    fingerprint,
    harness_ids,
    hidden_test_files,
    validate_task,
    validation_state,
)
from mergewright.sandbox import ExecResult, SandboxError
from mergewright.tasks import Task, import_swe_rebench
from tests.fakes import DIGEST, ROW_FILE, ScriptedSandbox

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


def test_hidden_test_files_leave_out_data_files_pytest_cannot_collect():
    data = "".join(
        f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n@@ -1 +1 @@\n-x\n+y\n"
        for p in ("publish/test-results.json", "tests/data.csv", "docs/a.md", "tests/conftest.py")
    )

    assert hidden_test_files(HIDDEN + data) == ["tests/test_app.py", "tests/conftest.py"]


def test_harness_ids_cut_at_the_first_space_and_pass_only_if_every_member_passed():
    results = {
        "t.py::a[> ]": "PASSED",
        "t.py::a[>  ]": "PASSED",
        "t.py::b[x y]": "PASSED",
        "t.py::b[x  z]": "FAILED",
        "t.py::b[x w]": "PASSED",  # a later pass does not hide the failure
        "t.py::c": "PASSED",
    }

    assert harness_ids(results) == {
        "t.py::a[>": "PASSED",
        "t.py::b[x": "FAILED",
        "t.py::c": "PASSED",
    }


def test_evaluate_matches_exact_ids_first_and_cut_ids_as_a_fallback(task: Task, tmp_path: Path):
    task = task.model_copy(update={"f2p": ["t.py::a[>"], "p2p": ["t.py::b[x y]"]})
    log = "FAILED t.py::a[> ]\nPASSED t.py::b[x y]\ndone in 1s\n"

    result = evaluate(
        task, APP, tmp_path, env=FakeEnv([ScriptedSandbox([ExecResult(0, log, False)])])
    )

    assert result.tests == {"t.py::a[>": "FAILED", "t.py::b[x y]": "PASSED"}
    assert result.f2p_failing == ["t.py::a[>"] and result.p2p_failing == []


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
    assert record["fingerprint"] == fingerprint(task)
    assert validation_state(task) == ("valid", record)
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


def test_a_record_is_stale_after_a_task_file_changes_and_missing_before_validation(task, tmp_path):
    assert validation_state(task) == ("not validated", None)
    validate(task, tmp_path, good_runs(task))

    (task.path / "test.patch").write_text(task.test_patch_text() + "\n")

    assert validation_state(task)[0] == "stale"


def test_a_record_without_a_fingerprint_is_stale(task: Task):
    (task.path / VALIDATION_FILE).write_text(json.dumps({"valid": True, "validated_at": "x"}))

    assert validation_state(task)[0] == "stale"


def test_the_fingerprint_covers_the_verdict_code_and_the_replay_file(task: Task, tmp_path: Path):
    package = tmp_path / "pkg"
    (package / "sandbox").mkdir(parents=True)
    for name in ("oracle.py", "tools.py", "sandbox/docker.py", "graph.py"):
        (package / name).write_text("# v1\n")
    first = fingerprint(task, package)

    (package / "graph.py").write_text("# not verdict code\n")
    assert fingerprint(task, package) == first
    (package / "sandbox" / "docker.py").write_text("# v2\n")
    second = fingerprint(task, package)
    (task.path / "replay.json").write_text("{}")

    assert len({first, second, fingerprint(task, package)}) == 3
    assert first.startswith("sha256:")
