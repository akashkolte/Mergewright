"""The eval loop with a fake agent run and a fake oracle: no Docker, no model, no wall clock."""

import json
import threading
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from types import SimpleNamespace

import pytest

from mergewright.config import Limits, Price
from mergewright.evaluation import EvalRefused, EvalSpec, failure_category, git_state, run_eval
from mergewright.llm import ScriptedLLM
from mergewright.oracle import OracleResult, fingerprint
from mergewright.report import load_eval, render
from mergewright.runlog import RunLog
from mergewright.runner import RunResult
from mergewright.sandbox import SandboxError
from mergewright.tasks import Task
from tests.fakes import make_task, mark_valid

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=UTC)
SPEC = EvalSpec("dev", "base", 2, "replay", Price(1.0, 10.0), Limits())
PATCH = "diff --git a/src/app.py b/src/app.py\n"


class FakeRun:
    """Stands in for run_agent: writes a small run log and returns the next scripted outcome.

    outcomes is a list (in call order) or a dict keyed by "<task>-<system>-<k>". With a barrier,
    each run waits for the other parties first, so runs that do not overlap fail.
    """

    def __init__(self, outcomes: list | dict, barrier: threading.Barrier | None = None) -> None:
        self.outcomes = outcomes if isinstance(outcomes, dict) else list(outcomes)
        self.barrier = barrier
        self.calls: list[str] = []

    def __call__(self, env, issue, llm, runs_dir, limits, run_id, echo=None) -> RunResult:
        self.calls.append(run_id)
        (runs_dir / run_id / "workspace").mkdir(parents=True)  # a copy of the repo
        log = RunLog(runs_dir / run_id / "run.jsonl", echo=None)
        log.event("llm_call", step=1, prompt_tokens=1000, completion_tokens=100)
        log.event("tool_call", agent="coder", tool="read_file")
        log.event("llm_call", step=2, prompt_tokens=2000, completion_tokens=200)
        log.event("tool_call", agent="tester", tool="run_tests")
        if self.barrier:
            self.barrier.wait()
        key = run_id.rsplit("-", 1)[0]
        outcome = self.outcomes[key] if isinstance(self.outcomes, dict) else self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        status, diff = outcome
        return RunResult(run_id, status, "why", 2, 1, runs_dir / run_id, diff)


def verdict(resolved: bool, infra_error: str = "") -> OracleResult:
    tests = {"t::a": "PASSED" if resolved else "FAILED"}
    failing = [] if resolved else ["t::a"]
    return OracleResult(resolved, tests, failing, [], True, infra_error=infra_error, seconds=3.0)


def evaluate_with(tmp_path: Path, tasks: list[Task], outcomes, verdicts: list, **kw) -> Path:
    """run_eval with fakes; keyword arguments replace them (spec, make_llm, run_eval's own)."""
    judged = iter(verdicts)
    ticks = count(0.0, 5.0)  # every trial takes 5 s on the fake clock
    options = dict(
        eval_id="e1", image_id=lambda image: "sha256:img", now=lambda: NOW,
        git=lambda: {"sha": "abc", "dirty": False}, run=FakeRun(outcomes),
        judge=lambda task, patch, work_dir: next(judged), clock=lambda: next(ticks), echo=None,
    )  # fmt: skip
    spec, make_llm = kw.pop("spec", SPEC), kw.pop("make_llm", lambda task: ScriptedLLM([]))
    return run_eval(
        tasks, spec, make_llm, tmp_path / "results", tmp_path / "work", **{**options, **kw}
    )


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


def test_an_endpoint_failure_is_recorded_as_infra_even_with_a_clean_verdict(tasks, tmp_path):
    outcomes = [("FAILED_INFRA", PATCH), ("DONE", PATCH), ("DONE", PATCH), ("DONE", PATCH)]

    out = evaluate_with(tmp_path, tasks, outcomes, [verdict(True)] * 4)

    lost = trial(out, "org__repo-1", 1)
    assert lost["failure_category"] == "infra" and lost["infra_error"] == lost["reason"] == "why"
    assert not lost["resolved"] and lost["error"] is None  # unresolved, apart from agent errors


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


def test_parallel_trials_give_the_same_records_as_serial_ones(tasks, tmp_path: Path):
    outcomes = {
        f"{t.id}-base-{k}": ("DONE", f"{PATCH}# {t.id} {k}\n") for t in tasks for k in (1, 2)
    }

    def judge(task, patch, work_dir):  # deterministic in any order: k = 1 resolves
        return verdict(patch.endswith(" 1\n"))

    serial, parallel = (
        evaluate_with(tmp_path / name, tasks, [], [], run=run, judge=judge, clock=lambda: 0.0,
                      jobs=jobs)
        for name, run, jobs in [
            ("serial", FakeRun(outcomes), 1),
            ("parallel", FakeRun(outcomes, threading.Barrier(2, timeout=10)), 2),  # k=1, 2 overlap
        ]
    )  # fmt: skip

    for task in tasks:
        for k in (1, 2):
            a, b = trial(serial, task.id, k), trial(parallel, task.id, k)
            assert a.pop("run_id") != b.pop("run_id") and a == b and a["resolved"] == (k == 1)
            patch = Path("runs", task.id, "base", f"{k}.patch")
            assert (serial / patch).read_text() == (parallel / patch).read_text()
    manifest = json.loads((parallel / "manifest.json").read_text())
    assert manifest["invocations"][0]["jobs"] == 2


def served(model: str) -> SimpleNamespace:
    return SimpleNamespace(model=model, served_models={model})


def test_resume_runs_only_the_missing_trials_and_merges_the_manifest(tasks, tmp_path: Path):
    first, second = tasks
    out = evaluate_with(
        tmp_path, [first], [("DONE", PATCH)] * 2, [verdict(True)] * 2,
        make_llm=lambda task: served("m-1"),
    )  # fmt: skip
    (out / "runs" / first.id / "base" / "2.json").unlink()  # interrupted before trial 2 ended
    run = FakeRun([("FAILED_NO_FIX", "")] * 3)
    later = datetime(2026, 11, 23, 10, 0, tzinfo=UTC)

    evaluate_with(
        tmp_path, tasks, [], [], run=run, resume=True, jobs=2, now=lambda: later,
        make_llm=lambda task: served("m-2"),
    )  # fmt: skip

    assert sorted(run_id.rsplit("-", 1)[0] for run_id in run.calls) == [
        f"{first.id}-base-2", f"{second.id}-base-1", f"{second.id}-base-2"
    ]  # fmt: skip
    assert trial(out, first.id, 1)["resolved"] and not trial(out, first.id, 2)["resolved"]
    manifest = json.loads((out / "manifest.json").read_text())
    assert sorted(manifest["tasks"]) == [first.id, second.id]
    assert manifest["model"]["served"] == ["m-1", "m-2"]
    assert (manifest["started_at"], manifest["finished_at"]) == (
        "2026-11-23T09:00:00Z", "2026-11-23T10:00:00Z"
    )  # fmt: skip
    assert [(i["tasks"], i["jobs"], i["finished_at"]) for i in manifest["invocations"]] == [
        ([first.id], 1, "2026-11-23T09:00:00Z"),
        ([first.id, second.id], 2, "2026-11-23T10:00:00Z"),
    ]
    assert "incomplete" not in render([load_eval(out)], seed=0, resamples=10)
    for task in tasks:  # complete tasks are not checked again: nothing is refused or run
        mark_valid(task, valid=False)
    nothing = FakeRun([])
    evaluate_with(tmp_path, tasks, [], [], run=nothing, resume=True, image_id=lambda image: None)
    assert nothing.calls == []


def test_resume_refuses_an_eval_made_by_another_system(tasks, tmp_path: Path):
    out = evaluate_with(tmp_path, tasks[:1], [("FAILED_NO_FIX", "")] * 2, [])
    (out / "runs" / tasks[0].id / "base" / "2.json").unlink()
    manifest = (out / "manifest.json").read_text()
    cases = [
        ({"spec": replace(SPEC, model="m-2")}, "model: 'replay' in the eval, 'm-2' now"),
        ({"git": lambda: {"sha": "def", "dirty": False}}, "git sha: 'abc' in the eval, 'def' now"),
        ({"git": lambda: {"sha": "abc", "dirty": True}}, "this git tree has uncommitted changes"),
        ({"image_id": lambda image: "sha256:new"}, "image or validation changed"),
        ({"eval_id": "e2"}, "e2/manifest.json is not readable"),
        ({"resume": False}, "use --resume e1 to add trials"),
    ]
    run = FakeRun([])
    for options, message in cases:
        with pytest.raises(EvalRefused, match=message):
            evaluate_with(tmp_path, tasks, [], [], run=run, **{"resume": True, **options})
    assert run.calls == [] and (out / "manifest.json").read_text() == manifest


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


def test_git_state_ignores_task_data_but_not_code(tmp_path: Path):
    """Validation records are rewritten before a resumed task runs; that must not block it."""
    import subprocess

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=tmp_path,
                       check=True, capture_output=True)  # fmt: skip

    record = tmp_path / "eval" / "tasks" / "org__r-1" / "validation.json"
    record.parent.mkdir(parents=True)
    record.write_text("{}")
    (tmp_path / "code.py").write_text("x = 1\n")
    git("init", "-q")
    git("add", "-A")
    git("commit", "-qm", "base")

    record.write_text('{"valid": true}')
    assert git_state(tmp_path)["dirty"] is False  # task data: tied to the eval by fingerprint
    (tmp_path / "code.py").write_text("x = 2\n")
    assert git_state(tmp_path)["dirty"] is True
