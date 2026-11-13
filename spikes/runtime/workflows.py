"""Workflows for the runtime spike. Workflow code: deterministic, no I/O.

This module is loaded inside Temporal's workflow sandbox, so it imports only temporalio and
langgraph (the plugin passes langgraph through). The graphs themselves are registered with the
plugin on the worker (spikes/runtime/worker.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from temporalio import workflow
from temporalio.langgraph import graph

# Task queues (spec Section 3, "Task queues"). Workflows run on SERVICE_QUEUE.
SERVICE_QUEUE = "service"
LLM_QUEUE = "llm"
SANDBOX_QUEUE = "sandbox"

AGENT_GRAPH = "agent"
PROBE_GRAPH = "interrupt-probe"


@dataclass
class AgentInput:
    state: dict[str, Any]  # SpikeState: hashes and counters only
    context: dict[str, Any]  # RunContext
    recursion_limit: int
    segment: int = 1  # 1 for the first run, +1 per continue-as-new
    history: list[int] = field(default_factory=list)  # event count of each earlier segment


@workflow.defn
class AgentWorkflow:
    """Runs the graph until one tester attempt ends, then continues-as-new if still RUNNING.

    The next segment starts from the carried state, so completed nodes are never re-run, and
    each segment's history holds one coder/tools/tester round trip only.
    """

    @workflow.run
    async def run(self, input: AgentInput) -> dict[str, Any]:
        app = graph(AGENT_GRAPH).compile()
        state = await app.ainvoke(
            input.state,
            context=input.context,
            config={"recursion_limit": input.recursion_limit},
        )
        history = [*input.history, workflow.info().get_current_history_length()]
        if state["status"] == "RUNNING":
            workflow.continue_as_new(
                AgentInput(
                    state=state,
                    context=input.context,
                    recursion_limit=input.recursion_limit,
                    segment=input.segment + 1,
                    history=history,
                )
            )
        return {**state, "segments": input.segment, "history_lengths": history}


@workflow.defn
class InterruptProbeWorkflow:
    """Criterion 4: interrupt() in an activity node, resumed by a Temporal signal."""

    def __init__(self) -> None:
        self._question: str | None = None
        self._reply: str | None = None

    @workflow.signal
    def resume(self, reply: str) -> None:
        self._reply = reply

    @workflow.query
    def pending(self) -> str | None:
        """The question the graph is waiting on, or None."""
        return self._question

    @workflow.run
    async def run(self, question: str) -> dict[str, Any]:
        app = graph(PROBE_GRAPH).compile(checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": workflow.info().workflow_id}}
        result = await app.ainvoke({"question": question, "answer": ""}, config)
        interrupts = result.get("__interrupt__") or []
        if interrupts:
            self._question = str(interrupts[0].value)
            await workflow.wait_condition(lambda: self._reply is not None)
            self._question = None
            result = await app.ainvoke(Command(resume=self._reply), config)
        return {"answer": result["answer"], "interrupted": bool(interrupts)}
