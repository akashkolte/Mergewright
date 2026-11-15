"""Worker processes for the runtime spike. All plugin setup is in this module.

    python -m spikes.runtime.worker --queues service,llm
    python -m spikes.runtime.worker --queues sandbox

Each process builds the graphs once and wraps them with one LangGraphPlugin. The plugin
changes the graph's nodes in place, so one plugin instance serves all workers in a process.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from temporalio.client import Client
from temporalio.common import RetryPolicy
from temporalio.langgraph import LangGraphPlugin
from temporalio.worker import Worker

from mergewright.llm import LLMClient, TurnReplayLLM
from spikes.runtime.connection import connect, connect_config, describe
from spikes.runtime.graph import SpikeNodes, build_spike_graph
from spikes.runtime.probe import build_probe_graph
from spikes.runtime.store import MessageStore
from spikes.runtime.workflows import (
    AGENT_GRAPH,
    LLM_QUEUE,
    PROBE_GRAPH,
    SANDBOX_QUEUE,
    SERVICE_QUEUE,
    AgentWorkflow,
    InterruptProbeWorkflow,
)

SPIKE_DIR = Path(os.environ.get("MERGEWRIGHT_SPIKE_DIR", "runs/spike"))
REPLAY_FILE = Path("examples/inputs/buggy_calc_replay.json")

RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_attempts=3)
TIMEOUTS = {
    "coder": timedelta(seconds=60),
    "tools": timedelta(seconds=30),
    "tester": timedelta(seconds=60),
    "ask": timedelta(seconds=10),
}
QUEUES = {"coder": LLM_QUEUE, "tools": SANDBOX_QUEUE, "tester": SANDBOX_QUEUE, "ask": LLM_QUEUE}


def node_options(timeouts: Mapping[str, timedelta] | None = None) -> dict[str, dict[str, Any]]:
    """Per-node activity options (criteria 2 and 5): queue, timeout, retry policy."""
    timeouts = {**TIMEOUTS, **(timeouts or {})}
    return {
        node: {
            "execute_in": "activity",
            "task_queue": QUEUES[node],
            "start_to_close_timeout": timeouts[node],
            "retry_policy": RETRY,
        }
        for node in QUEUES
    }


def make_plugin(
    llm: LLMClient, store: MessageStore, timeouts: Mapping[str, timedelta] | None = None
) -> LangGraphPlugin:
    options = node_options(timeouts)
    agent = build_spike_graph(SpikeNodes(llm, store), metadata=options)
    probe = build_probe_graph(options["ask"])
    return LangGraphPlugin(graphs={AGENT_GRAPH: agent, PROBE_GRAPH: probe})


def make_workers(client: Client, plugin: LangGraphPlugin, queues: Sequence[str]) -> list[Worker]:
    """One Worker per queue. Only the service queue hosts workflows."""
    workflows = [AgentWorkflow, InterruptProbeWorkflow]
    return [
        Worker(
            client,
            task_queue=queue,
            workflows=workflows if queue == SERVICE_QUEUE else [],
            plugins=[plugin],
        )
        for queue in queues
    ]


async def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--queues", required=True, help="comma-separated: service,llm,sandbox")
    parser.add_argument(
        "--timeout",
        action="append",
        default=[],
        metavar="NODE=SECONDS",
        help="override a node's start-to-close timeout, e.g. tester=5",
    )
    args = parser.parse_args(argv)
    queues = [q.strip() for q in args.queues.split(",") if q.strip()]
    unknown = set(queues) - {SERVICE_QUEUE, LLM_QUEUE, SANDBOX_QUEUE}
    if unknown:
        parser.error(f"unknown queues: {sorted(unknown)}")
    timeouts = {}
    for item in args.timeout:
        node, _, seconds = item.partition("=")
        if node not in TIMEOUTS or not seconds:
            parser.error(f"bad --timeout {item!r}")
        timeouts[node] = timedelta(seconds=float(seconds))

    plugin = make_plugin(
        TurnReplayLLM.from_file(REPLAY_FILE), MessageStore(SPIKE_DIR / "store"), timeouts
    )
    client = await connect()
    workers = make_workers(client, plugin, queues)
    print(
        f"worker pid={os.getpid()} queues={queues} timeouts={timeouts or 'default'} "
        f"server={describe(connect_config())}",
        flush=True,
    )
    await asyncio.gather(*(worker.run() for worker in workers))


if __name__ == "__main__":
    asyncio.run(main())
