"""Command line: `mergewright run ...` runs the agent; `mergewright eval ...` runs it on a split
and judges it; `mergewright report ...` turns eval results into Markdown tables;
`mergewright tasks ...` selects, imports, validates and lists eval tasks; `mergewright graph`
prints the graph."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import get_args

from mergewright.config import (
    ConfigError,
    Limits,
    Price,
    load_dotenv,
    load_live_settings,
    load_price,
)
from mergewright.environments import DEFAULT_IMAGE, LocalRepo
from mergewright.evaluation import SYSTEMS, EvalRefused, EvalSpec, run_eval
from mergewright.graph import assemble_graph, next_after_coder, next_after_tester
from mergewright.llm import LLMClient, OpenAIClient, RecordingLLM, ScriptedLLM
from mergewright.oracle import validate_task, validation_state
from mergewright.report import ReportError, load_eval, render
from mergewright.runner import run_agent
from mergewright.sandbox import SandboxError
from mergewright.selection import DEV_WHERE, dev_candidates
from mergewright.stats import RESAMPLES
from mergewright.tasks import (
    REPLAY_FILE,
    Split,
    Task,
    fetch_swe_rebench_row,
    fetch_swe_rebench_rows,
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

    ev = sub.add_parser("eval", help="run a system k times on each task of a split, then judge it")
    ev.add_argument("--split", choices=get_args(Split), required=True)
    ev.add_argument("--system", choices=SYSTEMS, default="base")
    ev.add_argument("--k", type=int, default=1, help="trials per task")
    ev.add_argument("--llm", choices=["live", "replay"], required=True)
    ev.add_argument("--tasks", nargs="+", metavar="ID", help="only these tasks of the split")
    ev.add_argument("--tasks-dir", type=Path, default=Path("eval/tasks"))
    ev.add_argument("--out", type=Path, default=Path("results"))
    ev.add_argument("--work-dir", type=Path, default=Path("runs/eval"))

    rep = sub.add_parser("report", help="Markdown tables with intervals from eval results")
    rep.add_argument("evals", nargs="+", type=Path, metavar="EVAL_DIR", help="results/<eval-id>")
    rep.add_argument("--out", type=Path, help="write the report here (default: print it)")
    rep.add_argument("--seed", type=int, default=0, help="seed of the bootstrap")
    rep.add_argument("--resamples", type=int, default=RESAMPLES, help="bootstrap resamples")

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
    val.add_argument(
        "--stale", action="store_true", help="only tasks with no record or a stale record"
    )
    val.add_argument("--runs", type=int, default=3)
    val.add_argument("--work-dir", type=Path, default=Path("runs/oracle"))
    lst = tasks_sub.add_parser("list", parents=[common], help="list tasks and validation status")
    lst.add_argument("--split", choices=get_args(Split), help="only tasks of this split")
    lst.add_argument(
        "--check",
        action="store_true",
        help="exit 1 unless every listed record is valid and current",
    )
    sel = tasks_sub.add_parser("select", help="dev candidates from SWE-rebench, in rule order")
    sel.add_argument("--count", type=int, default=30)
    sel.add_argument("--per-repo", type=int, default=1)

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


def _eval(args: argparse.Namespace) -> int:
    limits = Limits()  # system base: the default config, frozen
    try:
        if args.k < 1:
            raise ConfigError("--k must be at least 1")
        tasks = [task for task in load_tasks(args.tasks_dir) if task.split == args.split]
        if args.tasks:
            by_id = {task.id: task for task in tasks}
            unknown = [i for i in args.tasks if i not in by_id]
            if unknown:
                raise ConfigError(f"not in split {args.split}: {', '.join(unknown)}")
            tasks = [by_id[i] for i in args.tasks]
        if not tasks:
            raise ConfigError(f"no {args.split} tasks in {args.tasks_dir}")
        model, price, make_llm = _eval_llm(args.llm, tasks, limits)
        spec = EvalSpec(args.split, args.system, args.k, model, price, limits)
        out = run_eval(tasks, spec, make_llm, args.out, args.work_dir)
    except EvalRefused as exc:
        print(f"error: the eval cannot start:\n{exc}", file=sys.stderr)
        return EXIT_CONFIG
    except (ConfigError, SandboxError, ValueError, OSError) as exc:  # SandboxError: no Docker
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"manifest: {out / 'manifest.json'}")
    return EXIT_DONE


def _report(args: argparse.Namespace) -> int:
    try:
        if args.resamples < 1:
            raise ReportError("--resamples must be at least 1")
        report = render([load_eval(path) for path in args.evals], args.seed, args.resamples)
        if args.out:
            args.out.write_text(report, encoding="utf-8")
    except (ReportError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    print(f"report: {args.out}" if args.out else report, end="\n" if args.out else "")
    return EXIT_DONE


def _eval_llm(
    mode: str, tasks: list[Task], limits: Limits
) -> tuple[str, Price | None, Callable[[Task], LLMClient]]:
    """The model label, its price (None: unknown) and the client for each trial."""
    if mode == "replay":
        missing = [task.id for task in tasks if not (task.path / REPLAY_FILE).is_file()]
        if missing:
            raise ConfigError(f"no {REPLAY_FILE} (needed by --llm replay): {', '.join(missing)}")

        def replay(task: Task) -> LLMClient:  # a fresh script per trial
            return ScriptedLLM.from_file(task.path / REPLAY_FILE)

        return "replay", Price(0.0, 0.0), replay
    load_dotenv(Path(".env"), os.environ)
    client = OpenAIClient(load_live_settings(), timeout_s=limits.llm_timeout_s)
    return client.model, load_price(), lambda task: client


def _tasks(args: argparse.Namespace) -> int:
    try:
        if args.tasks_command == "import":
            row = fetch_swe_rebench_row(args.instance_id)
            task_dir = import_swe_rebench(
                row, args.tasks_dir, args.split, resolve_digest=resolve_image_digest
            )
            print(f"imported {task_dir}")
            return EXIT_DONE
        if args.tasks_command == "select":
            rows = fetch_swe_rebench_rows(DEV_WHERE)
            candidates = dev_candidates(rows, per_repo=args.per_repo)
            print(f"{len(rows)} rows in the window; {len(candidates)} candidates", file=sys.stderr)
            print("\n".join(candidates[: args.count]))
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
        listed = [task for task in tasks if args.split in (None, task.split)]
        for task in listed:
            print(f"{task.id}  {task.split}  {_validation_status(task)}")
        current = all(validation_state(task)[0] == "valid" for task in listed)
        return EXIT_FAILED if args.check and not current else EXIT_DONE
    if not tasks:
        print(f"error: no tasks in {args.tasks_dir}", file=sys.stderr)
        return EXIT_CONFIG
    if args.stale:
        tasks = [t for t in tasks if validation_state(t)[0] in ("not validated", "stale")]
        if not tasks:
            print("every validation record is current")
            return EXIT_DONE
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
    state, record = validation_state(task)
    return f"{state} ({record['validated_at']})" if record else state


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
    if args.command == "eval":
        return _eval(args)
    if args.command == "report":
        return _report(args)
    return _run(args) if args.command == "run" else _graph()


if __name__ == "__main__":
    sys.exit(main())
