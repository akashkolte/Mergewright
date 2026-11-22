"""Task files and the SWE-rebench importer. Fake docker, resolver and opener: no network."""

import io
import json
import urllib.error
from pathlib import Path

import pytest
from pydantic import ValidationError

from mergewright.environments import ImageRepo, LocalRepo, conda_test_command
from mergewright.sandbox import SandboxError, SandboxLimits
from mergewright.sandbox.process import CommandResult
from mergewright.tasks import (
    Task,
    fetch_swe_rebench_row,
    fetch_swe_rebench_rows,
    import_swe_rebench,
    resolve_image_digest,
    task_from_swe_rebench,
)

ROW_FILE = Path(__file__).parent / "data" / "swe_rebench" / "0b01001001__spectree-64.json"
TASK_ID = "0b01001001__spectree-64"
DIGEST = "sha256:918c24cdc06282801eca6134c44d0521eceb98e395d2916c660ec92a0df75efd"
IMAGE = "swerebench/sweb.eval.x86_64.0b01001001_1776_spectree-64"


@pytest.fixture
def row() -> dict:
    return json.loads(ROW_FILE.read_text())


def fake_resolver(calls: list[str]):
    def resolve(image: str) -> str:
        calls.append(image)
        return DIGEST

    return resolve


def valid_fields(**overrides) -> dict:
    fields = {
        "id": "owner__repo-1",
        "source": "swe-rebench",
        "split": "dev",
        "repo": "owner/repo",
        "base_sha": "a" * 40,
        "image": f"img@{DIGEST}",
        "f2p": ["tests/test_x.py::test_new"],
        "p2p": [],
        "test_cmd": "pytest",
        "created_at": "2020-10-12T13:21:50Z",
    }
    return {**fields, **overrides}


def result(code=0, output=""):
    return CommandResult(code=code, output=output, timed_out=False, truncated=False, seconds=0.1)


# --- Import -------------------------------------------------------------------------------


def test_import_writes_the_task_folder_and_load_reads_it_back(row, tmp_path: Path):
    resolved: list[str] = []

    task_dir = import_swe_rebench(row, tmp_path, "dev", resolve_digest=fake_resolver(resolved))
    task = Task.load(task_dir)

    assert task_dir == tmp_path / TASK_ID
    assert sorted(p.name for p in task_dir.iterdir()) == [
        "gold.patch",
        "issue.md",
        "task.json",
        "test.patch",
    ]
    assert (task_dir / "task.json").read_text().startswith('{\n  "id": ')  # indent=2
    assert resolved == [IMAGE]
    assert task.image == f"{IMAGE}@{DIGEST}"
    assert (task.id, task.source, task.split, task.repo) == (
        TASK_ID,
        "swe-rebench",
        "dev",
        "0b01001001/spectree",
    )
    assert task.base_sha == row["base_commit"]
    assert task.test_cmd == conda_test_command(row["install_config"]["test_cmd"])
    assert task.f2p == ["tests/test_utils.py::test_parse_params"]
    assert task.p2p == row["PASS_TO_PASS"]
    assert task.env == {}
    assert task.created_at == "2020-10-12T13:21:50Z"
    assert task.issue_text() == row["problem_statement"]
    assert task.gold_patch_text() == row["patch"]
    assert task.test_patch_text() == row["test_patch"]


def test_conversion_strips_the_tag_and_decodes_string_test_lists(row):
    row = {
        **row,
        "docker_image": f"{IMAGE}:latest",
        "FAIL_TO_PASS": json.dumps(row["FAIL_TO_PASS"]),
        "install_config": {**row["install_config"], "env_vars": {"LANG": "C", "N": 1}},
    }

    task, files = task_from_swe_rebench(row, "smoke", DIGEST)

    assert task.image == f"{IMAGE}@{DIGEST}"
    assert task.f2p == ["tests/test_utils.py::test_parse_params"]
    assert task.env == {"LANG": "C", "N": "1"}
    assert set(files) == {"issue.md", "gold.patch", "test.patch"}


def test_a_row_without_an_image_is_rejected(row, tmp_path: Path):
    row["docker_image"] = None

    with pytest.raises(ValueError, match="has no published image"):
        import_swe_rebench(row, tmp_path, "dev", resolve_digest=fake_resolver([]))
    assert not (tmp_path / TASK_ID).exists()


def test_import_refuses_an_existing_task_folder(row, tmp_path: Path):
    (tmp_path / TASK_ID).mkdir()

    with pytest.raises(FileExistsError):
        import_swe_rebench(row, tmp_path, "dev", resolve_digest=fake_resolver([]))
    assert list((tmp_path / TASK_ID).iterdir()) == []


def test_a_failed_import_leaves_nothing_behind(row, tmp_path: Path, monkeypatch):
    def fail(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", fail)

    with pytest.raises(OSError, match="disk full"):
        import_swe_rebench(row, tmp_path, "dev", resolve_digest=fake_resolver([]))
    assert list(tmp_path.iterdir()) == []  # no task folder, no staging folder


# --- Task ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": "../evil"},
        {"id": ".."},
        {"base_sha": "abc123"},
        {"f2p": []},
        {"image": f"{IMAGE}:latest"},  # swe-rebench images must be pinned by digest
        {"surprise": 1},  # extra fields are refused
        {"env": {"A; rm -rf /": "x"}},  # env names go into a shell command
    ],
)
def test_task_rejects_invalid_fields(overrides):
    with pytest.raises(ValidationError):
        Task(**valid_fields(**overrides))


def test_load_refuses_a_folder_not_named_after_the_task(tmp_path: Path):
    task_dir = tmp_path / "other-name"
    task_dir.mkdir()
    (task_dir / "task.json").write_text(json.dumps(valid_fields()))

    with pytest.raises(ValueError, match="does not match the task id"):
        Task.load(task_dir)


def test_task_refuses_a_file_outside_its_folder(tmp_path: Path):
    task_dir = tmp_path / "owner__repo-1"
    task_dir.mkdir()
    (tmp_path / "secret.patch").write_text("not for the task\n")
    fields = valid_fields(gold_patch="../secret.patch")
    (task_dir / "task.json").write_text(json.dumps(fields))

    with pytest.raises(ValueError, match="outside the task folder"):
        Task.load(task_dir).gold_patch_text()


def test_environment_is_the_pinned_image_with_the_task_limits():
    task = Task(
        **valid_fields(resources={"cpus": 1, "mem_floor_mb": 256, "mem_ceiling_mb": 512}),
    )

    def starter(**kwargs):
        raise AssertionError("not started in this test")

    env = task.environment(start_sandbox=starter)

    assert isinstance(env, ImageRepo)
    assert (env.image, env.base_sha, env.test_command, env.repo_dir) == (
        f"img@{DIGEST}",
        "a" * 40,
        "export PYTHONPATH=/testbed:/testbed/src && pytest",
        "/testbed",
    )
    assert env.limits == SandboxLimits(cpus=1, mem_floor_mb=256, mem_ceiling_mb=512)
    assert env.start_sandbox is starter


def test_environment_exports_the_task_env_before_the_test_command():
    task = Task(**valid_fields(env={"LANG": "C.UTF-8", "OPTS": "a b'c"}))

    assert task.environment().test_command == (
        "export PYTHONPATH=/testbed:/testbed/src && "
        "export LANG=C.UTF-8 && export OPTS='a b'\"'\"'c' && pytest"
    )


def test_environment_imports_the_workspace_first_and_task_env_can_override_it():
    task = Task(**valid_fields(repo_dir="/repo", env={"PYTHONPATH": "/custom"}))

    assert task.environment().test_command == (
        "export PYTHONPATH=/repo:/repo/src && export PYTHONPATH=/custom && pytest"
    )


def test_a_local_task_runs_its_repo_folder_committed_as_base_sha(tmp_path: Path):
    task_dir = tmp_path / "local__x-1"
    task_dir.mkdir()
    fields = valid_fields(
        id="local__x-1", source="local", split="smoke", image="mergewright-python:1",
        repo_dir="/workspace",
    )  # fmt: skip
    (task_dir / "task.json").write_text(json.dumps(fields))

    env = Task.load(task_dir).environment()

    assert isinstance(env, LocalRepo)
    assert (env.path, env.image, env.base_sha) == (
        task_dir / "repo",
        "mergewright-python:1",
        "a" * 40,
    )
    assert env.test_command == "export PYTHONPATH=/workspace:/workspace/src && pytest"
    assert env.limits == SandboxLimits()


# --- Digest and fetch -----------------------------------------------------------------------


def test_resolve_image_digest_reads_the_manifest_digest():
    calls = []

    def docker(args, input=None, timeout_s=None, max_output_bytes=None):
        calls.append(list(args))
        return result(0, f"Name:      docker.io/{IMAGE}:latest\nDigest:    {DIGEST}\n")

    assert resolve_image_digest(IMAGE, docker=docker) == DIGEST
    assert calls == [["buildx", "imagetools", "inspect", IMAGE]]  # metadata only, no pull


@pytest.mark.parametrize(
    ("answer", "message"),
    [(result(1, "not found"), "could not inspect"), (result(0, "Name: x\n"), "no digest")],
)
def test_resolve_image_digest_fails_clearly(answer, message):
    with pytest.raises(SandboxError, match=message):
        resolve_image_digest(IMAGE, docker=lambda *args, **kwargs: answer)


def test_fetch_retries_server_errors_then_returns_the_row():
    attempts = []

    def opener(request, timeout):
        attempts.append(request)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(b'{"rows": [{"row": {"instance_id": "a__b-1"}}]}')

    sleeps = []
    row = fetch_swe_rebench_row("a__b-1", opener=opener, sleep=sleeps.append)

    assert row == {"instance_id": "a__b-1"}
    assert len(attempts) == 3 and sleeps == [2, 4]
    assert "where=%22instance_id%22%3D%27a__b-1%27" in attempts[0].full_url
    assert attempts[0].get_header("User-agent") == "mergewright"


def test_fetch_rows_pages_until_the_total_is_reached():
    pages = [
        {"rows": [{"row": {"n": 1}}, {"row": {"n": 2}}], "num_rows_total": 3},
        {"rows": [{"row": {"n": 3}}], "num_rows_total": 3},
    ]
    urls = []

    def opener(request, timeout):
        urls.append(request.full_url)
        return io.BytesIO(json.dumps(pages[len(urls) - 1]).encode())

    rows = fetch_swe_rebench_rows('"x"=1', opener=opener, sleep=None, page_size=2)

    assert rows == [{"n": 1}, {"n": 2}, {"n": 3}]
    assert "offset=0&length=2" in urls[0] and "offset=2&length=2" in urls[1]


def test_fetch_refuses_an_id_with_a_quote():
    with pytest.raises(ValueError, match="bad instance id"):
        fetch_swe_rebench_row("a' OR 1=1 --", opener=None)
