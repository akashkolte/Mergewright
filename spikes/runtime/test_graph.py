"""The spike graph and store on plain LangGraph, without Temporal.

Run with: pytest spikes/runtime
"""

import asyncio
import json
from pathlib import Path

import pytest

from mergewright.config import Limits
from mergewright.graph import recursion_limit
from mergewright.llm import LLMResponse, ToolCall, TurnReplayLLM
from spikes.runtime.graph import InjectedFault, SpikeNodes, build_spike_graph, new_run
from spikes.runtime.store import MessageStore
from tests.conftest import BUGGY_LINE, EXAMPLES_DIR, FIXED_LINES

REPO = EXAMPLES_DIR / "buggy_calc"
ISSUE = "median is wrong for even-length input"
WRONG_FIX = "    return ordered[len(ordered) // 2 - 1]\n"


def edit(old: str, new: str) -> LLMResponse:
    args = json.dumps({"path": "calc.py", "old": old, "new": new})
    return LLMResponse(tool_calls=[ToolCall(id="c1", name="edit_file", arguments=args)])


def fixing_llm() -> TurnReplayLLM:
    return TurnReplayLLM([edit(BUGGY_LINE, FIXED_LINES), LLMResponse(content="fixed")])


def run(graph, state, context):
    app = graph.compile()
    config = {"recursion_limit": recursion_limit(Limits())}
    return asyncio.run(app.ainvoke(state, context=context, config=config))


@pytest.fixture
def store(tmp_path: Path) -> MessageStore:
    return MessageStore(tmp_path / "store")


def test_store_returns_the_same_hash_for_the_same_message(store: MessageStore):
    message = {"role": "user", "content": "hello"}

    digest = store.put(message)

    assert store.put(dict(reversed(list(message.items())))) == digest
    assert store.get(digest) == message


def test_store_rejects_unknown_and_malformed_hashes(store: MessageStore):
    with pytest.raises(KeyError):
        store.get("0" * 64)
    with pytest.raises(KeyError):
        store.get("../../etc/passwd")


def test_new_run_copies_the_repo_and_stores_the_first_messages(tmp_path: Path, store):
    state, context = new_run(REPO, tmp_path / "runs", "r1", ISSUE, store, clock=lambda: 100.0)

    workspace = Path(context["workspace"])
    assert workspace == tmp_path / "runs" / "r1" / "workspace"
    assert (workspace / "calc.py").read_text() == (REPO / "calc.py").read_text()
    assert context["started_at"] == 100.0 and context["faults"] == {}
    assert ISSUE in store.get(state["message_hashes"][1])["content"]


def test_state_holds_hashes_only(tmp_path: Path, store):
    graph = build_spike_graph(SpikeNodes(fixing_llm(), store))
    state, context = new_run(REPO, tmp_path / "runs", "r1", ISSUE, store)

    final = run(graph, state, context)

    assert final["status"] == "DONE"
    assert set(final) == {
        "message_hashes",
        "steps",
        "test_attempts",
        "status",
        "reason",
        "wants_tools",
    }
    assert ISSUE not in json.dumps(final)
    roles = [m["role"] for m in store.get_all(final["message_hashes"])]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]


def test_one_graph_serves_two_runs_each_in_its_own_workspace(tmp_path: Path, store):
    graph = build_spike_graph(SpikeNodes(fixing_llm(), store))  # built once, like a worker
    runs = [new_run(REPO, tmp_path / "runs", run_id, ISSUE, store) for run_id in ("r1", "r2")]

    finals = [run(graph, state, context) for state, context in runs]

    for final, (_, context) in zip(finals, runs, strict=True):
        assert final["status"] == "DONE"
        assert FIXED_LINES in (Path(context["workspace"]) / "calc.py").read_text()
    assert BUGGY_LINE in (REPO / "calc.py").read_text()


def test_graph_stops_after_one_failed_test_attempt(tmp_path: Path, store):
    # The workflow, not the graph, starts the next attempt (after continue-as-new).
    llm = TurnReplayLLM([edit(BUGGY_LINE, WRONG_FIX), LLMResponse(content="fixed")])
    state, context = new_run(REPO, tmp_path / "runs", "r1", ISSUE, store)

    final = run(build_spike_graph(SpikeNodes(llm, store)), state, context)

    assert final["status"] == "RUNNING"
    assert final["test_attempts"] == 1
    assert store.get(final["message_hashes"][-1])["role"] == "user"  # the test feedback


def test_wall_clock_cap_uses_the_run_start_time_from_the_context(tmp_path: Path, store):
    nodes = SpikeNodes(fixing_llm(), store, Limits(wall_clock_s=60), clock=lambda: 1_000.0)
    state, context = new_run(REPO, tmp_path / "runs", "r1", ISSUE, store, clock=lambda: 939.0)

    final = run(build_spike_graph(nodes), state, context)

    assert final["status"] == "FAILED_BUDGET"
    assert "wall clock" in final["reason"]


def test_injected_faults_fire_on_the_first_executions_only(tmp_path: Path, store):
    sleeps: list[float] = []
    nodes = SpikeNodes(fixing_llm(), store, sleep=sleeps.append)
    faults = {"tools": {"fail_times": 1, "sleep_s": 2.5, "sleep_times": 1}}
    state, context = new_run(REPO, tmp_path / "runs", "r1", ISSUE, store, faults=faults)
    graph = build_spike_graph(nodes)

    with pytest.raises(InjectedFault):
        run(graph, state, context)
    final = run(graph, state, context)  # second execution of tools: no fault

    assert final["status"] == "DONE"
    assert sleeps == [2.5]


def test_node_metadata_is_passed_through_for_the_runtime(store):
    metadata = {
        "coder": {"execute_in": "activity", "task_queue": "llm"},
        "tools": {"execute_in": "activity", "task_queue": "sandbox"},
        "tester": {"execute_in": "activity", "task_queue": "sandbox"},
    }

    graph = build_spike_graph(SpikeNodes(fixing_llm(), store), metadata=metadata)

    assert {name: graph.nodes[name].metadata for name in metadata} == metadata
