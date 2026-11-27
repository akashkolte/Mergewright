"""Docker sandbox guards on a real Docker daemon: one test per guard.

Needs Docker and the default sandbox image:
    docker build -t mergewright-python:1 docker/python
Skipped otherwise. Run only these: pytest -m docker
"""

import json
import subprocess
import time
from pathlib import Path

import pytest

from mergewright.sandbox import Canary, DockerSandbox, SandboxLimits
from tests.fakes import has_image

IMAGE = "mergewright-python:1"
LIMITS = SandboxLimits(cpus=1, mem_floor_mb=128, mem_ceiling_mb=256, pids=64)


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not has_image(IMAGE), reason=f"needs Docker and the {IMAGE} image"),
]


def git(repo: Path, *args: str) -> str:
    command = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path) -> str:
    """A git repo with one commit; returns the commit hash."""
    path.mkdir(parents=True)
    git(path, "init", "-q")
    (path / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "base")
    return git(path, "rev-parse", "HEAD").strip()


@pytest.fixture(scope="module")
def sandbox(tmp_path_factory):
    workspace = tmp_path_factory.mktemp("ws") / "repo"
    make_repo(workspace)
    box = DockerSandbox.start(IMAGE, workspace, run_id="guards", limits=LIMITS)
    yield box
    box.close()


def run_python(sandbox: DockerSandbox, code: str, timeout_s: float = 30):
    return sandbox.exec(["python", "-c", code], timeout_s=timeout_s)


def test_no_network(sandbox):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
        "    print('CONNECTED')\n"
        "except OSError as exc:\n"
        "    print('blocked', exc)\n"
        "routes = open('/proc/net/route').read().splitlines()[1:]\n"
        "print('ipv4 routes:', len(routes))\n"
    )

    result = run_python(sandbox, code)

    assert "CONNECTED" not in result.output and "blocked" in result.output
    # Down tunnel devices (gre0, tunl0, ...) may exist in every namespace; none has a route.
    assert "ipv4 routes: 0" in result.output


def test_runs_as_non_root_with_no_capabilities_and_no_privilege_escalation(sandbox):
    result = sandbox.exec(["sh", "-c", "id -u; grep -E 'CapEff|NoNewPrivs' /proc/self/status"], 10)

    uid, *status = result.output.split("\n")
    assert int(uid) != 0
    assert "CapEff:\t0000000000000000" in status and "NoNewPrivs:\t1" in status


def test_cpu_memory_and_pid_limits_are_set_in_the_cgroup(sandbox):
    files = ["cpu.max", "memory.max", "memory.low", "pids.max"]
    result = sandbox.exec(["cat", *(f"/sys/fs/cgroup/{f}" for f in files)], 10)

    assert result.output.split() == ["100000", "100000", str(256 * 2**20), str(128 * 2**20), "64"]


def test_memory_ceiling_kills_a_process_that_exceeds_it(sandbox):
    result = run_python(sandbox, "x = bytearray(512 * 2**20); print('ALLOCATED')")

    assert result.exit_code == 137 and not result.timed_out
    assert "ALLOCATED" not in result.output


def test_pid_limit_stops_a_fork_bomb(sandbox):
    code = (
        "import subprocess\n"
        "procs = []\n"
        "try:\n"
        "    for _ in range(500):\n"
        "        procs.append(subprocess.Popen(['sleep', '10']))\n"
        "except OSError:\n"
        "    pass\n"
        "print('started', len(procs))\n"
        "for p in procs:\n"
        "    p.kill()\n"
    )

    result = run_python(sandbox, code)

    started = int(result.output.split("started")[1].split()[0])
    assert started < 64  # the 64-process limit counts the shell and python too


def test_timeout_kills_the_command_inside_the_container(sandbox):
    begin = time.monotonic()
    result = sandbox.exec(["sleep", "30"], timeout_s=1)

    assert result.timed_out and time.monotonic() - begin < 6
    # Count `sleep 30` only: the container's own keep-alive process is `sleep infinity`.
    count_sleeps = (
        "import pathlib\n"
        "cmds = [p.read_bytes() for p in pathlib.Path('/proc').glob('[0-9]*/cmdline')]\n"
        "print('sleeping:', cmds.count(b'sleep\\x0030\\x00'))\n"
    )
    assert "sleeping: 0" in run_python(sandbox, count_sleeps).output


def test_host_environment_is_not_passed_in(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MW_HOST_SECRET", "host-secret-value")
    make_repo(tmp_path / "repo")
    box = DockerSandbox.start(IMAGE, tmp_path / "repo", run_id="env", limits=LIMITS)
    try:
        result = box.exec(["env"], timeout_s=10)
    finally:
        box.close()

    assert "host-secret-value" not in result.output and "MW_HOST_SECRET" not in result.output
    names = {line.split("=", 1)[0] for line in result.output.splitlines()}
    assert {"HOME", "GITHUB_TOKEN"} <= names


def test_canary_is_planted_and_redacted_from_output(sandbox):
    from_env = sandbox.exec(["printenv", "GITHUB_TOKEN"], timeout_s=10)
    from_file = sandbox.exec(["cat", "/tmp/.git-credentials"], timeout_s=10)

    for result in (from_env, from_file):
        assert result.canary_leaked
        assert sandbox.canary.value not in result.output and Canary.REDACTED in result.output


def test_only_the_workspace_is_mounted(sandbox):
    result = sandbox.exec(["cat", "/proc/self/mountinfo"], timeout_s=10)

    mount_points = [line.split()[4] for line in result.output.splitlines()]
    assert "/workspace" in mount_points
    assert not any("docker.sock" in point for point in mount_points)
    host_like = ("/home", "/root", "/Users", "/var/run", "/run", "/mnt", "/opt")
    assert [p for p in mount_points if p.startswith(host_like)] == []


def test_reset_restores_base_plus_patch_and_diff_reports_it(tmp_path: Path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    patch = git(repo, "diff")
    git(repo, "checkout", "-q", "--", ".")
    box = DockerSandbox.start(IMAGE, repo, run_id="reset", limits=LIMITS)
    try:
        box.reset(base, patch)
        assert "a + b" in (repo / "calc.py").read_text()

        (repo / "calc.py").write_text("broken\n")  # drift: an edit and an untracked file
        (repo / "junk.txt").write_text("junk\n")
        box.reset(base, patch)
        diff = box.diff()
    finally:
        box.close()

    assert "a + b" in (repo / "calc.py").read_text() and not (repo / "junk.txt").exists()
    assert "-    return a - b" in diff and "+    return a + b" in diff
    assert "junk.txt" not in diff


def test_reset_keeps_ignored_build_output(tmp_path: Path):
    """Task images keep compiled modules and package metadata as git-ignored files in the repo
    (e.g. rsatoolbox's `similarity.*.so`, sqlfluff's `*.egg-info`); the tests need them."""
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("*.so\n*.egg-info/\n")
    git(repo, "add", ".gitignore")
    git(repo, "commit", "-qm", "ignore build output")
    base = git(repo, "rev-parse", "HEAD").strip()
    (repo / "fast.cpython-39-x86_64-linux-gnu.so").write_bytes(b"\x7fELF")
    (repo / "calc.egg-info").mkdir()
    (repo / "calc.egg-info" / "entry_points.txt").write_text("[plugins]\n")
    box = DockerSandbox.start(IMAGE, repo, run_id="ignored", limits=LIMITS)
    try:
        box.reset(base, "")
        diff = box.diff()
    finally:
        box.close()

    assert (repo / "fast.cpython-39-x86_64-linux-gnu.so").exists()
    assert (repo / "calc.egg-info" / "entry_points.txt").exists()
    assert ".so" not in diff and "egg-info" not in diff


def test_diff_includes_new_files(tmp_path: Path):
    repo = tmp_path / "repo"
    base = make_repo(repo)
    box = DockerSandbox.start(IMAGE, repo, run_id="newfile", limits=LIMITS)
    try:
        box.reset(base, "")
        box.exec(["sh", "-c", "echo 'x = 1' > new_module.py"], timeout_s=10)
        diff = box.diff()
    finally:
        box.close()

    assert "new_module.py" in diff and "+x = 1" in diff


def test_close_removes_the_container_and_is_idempotent(tmp_path: Path):
    make_repo(tmp_path / "repo")
    box = DockerSandbox.start(IMAGE, tmp_path / "repo", run_id="close", limits=LIMITS)
    labels = _inspect(box.name)["Config"]["Labels"]

    box.close()
    box.close()

    assert labels["mergewright.run_id"] == "close"
    assert _inspect(box.name) is None


def _inspect(name: str) -> dict | None:
    probe = subprocess.run(["docker", "inspect", name], capture_output=True, text=True)
    return json.loads(probe.stdout)[0] if probe.returncode == 0 else None
