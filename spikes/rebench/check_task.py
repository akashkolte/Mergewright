"""Check one SWE-rebench task in its published image: F2P fails at base, passes with the fix.

Phase 0 spike 2 (agenda step 3). Run it on an x86-64 Linux host; on Apple Silicon the image
runs under emulation and the timings are not representative.

    python -m spikes.rebench.check_task fetch                 # first task with an image
    python -m spikes.rebench.check_task fetch --instance-id ID
    python -m spikes.rebench.check_task run runs/spike/rebench/tasks/<id>.json

`run` pulls the image, starts a container with no network, checks that /testbed is at
base_commit, applies the test patch, runs the tests (F2P must fail, P2P must pass), applies
the gold patch, runs them again (both must pass), and removes the container. It prints a JSON
report with timings and saves it under runs/spike/rebench/.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DATASET = "nebius/SWE-rebench"
DATASET_API = "https://datasets-server.huggingface.co"
OUT_DIR = Path("runs/spike/rebench")
REPO_DIR = "/testbed"  # SWE-bench layout; the images use it (checked on spectree-64)
ACTIVATE = "source /opt/conda/etc/profile.d/conda.sh && conda activate testbed"
RESULT_WORDS = ("PASSED", "FAILED", "ERROR", "XFAIL", "XPASS")


@dataclass
class DockerResult:
    code: int
    output: str  # stdout + stderr


Docker = Callable[..., DockerResult]  # (args, input=None, timeout=None) -> DockerResult


def docker_cli(args: Sequence[str], input: str | None = None, timeout: float | None = None):
    proc = subprocess.run(
        ["docker", *args], input=input, capture_output=True, text=True, timeout=timeout
    )
    return DockerResult(proc.returncode, proc.stdout + proc.stderr)


# --- Pure parts --------------------------------------------------------------------------


def parse_pytest_results(log: str) -> dict[str, str]:
    """Test id -> outcome, from pytest's `-rA` short summary lines."""
    results = {}
    for line in log.splitlines():
        word, _, rest = line.partition(" ")
        if word in RESULT_WORDS and rest:
            results[_strip_message(rest) if word in ("FAILED", "ERROR") else rest.strip()] = word
    return results


def _strip_message(rest: str) -> str:
    """'tests/x.py::t[a - b] - AssertionError' -> 'tests/x.py::t[a - b]' (brackets aware)."""
    depth = 0
    for i, char in enumerate(rest):
        depth += char == "["
        depth -= char == "]"
        if depth == 0 and rest.startswith(" - ", i):
            return rest[:i]
    return rest.strip()


def patched_files(patch: str) -> list[str]:
    """Files the test patch adds or changes (not deletes), in order."""
    files = re.findall(r"^\+\+\+ b/(\S+)$", patch, flags=re.MULTILINE)
    return list(dict.fromkeys(files))


def verdict(
    f2p: list[str], p2p: list[str], base: dict[str, str], gold: dict[str, str]
) -> dict[str, Any]:
    problems = []
    problems += [f"F2P passed before the fix: {t}" for t in f2p if base.get(t) == "PASSED"]
    problems += [
        f"F2P not passing after the fix: {t} ({gold.get(t, 'missing')})"
        for t in f2p
        if gold.get(t) != "PASSED"
    ]
    for label, results in (("before", base), ("after", gold)):
        problems += [
            f"P2P not passing {label} the fix: {t} ({results.get(t, 'missing')})"
            for t in p2p
            if results.get(t) != "PASSED"
        ]
    return {
        "pass": not problems,
        "f2p_failing_before": f"{sum(base.get(t) != 'PASSED' for t in f2p)}/{len(f2p)}",
        "f2p_passing_after": f"{sum(gold.get(t) == 'PASSED' for t in f2p)}/{len(f2p)}",
        "p2p_passing_after": f"{sum(gold.get(t) == 'PASSED' for t in p2p)}/{len(p2p)}",
        "problems": problems,
    }


# --- The check ---------------------------------------------------------------------------


class TaskChecker:
    def __init__(
        self,
        docker: Docker = docker_cli,
        clock: Callable[[], float] = time.monotonic,
        platform: str = "linux/amd64",
        test_timeout_s: float = 1800.0,
    ) -> None:
        self.docker = docker
        self.clock = clock
        self.platform = platform
        self.test_timeout_s = test_timeout_s

    def check(self, task: dict[str, Any], pull: bool = True) -> dict[str, Any]:
        image = task["docker_image"]
        report: dict[str, Any] = {
            "instance_id": task["instance_id"],
            "image": image,
            "platform": self.platform,
            "created_at": task.get("created_at"),
            "f2p": len(task["FAIL_TO_PASS"]),
            "p2p": len(task["PASS_TO_PASS"]),
            "timings_s": {},
        }
        if pull:
            pulled = self._timed(report, "pull", ["pull", "--platform", self.platform, image])
            if pulled.code:
                return self._fail(report, f"pull failed: {pulled.output[-500:]}")
        size = self.docker(["image", "inspect", "--format", "{{.Size}}", image])
        report["image_bytes"] = int(size.output.strip()) if size.code == 0 else None

        name = "mw-check-" + re.sub(r"[^a-zA-Z0-9_.-]", "-", task["instance_id"])
        self.docker(["rm", "-f", name])  # a container left by an interrupted earlier check
        started = self._timed(
            report,
            "container_start",
            ["run", "-d", "--platform", self.platform, "--network", "none", "--name", name, image],
        )
        if started.code:
            return self._fail(report, f"container did not start: {started.output[-500:]}")
        try:
            return self._check_in(name, task, report)
        finally:
            self.docker(["rm", "-f", name])

    def _check_in(self, name: str, task: dict[str, Any], report: dict[str, Any]):
        head = self._exec(name, ["git", "rev-parse", "HEAD"]).output.strip()
        if head != task["base_commit"]:
            return self._fail(report, f"HEAD is {head}, expected {task['base_commit']}")
        dirty = self._exec(name, ["git", "status", "--porcelain", "--untracked-files=no"])
        report["dirty_before"] = dirty.output.strip().splitlines()

        if self._apply(name, task["test_patch"]).code:
            return self._fail(report, "test patch did not apply")
        files = patched_files(task["test_patch"])
        base_log = self._tests(name, task, files, report, "base_tests")
        if self._apply(name, task["patch"]).code:
            return self._fail(report, "gold patch did not apply")
        gold_log = self._tests(name, task, files, report, "gold_tests")

        base, gold = parse_pytest_results(base_log), parse_pytest_results(gold_log)
        report["verdict"] = verdict(task["FAIL_TO_PASS"], task["PASS_TO_PASS"], base, gold)
        report["log_tail"] = {"base": base_log[-1500:], "gold": gold_log[-1500:]}
        return report

    def _tests(self, name, task, files, report, label) -> str:
        config = task.get("install_config") or {}
        exports = "".join(
            f"export {k}={shlex.quote(str(v))} && "
            for k, v in (config.get("env_vars") or {}).items()
        )
        command = f"{ACTIVATE} && {exports}{config['test_cmd']} {shlex.join(files)}"
        begin = self.clock()
        result = self._exec(name, ["bash", "-c", command], timeout=self.test_timeout_s)
        report["timings_s"][label] = round(self.clock() - begin, 2)
        return result.output

    def _apply(self, name: str, patch: str) -> DockerResult:
        return self.docker(
            ["exec", "-i", "-w", REPO_DIR, name, "git", "apply", "-v", "-"], input=patch
        )

    def _exec(self, name: str, argv: list[str], timeout: float | None = None) -> DockerResult:
        return self.docker(["exec", "-w", REPO_DIR, name, *argv], timeout=timeout)

    def _timed(self, report: dict[str, Any], label: str, args: list[str]) -> DockerResult:
        begin = self.clock()
        result = self.docker(args)
        report["timings_s"][label] = round(self.clock() - begin, 2)
        return result

    @staticmethod
    def _fail(report: dict[str, Any], problem: str) -> dict[str, Any]:
        report["verdict"] = {"pass": False, "problems": [problem]}
        return report


# --- Fetching a task ---------------------------------------------------------------------


def fetch_task(
    instance_id: str | None = None,
    offset: int = 0,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> dict[str, Any]:
    """One task row from the Hugging Face dataset viewer API (public; no token needed).

    Retries 5xx and connection errors with backoff (2 s, 4 s, ...): the API has outages.
    """
    common = {"dataset": DATASET, "config": "default", "split": "test", "length": 1}
    if instance_id:
        if "'" in instance_id:
            raise SystemExit(f"bad instance id {instance_id!r}")
        query = {**common, "where": f"\"instance_id\"='{instance_id}'", "offset": 0}
        url = f"{DATASET_API}/filter?{urllib.parse.urlencode(query)}"
    else:
        url = f"{DATASET_API}/rows?{urllib.parse.urlencode({**common, 'offset': offset})}"
    request = urllib.request.Request(url, headers={"User-Agent": "mergewright-spike"})
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=60) as response:
                rows = json.load(response)["rows"]
            break
        except urllib.error.HTTPError as exc:
            if exc.code < 500 or attempt == attempts:
                raise
        except urllib.error.URLError:
            if attempt == attempts:
                raise
        sleep(2**attempt)
    if not rows:
        raise SystemExit(f"no task found for {instance_id or f'offset {offset}'}")
    return rows[0]["row"]


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    f = sub.add_parser("fetch", help="download one task's metadata to runs/spike/rebench/tasks/")
    f.add_argument("--instance-id")
    f.add_argument("--offset", type=int, default=0)
    r = sub.add_parser("run", help="check one task in its image")
    r.add_argument("task", type=Path)
    r.add_argument("--no-pull", action="store_true", help="use the local image as it is")
    r.add_argument("--platform", default="linux/amd64")
    args = parser.parse_args(argv)

    if args.command == "fetch":
        task = fetch_task(args.instance_id, args.offset)
        path = OUT_DIR / "tasks" / f"{task['instance_id']}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(task, indent=2) + "\n")
        print(f"saved {path} (image {task['docker_image']})")
        return

    task = json.loads(args.task.read_text())
    report = TaskChecker(platform=args.platform).check(task, pull=not args.no_pull)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = OUT_DIR / f"check-{task['instance_id']}-{stamp}.json"
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "log_tail"}, indent=2))
    print(f"{'PASS' if report['verdict']['pass'] else 'FAIL'}; full report: {path}")


if __name__ == "__main__":
    main()
