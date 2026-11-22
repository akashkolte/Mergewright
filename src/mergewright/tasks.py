"""Eval tasks: the task.json model and the SWE-rebench importer (spec Section 4.5).

A task is a folder `eval/tasks/<id>/` with task.json, the issue text, the gold patch and the
hidden test patch (never shown to the agent). The importer pins each image by digest, so a
re-pushed tag cannot change what a task runs.
"""

from __future__ import annotations

import json
import re
import secrets
import shlex
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, field_validator, model_validator

from mergewright.environments import ImageRepo, conda_test_command
from mergewright.sandbox import SandboxError, SandboxLimits
from mergewright.sandbox.docker import Docker
from mergewright.sandbox.process import docker_cli

TASK_FILE = "task.json"
DATASET = "nebius/SWE-rebench"
DATASET_API = "https://datasets-server.huggingface.co"
_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DIGEST_LINE = re.compile(r"^Digest:\s+(sha256:[0-9a-f]{64})\s*$", re.MULTILINE)
_DEFAULT_LIMITS = SandboxLimits()

Source = Literal["swe-rebench", "mined", "swe-gym", "adversarial", "local"]
Split = Literal["smoke", "train", "dev", "held-out", "adversarial"]


class Resources(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpus: float = _DEFAULT_LIMITS.cpus
    mem_floor_mb: int = _DEFAULT_LIMITS.mem_floor_mb
    mem_ceiling_mb: int = _DEFAULT_LIMITS.mem_ceiling_mb

    def limits(self) -> SandboxLimits:
        return SandboxLimits(
            cpus=self.cpus, mem_floor_mb=self.mem_floor_mb, mem_ceiling_mb=self.mem_ceiling_mb
        )


class Task(BaseModel):
    """One task.json. Load it with Task.load() to read the files next to it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    source: Source
    split: Split
    repo: str
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    image: str
    repo_dir: str = "/testbed"
    issue: str = "issue.md"
    gold_patch: str = "gold.patch"
    test_patch: str = "test.patch"
    f2p: list[str] = Field(min_length=1)
    p2p: list[str]
    test_cmd: str  # the exact command run in the container
    env: dict[str, str] = {}
    created_at: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
    resources: Resources = Resources()
    timeout_s: int = Field(1800, gt=0)

    _dir: Path | None = PrivateAttr(default=None)

    @field_validator("id")
    @classmethod
    def _safe_id(cls, value: str) -> str:
        if not _ID.match(value) or value in (".", ".."):
            raise ValueError(f"id {value!r} must match {_ID.pattern} and not be '.' or '..'")
        return value

    @field_validator("env")
    @classmethod
    def _shell_names(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [name for name in value if not _ENV_NAME.match(name)]
        if bad:
            raise ValueError(f"env names must be shell variable names, got {bad}")
        return value

    @model_validator(mode="after")
    def _pinned_image(self) -> Task:
        if self.source == "swe-rebench" and "@sha256:" not in self.image:
            raise ValueError(f"a swe-rebench image must be pinned by digest, got {self.image!r}")
        return self

    @classmethod
    def load(cls, task_dir: Path) -> Task:
        task_dir = Path(task_dir).resolve()
        task = cls.model_validate_json((task_dir / TASK_FILE).read_bytes())
        if task_dir.name != task.id:
            raise ValueError(f"task folder {task_dir} does not match the task id {task.id!r}")
        task._dir = task_dir
        return task

    @property
    def path(self) -> Path:
        """The task's folder (set by Task.load)."""
        if self._dir is None:
            raise ValueError("this task was not loaded from a folder; use Task.load()")
        return self._dir

    def issue_text(self) -> str:
        return self._read(self.issue)

    def gold_patch_text(self) -> str:
        return self._read(self.gold_patch)

    def test_patch_text(self) -> str:
        return self._read(self.test_patch)

    def environment(self, **overrides: Any) -> ImageRepo:
        """The task's image as an Environment, env exported before the test command.

        Overrides (e.g. start_sandbox) pass through.
        """
        exports = "".join(f"export {k}={shlex.quote(v)} && " for k, v in self.env.items())
        return ImageRepo(
            image=self.image,
            base_sha=self.base_sha,
            test_command=exports + self.test_cmd,
            repo_dir=self.repo_dir,
            limits=self.resources.limits(),
            **overrides,
        )

    def _read(self, name: str) -> str:
        path = (self.path / name).resolve()
        if not path.is_relative_to(self.path):
            raise ValueError(f"{name!r} is outside the task folder {self.path}")
        return path.read_bytes().decode("utf-8")  # bytes: keep a patch's line endings as they are


def load_tasks(tasks_dir: Path) -> list[Task]:
    """Every task in tasks_dir, sorted by id. Hidden folders (an import in progress) are skipped."""
    found = sorted(Path(tasks_dir).glob(f"*/{TASK_FILE}"))
    return [Task.load(path.parent) for path in found if not path.parent.name.startswith(".")]


# --- SWE-rebench ----------------------------------------------------------------------------


def task_from_swe_rebench(
    row: dict[str, Any], split: Split, digest: str
) -> tuple[Task, dict[str, str]]:
    """A SWE-rebench row as a Task and its files (name -> text). No I/O."""
    config = row.get("install_config") or {}
    task = Task(
        id=row["instance_id"],
        source="swe-rebench",
        split=split,
        repo=row["repo"],
        base_sha=row["base_commit"],
        image=f"{_untagged(_published_image(row))}@{digest}",
        f2p=_test_ids(row["FAIL_TO_PASS"]),
        p2p=_test_ids(row["PASS_TO_PASS"]),
        test_cmd=conda_test_command(config["test_cmd"]),
        env={key: str(value) for key, value in (config.get("env_vars") or {}).items()},
        created_at=_utc(row["created_at"]),
    )
    files = {
        task.issue: row["problem_statement"],
        task.gold_patch: row["patch"],
        task.test_patch: row["test_patch"],
    }
    return task, files


def resolve_image_digest(image: str, docker: Docker = docker_cli) -> str:
    """The registry digest (sha256:...) of image. Reads the manifest only; pulls nothing."""
    result = docker(["buildx", "imagetools", "inspect", image], timeout_s=120)
    if result.code != 0:
        raise SandboxError(f"could not inspect {image}: {result.output.strip()[-500:]}")
    found = _DIGEST_LINE.search(result.output)
    if not found:
        raise SandboxError(f"no digest in the manifest of {image}")
    return found.group(1)


def import_swe_rebench(
    row: dict[str, Any],
    tasks_dir: Path,
    split: Split,
    resolve_digest: Callable[[str], str] = resolve_image_digest,
) -> Path:
    """Write tasks_dir/<id>/ for a SWE-rebench row, image pinned by digest. Never overwrites.

    The files go to a hidden sibling folder first, renamed into place when complete, so a failed
    import leaves nothing behind.
    """
    task, files = task_from_swe_rebench(row, split, resolve_digest(_published_image(row)))
    task_dir = Path(tasks_dir) / task.id
    if task_dir.exists():
        raise FileExistsError(f"{task_dir} already exists")
    task_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = task_dir.parent / f".{task.id}-{secrets.token_hex(4)}"
    staging.mkdir()
    try:
        task_json = json.dumps(task.model_dump(mode="json"), indent=2) + "\n"
        (staging / TASK_FILE).write_text(task_json, encoding="utf-8")
        for name, text in files.items():
            (staging / name).write_bytes(text.encode("utf-8"))
        staging.rename(task_dir)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return task_dir


def fetch_swe_rebench_row(
    instance_id: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> dict[str, Any]:
    """One row from the Hugging Face dataset viewer API (public; no token needed).

    Retries 5xx and connection errors with backoff (2 s, 4 s, ...): the API has outages.
    """
    if not _ID.match(instance_id):  # no quotes: the id goes into the filter expression
        raise ValueError(f"bad instance id {instance_id!r}")
    query = {
        "dataset": DATASET,
        "config": "default",
        "split": "test",
        "where": f"\"instance_id\"='{instance_id}'",
        "offset": 0,
        "length": 1,
    }
    url = f"{DATASET_API}/filter?{urllib.parse.urlencode(query)}"
    request = urllib.request.Request(url, headers={"User-Agent": "mergewright"})
    for attempt in range(1, attempts + 1):
        try:
            with opener(request, timeout=60) as response:
                rows = json.load(response)["rows"]
            break
        except urllib.error.HTTPError as exc:  # before URLError: it is a subclass
            if exc.code < 500 or attempt == attempts:
                raise
        except urllib.error.URLError:
            if attempt == attempts:
                raise
        sleep(2**attempt)
    if not rows:
        raise LookupError(f"no SWE-rebench task {instance_id!r}")
    return rows[0]["row"]


def _published_image(row: dict[str, Any]) -> str:
    image = row.get("docker_image")
    if not image:
        raise ValueError(f"{row.get('instance_id')} has no published image")
    return image


def _untagged(image: str) -> str:
    """'repo/name:tag' -> 'repo/name'. A registry port ('host:5000/name') is kept."""
    image = image.split("@", 1)[0]
    colon = image.rfind(":")
    return image[:colon] if colon > image.rfind("/") else image


def _test_ids(value: list[str] | str) -> list[str]:
    """SWE-rebench test lists come as lists or as JSON-encoded strings."""
    return list(json.loads(value) if isinstance(value, str) else value)


def _utc(value: str) -> str:
    """'2020-10-12 13:21:50' (UTC, no zone) -> '2020-10-12T13:21:50Z'."""
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
