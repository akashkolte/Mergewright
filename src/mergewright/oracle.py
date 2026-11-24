"""The oracle: does a candidate patch resolve a task, judged by its hidden tests (spec 4.5)?

Each evaluation runs in a fresh workspace and sandbox: base_sha + the candidate + the hidden
test patch, then the test patch's files. The candidate's edits to those test files are dropped
first, so an agent cannot pass by rewriting the tests. validate_task checks that a task is
usable at all: its F2P tests fail without the fix, everything passes with the gold patch, and
the outcomes are the same on every run.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import shlex
import shutil
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mergewright.environments import Environment
from mergewright.sandbox import SandboxError
from mergewright.tasks import REPLAY_FILE, TASK_FILE, Task, utc_stamp
from mergewright.tools import run_tests

VALIDATION_FILE = "validation.json"
_PACKAGE = Path(__file__).resolve().parent
# The code that decides a verdict; the pytest parser is in tools.py.
_VERDICT_CODE = ("environments.py", "oracle.py", "tasks.py", "tools.py", "sandbox/*.py")
_SECTION = re.compile(r"^diff --git ", re.MULTILINE)
_TOUCHED = re.compile(r"^(?:diff --git a/(\S+) b/(\S+)|--- a/(\S+)|\+\+\+ b/(\S+))", re.MULTILINE)
_ADDED_OR_CHANGED = re.compile(r"^\+\+\+ b/(\S+)$", re.MULTILINE)
# Copied from the SWE-rebench harness (NON_TEST_EXTS), whose runs made the F2P/P2P labels;
# "csv" has no dot there too.
_NON_TEST_ENDINGS = (
    ".json", ".png", "csv", ".txt", ".md", ".jpg", ".jpeg", ".pkl", ".yml", ".yaml", ".toml",
)  # fmt: skip


def combine_patches(candidate: str, test_patch: str) -> str:
    """The candidate without its file sections that touch a test-patch file, then the test patch."""
    hidden = _touched(test_patch)
    starts = [0, *(m.start() for m in _SECTION.finditer(candidate)), len(candidate)]
    sections = [candidate[a:b] for a, b in zip(starts, starts[1:], strict=False)]
    kept = "".join(s for s in sections if not hidden & _touched(s))
    if kept and not kept.endswith("\n"):
        kept += "\n"
    return kept + test_patch


def hidden_test_files(test_patch: str) -> list[str]:
    """The test files the test patch adds or changes (not deletes), in order: pytest's targets.

    Data files are left out, as the harness does: pytest stops on a path it cannot collect.
    """
    files = dict.fromkeys(_ADDED_OR_CHANGED.findall(test_patch))
    return [path for path in files if not path.endswith(_NON_TEST_ENDINGS)]


def harness_ids(results: dict[str, str]) -> dict[str, str]:
    """Outcomes keyed as the harness's log parser keys them: each id cut at its first space.

    The dataset's F2P/P2P ids come from that parser, so 't[> ]' is listed as 't[>'. Several
    tests can share a cut id; it is PASSED only if all of them passed, else the first other
    outcome. Ids without a space keep their key.
    """
    grouped: dict[str, str] = {}
    for test, outcome in results.items():
        key = test.split(" ", 1)[0]
        if grouped.get(key, "PASSED") == "PASSED":
            grouped[key] = outcome
    return grouped


def _touched(patch: str) -> set[str]:
    return {path for match in _TOUCHED.findall(patch) for path in match if path}


@dataclass(frozen=True)
class OracleResult:
    resolved: bool
    tests: dict[str, str]  # every F2P and P2P id -> outcome; "missing" if pytest did not report it
    f2p_failing: list[str]
    p2p_failing: list[str]
    patch_applies: bool  # meaningful only without an infra_error
    timed_out: bool = False
    infra_error: str = ""  # a SandboxError other than the patch not applying; not the candidate's
    seconds: float = 0.0


def evaluate(
    task: Task,
    patch: str,
    work_dir: Path,
    env: Environment | None = None,
    retries: int = 1,
    clock: Callable[[], float] = time.monotonic,
) -> OracleResult:
    """Judge patch by the task's hidden tests in a fresh workspace (removed afterwards).

    A patch that does not apply is not resolved. A SandboxError anywhere else is an
    infrastructure failure: it is retried `retries` times, then reported in infra_error.
    """
    begin = clock()
    env = env or task.environment()
    run_id = f"oracle-{secrets.token_hex(4)}"
    workspace = Path(work_dir) / run_id
    if workspace.exists():
        raise FileExistsError(f"{workspace} already exists")
    workspace.parent.mkdir(parents=True, exist_ok=True)
    test_patch = task.test_patch_text()
    error = ""
    for _ in range(retries + 1):
        try:
            result = _evaluate_once(task, patch, test_patch, env, workspace, run_id)
            return replace(result, seconds=round(clock() - begin, 2))
        except SandboxError as exc:
            error = str(exc) or type(exc).__name__
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
    result = _verdict(task, patch_applies=False, infra_error=error)
    return replace(result, seconds=round(clock() - begin, 2))


def _evaluate_once(
    task: Task, patch: str, test_patch: str, env: Environment, workspace: Path, run_id: str
) -> OracleResult:
    env.prepare(workspace)
    sandbox = env.start(workspace, run_id)
    try:
        try:
            sandbox.reset(task.base_sha, combine_patches(patch, test_patch))
        except SandboxError:  # the candidate (with the test patch) does not apply
            return _verdict(task, patch_applies=False)
        command = f"{env.test_command} {shlex.join(hidden_test_files(test_patch))}"
        report = run_tests(sandbox, command, timeout_s=task.timeout_s)
        return _verdict(task, report.results, timed_out=report.timed_out)
    finally:
        sandbox.close()


def _verdict(
    task: Task,
    results: dict[str, str] | None = None,
    patch_applies: bool = True,
    timed_out: bool = False,
    infra_error: str = "",
) -> OracleResult:
    results = results or {}
    cut = harness_ids(results)  # fallback for ids the harness's parser cut at a space
    tests = {test: results.get(test) or cut.get(test, "missing") for test in [*task.f2p, *task.p2p]}
    f2p_failing = [test for test in task.f2p if tests[test] != "PASSED"]
    p2p_failing = [test for test in task.p2p if tests[test] != "PASSED"]
    resolved = patch_applies and not (timed_out or infra_error or f2p_failing or p2p_failing)
    return OracleResult(
        resolved=resolved,
        tests=tests,
        f2p_failing=f2p_failing,
        p2p_failing=p2p_failing,
        patch_applies=patch_applies,
        timed_out=timed_out,
        infra_error=infra_error,
    )


# --- Task validation ----------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(UTC)


def fingerprint(task: Task, package: Path = _PACKAGE) -> str:
    """SHA-256 over the verdict code and the task's own files: whole files, path + bytes, sorted.

    File level on purpose: any edit, even a comment, makes a validation record stale (safe).
    The image digest is in task.json. A local task's repo/ is not hashed: LocalRepo checks
    that it still commits as base_sha.
    """
    files = {
        f"code/{path.relative_to(package).as_posix()}": path
        for pattern in _VERDICT_CODE
        for path in package.glob(pattern)
    }
    for name in (TASK_FILE, task.issue, task.gold_patch, task.test_patch, REPLAY_FILE):
        if name != REPLAY_FILE or (task.path / name).is_file():  # only replay.json is optional
            files[f"task/{name}"] = task.path / name
    digest = hashlib.sha256()
    for label in sorted(files):
        data = files[label].read_bytes()
        digest.update(f"{label}\0{len(data)}\0".encode() + data)
    return f"sha256:{digest.hexdigest()}"


def validation_state(task: Task) -> tuple[str, dict[str, Any] | None]:
    """The state of a task's validation record, and the record (None if there is none).

    One of "not validated", "stale" (made for other code or task files than the current ones,
    or before records had a fingerprint), "invalid" or "valid".
    """
    path = task.path / VALIDATION_FILE
    if not path.is_file():
        return "not validated", None
    record = json.loads(path.read_text(encoding="utf-8"))
    if record.get("fingerprint") != fingerprint(task):
        return "stale", record
    return ("valid" if record["valid"] else "invalid"), record


def validate_task(
    task: Task,
    work_dir: Path,
    runs: int = 3,
    evaluate: Callable[..., OracleResult] = evaluate,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Run the empty and the gold patch `runs` times; write validation.json next to task.json.

    Valid only if, on every run, no F2P test passes without the fix, every P2P test does, and
    every test passes with the gold patch; with no infrastructure error and no flaky test (one
    whose outcome differs between runs). task.json is never rewritten.
    """
    validated = fingerprint(task)  # before the runs: the code that was loaded
    gold = task.gold_patch_text()
    records: list[dict[str, Any]] = []
    problems: list[str] = []
    outcomes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for run in range(1, runs + 1):
        empty, fixed = evaluate(task, "", work_dir), evaluate(task, gold, work_dir)
        records.append({"run": run, "empty": asdict(empty), "gold": asdict(fixed)})
        problems += _problems(task, f"run {run}, empty patch", empty, fixed=False)
        problems += _problems(task, f"run {run}, gold patch", fixed, fixed=True)
        for label, result in (("empty", empty), ("gold", fixed)):
            if not result.infra_error:
                for test, outcome in result.tests.items():
                    outcomes[label, test].add(outcome)
    flaky = sorted({test for (_, test), seen in outcomes.items() if len(seen) > 1})
    if flaky:
        problems.append(f"flaky (outcome differs between runs): {', '.join(flaky)}")
    record = {
        "task": task.id,
        "valid": not problems,
        "problems": problems,
        "flaky": flaky,
        "image": task.image,
        "fingerprint": validated,
        "validated_at": utc_stamp(now()),
        "seconds": round(sum(r[k]["seconds"] for r in records for k in ("empty", "gold")), 2),
        "runs": records,
    }
    (task.path / VALIDATION_FILE).write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def _problems(task: Task, label: str, result: OracleResult, fixed: bool) -> list[str]:
    if result.infra_error:
        return [f"{label}: infrastructure error: {result.infra_error}"]
    if not result.patch_applies:
        return [f"{label}: the patch (with the test patch) does not apply"]
    if result.timed_out:
        return [f"{label}: the tests timed out"]
    if fixed:
        failing = result.f2p_failing + result.p2p_failing
        return [f"{label}: {test} is {result.tests[test]}, expected PASSED" for test in failing]
    return [
        f"{label}: F2P {test} passes without the fix"
        for test in task.f2p
        if result.tests[test] == "PASSED"
    ] + [
        f"{label}: P2P {test} is {result.tests[test]}, expected PASSED"
        for test in result.p2p_failing
    ]
