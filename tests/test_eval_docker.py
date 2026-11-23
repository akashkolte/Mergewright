"""`mergewright eval` end to end on the smoke split: recorded responses, real sandbox and oracle.

The committed validation records may be stale, so the test validates a copy of the smoke tasks
first (one run each) and evaluates that copy. Needs only mergewright-python:1.
"""

import json
import shutil
from pathlib import Path

import pytest

from mergewright.__main__ import EXIT_DONE, main
from mergewright.environments import DEFAULT_IMAGE
from mergewright.oracle import validate_task
from mergewright.tasks import Task
from tests.test_environments_docker import _has_image
from tests.test_smoke_docker import SMOKE

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _has_image(DEFAULT_IMAGE), reason=f"needs {DEFAULT_IMAGE}"),
]


def test_eval_on_the_smoke_split_resolves_every_task(tmp_path: Path):
    tasks_dir = tmp_path / "tasks"
    for task in SMOKE:
        shutil.copytree(task.path, tasks_dir / task.id)
        assert validate_task(Task.load(tasks_dir / task.id), tmp_path / "oracle", runs=1)["valid"]

    code = main(
        ["eval", "--split", "smoke", "--k", "1", "--llm", "replay", "--tasks-dir", str(tasks_dir),
         "--out", str(tmp_path / "results"), "--work-dir", str(tmp_path / "work")]
    )  # fmt: skip

    assert code == EXIT_DONE
    (out,) = (tmp_path / "results").iterdir()
    manifest = json.loads((out / "manifest.json").read_text())
    assert sorted(manifest["tasks"]) == sorted(task.id for task in SMOKE)
    assert all(entry["image_id"].startswith("sha256:") for entry in manifest["tasks"].values())
    for task in SMOKE:
        record = json.loads((out / "runs" / task.id / "base" / "1.json").read_text())
        assert record["resolved"], record
        assert record["tool_calls"] > 0 and record["test_attempts"] >= 1
        assert (out / "runs" / task.id / "base" / "1.trace.jsonl").is_file()
