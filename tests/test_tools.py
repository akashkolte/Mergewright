import json
from pathlib import Path

import pytest

from mergewright.sandbox import ExecResult
from mergewright.tools import (
    CODER_TOOLS,
    TOOLS,
    ToolError,
    Workspace,
    confirm_failures,
    edit_file,
    execute,
    gate,
    parse_pytest_results,
    read_file,
    run_tests,
    run_tests_for_coder,
    search_code,
)
from tests.conftest import BUGGY_LINE, FIXED_LINES
from tests.fakes import ScriptedSandbox

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


def test_edit_file_shows_the_exact_text_when_only_whitespace_differs(buggy_calc: Path):
    calc = buggy_calc / "calc.py"
    before = calc.read_text()
    # As copied from read_file output: two extra spaces of indentation on the second line.
    old = "ordered = sorted(values)\n      return ordered[len(ordered) // 2]"

    with pytest.raises(ToolError) as error:
        edit_file(Workspace(buggy_calc), "calc.py", old, "x")

    message = str(error.value)
    assert "lines 15-16 match it except for whitespace" in message
    assert (
        "<<<\n    ordered = sorted(values)\n    return ordered[len(ordered) // 2]\n>>>" in message
    )
    assert calc.read_text() == before  # the edit stays exact: nothing is written


def test_edit_file_ambiguous_match_is_an_error(buggy_calc: Path):
    with pytest.raises(ToolError, match="found 3 times"):
        edit_file(Workspace(buggy_calc), "calc.py", "if not values:", "if values == []:")


def test_edit_file_blocks_paths_outside_the_workspace(buggy_calc: Path):
    with pytest.raises(ToolError, match="outside"):
        edit_file(Workspace(buggy_calc), "../calc.py", "a", "b")


@pytest.mark.parametrize(
    "path",
    [
        "tests/test_calc.py",
        ".github/workflows/ci.yml",
        ".git/config",
        "conftest.py",
        # pytest config: an edit here could skip or deselect the failing tests
        "pytest.ini",
        "pyproject.toml",
        "setup.cfg",
        "tox.ini",
    ],
)
def test_edit_file_protects_tests_test_config_and_ci(buggy_calc: Path, path: str):
    target = buggy_calc / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()

    with pytest.raises(ToolError, match="not allowed"):
        edit_file(Workspace(buggy_calc), path, "a", "b")


# --- run_tests -----------------------------------------------------------------------------

FAILING_RUN = """\
....F.
=========================== short test summary info ============================
FAILED tests/test_calc.py::test_median_even_length - assert 3 == 2.5
1 failed, 5 passed in 0.03s
"""
COLLECTION_ERRORS_RUN = """\
E   ModuleNotFoundError: No module named 'flask'
=========================== short test summary info ============================
ERROR tests/test_plugin_flask.py
ERROR tests/test_spec.py
12 passed, 2 errors in 4.56s
"""


def scripted(output: str, exit_code: int, **kwargs) -> ScriptedSandbox:
    return ScriptedSandbox([ExecResult(exit_code, output, False, **kwargs)])


def test_run_tests_runs_the_command_in_the_sandbox_without_cache_writes():
    sandbox = scripted("6 passed in 0.01s\n", 0)

    run_tests(sandbox, "python -m pytest -q", timeout_s=42)

    (argv,) = sandbox.commands
    assert argv[:2] == ["bash", "-c"]
    assert "PYTHONDONTWRITEBYTECODE=1" in argv[2] and argv[2].endswith("python -m pytest -q")


def test_run_tests_reports_the_failing_test():
    report = run_tests(scripted(FAILING_RUN, 1), "pytest")

    assert not report.passed
    assert (report.num_passed, report.num_failed) == (5, 1)
    assert report.failures == ["tests/test_calc.py::test_median_even_length"]
    assert report.errors == []
    assert report.results == {"tests/test_calc.py::test_median_even_length": "FAILED"}


def test_parse_pytest_results_strips_messages_but_not_bracketed_ids():
    log = """\
PASSED tests/test_a.py::test_ok
PASSED tests/test_a.py::test_param[x - y]
FAILED tests/test_a.py::test_param[a - b] - AssertionError: 1 - 2
ERROR tests/test_b.py - ModuleNotFoundError: No module named 'flask'
XFAIL tests/test_a.py::test_known
1 failed, 2 passed, 1 xfailed, 1 error in 0.10s
"""

    assert parse_pytest_results(log) == {
        "tests/test_a.py::test_ok": "PASSED",
        "tests/test_a.py::test_param[x - y]": "PASSED",
        "tests/test_a.py::test_param[a - b]": "FAILED",
        "tests/test_b.py": "ERROR",
        "tests/test_a.py::test_known": "XFAIL",
    }


def test_run_tests_reports_a_pass():
    report = run_tests(scripted("6 passed in 0.01s\n", 0), "pytest")

    assert report.passed and (report.num_passed, report.num_failed) == (6, 0)


def test_run_tests_keeps_collection_errors_apart_from_failures():
    report = run_tests(scripted(COLLECTION_ERRORS_RUN, 1), "pytest")

    assert report.errors == ["tests/test_plugin_flask.py", "tests/test_spec.py"]
    assert report.failures == [] and report.num_passed == 12 and report.num_errors == 2


def test_gate_ignores_failures_and_errors_known_at_base_and_nothing_else():
    known = frozenset({"tests/test_plugin_flask.py", "tests/test_spec.py"})
    errors_only = run_tests(scripted(COLLECTION_ERRORS_RUN, 1), "pytest")
    failing = run_tests(scripted(FAILING_RUN, 1), "pytest")

    assert gate(errors_only, known) == []
    assert gate(errors_only, frozenset()) == ["tests/test_plugin_flask.py", "tests/test_spec.py"]
    assert gate(failing, known) == ["tests/test_calc.py::test_median_even_length"]
    assert gate(failing, frozenset({"tests/test_calc.py::test_median_even_length"})) == []


def test_parse_ignores_captured_log_lines_and_the_tail_drops_passed_lines():
    output = (
        "ERROR    pkg.helper:helper.py:101 A template error occured\n"
        "PASSED tests/test_a.py::test_ok\n"
        "1 passed in 0.01s\n"
    )

    report = run_tests(scripted(output, 0), "pytest")

    assert report.results == {"tests/test_a.py::test_ok": "PASSED"} and report.errors == []
    assert "PASSED tests/test_a.py::test_ok" not in report.log_tail
    assert "1 passed in 0.01s" in report.log_tail


def test_coder_run_tests_says_pass_and_finish_even_with_pytest_exit_code_1():
    # Seen in pilot 4: pytest exited 1 with nothing failed; the model read it as a failure.
    result = run_tests_for_coder(scripted("38 passed in 2.1s\n", 1), "pytest", "{}", 60)

    assert result.output.startswith("All selected tests passed. 38 passed, 0 failed, 0 errors.")
    assert "call finish" in result.output and "exit code" not in result.output


def test_coder_run_tests_marks_failures_that_were_there_at_base():
    known = frozenset({"tests/test_calc.py::test_median_even_length"})

    result = run_tests_for_coder(scripted(FAILING_RUN, 1), "pytest", "{}", 60, known)

    assert result.output.startswith("No new failures.")
    assert "Already failing before your change (not caused by you):\n- tests/test_calc.py" in (
        result.output
    )


def test_gate_fails_when_no_test_ran():
    report = run_tests(scripted(COLLECTION_ERRORS_RUN.replace("12 passed, ", ""), 1), "pytest")

    assert gate(report, frozenset(report.errors)) == ["(no test passed)"]


def test_run_tests_timeout_is_a_result_not_an_exception():
    sandbox = ScriptedSandbox([ExecResult(124, "", True, seconds=1.0)])

    report = run_tests(sandbox, "pytest", timeout_s=1)

    assert report.timed_out and not report.passed
    assert gate(report, frozenset()) == ["(timed out)"]


def test_run_tests_passes_on_the_canary_flag():
    report = run_tests(scripted("[REDACTED:canary]\n1 passed\n", 0, canary_leaked=True), "pytest")

    assert report.canary_leaked


# --- registry ------------------------------------------------------------------------------


def test_execute_rejects_unknown_tool(buggy_calc: Path):
    result = execute("delete_repo", "{}", Workspace(buggy_calc), CODER_TOOLS)

    assert not result.ok and "unknown tool" in result.output


def test_execute_enforces_the_allow_list(buggy_calc: Path):
    result = execute("search_code", '{"pattern": "x"}', Workspace(buggy_calc), ("read_file",))

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


def test_coder_run_tests_runs_the_chosen_ids_shell_quoted_and_lists_failures():
    still_failing = ExecResult(1, FAILING_RUN, False)
    sandbox = ScriptedSandbox([ExecResult(1, FAILING_RUN, False), still_failing])
    ids = ["tests/test_calc.py::test_median_even_length", "tests/x.py::t[a b]"]

    result = run_tests_for_coder(sandbox, "pytest -q", json.dumps({"test_ids": ids}), 60)

    chosen, rerun = sandbox.commands
    assert chosen[2].endswith(
        "pytest -q tests/test_calc.py::test_median_even_length 'tests/x.py::t[a b]'"
    )
    assert rerun[2].endswith("pytest -q tests/test_calc.py::test_median_even_length")
    assert result.ok and "5 passed, 1 failed" in result.output
    assert "- tests/test_calc.py::test_median_even_length" in result.output


def test_confirm_failures_drops_a_test_that_passes_when_re_run_alone():
    blamed = ["tests/test_slug.py::test_slug[a-1]", "(no test passed)"]
    flaky = ScriptedSandbox([ExecResult(0, "1 passed in 0.01s\n", False)])
    real = ScriptedSandbox([ExecResult(1, "FAILED tests/test_slug.py::test_slug[a-1]\n", False)])
    lost = ScriptedSandbox([ExecResult(124, "", True)])

    assert confirm_failures(flaky, "pytest", blamed, 60) == (
        ["(no test passed)"],
        ["tests/test_slug.py::test_slug[a-1]"],
    )
    assert confirm_failures(real, "pytest", blamed, 60) == (blamed, [])
    assert confirm_failures(lost, "pytest", blamed, 60) == (blamed, [])  # in doubt: failing
    assert confirm_failures(ScriptedSandbox([]), "pytest", ["(timed out)"], 60) == (
        ["(timed out)"],
        [],
    )  # nothing to re-run


def test_coder_run_tests_says_when_a_failure_was_flaky():
    sandbox = ScriptedSandbox(
        [ExecResult(1, FAILING_RUN, False), ExecResult(0, "1 passed in 0.01s\n", False)]
    )

    result = run_tests_for_coder(sandbox, "pytest", "{}", 60)

    assert result.output.startswith("No new failures.")
    assert "passed when re-run alone (flaky; ignored):\n- tests/test_calc.py" in result.output


def test_coder_run_tests_refuses_options_and_never_runs_them():
    sandbox = ScriptedSandbox([])

    result = run_tests_for_coder(sandbox, "pytest", json.dumps({"test_ids": ["-p", "x"]}), 60)

    assert not result.ok and "not a test file or node id" in result.output
    assert sandbox.commands == []


def test_run_tests_needs_the_sandbox_so_execute_refuses_it(buggy_calc: Path):
    result = execute("run_tests", "{}", Workspace(buggy_calc), CODER_TOOLS)

    assert not result.ok and "runs in the sandbox" in result.output


def test_every_tool_has_a_valid_schema():
    for name, spec in TOOLS.items():
        schema = spec.schema()
        assert schema["function"]["name"] == name
        assert schema["function"]["parameters"]["type"] == "object"
