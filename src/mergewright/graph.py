"""The workflow graph: a bounded Coder <-> Tester loop in LangGraph.

    START -> coder -> tools -> coder ...        (the model calls read/search/edit tools)
             tools -> tester                   (the model called finish)
             coder -> tester                   (the model replied without a tool call)
             tester -> coder                   (tests fail, attempts left)
             tester -> END                     (DONE, or FAILED_NO_FIX)
             coder -> END                      (FAILED_BUDGET or FAILED_ERROR)

The node bodies are plain functions that take their deps as an argument, so a durable runtime
(spec Section 4.2) can reuse them. This module has no Temporal code.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from mergewright.config import Limits
from mergewright.llm import LLMClient, LLMError
from mergewright.prompts import CODER_SYSTEM_PROMPT, issue_message, test_failure_message
from mergewright.runlog import RunLog
from mergewright.sandbox import Sandbox
from mergewright.tools import (
    CODER_TOOLS,
    DEFAULT_TEST_COMMAND,
    TOOLS,
    Workspace,
    execute,
    gate,
    run_tests,
    run_tests_for_coder,
)

Status = Literal[
    "RUNNING", "DONE", "FAILED_NO_FIX", "FAILED_BUDGET", "FAILED_ERROR", "FAILED_POLICY"
]


class AgentState(TypedDict):
    messages: list[dict[str, Any]]
    steps: int  # LLM calls so far
    test_attempts: int
    status: Status
    reason: str


@dataclass
class AgentDeps:
    """Everything the nodes need from outside. Injected, so tests can use fakes."""

    llm: LLMClient
    workspace: Workspace  # host side, for the file tools
    log: RunLog
    sandbox: Sandbox  # where the workspace is mounted and the tests run
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], float] = time.monotonic
    test_command: str = DEFAULT_TEST_COMMAND
    known_errors: frozenset[str] = frozenset()  # ERROR ids at base; the gate ignores them


def initial_state(issue: str) -> AgentState:
    return {
        "messages": [
            {"role": "system", "content": CODER_SYSTEM_PROMPT},
            {"role": "user", "content": issue_message(issue)},
        ],
        "steps": 0,
        "test_attempts": 0,
        "status": "RUNNING",
        "reason": "",
    }


# Node bodies. Each runtime wraps them: build_graph below runs them in-process, and a durable
# runtime calls them from its own nodes. They take deps as an argument, so one graph can serve
# many runs.


def coder_step(state: AgentState, deps: AgentDeps, started: float) -> dict[str, Any]:
    """One LLM call. started is the run's start time on deps.clock, for the wall-clock cap."""
    if state["steps"] >= deps.limits.max_steps:
        return {"status": "FAILED_BUDGET", "reason": f"step cap {deps.limits.max_steps}"}
    if deps.clock() - started > deps.limits.wall_clock_s:
        return {"status": "FAILED_BUDGET", "reason": f"wall clock {deps.limits.wall_clock_s}s"}
    step = state["steps"] + 1
    begin = deps.clock()
    try:
        response = deps.llm.complete(state["messages"], [TOOLS[n].schema() for n in CODER_TOOLS])
    except LLMError as exc:
        deps.log.event("llm_error", step=step, error=str(exc))
        return {"status": "FAILED_ERROR", "reason": str(exc), "steps": step}
    deps.log.event(
        "llm_call",
        step=step,
        model=deps.llm.model,
        tool_calls=[call.name for call in response.tool_calls],
        content=response.content,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        seconds=round(deps.clock() - begin, 3),
    )
    return {"messages": [*state["messages"], response.as_message()], "steps": step}


def tools_step(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    """Run the tool calls in the last assistant message."""
    results = []
    for call in state["messages"][-1].get("tool_calls", []):
        name, args = call["function"]["name"], call["function"]["arguments"]
        begin = deps.clock()
        if name == "run_tests":
            result = run_tests_for_coder(
                deps.sandbox, deps.test_command, args, deps.limits.tool_timeout_s
            )
        else:
            result = execute(name, args, deps.workspace, CODER_TOOLS)
        # A command in the sandbox can copy the canary into a workspace file.
        output, leaked = deps.sandbox.canary.redact(result.output)
        deps.log.event(
            "tool_call",
            agent="coder",
            tool=name,
            args=args,
            ok=result.ok,
            output=output,
            canary_leaked=leaked,
            seconds=round(deps.clock() - begin, 3),
        )
        results.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    return {"messages": [*state["messages"], *results]}


def tester_step(state: AgentState, deps: AgentDeps) -> dict[str, Any]:
    """Run the test suite and decide: done, give up, or send the failures to the coder."""
    attempt = state["test_attempts"] + 1
    begin = deps.clock()
    report = run_tests(deps.sandbox, deps.test_command, timeout_s=deps.limits.tool_timeout_s)
    problems = gate(report, deps.known_errors)
    deps.log.event(
        "tool_call",
        agent="tester",
        tool="run_tests",
        args="{}",
        ok=not problems,
        output=report.log_tail,
        seconds=round(deps.clock() - begin, 3),
    )
    deps.log.event("test_result", attempt=attempt, problems=problems, **asdict(report))
    if not problems:
        return {"test_attempts": attempt, "status": "DONE", "reason": "all tests pass"}
    if attempt >= deps.limits.max_test_attempts:
        return {
            "test_attempts": attempt,
            "status": "FAILED_NO_FIX",
            "reason": f"tests still fail after {attempt} attempts",
        }
    feedback = {
        "role": "user",
        "content": test_failure_message(problems, report.log_tail),
    }
    return {"test_attempts": attempt, "messages": [*state["messages"], feedback]}


def next_after_coder(state: AgentState) -> str:
    if state["status"] != "RUNNING":
        return END
    return "tools" if state["messages"][-1].get("tool_calls") else "tester"


def next_after_tools(state: AgentState) -> str:
    """To the tester when the assistant message whose calls just ran called finish."""
    assistant = next(m for m in reversed(state["messages"]) if m["role"] == "assistant")
    names = [call["function"]["name"] for call in assistant.get("tool_calls", [])]
    return "tester" if "finish" in names else "coder"


def next_after_tester(state: AgentState) -> str:
    return END if state["status"] != "RUNNING" else "coder"


def assemble_graph(
    *,
    coder: Callable[..., Any],
    tools: Callable[..., Any],
    tester: Callable[..., Any],
    after_coder: Callable[..., Any],
    after_tools: Callable[..., Any],
    after_tester: Callable[..., Any],
    state_schema: type = AgentState,
    context_schema: type | None = None,
    metadata: Mapping[str, dict[str, Any]] | None = None,
) -> StateGraph:
    """The Coder <-> Tester topology, with the given node and edge functions. Not compiled.

    metadata maps a node name to its LangGraph node metadata (a durable runtime reads its
    per-node settings from there).
    """
    metadata = metadata or {}
    graph = StateGraph(state_schema, context_schema)
    for name, node in (("coder", coder), ("tools", tools), ("tester", tester)):
        graph.add_node(name, node, metadata=metadata.get(name))
    graph.add_edge(START, "coder")
    graph.add_conditional_edges("coder", after_coder, ["tools", "tester", END])
    graph.add_conditional_edges("tools", after_tools, ["coder", "tester"])
    graph.add_conditional_edges("tester", after_tester, ["coder", END])
    return graph


def build_graph(deps: AgentDeps):
    """Build and compile the in-process graph for one run."""
    started = deps.clock()
    return assemble_graph(
        coder=lambda state: coder_step(state, deps, started),
        tools=lambda state: tools_step(state, deps),
        tester=lambda state: tester_step(state, deps),
        after_coder=next_after_coder,
        after_tools=next_after_tools,
        after_tester=next_after_tester,
    ).compile()


def recursion_limit(limits: Limits) -> int:
    """LangGraph's superstep cap. Must sit above our own step cap, so our cap decides."""
    return limits.max_steps * 2 + limits.max_test_attempts + 10
