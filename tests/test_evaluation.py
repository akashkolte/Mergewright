"""The eval loop with a fake agent run and a fake oracle: no Docker, no model, no wall clock."""

import json
from datetime import UTC, datetime
from itertools import count
from pathlib import Path

import pytest

from mergewright.config import Limits, Price
from mergewright.evaluation import EvalRefused, EvalSpec, failure_category, run_eval
from mergewright.llm import ScriptedLLM
from mergewright.oracle import VALIDATION_FILE, OracleResult, fingerprint
from mergewright.runlog import RunLog
from mergewright.runner import RunResult
from mergewright.sandbox import SandboxError
from mergewright.tasks import Task, import_swe_rebench
from tests.test_tasks import DIGEST, ROW_FILE

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
SPEC = EvalSpec("dev", "base", 2, "replay", Price(1.0, 10.0), Limits())
PATCH = "diff --git a/src/app.py b/src/app.py\n"


def make_task(tasks_dir: Path, task_id: str) -> Task:
    row = {**json.loads(ROW_FILE.read_text()), "instance_id": task_id}
    return Task.load(import_swe_rebench(row, tasks_dir, "dev", lambda image: DIGEST))


def mark_valid(task: Task, valid: bool = True) -> None:
    record = {"valid": valid, "fingerprint": fingerprint(task), "validated_at": "x"}
    (task.path / VALIDATION_FILE).write_text(json.dumps(record))


class FakeRun:
    """Stands in for run_agent: writes a small run log and returns the next scripted outcome."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[str] = []

    def __call__(self, env, issue, llm, runs_dir, limits, run_id, echo=None) -> RunResult:
        self.calls.append(run_id)
        (runs_dir / run_id / "workspace").mkdir(parents=True)  # a copy of the repo
        log = RunLog(runs_dir / run_id / "run.jsonl", echo=None)
        log.event("llm_call", step=1, prompt_tokens=1000, completion_tokens=100)
        log.event("tool_call", agent="coder", tool="read_file")
        log.event("llm_call", step=2, prompt_tokens=2000, completion_tokens=200)
        log.event("tool_call", agent="tester", tool="run_tests")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, diff = outcome
        return RunResult(run_id, status, "why", 2, 1, runs_dir / run_id, diff)


def verdict(resolved: bool, infra_error: str = "") -> OracleResult:
    tests = {"t::a": "PASSED" if resolved else "FAILED"}
    failing = [] if resolved else ["t::a"]
    return OracleResult(resolved, tests, failing, [], True, infra_error=infra_error, seconds=3.0)


def evaluate_with(tmp_path: Path, tasks: list[Task], outcomes: list, verdicts: list, **kw) -> Path:
    judged = iter(verdicts)
    ticks = count(0.0, 5.0)  # every trial takes 5 s on the fake clock
    return run_eval(
        tasks, kw.pop("spec", SPEC), lambda task: ScriptedLLM([]), tmp_path / "results",
        tmp_path / "work", eval_id="e1", image_id=lambda image: "sha256:img", now=lambda: NOW,
        git=lambda: {"sha": "abc", "dirty": False}, run=FakeRun(outcomes),
        judge=lambda task, patch, work_dir: next(judged), clock=lambda: next(ticks), echo=None,
    )  # fmt: skip


def trial(out: Path, task: str, k: int) -> dict:
    return json.loads((out / "runs" / task / "base" / f"{k}.json").read_text())


@pytest.fixture
def tasks(tmp_path: Path) -> list[Task]:
    found = [make_task(tmp_path / "tasks", f"org__repo-{i}") for i in (1, 2)]
    for task in found:
        mark_valid(task)
    return found


def test_each_trial_is_judged_and_written_with_its_patch_trace_and_usage(tasks, tmp_path):
    outcomes = [("DONE", PATCH), ("DONE", PATCH), ("FAILED_NO_FIX", ""), ("FAILED_BUDGET", PATCH)]

    out = evaluate_with(tmp_path, tasks, outcomes, [verdict(True), verdict(False), verdict(True)])

    first = trial(out, "org__repo-1", 1)
    assert first["resolved"] and first["failure_category"] == "resolved"
    assert (first["tokens_in"], first["tokens_out"], first["tool_calls"]) == (3000, 300, 1)
    assert first["cost_usd"] == 0.006  # 3000 x $1/M + 300 x $10/M
    assert first["oracle"] == {"t::a": "PASSED"} and first["wall_s"] == 5.0
    assert first["patch_hash"].startswith("sha256:") and first["queue_s"] is None
    assert (out / "runs" / "org__repo-1" / "base" / "1.patch").read_text() == PATCH
    assert (out / "runs" / "org__repo-1" / "base" / "1.trace.jsonl").is_file()
    assert list((tmp_path / "work").rglob("workspace")) == []  # removed after each trial
    overclaim = trial(out, "org__repo-1", 2)
    assert overclaim["overclaim"] and overclaim["failure_category"] == "overclaim"
    no_fix = trial(out, "org__repo-2", 1)
    assert no_fix["oracle"] is None and no_fix["failure_category"] == "no_fix"  # empty patch
    gave_up = trial(out, "org__repo-2", 2)  # the patch passes, but the agent did not deliver it
    assert gave_up["patch_resolves"] and not gave_up["resolved"]
    assert gave_up["failure_category"] == "budget"


def test_a_trial_that_raises_is_recorded_and_the_eval_goes_on(tasks, tmp_path: Path):
    outcomes = [RuntimeError("bug"), SandboxError("daemon gone"), ("DONE", PATCH), ("DONE", PATCH)]
    verdicts = [verdict(False, infra_error="container died"), verdict(True)]

    out = evaluate_with(tmp_path, tasks, outcomes, verdicts)

    crashed, infra = trial(out, "org__repo-1", 1), trial(out, "org__repo-1", 2)
    assert crashed["failure_category"] == "error" and crashed["error"] == "RuntimeError: bug"
    assert crashed["status"] == "ERROR" and crashed["tokens_in"] == 3000  # the log survives
    assert infra["failure_category"] == "infra" and infra["error"] is None
    assert infra["infra_error"] == infra["reason"] == "SandboxError: daemon gone"
    oracle_infra = trial(out, "org__repo-2", 1)
    assert oracle_infra["failure_category"] == "infra" and not oracle_infra["overclaim"]
    assert trial(out, "org__repo-2", 2)["resolved"]


def test_the_manifest_records_provenance_and_each_tasks_fingerprint(tasks, tmp_path):
    out = evaluate_with(tmp_path, tasks, [("FAILED_NO_FIX", "")] * 4, [])

    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["eval_id"] == "e1" and (manifest["k"], manifest["n"]) == (2, 1)
    assert manifest["git"] == {"sha": "abc", "dirty": False} and manifest["runtime"] == "R0"
    assert manifest["started_at"] == manifest["finished_at"] == "2026-11-23T09:00:00Z"
    assert manifest["model"] == {"requested": "replay", "served": []}
    assert manifest["caps"]["max_steps"] == Limits().max_steps
    assert manifest["prompt_hash"].startswith("sha256:") and manifest["versions"]["python"]
    entry = manifest["tasks"]["org__repo-1"]
    assert entry["fingerprint"] == fingerprint(tasks[0]) and entry["image_id"] == "sha256:img"
    assert entry["resources"]["mem_ceiling_mb"] == tasks[0].resources.mem_ceiling_mb


def test_eval_refuses_invalid_stale_unvalidated_tasks_and_missing_images(tmp_path: Path):
    invalid, stale, missing = (make_task(tmp_path / "t", f"org__r-{i}") for i in (1, 2, 3))
    mark_valid(invalid, valid=False)
    mark_valid(stale)
    (stale.path / "issue.md").write_text("changed after validation")

    with pytest.raises(EvalRefused) as refused:
        run_eval(
            [invalid, stale, missing], SPEC, lambda task: ScriptedLLM([]), tmp_path / "results",
            tmp_path / "work", image_id=lambda image: None, echo=None,
        )  # fmt: skip

    message = str(refused.value)
    assert "org__r-1: validation is invalid" in message
    assert "org__r-2: validation is stale" in message
    assert "org__r-3: validation is not validated" in message
    assert message.count("is not on this host; pull it first") == 3
    assert not (tmp_path / "results").exists()  # nothing ran


@pytest.mark.parametrize(
    ("status", "resolved", "infra", "error", "expected"),
    [
        ("DONE", True, "", "", "resolved"),
        ("DONE", False, "", "", "overclaim"),
        ("FAILED_NO_FIX", False, "", "", "no_fix"),
        ("FAILED_BUDGET", False, "", "", "budget"),
        ("FAILED_POLICY", False, "", "", "policy"),
        ("FAILED_ERROR", False, "", "", "error"),
        ("DONE", False, "", "KeyError: x", "error"),
        ("ERROR", False, "SandboxError: gone", "", "infra"),
    ],
)
def test_failure_category(status, resolved, infra, error, expected):
    assert failure_category(status, resolved, infra, error) == expected
