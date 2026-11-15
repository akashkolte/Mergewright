"""Connection settings and action counting for the Temporal Cloud spike. No network."""

from pathlib import Path

import pytest
from temporalio.api.enums.v1 import EventType
from temporalio.api.history.v1 import (
    ActivityTaskScheduledEventAttributes,
    ActivityTaskStartedEventAttributes,
    HistoryEvent,
)

from spikes.runtime.actions import ActionCount, count_actions, runs_per_budget
from spikes.runtime.connection import connect_config, describe

CONFIG = """
[profile.cloud]
address = "ns.acct.tmprl.cloud:7233"
namespace = "ns.acct"
api_key = "placeholder-key"
"""


def config_file(tmp_path: Path) -> str:
    path = tmp_path / "temporal.toml"
    path.write_text(CONFIG)
    return str(path)


def test_default_is_the_local_dev_server(tmp_path: Path):
    env = {"TEMPORAL_CONFIG_FILE": str(tmp_path / "missing.toml")}

    assert connect_config(env) == {"target_host": "localhost:7233", "namespace": "default"}


def test_cloud_profile_uses_the_cli_profile_with_tls(tmp_path: Path):
    env = {"TEMPORAL_CONFIG_FILE": config_file(tmp_path), "TEMPORAL_PROFILE": "cloud"}

    config = connect_config(env)

    assert config["target_host"] == "ns.acct.tmprl.cloud:7233"
    assert config["namespace"] == "ns.acct"
    assert config["tls"] is True and config["api_key"] == "placeholder-key"


def test_describe_never_shows_the_key(tmp_path: Path):
    env = {"TEMPORAL_CONFIG_FILE": config_file(tmp_path), "TEMPORAL_PROFILE": "cloud"}

    text = describe(connect_config(env))

    assert "placeholder-key" not in text
    assert text == "ns.acct.tmprl.cloud:7233 namespace=ns.acct tls api-key"


def event(kind: EventType.ValueType, event_id: int = 0, **attrs) -> HistoryEvent:
    return HistoryEvent(event_id=event_id, event_type=kind, **attrs)


def scheduled(event_id: int) -> HistoryEvent:
    return event(
        EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED,
        event_id,
        activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(),
    )


def started(scheduled_id: int, attempt: int) -> HistoryEvent:
    return event(
        EventType.EVENT_TYPE_ACTIVITY_TASK_STARTED,
        activity_task_started_event_attributes=ActivityTaskStartedEventAttributes(
            scheduled_event_id=scheduled_id, attempt=attempt
        ),
    )


def test_count_actions_bills_each_activity_attempt_and_each_start():
    events = [
        event(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED),
        event(EventType.EVENT_TYPE_WORKFLOW_TASK_COMPLETED),  # not billed
        scheduled(5),
        started(5, attempt=1),
        scheduled(8),
        started(8, attempt=3),  # history keeps only the last attempt: 3 attempts billed
        scheduled(11),  # scheduled, never started: not billed
        event(EventType.EVENT_TYPE_TIMER_STARTED),
        event(EventType.EVENT_TYPE_WORKFLOW_EXECUTION_SIGNALED),
        event(EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED),
    ]

    count = count_actions(events)

    assert count == ActionCount(
        workflow_starts=1, activity_attempts=4, timers=1, signals=1, child_starts=1
    )
    assert count.total == 1 + 4 + 1 + 1 + 2  # a child workflow start is 2 actions


def test_action_counts_add_up_across_continue_as_new_segments():
    first = ActionCount(workflow_starts=1, activity_attempts=4)
    second = ActionCount(workflow_starts=1, activity_attempts=3, signals=1)

    assert (first + second).total == 10


def test_runs_per_budget_includes_the_developer_plan_fee():
    # $150 / ($50 per million x 1.10) = 2.727M actions; at 10 actions per run: 272,727 runs
    assert runs_per_budget(10, budget_usd=150) == pytest.approx(272_727, rel=1e-4)
    assert runs_per_budget(0, budget_usd=150) == float("inf")
