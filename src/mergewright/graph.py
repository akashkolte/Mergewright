"""The workflow graph: a bounded Coder <-> Tester loop in LangGraph.

    START -> coder -> tools -> coder ...        (the model calls read/search/edit tools)
             coder -> tester                   (the model says it is done)
             tester -> coder                   (tests fail, attempts left)
             tester -> END                     (DONE, or FAILED_NO_FIX)
             coder -> END                      (FAILED_BUDGET or FAILED_ERROR)

Later milestones run this graph inside one Temporal activity; Temporal then owns retries,
crash recovery and the human approval gates. This module has no Temporal code.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from mergewright.config import Limits
from mergewright.llm import LLMClient, LLMError
from mergewright.prompts import CODER_SYSTEM_PROMPT, issue_message, test_failure_message
from mergewright.runlog import RunLog
from mergewright.tools import CODER_TOOLS, TOOLS, Workspace, execute, run_tests

Status = Literal["RUNNING", "DONE", "FAILED_NO_FIX", "FAILED_BUDGET", "FAILED_ERROR"]


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
    workspace: Workspace
    log: RunLog
    limits: Limits = field(default_factory=Limits)
    clock: Callable[[], float] = time.monotonic


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


def build_graph(deps: AgentDeps):
    """Build and compile the graph. The nodes close over deps."""
    coder_schemas = [TOOLS[name].schema() for name in CODER_TOOLS]
    started = deps.clock()

    def coder(state: AgentState) -> dict[str, Any]:
        if state["steps"] >= deps.limits.max_steps:
            return {"status": "FAILED_BUDGET", "reason": f"step cap {deps.limits.max_steps}"}
        if deps.clock() - started > deps.limits.wall_clock_s:
            return {"status": "FAILED_BUDGET", "reason": f"wall clock {deps.limits.wall_clock_s}s"}
        step = state["steps"] + 1
        begin = deps.clock()
        try:
            response = deps.llm.complete(state["messages"], coder_schemas)
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

    def tools(state: AgentState) -> dict[str, Any]:
        results = []
        for call in state["messages"][-1].get("tool_calls", []):
            name, args = call["function"]["name"], call["function"]["arguments"]
            begin = deps.clock()
            result = execute(name, args, deps.workspace, CODER_TOOLS)
            deps.log.event(
                "tool_call",
                agent="coder",
                tool=name,
                args=args,
                ok=result.ok,
                output=result.output,
                seconds=round(deps.clock() - begin, 3),
            )
            results.append({"role": "tool", "tool_call_id": call["id"], "content": result.output})
        return {"messages": [*state["messages"], *results]}

    def tester(state: AgentState) -> dict[str, Any]:
        attempt = state["test_attempts"] + 1
        begin = deps.clock()
        report = run_tests(deps.workspace, timeout_s=deps.limits.tool_timeout_s)
        deps.log.event(
            "tool_call",
            agent="tester",
            tool="run_tests",
            args="{}",
            ok=report.passed,
            output=report.log_tail,
            seconds=round(deps.clock() - begin, 3),
        )
        deps.log.event("test_result", attempt=attempt, **asdict(report))
        if report.passed:
            return {"test_attempts": attempt, "status": "DONE", "reason": "all tests pass"}
        if attempt >= deps.limits.max_test_attempts:
            return {
                "test_attempts": attempt,
                "status": "FAILED_NO_FIX",
                "reason": f"tests still fail after {attempt} attempts",
            }
        feedback = {
            "role": "user",
            "content": test_failure_message(report.failures, report.log_tail),
        }
        return {"test_attempts": attempt, "messages": [*state["messages"], feedback]}

    def after_coder(state: AgentState) -> str:
        if state["status"] != "RUNNING":
            return END
        return "tools" if state["messages"][-1].get("tool_calls") else "tester"

    def after_tester(state: AgentState) -> str:
        return END if state["status"] != "RUNNING" else "coder"

    graph = StateGraph(AgentState)
    graph.add_node("coder", coder)
    graph.add_node("tools", tools)
    graph.add_node("tester", tester)
    graph.add_edge(START, "coder")
    graph.add_conditional_edges("coder", after_coder, ["tools", "tester", END])
    graph.add_edge("tools", "coder")
    graph.add_conditional_edges("tester", after_tester, ["coder", END])
    return graph.compile()


def recursion_limit(limits: Limits) -> int:
    """LangGraph's superstep cap. Must sit above our own step cap, so our cap decides."""
    return limits.max_steps * 2 + limits.max_test_attempts + 10
