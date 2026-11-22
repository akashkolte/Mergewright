"""Command line: `mergewright run ...` runs the agent; `mergewright tasks ...` imports, validates
and lists eval tasks; `mergewright graph` prints the graph."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import get_args

from mergewright.config import ConfigError, Limits, load_dotenv, load_live_settings
from mergewright.environments import DEFAULT_IMAGE, LocalRepo
from mergewright.graph import assemble_graph, next_after_coder, next_after_tester
from mergewright.llm import LLMClient, OpenAIClient, RecordingLLM, ScriptedLLM
from mergewright.oracle import VALIDATION_FILE, validate_task
from mergewright.runner import run_agent
from mergewright.sandbox import SandboxError
from mergewright.tasks import (
    Split,
    Task,
    fetch_swe_rebench_row,
    import_swe_rebench,
    load_tasks,
    resolve_image_digest,
)

EXIT_DONE, EXIT_FAILED, EXIT_CONFIG = 0, 1, 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mergewright", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the agent on one repository and issue")
    run.add_argument("--llm", choices=["live", "replay"], required=True)
    run.add_argument("--repo", type=Path, default=Path("examples/buggy_calc"))
    run.add_argument("--issue", type=Path, default=Path("examples/inputs/buggy_calc_issue.md"))
    run.add_argument(
        "--replay-file",
        type=Path,
        default=Path("examples/inputs/buggy_calc_replay.json"),
        help="recorded model responses for --llm replay",
    )
    run.add_argument("--record", type=Path, help="save live responses to this replay file")
    run.add_argument("--runs-dir", type=Path, default=Path("runs"))
    run.add_argument(
        "--image",
        default=DEFAULT_IMAGE,
        help="Docker image the repo's tests run in (build the default: docker/python)",
    )

    tasks = sub.add_parser("tasks", help="import, validate and list eval tasks")
    tasks_sub = tasks.add_subparsers(dest="tasks_command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--tasks-dir", type=Path, default=Path("eval/tasks"))
    imp = tasks_sub.add_parser("import", parents=[common], help="import one SWE-rebench task")
    imp.add_argument("instance_id")
    imp.add_argument("--split", choices=get_args(Split), required=True)
    val = tasks_sub.add_parser(
        "validate", parents=[common], help="check tasks with the empty and gold patches"
    )
    val.add_argument("ids", nargs="*", help="task ids (default: every task)")
    val.add_argument("--runs", type=int, default=3)
    val.add_argument("--work-dir", type=Path, default=Path("runs/oracle"))
    tasks_sub.add_parser("list", parents=[common], help="list tasks and their validation status")

    sub.add_parser("graph", help="print the workflow graph as Mermaid")
    return parser


def _make_llm(args: argparse.Namespace, limits: Limits) -> LLMClient:
    if args.llm == "replay":
        if not args.replay_file.is_file():
            raise ConfigError(f"replay file not found: {args.replay_file}")
        return ScriptedLLM.from_file(args.replay_file)
    load_dotenv(Path(".env"), os.environ)
    client: LLMClient = OpenAIClient(load_live_settings(), timeout_s=limits.llm_timeout_s)
    return RecordingLLM(client, args.record) if args.record else client


def _run(args: argparse.Namespace) -> int:
    limits = Limits()
    for path, what in ((args.repo, "repository"), (args.issue, "issue file")):
        if not path.exists():
            print(f"error: {what} not found: {path}", file=sys.stderr)
            return EXIT_CONFIG
    try:
        llm = _make_llm(args, limits)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    env = LocalRepo(args.repo, image=args.image)
    try:
        result = run_agent(env, args.issue.read_text(), llm, args.runs_dir, limits)
    except SandboxError as exc:  # no Docker, daemon not running, image missing, ...
        print(f"error: the sandbox could not start: {exc}", file=sys.stderr)
        print("Docker is required; see the README for setup.", file=sys.stderr)
        return EXIT_CONFIG
    print("\n--- diff ---")
    print(result.diff or "(no changes)")
    print(f"--- status: {result.status} ({result.reason}) ---")
    print(f"run log: {result.run_dir / 'run.jsonl'}")
    return EXIT_DONE if result.status == "DONE" else EXIT_FAILED


def _tasks(args: argparse.Namespace) -> int:
    try:
        if args.tasks_command == "import":
            row = fetch_swe_rebench_row(args.instance_id)
            task_dir = import_swe_rebench(
                row, args.tasks_dir, args.split, resolve_digest=resolve_image_digest
            )
            print(f"imported {task_dir}")
            return EXIT_DONE
        ids = getattr(args, "ids", None)
        tasks = [Task.load(args.tasks_dir / i) for i in ids] if ids else load_tasks(args.tasks_dir)
    except SandboxError as exc:  # resolving the digest needs Docker
        print(f"error: {exc}", file=sys.stderr)
        print("Docker is required; see the README for setup.", file=sys.stderr)
        return EXIT_CONFIG
    except (LookupError, ValueError, OSError) as exc:  # not found, bad task, network, exists
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    if args.tasks_command == "list":
        for task in tasks:
            print(f"{task.id}  {task.split}  {_validation_status(task)}")
        return EXIT_DONE
    if not tasks:
        print(f"error: no tasks in {args.tasks_dir}", file=sys.stderr)
        return EXIT_CONFIG
    all_valid = True
    for task in tasks:
        record = validate_task(task, args.work_dir, runs=args.runs)
        all_valid &= record["valid"]
        detail = f"valid ({record['seconds']} s)" if record["valid"] else _invalid(record)
        print(f"{task.id}: {detail}", flush=True)
    return EXIT_DONE if all_valid else EXIT_FAILED


def _invalid(record: dict) -> str:
    problems = record["problems"]
    return f"INVALID, {len(problems)} problem(s), first: {problems[0]}"


def _validation_status(task: Task) -> str:
    path = task.path / VALIDATION_FILE
    if not path.is_file():
        return "not validated"
    record = json.loads(path.read_text(encoding="utf-8"))
    return f"{'valid' if record['valid'] else 'invalid'} ({record['validated_at']})"


def _graph() -> int:
    # The graph's shape does not depend on the node bodies, so no-op nodes are enough to draw it.
    def noop(state):
        return {}

    graph = assemble_graph(
        coder=noop,
        tools=noop,
        tester=noop,
        after_coder=next_after_coder,
        after_tester=next_after_tester,
    )
    print(graph.compile().get_graph().draw_mermaid())
    return EXIT_DONE


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "tasks":
        return _tasks(args)
    return _run(args) if args.command == "run" else _graph()


if __name__ == "__main__":
    sys.exit(main())
