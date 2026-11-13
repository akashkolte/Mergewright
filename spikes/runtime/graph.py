"""The Coder <-> Tester graph shaped for the Temporal LangGraph plugin (runtime spike).

What the plugin needs, and how this module meets it:

- The plugin takes one uncompiled graph per worker process and wraps each node as an activity.
  It rejects closures and local functions as nodes, so the nodes are methods of SpikeNodes,
  which holds the worker-wide dependencies (LLM client, message store, limits, clock).
- Per-run values (workspace, log, start time, injected faults) come from the run context.
- State holds message hashes and counters only (spec Section 4.2). Nodes read and write the
  message bodies through the MessageStore.
- Edges run in the workflow, so they cannot read messages. The coder sets `wants_tools`.
- Edge functions are async; the workflow sandbox blocks sync ones.
- The graph stops after each tester attempt. The workflow decides whether to continue, and
  continues-as-new to keep history bounded.

No Temporal imports here. The runtime passes node metadata (execute_in, task_queue, timeouts,
retry policy) in through build_spike_graph.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph
from langgraph.runtime import Runtime

from mergewright.config import Limits
from mergewright.graph import (
    AgentDeps,
    AgentState,
    Status,
    assemble_graph,
    coder_step,
    initial_state,
    tester_step,
    tools_step,
)
from mergewright.llm import LLMClient
from mergewright.runlog import RunLog
from mergewright.runner import copy_workspace
from mergewright.tools import Workspace
from spikes.runtime.store import MessageStore


class SpikeState(TypedDict):
    message_hashes: list[str]
    steps: int
    test_attempts: int
    status: Status
    reason: str
    wants_tools: bool  # the last coder reply asked for tools


class Fault(TypedDict, total=False):
    """Test-only fault for one node. Counts every execution of that node in the run."""

    sleep_s: float  # sleep this long ...
    sleep_times: int  # ... on the first sleep_times executions
    fail_times: int  # raise on the first fail_times executions


class RunContext(TypedDict):
    """Per-run values. Plain types, so Temporal can serialize them across the activity boundary."""

    run_id: str
    workspace: str
    log_path: str
    started_at: float  # time.time() when the run started
    faults: dict[str, Fault]


class InjectedFault(RuntimeError):
    """Raised by a node on purpose, to test retries."""


def new_run(
    repo: Path,
    runs_dir: Path,
    run_id: str,
    issue: str,
    store: MessageStore,
    clock: Callable[[], float] = time.time,
    faults: dict[str, Fault] | None = None,
) -> tuple[SpikeState, RunContext]:
    """Copy the repo into runs_dir/run_id/workspace and store the first messages.

    Returns the start state (hashes only) and the run context.
    """
    run_dir = runs_dir / run_id
    workspace = run_dir / "workspace"
    copy_workspace(repo, workspace)
    first = initial_state(issue)
    state = SpikeState(
        message_hashes=store.put_all(first["messages"]),
        steps=first["steps"],
        test_attempts=first["test_attempts"],
        status=first["status"],
        reason=first["reason"],
        wants_tools=False,
    )
    context = RunContext(
        run_id=run_id,
        workspace=str(workspace),
        log_path=str(run_dir / "run.jsonl"),
        started_at=clock(),
        faults=faults or {},
    )
    return state, context


class SpikeNodes:
    """The three graph nodes. One instance per worker process, shared by all runs."""

    def __init__(
        self,
        llm: LLMClient,
        store: MessageStore,
        limits: Limits | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.llm = llm
        self.store = store
        self.limits = limits or Limits()
        self.clock = clock
        self.sleep = sleep

    # The steps block (LLM call, subprocess), so they run in a thread, off the event loop.
    async def coder(self, state: SpikeState, runtime: Runtime[RunContext]) -> dict[str, Any]:
        return await asyncio.to_thread(self._run, "coder", state, runtime.context)

    async def tools(self, state: SpikeState, runtime: Runtime[RunContext]) -> dict[str, Any]:
        return await asyncio.to_thread(self._run, "tools", state, runtime.context)

    async def tester(self, state: SpikeState, runtime: Runtime[RunContext]) -> dict[str, Any]:
        return await asyncio.to_thread(self._run, "tester", state, runtime.context)

    def _run(self, node: str, state: SpikeState, context: RunContext) -> dict[str, Any]:
        self._inject_fault(node, context)
        deps = AgentDeps(
            llm=self.llm,
            workspace=Workspace(Path(context["workspace"])),
            log=RunLog(Path(context["log_path"]), echo=None),
            limits=self.limits,
            clock=self.clock,
        )
        full = AgentState(
            messages=self.store.get_all(state["message_hashes"]),
            steps=state["steps"],
            test_attempts=state["test_attempts"],
            status=state["status"],
            reason=state["reason"],
        )
        if node == "coder":
            update = coder_step(full, deps, context["started_at"])
        elif node == "tools":
            update = tools_step(full, deps)
        else:
            update = tester_step(full, deps)
        return self._to_hashes(node, update)

    def _to_hashes(self, node: str, update: dict[str, Any]) -> dict[str, Any]:
        """Replace the full message list in a step's update with hashes."""
        update = dict(update)
        messages = update.pop("messages", None)
        if messages is not None:
            update["message_hashes"] = self.store.put_all(messages)
        if node == "coder":
            update["wants_tools"] = bool(messages and messages[-1].get("tool_calls"))
        return update

    def _inject_fault(self, node: str, context: RunContext) -> None:
        fault = context["faults"].get(node)
        if not fault:
            return
        counter = Path(context["log_path"]).parent / "faults" / f"{node}.count"
        counter.parent.mkdir(parents=True, exist_ok=True)
        execution = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(execution))
        if execution <= fault.get("sleep_times", 0):
            self.sleep(fault.get("sleep_s", 0.0))
        if execution <= fault.get("fail_times", 0):
            raise InjectedFault(f"injected failure {execution} in {node}")


async def after_coder(state: SpikeState) -> str:
    if state["status"] != "RUNNING":
        return END
    return "tools" if state["wants_tools"] else "tester"


async def after_tester(state: SpikeState) -> str:
    return END  # one tester attempt per graph run; the workflow decides what comes next


def build_spike_graph(
    nodes: SpikeNodes, metadata: Mapping[str, dict[str, Any]] | None = None
) -> StateGraph:
    """Build the graph once per worker. Not compiled: the plugin compiles it in the workflow."""
    return assemble_graph(
        coder=nodes.coder,
        tools=nodes.tools,
        tester=nodes.tester,
        after_coder=after_coder,
        after_tester=after_tester,
        state_schema=SpikeState,
        context_schema=RunContext,
        metadata=metadata,
    )
