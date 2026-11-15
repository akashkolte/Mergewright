"""The SWE-rebench task check, with a fake docker. No Docker or network needed."""

from spikes.rebench.check_task import (
    DockerResult,
    TaskChecker,
    parse_pytest_results,
    patched_files,
    verdict,
)

TEST_PATCH = """diff --git a/tests/common.py b/tests/common.py
--- a/tests/common.py
+++ b/tests/common.py
@@ -1 +1 @@
-x
+y
diff --git a/tests/test_utils.py b/tests/test_utils.py
--- a/tests/test_utils.py
+++ b/tests/test_utils.py
@@ -1 +1 @@
-a
+b
diff --git a/tests/test_old.py b/tests/test_old.py
deleted file mode 100644
--- a/tests/test_old.py
+++ /dev/null
"""

TASK = {
    "instance_id": "owner__repo-1",
    "docker_image": "swerebench/sweb.eval.x86_64.owner_1776_repo-1",
    "base_commit": "abc123",
    "test_patch": TEST_PATCH,
    "patch": "diff --git a/src/x.py b/src/x.py\n",
    "install_config": {"test_cmd": "pytest -rA -p no:cacheprovider"},
    "FAIL_TO_PASS": ["tests/test_utils.py::test_new"],
    "PASS_TO_PASS": ["tests/test_utils.py::test_old[a - b]", "tests/test_utils.py::test_other"],
    "created_at": "2025-01-01 00:00:00",
}

BASE_LOG = """
tests/test_utils.py .F.
=========================== short test summary info ============================
PASSED tests/test_utils.py::test_old[a - b]
PASSED tests/test_utils.py::test_other
FAILED tests/test_utils.py::test_new - AssertionError: assert 1 == 2
"""
GOLD_LOG = """
PASSED tests/test_utils.py::test_old[a - b]
PASSED tests/test_utils.py::test_other
PASSED tests/test_utils.py::test_new
"""


def test_parse_pytest_results_reads_the_short_summary_lines():
    results = parse_pytest_results(BASE_LOG + "ERROR tests/test_broken.py - ImportError\n")

    assert results == {
        "tests/test_utils.py::test_old[a - b]": "PASSED",
        "tests/test_utils.py::test_other": "PASSED",
        "tests/test_utils.py::test_new": "FAILED",
        "tests/test_broken.py": "ERROR",
    }


def test_patched_files_skips_deleted_files():
    assert patched_files(TEST_PATCH) == ["tests/common.py", "tests/test_utils.py"]


def test_verdict_passes_when_f2p_fails_before_and_passes_after():
    base = parse_pytest_results(BASE_LOG)
    gold = parse_pytest_results(GOLD_LOG)

    result = verdict(TASK["FAIL_TO_PASS"], TASK["PASS_TO_PASS"], base, gold)

    assert result["pass"] is True
    assert result["problems"] == []


def test_verdict_reports_each_broken_expectation():
    base = {"tests/test_utils.py::test_new": "PASSED"}  # F2P already passes: bad task
    gold = {"tests/test_utils.py::test_new": "PASSED", "tests/test_utils.py::test_other": "FAILED"}

    result = verdict(TASK["FAIL_TO_PASS"], TASK["PASS_TO_PASS"], base, gold)

    assert result["pass"] is False
    assert "F2P passed before the fix: tests/test_utils.py::test_new" in result["problems"]
    assert (
        "P2P not passing after the fix: tests/test_utils.py::test_other (FAILED)"
        in result["problems"]
    )
    assert any("test_old[a - b] (missing)" in p for p in result["problems"])


class FakeDocker:
    """Records docker CLI calls. Answers the first (match, answer) whose match is in the
    command; an answer that is a list gives its items in order, one per call."""

    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def __call__(self, args, input=None, timeout=None):
        self.calls.append((args, input))
        command = " ".join(args)
        for match, answer in self.answers:
            if match in command:
                return answer.pop(0) if isinstance(answer, list) else answer
        return DockerResult(0, "")


def ok(output=""):
    return DockerResult(0, output)


def checker_with(docker):
    ticks = iter(range(1000))
    return TaskChecker(docker, clock=lambda: float(next(ticks)), platform="linux/amd64")


def test_check_runs_base_then_gold_and_passes():
    docker = FakeDocker(
        [
            ("image inspect", ok("5050000000")),
            ("rev-parse HEAD", ok("abc123\n")),
            ("conda activate testbed", [DockerResult(1, BASE_LOG), DockerResult(0, GOLD_LOG)]),
        ]
    )

    report = checker_with(docker).check(TASK, pull=False)

    assert report["verdict"]["pass"] is True
    assert report["image_bytes"] == 5050000000
    commands = [" ".join(args) for args, _ in docker.calls]
    run = next(c for c in commands if c.startswith("run -d"))
    assert "--network none" in run and "--platform linux/amd64" in run
    patches = [stdin for args, stdin in docker.calls if stdin]
    assert patches == [TASK["test_patch"], TASK["patch"]]  # test patch first, then the fix
    test_runs = [c for c in commands if "conda activate testbed" in c]
    assert len(test_runs) == 2
    assert "tests/common.py tests/test_utils.py" in test_runs[0]
    assert any(c.startswith("rm -f") for c in commands)  # container removed


def test_check_stops_when_the_repo_is_not_at_the_base_commit():
    docker = FakeDocker([("image inspect", ok("1")), ("rev-parse HEAD", ok("other\n"))])

    report = checker_with(docker).check(TASK, pull=False)

    assert report["verdict"]["pass"] is False
    assert "HEAD is other, expected abc123" in report["verdict"]["problems"][0]
    assert any(" ".join(a).startswith("rm -f") for a, _ in docker.calls)


def test_check_stops_when_a_patch_does_not_apply():
    docker = FakeDocker(
        [
            ("image inspect", ok("1")),
            ("rev-parse HEAD", ok("abc123\n")),
            ("git apply", DockerResult(1, "error: patch failed")),
        ]
    )

    report = checker_with(docker).check(TASK, pull=False)

    assert report["verdict"]["pass"] is False
    assert "test patch did not apply" in report["verdict"]["problems"][0]


def test_fetch_task_retries_server_errors_then_returns_the_row():
    import io
    import urllib.error

    from spikes.rebench.check_task import fetch_task

    attempts = []

    def opener(request, timeout):
        attempts.append(request.full_url)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(b'{"rows": [{"row": {"instance_id": "a__b-1"}}]}')

    sleeps = []
    task = fetch_task("a__b-1", opener=opener, sleep=sleeps.append)

    assert task == {"instance_id": "a__b-1"}
    assert len(attempts) == 3 and sleeps == [2, 4]
    assert "where=%22instance_id%22%3D%27a__b-1%27" in attempts[0]
