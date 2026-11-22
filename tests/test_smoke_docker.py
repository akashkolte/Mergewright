"""The smoke split end to end: agent on recorded responses, then the hidden-test oracle.

Needs only the default sandbox image (docker build -t mergewright-python:1 docker/python).
"""

from pathlib import Path

import pytest

from mergewright.environments import DEFAULT_IMAGE
from mergewright.llm import ScriptedLLM
from mergewright.oracle import evaluate
from mergewright.runner import run_agent
from mergewright.tasks import REPLAY_FILE, Task, load_tasks
from tests.test_environments_docker import _has_image

TASKS_DIR = Path(__file__).resolve().parents[1] / "eval" / "tasks"
SMOKE = [task for task in load_tasks(TASKS_DIR) if task.split == "smoke"]

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _has_image(DEFAULT_IMAGE), reason=f"needs {DEFAULT_IMAGE}"),
]


def test_the_smoke_split_has_five_tasks():
    assert len(SMOKE) == 5


@pytest.mark.parametrize("task", SMOKE, ids=[task.id for task in SMOKE])
def test_the_recorded_run_resolves_the_task(task: Task, tmp_path: Path):
    llm = ScriptedLLM.from_file(task.path / REPLAY_FILE)

    run = run_agent(task.environment(), task.issue_text(), llm, tmp_path / "runs", echo=None)
    verdict = evaluate(task, run.diff, tmp_path / "work")

    assert run.status == "DONE", run.reason
    assert verdict.resolved, verdict
