import json
import os
from pathlib import Path

import pytest

from mergewright.tools import (
    CODER_TOOLS,
    TOOLS,
    ToolError,
    Workspace,
    edit_file,
    execute,
    read_file,
    run_tests,
    search_code,
)
from tests.conftest import BUGGY_LINE, FIXED_LINES

# --- read_file -----------------------------------------------------------------------------


def test_read_file_returns_numbered_line_range(buggy_calc: Path):
    out = read_file(Workspace(buggy_calc), "calc.py", start=11, end=12)

    assert out.splitlines() == [
        "  11  def median(values: list[float]) -> float:",
        '  12      """Return the median of values."""',
    ]


def test_read_file_missing_file_is_an_error(buggy_calc: Path):
    with pytest.raises(ToolError, match="file not found"):
        read_file(Workspace(buggy_calc), "nope.py")


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/passwd", "tests/../../outside.txt"])
def test_read_file_blocks_paths_outside_the_workspace(buggy_calc: Path, path: str):
    (buggy_calc.parent / "outside.txt").write_text("secret")

    with pytest.raises(ToolError):
        read_file(Workspace(buggy_calc), path)


def test_read_file_blocks_symlink_escape(buggy_calc: Path):
    (buggy_calc.parent / "outside.txt").write_text("secret")
    (buggy_calc / "link.txt").symlink_to(buggy_calc.parent / "outside.txt")

    with pytest.raises(ToolError, match="outside"):
        read_file(Workspace(buggy_calc), "link.txt")


def test_read_file_truncates_long_files(buggy_calc: Path):
    (buggy_calc / "big.py").write_text("\n".join(f"x = {i}" for i in range(1000)))

    out = read_file(Workspace(buggy_calc), "big.py")

    assert out.endswith("[truncated]")
    assert len(out.splitlines()) == 401  # 400 lines plus the marker


# --- search_code ---------------------------------------------------------------------------


def test_search_code_returns_path_and_line(buggy_calc: Path):
    hits = search_code(Workspace(buggy_calc), r"def median")

    assert hits == [
        {"path": "calc.py", "line": 11, "text": "def median(values: list[float]) -> float:"}
    ]


def test_search_code_no_match_is_an_empty_list(buggy_calc: Path):
    assert search_code(Workspace(buggy_calc), r"def does_not_exist") == []


def test_search_code_invalid_regex_is_an_error(buggy_calc: Path):
    with pytest.raises(ToolError, match="invalid regular expression"):
        search_code(Workspace(buggy_calc), r"def (")


# --- edit_file -----------------------------------------------------------------------------


def test_edit_file_replaces_a_unique_match(buggy_calc: Path):
    edit_file(Workspace(buggy_calc), "calc.py", BUGGY_LINE, FIXED_LINES)

    assert FIXED_LINES in (buggy_calc / "calc.py").read_text()


def test_edit_file_old_text_not_found_is_an_error(buggy_calc: Path):
    with pytest.raises(ToolError, match="not found"):
        edit_file(Workspace(buggy_calc), "calc.py", "no such text", "x")


def test_edit_file_ambiguous_match_is_an_error(buggy_calc: Path):
    with pytest.raises(ToolError, match="found 3 times"):
        edit_file(Workspace(buggy_calc), "calc.py", "if not values:", "if values == []:")


def test_edit_file_blocks_paths_outside_the_workspace(buggy_calc: Path):
    with pytest.raises(ToolError, match="outside"):
        edit_file(Workspace(buggy_calc), "../calc.py", "a", "b")


@pytest.mark.parametrize(
    "path", ["tests/test_calc.py", ".github/workflows/ci.yml", ".git/config", "conftest.py"]
)
def test_edit_file_protects_tests_and_ci(buggy_calc: Path, path: str):
    target = buggy_calc / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()

    with pytest.raises(ToolError, match="not allowed"):
        edit_file(Workspace(buggy_calc), path, "a", "b")


# --- run_tests -----------------------------------------------------------------------------


def test_run_tests_reports_the_failing_test(buggy_calc: Path):
    report = run_tests(Workspace(buggy_calc))

    assert not report.passed
    assert (report.num_passed, report.num_failed) == (5, 1)
    assert report.failures == ["tests/test_calc.py::test_median_even_length"]


def test_run_tests_reports_a_pass_after_the_fix(fixed_calc: Path):
    report = run_tests(Workspace(fixed_calc))

    assert report.passed
    assert (report.num_passed, report.num_failed) == (6, 0)


def test_run_tests_timeout_is_a_result_not_an_exception(buggy_calc: Path):
    (buggy_calc / "tests" / "test_slow.py").write_text(
        "import time\n\ndef test_slow():\n    time.sleep(10)\n"
    )

    report = run_tests(Workspace(buggy_calc), timeout_s=1)

    assert report.timed_out and not report.passed


def test_run_tests_subprocess_gets_no_secrets(buggy_calc: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MERGEWRIGHT_API_KEY", "canary-secret-value")
    (buggy_calc / "tests" / "test_env.py").write_text(
        "import os\n\ndef test_no_secret():\n    assert 'MERGEWRIGHT_API_KEY' not in os.environ\n"
    )

    report = run_tests(Workspace(buggy_calc))

    assert "tests/test_env.py::test_no_secret" not in report.failures
    assert os.environ["MERGEWRIGHT_API_KEY"] == "canary-secret-value"  # parent unchanged


# --- registry ------------------------------------------------------------------------------


def test_execute_rejects_unknown_tool(buggy_calc: Path):
    result = execute("delete_repo", "{}", Workspace(buggy_calc), CODER_TOOLS)

    assert not result.ok and "unknown tool" in result.output


def test_execute_enforces_the_allow_list(buggy_calc: Path):
    result = execute("run_tests", "{}", Workspace(buggy_calc), CODER_TOOLS)

    assert not result.ok and "not allowed" in result.output


@pytest.mark.parametrize("raw_args", ["{not json", '{"pattern": 5}', "{}"])
def test_execute_rejects_invalid_arguments(buggy_calc: Path, raw_args: str):
    result = execute("search_code", raw_args, Workspace(buggy_calc), CODER_TOOLS)

    assert not result.ok and "invalid arguments" in result.output


def test_execute_returns_tool_errors_as_results(buggy_calc: Path):
    result = execute(
        "read_file", json.dumps({"path": "nope.py"}), Workspace(buggy_calc), CODER_TOOLS
    )

    assert not result.ok and "file not found" in result.output


def test_every_tool_has_a_valid_schema():
    for name, spec in TOOLS.items():
        schema = spec.schema()
        assert schema["function"]["name"] == name
        assert schema["function"]["parameters"]["type"] == "object"
