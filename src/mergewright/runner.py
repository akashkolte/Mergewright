"""One agent run: fill a workspace, start its sandbox, run the graph, write the log and the diff."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mergewright.config import Limits
from mergewright.environments import Environment
from mergewright.graph import AgentDeps, build_graph, initial_state, recursion_limit
from mergewright.llm import LLMClient
from mergewright.runlog import RunLog
from mergewright.sandbox import CanaryLeakError
from mergewright.tools import Workspace, run_tests


@dataclass
class RunResult:
    run_id: str
    status: str
    reason: str
    steps: int
    test_attempts: int
    run_dir: Path
    diff: str


def run_agent(
    env: Environment,
    issue: str,
    llm: LLMClient,
    runs_dir: Path,
    limits: Limits | None = None,
    run_id: str | None = None,
    echo: Callable[[str], None] | None = print,
) -> RunResult:
    limits = limits or Limits()
    run_id = run_id or uuid.uuid4().hex[:8]
    run_dir = runs_dir / run_id
    workspace_dir = run_dir / "workspace"
    env.prepare(workspace_dir)

    log = RunLog(run_dir / "run.jsonl", echo=echo)
    log.event("run_started", run_id=run_id, environment=env.name, model=llm.model, issue=issue)
    sandbox = env.start(workspace_dir, run_id)
    log.redact = lambda text: sandbox.canary.redact(text)[0]
    try:
        # Tests that already fail or error at base (e.g. a dependency the image lacks) are known
        # gaps of the environment: the gate ignores them and the Coder sees them marked.
        baseline = run_tests(sandbox, env.test_command, timeout_s=limits.tool_timeout_s)
        log.event(
            "test_baseline",
            failures=baseline.failures,
            errors=baseline.errors,
            num_passed=baseline.num_passed,
            timed_out=baseline.timed_out,
        )
        deps = AgentDeps(
            llm=llm,
            workspace=Workspace(workspace_dir),
            log=log,
            sandbox=sandbox,
            limits=limits,
            test_command=env.test_command,
            known_failing=frozenset(baseline.failures + baseline.errors),
        )
        final = build_graph(deps).invoke(
            initial_state(issue), config={"recursion_limit": recursion_limit(limits)}
        )
        status, reason = final["status"], final["reason"]
        try:
            diff = env.diff(workspace_dir, sandbox)  # raises CanaryLeakError on the canary
        except CanaryLeakError:
            # Never let the canary leave: withhold the diff and fail the run.
            status, reason, diff = "FAILED_POLICY", "the canary is in the diff", ""
    finally:
        sandbox.close()
    log.event(
        "run_finished",
        status=status,
        reason=reason,
        steps=final["steps"],
        test_attempts=final["test_attempts"],
        diff=diff,
    )
    return RunResult(
        run_id=run_id,
        status=status,
        reason=reason,
        steps=final["steps"],
        test_attempts=final["test_attempts"],
        run_dir=run_dir,
        diff=diff,
    )
