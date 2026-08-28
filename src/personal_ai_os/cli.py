"""Command line interface.

``paios doctor`` is the important one. When someone -- a future local agent,
or you six months from now -- opens this repository cold, the first question
is "does this machine actually work?", and answering it in one command is the
difference between resuming work and re-deriving the environment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from personal_ai_os import __version__
from personal_ai_os.config.loader import load_settings
from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.memory.tasks import TaskPriority, TaskStatus, TaskStore
from personal_ai_os.observability.logging import setup_logging
from personal_ai_os.observability.trace import (
    find_trace,
    latest_trace,
    read_trace,
)
from personal_ai_os.runtime import Runtime

OK = "OK  "
WARN = "WARN"
FAIL = "FAIL"


def _status(label: str, state: str, detail: str = "") -> str:
    return f"  [{state}] {label:<28} {detail}".rstrip()


# --- doctor ----------------------------------------------------------------


def cmd_doctor(args: argparse.Namespace) -> int:
    print(f"Personal AI OS {__version__} -- environment check\n")
    failures = 0

    try:
        settings = load_settings(args.workspace)
    except PersonalAIOSError as exc:
        print(_status("configuration", FAIL, str(exc).splitlines()[0]))
        print("\nCannot continue without valid configuration.")
        return 1

    print(_status("configuration", OK, "config/default.yaml validated"))
    print(_status("workspace root", OK, str(settings.workspace_root)))

    runtime = Runtime.build(settings=settings, configure_logging=False)
    try:
        # Filesystem jail
        roots = settings.resolved_allowed_roots()
        print(_status("allowed roots", OK, ", ".join(str(r) for r in roots)))

        # Traces
        runs_dir = runtime.runs_dir
        try:
            runs_dir.mkdir(parents=True, exist_ok=True)
            probe = runs_dir / ".write-probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            print(_status("runs directory", OK, str(runs_dir)))
        except OSError as exc:
            failures += 1
            print(_status("runs directory", FAIL, f"{runs_dir}: {exc}"))

        # Structured memory
        try:
            task_count = TaskStore(runtime.store).count()
            print(
                _status(
                    "database",
                    OK,
                    f"{runtime.store.path} (schema v{runtime.store.version}, "
                    f"{task_count} tasks)",
                )
            )
        except PersonalAIOSError as exc:
            failures += 1
            print(_status("database", FAIL, str(exc)))

        # Models
        print()
        print(f"  models ({settings.models.provider}):")
        any_model = False
        for tier, health in runtime.models.health_report():
            if health is None:
                print(
                    _status(
                        f"  tier {tier}", WARN, "unmapped -- router will degrade past it"
                    )
                )
                continue
            if health.ok:
                any_model = True
                print(_status(f"  tier {tier}", OK, health.model))
            elif health.server_reachable:
                failures += 1
                print(_status(f"  tier {tier}", FAIL, f"{health.model}: {health.detail}"))
            else:
                failures += 1
                print(_status(f"  tier {tier}", FAIL, health.detail))
        if not any_model:
            print("\n  No usable model. Is `ollama serve` running, and have you pulled one?")

        # Tools and agents
        print()
        print(_status("tools registered", OK, ", ".join(runtime.tools.names())))
        if len(runtime.agents):
            print(_status("agents registered", OK, ", ".join(runtime.agents.names())))
        else:
            print(_status("agents registered", WARN, "no manifests found in agents/"))

        # Permissions
        policy = settings.permissions.policy
        summary = ", ".join(f"{k.value}={v}" for k, v in sorted(policy.items()))
        mode = "interactive" if settings.permissions.interactive else "non-interactive"
        print(_status("permission policy", OK, f"{mode}; {summary}"))

    finally:
        runtime.close()

    print()
    if failures:
        print(f"{failures} check(s) failed.")
        return 1
    print("All checks passed.")
    return 0


# --- models ----------------------------------------------------------------


def cmd_models(args: argparse.Namespace) -> int:
    runtime = Runtime.build(workspace_root=args.workspace, configure_logging=False)
    try:
        print(f"provider: {runtime.settings.models.provider}")
        print(f"base_url: {runtime.settings.models.ollama.base_url}\n")
        print(f"  {'TIER':<8} {'MODEL':<28} {'TEMP':<6} {'NUM_CTX':<9} STATUS")

        for info in runtime.models.describe():
            if not info.mapped:
                print(f"  {info.tier:<8} {'(unmapped)':<28} {'-':<6} {'-':<9} degrades down")
                continue
            health = runtime.models.get_tier(info.tier).health()
            status = "available" if health.ok else (health.detail or "unavailable")
            print(
                f"  {info.tier:<8} {info.model:<28} {info.temperature:<6} "
                f"{info.num_ctx:<9} {status}"
            )

        if runtime.settings.models.roles:
            print("\n  roles:")
            for role, tier in sorted(runtime.settings.models.roles.items()):
                print(f"    {role:<12} -> {tier}")
        print(f"\n  default tier: {runtime.settings.models.default_tier}")
    finally:
        runtime.close()
    return 0


# --- agents ----------------------------------------------------------------


def cmd_agents(args: argparse.Namespace) -> int:
    runtime = Runtime.build(workspace_root=args.workspace, configure_logging=False)
    try:
        if not len(runtime.agents):
            print("No agents registered. Add a manifest to agents/*.yaml")
            return 0

        for spec in runtime.agents.all():
            pref = spec.model
            want = pref.model or pref.role or pref.tier or "default"
            try:
                resolved = runtime.select_model(spec).model.name
            except PersonalAIOSError as exc:
                resolved = f"<unresolved: {exc}>"

            print(f"\n  {spec.name}  (v{spec.version})")
            print(f"    {spec.description.strip()}")
            print(f"    model       : {want} -> {resolved}")
            print(f"    tools       : {', '.join(spec.tools) or '<none>'}")
            print(
                f"    permissions : "
                f"{', '.join(p.value for p in spec.permissions) or '<none>'}"
            )
            print(f"    limits      : max_iterations={spec.max_iterations or '-'}")
            if spec.requires_human_approval:
                print("    approval    : always requires a human")
        print()
    finally:
        runtime.close()
    return 0


# --- run -------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> int:
    runtime = Runtime.build(workspace_root=args.workspace)
    try:
        result = runtime.run_agent(args.agent, args.objective)
    except PersonalAIOSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.close()

    print()
    print(result.output or "(no output)")
    print()
    print(
        f"  [{'ok' if result.ok else result.stop_reason.value}] "
        f"agent={result.agent} model={result.model} "
        f"iterations={result.iterations} tool_calls={result.tool_calls}"
    )
    if result.error:
        print(f"  error: {result.error}")
    print(f"  trace: paios trace {result.run_id}")
    return 0 if result.ok else 1


# --- tasks -----------------------------------------------------------------


def cmd_tasks(args: argparse.Namespace) -> int:
    """Human access to the task list, with no model in the loop.

    Useful on its own, and useful for verifying that what an agent claims it
    saved is actually in the database.
    """
    runtime = Runtime.build(workspace_root=args.workspace, configure_logging=False)
    try:
        tasks = TaskStore(runtime.store)

        if args.task_command == "add":
            task = tasks.add(
                args.title,
                notes=args.notes or "",
                priority=TaskPriority(args.priority),
                due_date=args.due,
            )
            print(f"added {task.summary()}")
            return 0

        if args.task_command == "done":
            task = tasks.complete(args.id)
            print(f"completed {task.summary()}")
            return 0

        status = TaskStatus(args.status) if args.status else None
        found = tasks.list(status=status, include_done=args.all, limit=args.limit)
        if not found:
            print("no tasks")
            return 0
        for task in found:
            print(f"  {task.summary()}")
        print(f"\n  {len(found)} shown, {tasks.count()} total")
        return 0
    finally:
        runtime.close()


# --- eval ------------------------------------------------------------------


def _eval_dirs(workspace: Path | None) -> tuple[Path, Path, Path]:
    settings = load_settings(workspace)
    root = settings.workspace_root
    return root, root / "evaluations" / "cases", root / "evaluations" / "results"


def cmd_eval(args: argparse.Namespace) -> int:
    from personal_ai_os.evaluation.case import load_suites
    from personal_ai_os.evaluation.report import SuiteResult, compare, render
    from personal_ai_os.evaluation.runner import EvalRunner

    root, cases_dir, results_dir = _eval_dirs(args.workspace)

    if args.eval_command == "compare":
        left = SuiteResult.load(Path(args.left))
        right = SuiteResult.load(Path(args.right))
        if left.suite != right.suite:
            print(
                f"warning: comparing different suites "
                f"({left.suite} vs {right.suite})",
                file=sys.stderr,
            )
        print(compare(left, right))
        return 0

    suites = load_suites(cases_dir)
    if not suites:
        print(f"no evaluation suites in {cases_dir}")
        return 1

    if args.eval_command == "list":
        for suite in suites:
            print(f"\n  {suite.suite}  ({len(suite.cases)} cases, "
                  f"{suite.total_runs()} runs)")
            if suite.description:
                print(f"    {suite.description.strip()}")
            for case in suite.cases:
                checks = ", ".join(c.describe() for c in case.checks)
                print(f"      {case.name:<28} x{case.repeat}  [{checks}]")
        print()
        return 0

    # run
    selected = [s for s in suites if not args.suite or s.suite == args.suite]
    if not selected:
        known = ", ".join(s.suite for s in suites)
        print(f"no suite named {args.suite!r}. Available: {known}", file=sys.stderr)
        return 1

    runner = EvalRunner(repo_root=root, model=args.model, repeat=args.repeat)
    failures = 0

    for suite in selected:
        total = sum(args.repeat or c.repeat for c in suite.cases)
        print(f"\nrunning {suite.suite}: {len(suite.cases)} cases, {total} runs")
        if args.model:
            print(f"pinned model: {args.model}")
        print("this calls a local model repeatedly and will take a while.\n")

        result = runner.run_suite(suite)
        print(render(result))

        if not args.no_save:
            path = result.save(results_dir)
            print(f"\n  saved: {path.relative_to(root)}")
        if result.pass_rate < 1.0:
            failures += 1

    # A non-zero exit means "something did not pass", which is a measurement,
    # not a broken harness. Both are useful; they are just different questions.
    return 1 if failures else 0


# --- trace -----------------------------------------------------------------


def cmd_trace(args: argparse.Namespace) -> int:
    settings = load_settings(args.workspace)
    runs_dir = settings.resolved_path(settings.paths.runs_dir)

    path = (
        latest_trace(runs_dir) if args.run_id in (None, "latest")
        else find_trace(runs_dir, args.run_id)
    )
    if path is None:
        print(f"no trace found in {runs_dir}", file=sys.stderr)
        return 1

    print(f"{path}\n")
    for event in read_trace(path):
        # Sub-agents share their parent's trace, so indentation by depth is
        # what makes a nested delegation readable as one run.
        depth = int(event.data.get("depth") or 0)
        pad = "  " * depth
        print(f"  {event.seq:>3}  {event.ts}  {pad}{event.type}")
        if args.verbose:
            for key, value in event.data.items():
                rendered = str(value)
                if not args.full and len(rendered) > 160:
                    rendered = rendered[:160] + "..."
                print(f"         {pad}{key}: {rendered}")
    return 0


# --- entry point -----------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="paios",
        description="Personal AI OS -- a local-first personal agent platform.",
    )
    parser.add_argument("--version", action="version", version=f"paios {__version__}")
    parser.add_argument(
        "--workspace",
        type=Path,
        default=None,
        help="repository root (default: discovered by walking up from cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="check that this machine can run the system").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("models", help="show the model ladder and availability").set_defaults(
        func=cmd_models
    )
    sub.add_parser("agents", help="list registered agents").set_defaults(func=cmd_agents)

    run_p = sub.add_parser("run", help="run an agent")
    run_p.add_argument("agent")
    run_p.add_argument("objective")
    run_p.set_defaults(func=cmd_run)

    tasks_p = sub.add_parser("tasks", help="inspect or edit the task list directly")
    tasks_p.set_defaults(func=cmd_tasks, task_command="list")
    tasks_sub = tasks_p.add_subparsers(dest="task_command")

    tasks_p.add_argument("--status", choices=[s.value for s in TaskStatus])
    tasks_p.add_argument("--all", action="store_true", help="include finished tasks")
    tasks_p.add_argument("--limit", type=int, default=50)

    add_p = tasks_sub.add_parser("add", help="add a task")
    add_p.add_argument("title")
    add_p.add_argument("--notes", default="")
    add_p.add_argument(
        "--priority", choices=[p.value for p in TaskPriority], default="normal"
    )
    add_p.add_argument("--due", default=None, help="ISO date, e.g. 2026-09-07")
    add_p.set_defaults(func=cmd_tasks, task_command="add")

    done_p = tasks_sub.add_parser("done", help="mark a task complete")
    done_p.add_argument("id", type=int)
    done_p.set_defaults(func=cmd_tasks, task_command="done")

    eval_p = sub.add_parser("eval", help="measure how well agents actually perform")
    eval_p.set_defaults(func=cmd_eval, eval_command="list")
    eval_sub = eval_p.add_subparsers(dest="eval_command")

    eval_list = eval_sub.add_parser("list", help="show available suites and cases")
    eval_list.set_defaults(func=cmd_eval, eval_command="list")

    eval_run = eval_sub.add_parser("run", help="run a suite and score it")
    eval_run.add_argument("suite", nargs="?", default=None, help="omit to run all")
    eval_run.add_argument(
        "--model", default=None, help="pin every tier to this model, e.g. qwen2.5:3b-instruct"
    )
    eval_run.add_argument(
        "--repeat", type=int, default=None, help="override each case's repeat count"
    )
    eval_run.add_argument("--no-save", action="store_true", help="do not write a result file")
    eval_run.set_defaults(func=cmd_eval, eval_command="run")

    eval_cmp = eval_sub.add_parser("compare", help="compare two saved results")
    eval_cmp.add_argument("left")
    eval_cmp.add_argument("right")
    eval_cmp.set_defaults(func=cmd_eval, eval_command="compare")

    trace_p = sub.add_parser("trace", help="replay a recorded run")
    trace_p.add_argument("run_id", nargs="?", default="latest")
    trace_p.add_argument("-v", "--verbose", action="store_true", help="show event data")
    trace_p.add_argument("--full", action="store_true", help="do not truncate values")
    trace_p.set_defaults(func=cmd_trace)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging("INFO")
    try:
        return int(args.func(args))
    except PersonalAIOSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
