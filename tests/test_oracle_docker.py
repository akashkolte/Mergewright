"""The oracle end to end on the real spectree-64 image. Skipped unless the image is present.

docker pull swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64   (1.3 GB)
"""

import json
from pathlib import Path

import pytest

from mergewright.oracle import evaluate, validate_task
from mergewright.tasks import Task, import_swe_rebench
from tests.fakes import DIGEST, ROW_FILE, SPECTREE, has_image

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not has_image(SPECTREE), reason=f"needs {SPECTREE}"),
]


@pytest.fixture
def task(tmp_path: Path) -> Task:
    row = json.loads(ROW_FILE.read_text())
    return Task.load(import_swe_rebench(row, tmp_path / "tasks", "dev", lambda image: DIGEST))


def test_gold_resolves_and_the_empty_patch_does_not(task: Task, tmp_path: Path):
    gold = evaluate(task, task.gold_patch_text(), tmp_path / "work")
    empty = evaluate(task, "", tmp_path / "work")

    assert gold.resolved, gold
    assert not empty.resolved and empty.f2p_failing == task.f2p and empty.p2p_failing == []


def test_the_task_validates(task: Task, tmp_path: Path):
    record = validate_task(task, tmp_path / "work", runs=3)

    assert record["valid"], record["problems"]
