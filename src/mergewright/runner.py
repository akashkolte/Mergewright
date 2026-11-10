"""One agent run: copy the repo into a workspace, run the graph, write the log and the diff."""

from __future__ import annotations

import difflib
import shutil
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from mergewright.config import Limits
from mergewright.graph import AgentDeps, build_graph, initial_state, recursion_limit
from mergewright.llm import LLMClient
from mergewright.runlog import RunLog
from mergewright.tools import SKIP_DIRS, Workspace


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
    repo: Path,
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
    shutil.copytree(repo, workspace_dir, ignore=shutil.ignore_patterns(*SKIP_DIRS))

    log = RunLog(run_dir / "run.jsonl", echo=echo)
    log.event("run_started", run_id=run_id, repo=str(repo), model=llm.model, issue=issue)
    deps = AgentDeps(llm=llm, workspace=Workspace(workspace_dir), log=log, limits=limits)
    final = build_graph(deps).invoke(
        initial_state(issue), config={"recursion_limit": recursion_limit(limits)}
    )
    diff = workspace_diff(repo, workspace_dir)
    log.event(
        "run_finished",
        status=final["status"],
        reason=final["reason"],
        steps=final["steps"],
        test_attempts=final["test_attempts"],
        diff=diff,
    )
    return RunResult(
        run_id=run_id,
        status=final["status"],
        reason=final["reason"],
        steps=final["steps"],
        test_attempts=final["test_attempts"],
        run_dir=run_dir,
        diff=diff,
    )


def workspace_diff(original: Path, changed: Path) -> str:
    """Unified diff of every text file that differs between the two trees."""

    def files(root: Path) -> set[str]:
        return {
            p.relative_to(root).as_posix()
            for p in root.rglob("*")
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(root).parts)
        }

    chunks: list[str] = []
    for rel in sorted(files(original) | files(changed)):
        before = _lines(original / rel)
        after = _lines(changed / rel)
        if before != after:
            chunks.extend(difflib.unified_diff(before, after, f"a/{rel}", f"b/{rel}"))
    return "".join(chunks)


def _lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
