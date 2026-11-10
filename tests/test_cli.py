import json
from pathlib import Path

import pytest

from mergewright.__main__ import EXIT_CONFIG, EXIT_DONE, main
from tests.conftest import BUGGY_LINE, EXAMPLES_DIR, REPO_ROOT


@pytest.fixture(autouse=True)
def _in_repo_root(monkeypatch: pytest.MonkeyPatch):
    """The CLI defaults are relative to the repository root, as in the README."""
    monkeypatch.chdir(REPO_ROOT)
    for key in ("MERGEWRIGHT_MODEL", "MERGEWRIGHT_API_BASE", "MERGEWRIGHT_API_KEY"):
        monkeypatch.delenv(key, raising=False)


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


def test_run_leaves_the_example_repo_unchanged(tmp_path: Path):
    main(["run", "--llm", "replay", "--runs-dir", str(tmp_path)])

    assert BUGGY_LINE in (EXAMPLES_DIR / "buggy_calc" / "calc.py").read_text()


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


def test_graph_command_prints_mermaid(capsys):
    assert main(["graph"]) == EXIT_DONE

    out = capsys.readouterr().out
    assert "graph TD" in out and "coder" in out and "tester" in out
