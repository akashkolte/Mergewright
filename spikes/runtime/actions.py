"""Temporal Cloud billable actions per run, counted from workflow history (agenda step 5).

    TEMPORAL_PROFILE=cloud python -m spikes.runtime.actions agent-c1 agent-c2 ...

Counting rules (docs.temporal.io/cloud/actions, read 2026-11-15) for what these workflows use:
one action per workflow start (continue-as-new included), per activity attempt (retries
included), per timer, per signal; two per child workflow start. Queries, heartbeats and
Cloud UI views are also billed but are not in the history, so they are not counted here; the
agent workflow uses none of them. Temporal's own metrics may differ by up to 5 %.

Price (docs.temporal.io/cloud/pricing, read 2026-11-15): $50 per million actions on the
Developer plan, plus 10 % of usage spend. Storage is billed too, but is tiny for these
histories (tens of KB per run).
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields

from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import HistoryEvent
from temporalio.client import Client

from spikes.runtime.connection import connect, connect_config, describe
from spikes.runtime.evidence import run_ids

PRICE_PER_MILLION_USD = 50.0
PLAN_FEE = 0.10  # Developer plan: 10 % of usage spend

E = EventType


@dataclass
class ActionCount:
    workflow_starts: int = 0
    activity_attempts: int = 0
    timers: int = 0
    signals: int = 0
    child_starts: int = 0
    updates: int = 0
    search_attribute_upserts: int = 0

    @property
    def total(self) -> int:
        return sum(getattr(self, f.name) for f in fields(self)) + self.child_starts  # child = 2

    def __add__(self, other: ActionCount) -> ActionCount:
        return ActionCount(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))


def count_actions(events: Iterable[HistoryEvent]) -> ActionCount:
    """Billable actions in one run's history.

    History keeps only an activity's last attempt: its ActivityTaskStarted event carries the
    attempt number, so attempts billed = that number. An activity that is still retrying may
    have no started event yet; count finished workflows only.
    """
    count = ActionCount()
    for event in events:
        kind = event.event_type
        if kind == E.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED:
            count.workflow_starts += 1
        elif kind == E.EVENT_TYPE_ACTIVITY_TASK_STARTED:
            count.activity_attempts += event.activity_task_started_event_attributes.attempt
        elif kind == E.EVENT_TYPE_TIMER_STARTED:
            count.timers += 1
        elif kind == E.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED:
            count.signals += 1
        elif kind == E.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED:
            count.child_starts += 1
        elif kind in (
            E.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_ACCEPTED,
            E.EVENT_TYPE_WORKFLOW_EXECUTION_UPDATE_REJECTED,
        ):
            count.updates += 1
        elif kind == E.EVENT_TYPE_UPSERT_WORKFLOW_SEARCH_ATTRIBUTES:
            count.search_attribute_upserts += 1
    return count


def runs_per_budget(
    actions_per_run: float,
    budget_usd: float = 150.0,
    price_per_million_usd: float = PRICE_PER_MILLION_USD,
    plan_fee: float = PLAN_FEE,
) -> float:
    if actions_per_run <= 0:
        return float("inf")
    usd_per_action = price_per_million_usd / 1_000_000 * (1 + plan_fee)
    return budget_usd / usd_per_action / actions_per_run


async def count_workflow(client: Client, workflow_id: str) -> ActionCount:
    """Actions for a workflow and all of its continue-as-new runs."""
    total = ActionCount()
    for run_id in await run_ids(client, workflow_id):
        history = await client.get_workflow_handle(workflow_id, run_id=run_id).fetch_history()
        total += count_actions(history.events)
    return total


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("workflow_ids", nargs="+")
    parser.add_argument("--budget", type=float, default=150.0, help="credits in USD")
    args = parser.parse_args()
    print(f"server: {describe(connect_config())}")
    client = await connect()
    per_run = {wid: await count_workflow(client, wid) for wid in args.workflow_ids}
    mean = sum(c.total for c in per_run.values()) / len(per_run)
    report = {
        "runs": {wid: {**asdict(c), "total": c.total} for wid, c in per_run.items()},
        "mean_actions_per_run": round(mean, 2),
        "usd_per_run": round(mean * PRICE_PER_MILLION_USD / 1e6 * (1 + PLAN_FEE), 6),
        f"runs_per_{args.budget:g}_usd": int(runs_per_budget(mean, args.budget)),
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
