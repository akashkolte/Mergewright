"""Criterion 1: kill -9 a worker process mid-run; no completed node may run again.

Needs a running Temporal server (temporal server start-dev).

    python -m spikes.runtime.kill_test sandbox   # kill the worker running the tools node
    python -m spikes.runtime.kill_test service   # kill the worker hosting the workflow + llm

The run's first tools call sleeps (injected fault), so the kill lands while coder step 1 has
completed and tools is in flight. The script then restarts the worker and waits for the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from temporalio.client import Client

from spikes.runtime.evidence import collect
from spikes.runtime.start import ISSUE_FILE, REPO, start_agent
from spikes.runtime.worker import SPIKE_DIR, TEMPORAL_ADDRESS

ROLES = {"service": "service,llm", "sandbox": "sandbox"}


def spawn(role: str, logs: Path) -> subprocess.Popen:
    log = (logs / f"worker-{role}.log").open("a")
    return subprocess.Popen(
        [sys.executable, "-m", "spikes.runtime.worker", "--queues", ROLES[role]],
        stdout=log,
        stderr=subprocess.STDOUT,
    )


async def wait_for(condition, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        await asyncio.sleep(0.2)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("victim", choices=sorted(ROLES))
    parser.add_argument("--sleep", type=float, default=15.0, help="first tools call sleeps")
    args = parser.parse_args()

    run_id = f"kill-{args.victim}-{uuid.uuid4().hex[:6]}"
    run_dir = SPIKE_DIR / "runs" / run_id
    logs = SPIKE_DIR / "logs" / run_id
    logs.mkdir(parents=True, exist_ok=True)
    workers = {role: spawn(role, logs) for role in ROLES}
    try:
        client = await Client.connect(TEMPORAL_ADDRESS)
        faults = {"tools": {"sleep_s": args.sleep, "sleep_times": 1}}
        handle = await start_agent(client, REPO, ISSUE_FILE.read_text(), SPIKE_DIR, run_id, faults)
        counter = run_dir / "faults" / "tools.count"
        await wait_for(counter.exists, 60, "the first tools call")

        victim = workers[args.victim]
        victim.send_signal(signal.SIGKILL)
        victim.wait()
        killed_at = time.monotonic()
        print(f"killed {args.victim} worker pid={victim.pid} while tools was in flight")
        workers[args.victim] = spawn(args.victim, logs)

        result = await asyncio.wait_for(handle.result(), timeout=300)
        print(f"run finished {time.monotonic() - killed_at:.1f}s after the kill")
        evidence = await collect(
            client, handle.id, "mean of the two middle values", run_dir / "run.jsonl"
        )
        print(
            json.dumps(
                {
                    "workflow_id": handle.id,
                    "status": result["status"],
                    "llm_steps": evidence.llm_steps,
                    "duplicate_llm_steps": evidence.duplicate_llm_steps(),
                    "tools_executions": int(counter.read_text()),
                    "activities": [
                        {k: getattr(a, k) for k in ("name", "attempt", "outcome", "last_failure")}
                        for a in evidence.activities
                    ],
                },
                indent=2,
            )
        )
    finally:
        for worker in workers.values():
            worker.terminate()
        for worker in workers.values():
            worker.wait(timeout=10)


if __name__ == "__main__":
    asyncio.run(main())
