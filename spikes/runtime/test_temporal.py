"""Gate criteria 2-5 on a real Temporal server (criterion 1 needs a process kill; see
docs/spikes.md). Each test starts a throwaway dev server from the installed `temporal` CLI,
so nothing is downloaded. Skipped when the CLI is missing.
"""

import asyncio
import json
import shutil
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment

from mergewright.llm import LLMResponse, ToolCall, TurnReplayLLM
from spikes.runtime.evidence import collect
from spikes.runtime.start import start_agent
from spikes.runtime.store import MessageStore
from spikes.runtime.worker import make_plugin, make_workers
from spikes.runtime.workflows import (
    LLM_QUEUE,
    SANDBOX_QUEUE,
    SERVICE_QUEUE,
    InterruptProbeWorkflow,
)
from tests.conftest import BUGGY_LINE, EXAMPLES_DIR, FIXED_LINES

TEMPORAL_CLI = shutil.which("temporal")
pytestmark = pytest.mark.skipif(TEMPORAL_CLI is None, reason="needs the temporal CLI")

REPO = EXAMPLES_DIR / "buggy_calc"
ISSUE = (EXAMPLES_DIR / "inputs" / "buggy_calc_issue.md").read_text()
ISSUE_PHRASE = "mean of the two middle values"  # must never appear in a Temporal payload
WRONG_FIX = "    return ordered[len(ordered) // 2 - 1]\n"


def edit(old: str, new: str) -> LLMResponse:
    args = json.dumps({"path": "calc.py", "old": old, "new": new})
    return LLMResponse(tool_calls=[ToolCall(id="c1", name="edit_file", arguments=args)])


def replay_llm() -> TurnReplayLLM:
    return TurnReplayLLM.from_file(EXAMPLES_DIR / "inputs" / "buggy_calc_replay.json")


@asynccontextmanager
async def temporal(llm, spike_dir: Path, timeouts=None):
    """A throwaway server plus service, llm and sandbox workers in this process."""
    plugin = make_plugin(llm, MessageStore(spike_dir / "store"), timeouts)
    env = await WorkflowEnvironment.start_local(dev_server_existing_path=TEMPORAL_CLI)
    async with env, AsyncExitStack() as stack:
        for worker in make_workers(env.client, plugin, [SERVICE_QUEUE, LLM_QUEUE, SANDBOX_QUEUE]):
            await stack.enter_async_context(worker)
        yield env.client


async def run_agent(client: Client, spike_dir: Path, faults=None):
    handle = await start_agent(client, REPO, ISSUE, spike_dir, "r1", faults)
    result = await handle.result()
    log = spike_dir / "runs" / "r1" / "run.jsonl"
    return result, await collect(client, handle.id, ISSUE_PHRASE, log)


def test_nodes_run_on_their_own_queues_and_payloads_carry_hashes_only(tmp_path: Path):
    async def scenario():
        async with temporal(replay_llm(), tmp_path) as client:
            return await run_agent(client, tmp_path)

    result, evidence = asyncio.run(scenario())

    assert result["status"] == "DONE"
    assert FIXED_LINES.splitlines()[0] in (tmp_path / "runs/r1/workspace/calc.py").read_text()
    assert evidence.queues_by_node() == {
        "agent.coder": {"llm"},
        "agent.tools": {"sandbox"},
        "agent.tester": {"sandbox"},
    }
    assert [s.grep_hits for s in evidence.segments] == [0]
    assert evidence.llm_steps == [1, 2, 3, 4]


def test_each_test_attempt_continues_as_new_and_history_stays_flat(tmp_path: Path):
    llm = TurnReplayLLM(
        [
            edit(BUGGY_LINE, WRONG_FIX),
            LLMResponse(content="fixed"),
            edit(WRONG_FIX, FIXED_LINES),
            LLMResponse(content="fixed now"),
        ]
    )

    async def scenario():
        async with temporal(llm, tmp_path) as client:
            return await run_agent(client, tmp_path)

    result, evidence = asyncio.run(scenario())

    assert result["status"] == "DONE" and result["test_attempts"] == 2
    assert result["segments"] == 2
    first, second = evidence.segments
    assert first.events == second.events  # same shape of work, same history size
    assert all(s.grep_hits == 0 for s in evidence.segments)
    assert evidence.duplicate_llm_steps() == []


def test_retry_policy_from_node_metadata_retries_a_failing_node(tmp_path: Path):
    async def scenario():
        async with temporal(replay_llm(), tmp_path) as client:
            return await run_agent(client, tmp_path, {"tools": {"fail_times": 2}})

    result, evidence = asyncio.run(scenario())

    assert result["status"] == "DONE"
    first_tools = next(a for a in evidence.activities if a.name == "agent.tools")
    assert first_tools.attempt == 3
    assert "injected failure 2" in first_tools.last_failure


def test_start_to_close_timeout_from_node_metadata_retries_a_slow_node(tmp_path: Path):
    timeouts = {"tester": timedelta(seconds=2)}
    faults = {"tester": {"sleep_s": 4, "sleep_times": 1}}

    async def scenario():
        async with temporal(replay_llm(), tmp_path, timeouts) as client:
            return await run_agent(client, tmp_path, faults)

    result, evidence = asyncio.run(scenario())

    assert result["status"] == "DONE"
    tester = next(a for a in evidence.activities if a.name == "agent.tester")
    assert tester.attempt == 2
    assert tester.last_failure.startswith("timeout")


def test_interrupt_in_an_activity_node_resumes_from_a_signal(tmp_path: Path):
    async def scenario():
        async with temporal(replay_llm(), tmp_path) as client:
            handle = await client.start_workflow(
                InterruptProbeWorkflow.run, "approve?", id="probe-1", task_queue=SERVICE_QUEUE
            )
            for _ in range(100):
                if await handle.query(InterruptProbeWorkflow.pending) == "approve?":
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("the graph never paused on interrupt()")
            await handle.signal(InterruptProbeWorkflow.resume, "yes")
            return await handle.result()

    assert asyncio.run(scenario()) == {"answer": "yes", "interrupted": True}
