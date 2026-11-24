import json
from pathlib import Path

import pytest

import mergewright.__main__ as cli
from mergewright.__main__ import EXIT_CONFIG, EXIT_DONE, EXIT_FAILED, main
from mergewright.environments import DEFAULT_IMAGE
from tests.conftest import BUGGY_LINE, EXAMPLES_DIR, REPO_ROOT
from tests.fakes import DIGEST, ROW_FILE, TASK_ID, has_image, make_task, mark_valid


@pytest.fixture(autouse=True)
def _in_repo_root(monkeypatch: pytest.MonkeyPatch):
    """The CLI defaults are relative to the repository root, as in the README."""
    monkeypatch.chdir(REPO_ROOT)
    for key in ("MERGEWRIGHT_MODEL", "MERGEWRIGHT_API_BASE", "MERGEWRIGHT_API_KEY"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.docker
@pytest.mark.skipif(not has_image(DEFAULT_IMAGE), reason=f"needs Docker and {DEFAULT_IMAGE}")
def test_replay_run_reaches_done_and_logs_every_tool(tmp_path: Path):
    code = main(["run", "--llm", "replay", "--runs-dir", str(tmp_path)])

    assert code == EXIT_DONE
    (log_file,) = tmp_path.glob("*/run.jsonl")
    events = [json.loads(line) for line in log_file.read_text().splitlines()]
    assert {e["tool"] for e in events if e["event"] == "tool_call"} == {
        "search_code",
        "read_file",
        "edit_file",
        "run_tests",
    }
    assert events[-1]["event"] == "run_finished" and events[-1]["status"] == "DONE"
    assert BUGGY_LINE in (EXAMPLES_DIR / "buggy_calc" / "calc.py").read_text()  # original intact


def test_live_mode_without_a_model_fails_fast(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # no .env here
    issue = tmp_path / "issue.md"
    issue.write_text("bug")

    code = main(
        ["run", "--llm", "live", "--repo", str(EXAMPLES_DIR / "buggy_calc"), "--issue", str(issue)]
    )

    assert code == EXIT_CONFIG
    assert "MERGEWRIGHT_MODEL" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()  # nothing ran


def test_missing_issue_file_is_a_clear_error(tmp_path: Path, capsys):
    code = main(["run", "--llm", "replay", "--issue", str(tmp_path / "missing.md")])

    assert code == EXIT_CONFIG
    assert "issue file not found" in capsys.readouterr().err


def test_run_without_docker_fails_with_a_clear_message(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))  # no docker binary on PATH

    code = main(["run", "--llm", "replay", "--runs-dir", str(tmp_path / "runs")])

    assert code == EXIT_CONFIG
    assert "Docker" in capsys.readouterr().err


def test_graph_command_prints_mermaid(capsys):
    assert main(["graph"]) == EXIT_DONE

    out = capsys.readouterr().out
    assert "graph TD" in out and "coder" in out and "tester" in out


def test_tasks_import_then_list(tmp_path: Path, monkeypatch, capsys):
    row = json.loads(ROW_FILE.read_text())
    fetched = []
    monkeypatch.setattr(cli, "fetch_swe_rebench_row", lambda i: fetched.append(i) or row)
    monkeypatch.setattr(cli, "resolve_image_digest", lambda image: DIGEST)
    tasks_dir = tmp_path / "tasks"

    imported = main(["tasks", "import", TASK_ID, "--split", "dev", "--tasks-dir", str(tasks_dir)])
    listed = main(["tasks", "list", "--tasks-dir", str(tasks_dir)])

    assert (imported, listed) == (EXIT_DONE, EXIT_DONE) and fetched == [TASK_ID]
    assert (tasks_dir / TASK_ID / "task.json").is_file()
    assert capsys.readouterr().out.splitlines()[-1] == f"{TASK_ID}  dev  not validated"


def test_tasks_validate_stale_runs_only_missing_and_stale_records(tmp_path, monkeypatch, capsys):
    current, stale, new = (make_task(tmp_path, f"org__r-{i}") for i in (1, 2, 3))
    mark_valid(current)
    mark_valid(stale)
    (stale.path / "gold.patch").write_text("changed")
    validated = []

    def fake_validate(task, work_dir, runs):
        validated.append(task.id)
        return {"valid": True, "seconds": 1.0, "problems": []}

    monkeypatch.setattr(cli, "validate_task", fake_validate)

    listed = main(["tasks", "list", "--tasks-dir", str(tmp_path)])
    states = [line.split("  ")[2] for line in capsys.readouterr().out.splitlines()]
    code = main(["tasks", "validate", "--stale", "--tasks-dir", str(tmp_path)])

    assert listed == code == EXIT_DONE
    assert states == ["valid (x)", "stale (x)", "not validated"]
    assert validated == ["org__r-2", "org__r-3"]


def test_tasks_list_check_fails_unless_every_listed_record_is_current(tmp_path: Path, capsys):
    current, stale = make_task(tmp_path, "org__r-1"), make_task(tmp_path, "org__r-2")
    mark_valid(current)
    mark_valid(stale)
    gold = stale.path / "gold.patch"
    original = gold.read_bytes()
    gold.write_text("changed")
    common = ["tasks", "list", "--tasks-dir", str(tmp_path), "--check"]

    assert main(common) == EXIT_FAILED  # org__r-2 is stale
    assert main([*common, "--split", "smoke"]) == EXIT_DONE  # no smoke tasks listed
    gold.write_bytes(original)  # current again
    assert main(common) == EXIT_DONE


def test_eval_checks_its_inputs_before_any_run(tmp_path: Path, capsys):
    make_task(tmp_path / "tasks", "org__r-1")  # no replay.json
    base = ["eval", "--split", "dev", "--llm", "replay", "--tasks-dir", str(tmp_path / "tasks"),
            "--out", str(tmp_path / "results")]  # fmt: skip

    no_replay = main(base)
    unknown = main([*base, "--tasks", "org__r-9"])
    no_jobs = main([*base, "--jobs", "0"])

    assert no_replay == unknown == no_jobs == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "no replay.json (needed by --llm replay): org__r-1" in err
    assert "--k and --jobs must be at least 1" in err
    assert "not in split dev: org__r-9" in err
    assert not (tmp_path / "results").exists()
