"""`mergewright eval`: run a system k times on each task, judge every run with the oracle.

Writes results/<eval-id>/ (spec Section 4.5): manifest.json, and per trial
runs/<task>/<system>/<k>.json with the patch (<k>.patch) and the run log (<k>.trace.jsonl).
Each trial file is written as soon as the trial ends, so an interrupted eval keeps its
finished trials. A trial that raises is recorded as "infra" or "error"; the eval goes on.

Fields that need later phases (queue time, limiter wait, cached tokens, tamper events,
selected and oracle-best candidate) are written as null.
"""

from __future__ import annotations

import hashlib
import json
import platform
import secrets
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from mergewright import prompts
from mergewright.config import Limits, Price
from mergewright.llm import LLMClient
from mergewright.oracle import OracleResult, evaluate, fingerprint, validation_state
from mergewright.runlog import RunLog
from mergewright.runner import RunResult, run_agent
from mergewright.sandbox import SandboxError
from mergewright.sandbox.docker import Docker
from mergewright.sandbox.process import docker_cli
from mergewright.tasks import Task

SYSTEMS = ("base",)  # base: the Milestone 2 agent, default config, frozen (spec 9.2)
RUNTIME = "R0"  # in-process LangGraph; the durable runtimes come in Phase 3
_PACKAGES = ("mergewright", "langgraph", "openai", "pydantic")


class EvalRefused(Exception):
    """A selected task cannot be evaluated; the message lists every reason."""


@dataclass(frozen=True)
class EvalSpec:
    split: str
    system: str
    k: int
    model: str  # the configured model, or "replay"
    price: Price | None  # None: the cost is unknown
    limits: Limits = Limits()


def local_image_id(image: str, docker: Docker = docker_cli) -> str | None:
    """The local image's ID (sha256:...), or None if it is not on this host. Never pulls."""
    result = docker(["image", "inspect", "--format", "{{.Id}}", image], timeout_s=60)
    return result.output.strip() if result.code == 0 else None


def preflight(
    tasks: list[Task], image_id: Callable[[str], str | None] = local_image_id
) -> dict[str, dict[str, Any]]:
    """Each task's validation fingerprint and image ID, for the manifest.

    Raises EvalRefused if a task's validation record is missing, invalid or stale, or its
    image is not local.
    """
    problems, found = [], {}
    for task in tasks:
        state, _ = validation_state(task)
        if state != "valid":
            problems.append(
                f"{task.id}: validation is {state}; run `mergewright tasks validate --stale`"
            )
        image = image_id(task.image)
        if image is None:
            problems.append(
                f"{task.id}: image {task.image} is not on this host; pull it first "
                "(scripts/cloud-setup.sh pulls the pinned task images)"
            )
        found[task.id] = {
            "image": task.image,
            "image_id": image,
            "fingerprint": fingerprint(task),
            "resources": asdict(task.resources.limits()),
            "timeout_s": task.timeout_s,
        }
    if problems:
        raise EvalRefused("\n".join(problems))
    return found


def failure_category(status: str, resolved: bool, infra_error: str, error: str) -> str:
    """A mechanical label from status and verdict; the human error labels come in Phase 2."""
    if infra_error:
        return "infra"
    if error:
        return "error"
    if resolved:
        return "resolved"
    if status == "DONE":
        return "overclaim"
    return {"FAILED_BUDGET": "budget", "FAILED_POLICY": "policy", "FAILED_NO_FIX": "no_fix"}.get(
        status, "error"
    )


def run_usage(log_file: Path) -> dict[str, int]:
    """Tokens and coder tool calls from a run log (the Tester's run_tests is in test_attempts)."""
    events = RunLog(log_file, echo=None).events() if log_file.is_file() else []
    calls = [e for e in events if e["event"] == "llm_call"]
    return {
        "tokens_in": sum(e.get("prompt_tokens", 0) for e in calls),
        "tokens_out": sum(e.get("completion_tokens", 0) for e in calls),
        "tool_calls": sum(1 for e in events if e["event"] == "tool_call" and e["agent"] == "coder"),
    }


def run_trial(
    task: Task,
    spec: EvalSpec,
    k: int,
    llm: LLMClient,
    trial_dir: Path,
    work_dir: Path,
    run: Callable[..., RunResult] = run_agent,
    judge: Callable[..., OracleResult] = evaluate,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """One trial: run the agent, judge its patch, write <k>.json, <k>.patch, <k>.trace.jsonl.

    resolved means the system delivered the patch (status DONE) and the oracle passed it. The
    oracle also judges a patch the agent gave up on; that verdict is in patch_resolves.
    """
    run_id = f"{task.id}-{spec.system}-{k}-{secrets.token_hex(3)}"
    result: RunResult | None = None
    verdict: OracleResult | None = None
    error = infra_error = ""
    begin = clock()
    try:
        result = run(
            task.environment(), task.issue_text(), llm, work_dir / "runs", spec.limits, run_id,
            echo=None,
        )  # fmt: skip
    except Exception as exc:  # one trial never stops the eval
        error, infra_error = _describe(exc)
    wall_s = round(clock() - begin, 2)
    if result and result.diff:
        try:
            verdict = judge(task, result.diff, work_dir / "oracle")
            infra_error = verdict.infra_error
        except Exception as exc:
            error, infra_error = _describe(exc)
    status = result.status if result else "ERROR"
    patch_resolves = bool(verdict and verdict.resolved)
    resolved = status == "DONE" and patch_resolves
    category = failure_category(status, resolved, infra_error, error)
    diff = result.diff if result else ""
    log_file = work_dir / "runs" / run_id / "run.jsonl"
    trial_dir.mkdir(parents=True, exist_ok=True)
    if log_file.is_file():
        shutil.copyfile(log_file, trial_dir / f"{k}.trace.jsonl")
    # The repo copy is large and the patch and trace are kept: free the disk for the next trial.
    shutil.rmtree(work_dir / "runs" / run_id / "workspace", ignore_errors=True)
    if diff:
        (trial_dir / f"{k}.patch").write_text(diff, encoding="utf-8")
    usage = run_usage(log_file)
    record = {
        "task": task.id,
        "system": spec.system,
        "runtime": RUNTIME,
        "k": k,
        "candidates": 1,
        "status": status,
        "reason": result.reason if result else error or infra_error,
        "oracle": verdict.tests if verdict else None,
        "resolved": resolved,
        "patch_resolves": patch_resolves,
        "overclaim": category == "overclaim",
        "selected_candidate": None,
        "oracle_best_candidate": None,
        **usage,
        "cached_tokens": None,
        "cost_usd": _cost(spec.price, usage),
        "wall_s": wall_s,
        "oracle_s": verdict.seconds if verdict else None,
        "queue_s": None,
        "limiter_wait_s": None,
        "steps": result.steps if result else None,
        "test_attempts": result.test_attempts if result else None,
        "tamper_events": None,
        "infra_error": infra_error or None,
        "error": error or None,
        "patch_hash": f"sha256:{hashlib.sha256(diff.encode()).hexdigest()}" if diff else None,
        "failure_category": category,
        "run_id": run_id,
    }
    _write_json(trial_dir / f"{k}.json", record)
    return record


def git_state() -> dict[str, Any]:
    """The checked-out commit, and whether tracked files have uncommitted changes."""
    root = Path(__file__).resolve().parents[2]

    def git(*args: str) -> str | None:
        try:
            proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
        except OSError:  # no git
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    status = git("status", "--porcelain", "--untracked-files=no")  # results/ is untracked
    return {"sha": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def run_eval(
    tasks: list[Task],
    spec: EvalSpec,
    make_llm: Callable[[Task], LLMClient],
    results_dir: Path,
    work_dir: Path,
    eval_id: str | None = None,
    image_id: Callable[[str], str | None] = local_image_id,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    git: Callable[[], dict[str, Any]] = git_state,
    run: Callable[..., RunResult] = run_agent,
    judge: Callable[..., OracleResult] = evaluate,
    clock: Callable[[], float] = time.monotonic,
    echo: Callable[[str], None] | None = print,
) -> Path:
    """Run every task k times; return results_dir/<eval-id>. Raises EvalRefused before any run.

    make_llm gives the model client for one trial (a fresh replay per trial, or one live client).
    """
    task_info = preflight(tasks, image_id)
    started = now()
    eval_id = eval_id or f"{started:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"
    out = results_dir / eval_id
    out.mkdir(parents=True)  # never into an existing eval
    manifest = _manifest(eval_id, spec, task_info, started, git())
    _write_json(out / "manifest.json", manifest)  # rewritten at the end
    served: set[str] = set()
    resolved = 0
    for task in tasks:
        for k in range(1, spec.k + 1):
            llm = make_llm(task)
            record = run_trial(
                task, spec, k, llm, out / "runs" / task.id / spec.system, work_dir / eval_id,
                run=run, judge=judge, clock=clock,
            )  # fmt: skip
            served |= getattr(llm, "served_models", set())
            resolved += record["resolved"]
            if echo:
                echo(f"{task.id} k={k}: {record['failure_category']} ({record['status']})")
    manifest["model"]["served"] = sorted(served)
    manifest["finished_at"] = _utc(now())
    _write_json(out / "manifest.json", manifest)
    if echo:
        echo(f"resolved {resolved} of {len(tasks) * spec.k} trials; results in {out}")
    return out


def _manifest(
    eval_id: str,
    spec: EvalSpec,
    task_info: dict[str, dict[str, Any]],
    started: datetime,
    git: dict[str, Any],
) -> dict[str, Any]:
    return {
        "eval_id": eval_id,
        "split": spec.split,
        "system": spec.system,
        "runtime": RUNTIME,
        "k": spec.k,
        "n": 1,
        "seed": None,  # temperature 0, no seed
        "temperature": 0,
        "model": {"requested": spec.model, "served": []},
        "price": asdict(spec.price) if spec.price else None,
        "started_at": _utc(started),
        "finished_at": None,
        "git": git,
        "versions": {"python": platform.python_version(), **_versions()},
        "prompt_hash": f"sha256:{hashlib.sha256(Path(prompts.__file__).read_bytes()).hexdigest()}",
        "caps": asdict(spec.limits),
        "tasks": task_info,
    }


def _describe(exc: Exception) -> tuple[str, str]:
    """(error, infra_error): a SandboxError is the infrastructure's, anything else ours."""
    message = f"{type(exc).__name__}: {exc}"
    return ("", message) if isinstance(exc, SandboxError) else (message, "")


def _cost(price: Price | None, usage: dict[str, int]) -> float | None:
    return round(price.cost(usage["tokens_in"], usage["tokens_out"]), 6) if price else None


def _versions() -> dict[str, str | None]:
    def version(name: str) -> str | None:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return None

    return {name: version(name) for name in _PACKAGES}


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
