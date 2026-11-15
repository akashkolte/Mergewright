"""Start spike workflows on the local Temporal server.

    python -m spikes.runtime.start agent [--run-id ID] [--faults JSON] [--no-wait]
    python -m spikes.runtime.start probe [--no-wait]

The probe waits on a signal:
    temporal workflow signal -w <id> --name resume --input '"yes"'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path

from temporalio.client import Client

from mergewright.config import Limits
from mergewright.graph import recursion_limit
from spikes.runtime.connection import connect, connect_config, describe
from spikes.runtime.graph import new_run
from spikes.runtime.store import MessageStore
from spikes.runtime.worker import SPIKE_DIR
from spikes.runtime.workflows import (
    SERVICE_QUEUE,
    AgentInput,
    AgentWorkflow,
    InterruptProbeWorkflow,
)

REPO = Path("examples/buggy_calc")
ISSUE_FILE = Path("examples/inputs/buggy_calc_issue.md")


async def start_agent(
    client: Client,
    repo: Path,
    issue: str,
    spike_dir: Path,
    run_id: str,
    faults: dict | None = None,
    limits: Limits | None = None,
):
    state, context = new_run(
        repo, spike_dir / "runs", run_id, issue, MessageStore(spike_dir / "store"), faults=faults
    )
    workflow_input = AgentInput(
        state=dict(state),
        context=dict(context),
        recursion_limit=recursion_limit(limits or Limits()),
    )
    return await client.start_workflow(
        AgentWorkflow.run, workflow_input, id=f"agent-{run_id}", task_queue=SERVICE_QUEUE
    )


async def main() -> None:
    parser = argparse.ArgumentParser(description="Start a spike workflow.")
    parser.add_argument("kind", choices=["agent", "probe"])
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--faults", default="{}", help='e.g. {"tools": {"fail_times": 2}}')
    parser.add_argument("--no-wait", action="store_true")
    args = parser.parse_args()
    run_id = args.run_id or uuid.uuid4().hex[:8]
    print(f"server: {describe(connect_config())}")
    client = await connect()
    begin = time.monotonic()
    if args.kind == "agent":
        handle = await start_agent(
            client, REPO, ISSUE_FILE.read_text(), SPIKE_DIR, run_id, json.loads(args.faults)
        )
        print(f"workflow={handle.id} log={SPIKE_DIR / 'runs' / run_id / 'run.jsonl'}")
    else:
        handle = await client.start_workflow(
            InterruptProbeWorkflow.run,
            "approve the diff?",
            id=f"probe-{run_id}",
            task_queue=SERVICE_QUEUE,
        )
        print(f"workflow={handle.id}")
    if not args.no_wait:
        print(json.dumps(await handle.result(), indent=2))
        print(f"finished in {time.monotonic() - begin:.1f} s")


if __name__ == "__main__":
    asyncio.run(main())
