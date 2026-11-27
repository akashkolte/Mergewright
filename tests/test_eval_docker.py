"""`mergewright eval` end to end on the smoke split (5 tasks): recorded responses, real
sandbox and oracle; every trial must reach DONE and resolve.

The committed validation records may be stale, so the test validates a copy of the smoke tasks
first (one run each) and evaluates that copy: four tasks with --jobs 2, then --resume adds the
fifth. Needs only mergewright-python:1.
"""

import json
import shutil
from functools import partial
from pathlib import Path

import pytest

from mergewright import __main__ as cli
from mergewright.__main__ import EXIT_DONE, main
from mergewright.environments import DEFAULT_IMAGE
from mergewright.evaluation import run_eval
from mergewright.oracle import validate_task
from mergewright.tasks import Task, load_tasks
from tests.fakes import has_image

TASKS_DIR = Path(__file__).resolve().parents[1] / "eval" / "tasks"
SMOKE = [task for task in load_tasks(TASKS_DIR) if task.split == "smoke"]

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not has_image(DEFAULT_IMAGE), reason=f"needs {DEFAULT_IMAGE}"),
]


def test_eval_on_the_smoke_split_resolves_every_task(tmp_path: Path, monkeypatch):
    assert len(SMOKE) == 5
    tasks_dir = tmp_path / "tasks"
    for task in SMOKE:
        shutil.copytree(task.path, tasks_dir / task.id)
        assert validate_task(Task.load(tasks_dir / task.id), tmp_path / "oracle", runs=1)["valid"]
    # --resume refuses a dirty git tree, and the tree may be dirty while working on the code.
    monkeypatch.setattr(
        cli, "run_eval", partial(run_eval, git=lambda: {"sha": "x", "dirty": False})
    )
    ids = [task.id for task in SMOKE]
    common = ["eval", "--split", "smoke", "--k", "2", "--jobs", "2", "--llm", "replay",
              "--tasks-dir", str(tasks_dir), "--out", str(tmp_path / "results"),
              "--work-dir", str(tmp_path / "work")]  # fmt: skip

    assert main([*common, "--eval-id", "e2e", "--tasks", *ids[:-1]]) == EXIT_DONE
    assert main([*common, "--resume", "e2e"]) == EXIT_DONE  # runs only the fifth task

    out = tmp_path / "results" / "e2e"
    manifest = json.loads((out / "manifest.json").read_text())
    assert sorted(manifest["tasks"]) == sorted(ids)
    assert [i["tasks"] for i in manifest["invocations"]] == [ids[:-1], ids[-1:]]
    assert all(entry["image_id"].startswith("sha256:") for entry in manifest["tasks"].values())
    for task_id in ids:
        for k in (1, 2):
            record = json.loads((out / "runs" / task_id / "base" / f"{k}.json").read_text())
            assert record["resolved"], record  # resolved implies status DONE
            assert record["tool_calls"] > 0 and record["test_attempts"] >= 1
            assert (out / "runs" / task_id / "base" / f"{k}.trace.jsonl").is_file()
