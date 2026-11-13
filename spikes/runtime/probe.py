"""A one-node graph for criterion 4: interrupt() inside an activity node.

Kept apart from the agent graph, so the agent's topology does not change for a test. The
interrupt mechanism under test is the plugin's, which is the same for any graph.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, TypedDict

from langgraph.graph import START, StateGraph
from langgraph.types import interrupt


class ProbeState(TypedDict):
    question: str
    answer: str


async def ask(state: ProbeState) -> dict[str, Any]:
    """Pause the graph with the question; return the reply it resumes with."""
    return {"answer": str(interrupt(state["question"]))}


def build_probe_graph(metadata: Mapping[str, Any]) -> StateGraph:
    graph = StateGraph(ProbeState)
    graph.add_node("ask", ask, metadata=dict(metadata))
    graph.add_edge(START, "ask")
    return graph
