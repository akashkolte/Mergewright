"""Evidence for the gate criteria, read from Temporal's workflow history and the run log.

    python -m spikes.runtime.evidence <workflow-id> [--grep TEXT]

Note: Temporal records only the last attempt of an activity (its ActivityTaskStarted event
carries the attempt number and the last failure); earlier attempts add no events.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from temporalio.api.enums.v1 import EventType
from temporalio.client import Client

from spikes.runtime.connection import connect


@dataclass
class Activity:
    name: str
    queue: str
    attempt: int = 0
    outcome: str = "pending"  # completed | failed | timed_out | pending
    last_failure: str = ""


@dataclass
class Segment:
    """One run of the workflow (continue-as-new starts a new one)."""

    run_id: str
    events: int
    payload_bytes: int
    grep_hits: int
    activities: list[Activity] = field(default_factory=list)


@dataclass
class Evidence:
    workflow_id: str
    segments: list[Segment]
    llm_steps: list[int]  # step number of each llm_call event in the run log

    @property
    def activities(self) -> list[Activity]:
        return [a for s in self.segments for a in s.activities]

    def queues_by_node(self) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        for activity in self.activities:
            result.setdefault(activity.name, set()).add(activity.queue)
        return result

    def duplicate_llm_steps(self) -> list[int]:
        return sorted(step for step, n in Counter(self.llm_steps).items() if n > 1)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["queues_by_node"] = {k: sorted(v) for k, v in self.queues_by_node().items()}
        data["duplicate_llm_steps"] = self.duplicate_llm_steps()
        return data


async def collect(
    client: Client, workflow_id: str, grep: str = "", log_path: Path | None = None
) -> Evidence:
    runs = await run_ids(client, workflow_id)
    segments = [await _segment(client, workflow_id, run_id, grep) for run_id in runs]
    llm_steps: list[int] = []
    if log_path and log_path.is_file():
        for line in log_path.read_text().splitlines():
            event = json.loads(line)
            if event["event"] == "llm_call":
                llm_steps.append(event["step"])
    return Evidence(workflow_id=workflow_id, segments=segments, llm_steps=llm_steps)


async def run_ids(client: Client, workflow_id: str) -> list[str]:
    """Run ids of the continue-as-new chain, oldest first, by following the history."""
    handle = client.get_workflow_handle(workflow_id)
    first = (await handle.describe()).raw_description.workflow_execution_info
    run_ids = [first.first_run_id or first.execution.run_id]
    while True:
        history = client.get_workflow_handle(workflow_id, run_id=run_ids[-1])
        next_run = ""
        async for event in history.fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_WORKFLOW_EXECUTION_CONTINUED_AS_NEW:
                attrs = event.workflow_execution_continued_as_new_event_attributes
                next_run = attrs.new_execution_run_id
        if not next_run:
            return run_ids
        run_ids.append(next_run)


async def _segment(client: Client, workflow_id: str, run_id: str, grep: str) -> Segment:
    history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
    activities: dict[int, Activity] = {}
    for event in history.events:
        kind = event.event_type
        if kind == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
            attrs = event.activity_task_scheduled_event_attributes
            activities[event.event_id] = Activity(
                name=attrs.activity_type.name, queue=attrs.task_queue.name
            )
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            attrs = event.activity_task_started_event_attributes
            activity = activities[attrs.scheduled_event_id]
            activity.attempt = attrs.attempt
            if attrs.HasField("last_failure"):
                activity.last_failure = _failure_text(attrs.last_failure)
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED:
            sid = event.activity_task_completed_event_attributes.scheduled_event_id
            activities[sid].outcome = "completed"
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED:
            attrs = event.activity_task_failed_event_attributes
            activities[attrs.scheduled_event_id].outcome = "failed"
            activities[attrs.scheduled_event_id].last_failure = _failure_text(attrs.failure)
        elif kind == EventType.EVENT_TYPE_ACTIVITY_TASK_TIMED_OUT:
            attrs = event.activity_task_timed_out_event_attributes
            activities[attrs.scheduled_event_id].outcome = "timed_out"
            activities[attrs.scheduled_event_id].last_failure = _failure_text(attrs.failure)
    payloads = list(_payload_data(history.to_json_dict()))
    return Segment(
        run_id=run_id,
        events=len(history.events),
        payload_bytes=sum(len(p) for p in payloads),
        grep_hits=sum(grep.encode() in p for p in payloads) if grep else 0,
        activities=list(activities.values()),
    )


def _failure_text(failure: Any) -> str:
    if failure.HasField("timeout_failure_info"):
        return f"timeout ({failure.message})"
    return failure.message


def _payload_data(node: Any):
    """Yield the decoded bytes of every payload in a history JSON dict."""
    if isinstance(node, dict):
        if "metadata" in node and isinstance(node.get("data"), str):
            yield base64.b64decode(node["data"])
        for value in node.values():
            yield from _payload_data(value)
    elif isinstance(node, list):
        for value in node:
            yield from _payload_data(value)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Print gate evidence for one workflow.")
    parser.add_argument("workflow_id")
    parser.add_argument("--grep", default="", help="text that must not appear in any payload")
    parser.add_argument("--log", type=Path, help="run.jsonl of the run, for LLM call counts")
    args = parser.parse_args()
    client = await connect()
    evidence = await collect(client, args.workflow_id, args.grep, args.log)
    print(json.dumps(evidence.to_dict(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
