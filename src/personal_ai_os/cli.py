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
        # A property of the server, not of any tier, so it is reported once.
        # This is where someone checks what they are running: ADR-010's
        # wire-format findings are re-confirmed against it on every bump, and
        # ADR-041 records it into evaluation results.
        server_version = ""
        for tier, health in runtime.models.health_report():
            if health is None:
                print(
                    _status(
                        f"  tier {tier}", WARN, "unmapped -- router will degrade past it"
                    )
                )
                continue
            server_version = server_version or health.runtime_version
            if health.ok:
                any_model = True
                print(_status(f"  tier {tier}", OK, health.model))
            elif health.server_reachable:
                failures += 1
                print(_status(f"  tier {tier}", FAIL, f"{health.model}: {health.detail}"))
            else:
                failures += 1
                print(_status(f"  tier {tier}", FAIL, health.detail))
        if server_version:
            print(_status("  server version", OK, server_version))
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


# --- plans -----------------------------------------------------------------


def cmd_plans(args: argparse.Namespace) -> int:
    """What top-level runs actually did, and how to continue an interrupted one.

    The record is built from observed `AgentResult`s, not from anything a model
    said about itself (ADR-065) -- so this is also the way to check whether a
    delegation an agent described actually happened.
    """
    from personal_ai_os.memory.plans import PlanStatus, StepStatus

    runtime = Runtime.build(workspace_root=args.workspace, configure_logging=False)
    try:
        plans = runtime.plans

        if args.plan_command == "show":
            plan = plans.get(args.id)
            print(f"\n{plan.summary()}\n")
            steps = plans.steps(args.id)
            if not steps:
                print("  no steps recorded")
            for step in steps:
                print(f"  {step.summary()}")
                if step.status is StepStatus.DONE and step.output:
                    print(f"        -> {step.output.strip()[:160]}")
                if step.status is StepStatus.FAILED:
                    print(f"        -> failed: {step.error}")
                if step.status is StepStatus.PENDING:
                    # The honest phrasing: the store cannot tell "crashed before
                    # returning" from "succeeded but crashed before persistence".
                    print("        -> started; outcome never recorded")
            print()
            return 0

        if args.plan_command == "abandon":
            plan = plans.abandon(args.id)
            print(f"abandoned {plan.summary()}")
            return 0

        if args.plan_command == "resume":
            result = runtime.resume_plan(args.id)
            print()
            print(result.output or "(no output)")
            print()
            print(
                f"  [{'ok' if result.ok else result.stop_reason.value}] "
                f"plan={args.id} iterations={result.iterations} "
                f"tool_calls={result.tool_calls}"
            )
            print(f"  trace: paios trace {result.run_id}")
            return 0 if result.ok else 1

        status = PlanStatus(args.status) if args.status else None
        found = plans.list(status=status, limit=args.limit)
        if not found:
            print("no plans")
            return 0
        for plan in found:
            steps = plans.steps(plan.id or 0)
            done = sum(1 for s in steps if s.status is StepStatus.DONE)
            pending = sum(1 for s in steps if s.status is StepStatus.PENDING)
            extra = f", {pending} unconfirmed" if pending else ""
            print(f"  {plan.summary()}  ({done}/{len(steps)} done{extra})")
        print(f"\n  {len(found)} shown")
        return 0
    except PersonalAIOSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        runtime.close()


# --- finance ---------------------------------------------------------------


def cmd_finance(args: argparse.Namespace) -> int:
    """Inspect the ledger with no model in the loop.

    Useful on its own, and the way to check that a figure an agent quoted is
    actually what the database holds.
    """
    from personal_ai_os.memory.finance import FinanceStore, format_minor

    runtime = Runtime.build(workspace_root=args.workspace, configure_logging=False)
    try:
        finance = FinanceStore(runtime.store)

        if args.amount:
            result = finance.affordability(args.amount)
            print(result.summary)
            print(f"\n  {result.explanation}")
            return 0 if result.verdict.value != "not_affordable" else 1

        accounts = finance.accounts()
        currency = accounts[0].currency if accounts else "PHP"

        print("\n  ACCOUNTS")
        for account in accounts or []:
            print(f"    {account.summary}")
        if not accounts:
            print("    (none)")
        else:
            print(f"    {'total':<20} {format_minor(finance.total_balance_minor(), currency)}")

        print("\n  RECURRING")
        for commitment in finance.commitments() or []:
            print(f"    {commitment.render(currency)}")
        if not finance.commitments():
            print("    (none)")

        print("\n  GOALS")
        for goal in finance.goals() or []:
            print(f"    {goal.render(currency)}")
        if not finance.goals():
            print("    (none)")
        print()
        return 0
    finally:
        runtime.close()


# --- eval ------------------------------------------------------------------


def _eval_dirs(workspace: Path | None) -> tuple[Path, Path, Path]:
    settings = load_settings(workspace)
    root = settings.workspace_root
    return root, root / "evaluations" / "cases", root / "evaluations" / "results"


def cmd_eval(args: argparse.Namespace) -> int:
    from personal_ai_os.evaluation.case import DEFAULT_SPLITS, load_suites
    from personal_ai_os.evaluation.report import (
        SuiteResult,
        compare,
        load_history,
        render,
        render_history,
    )
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
        # The series both points sit in. This view is where "did B regress
        # against A" gets decided, and where a single stored A was twice
        # mistaken for a property (ADR-043).
        history = load_history(results_dir, left.suite, left.model)
        print(compare(left, right, history if history else None))
        return 0

    if args.eval_command == "history":
        model = args.model or ""
        if not model:
            print("--model is required: history is per suite AND model",
                  file=sys.stderr)
            return 1
        history = load_history(
            results_dir, args.suite, model, include_holdout=args.include_holdout
        )
        if not history:
            print(f"no recorded results for {args.suite!r} on {model!r}")
            return 1
        print(render_history(history))
        return 0

    suites = load_suites(cases_dir)
    if not suites:
        print(f"no evaluation suites in {cases_dir}")
        return 1

    if args.eval_command == "list":
        for suite in suites:
            counts = suite.split_counts()
            split_note = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
            # A probe says so wherever it is listed. Its cases are deliberately
            # adversarial, so its numbers are not a product score (ADR-048).
            mark = "  ** PROBE -- diagnostic, not a benchmark **" if (
                suite.kind == "probe"
            ) else ""
            print(f"\n  {suite.suite}  ({len(suite.cases)} cases, {split_note}){mark}")
            if suite.description:
                print(f"    {suite.description.strip()}")
            for case in suite.cases:
                checks = ", ".join(c.describe() for c in case.checks)
                tag = "" if case.split == "train" else f" [{case.split}]"
                print(f"      {case.name:<32} x{case.repeat}{tag}  [{checks}]")
        print()
        return 0

    # run
    selected = [s for s in suites if not args.suite or s.suite == args.suite]
    if not selected:
        known = ", ".join(s.suite for s in suites)
        print(f"no suite named {args.suite!r}. Available: {known}", file=sys.stderr)
        return 1

    splits: tuple[str, ...] = (
        ("holdout",) if args.split == "holdout" else DEFAULT_SPLITS
    )
    runner = EvalRunner(
        repo_root=root,
        model=args.model,
        repeat=args.repeat,
        trace_dir=args.trace_dir,
    )
    failures = 0

    for suite in selected:
        cases = suite.select(splits)  # type: ignore[arg-type]
        if not cases:
            print(f"\n{suite.suite}: no cases in split {'+'.join(splits)}")
            continue
        total = sum(args.repeat or c.repeat for c in cases)
        print(f"\nrunning {suite.suite}: {len(cases)} cases, {total} runs")
        if args.model:
            print(f"pinned model: {args.model}")
        if args.trace_dir:
            # Said out loud so a traced run is never mistaken for an ordinary
            # one. It scores identically -- tracing is write-only -- but it
            # leaves artifacts, and where they went is the useful half.
            print(f"tracing to: {Path(args.trace_dir).resolve()}")
        if args.split == "holdout":
            # Deliberately noisy. Every holdout run is a measurement that
            # should not be repeated casually (ADR-027).
            print("*** HOLDOUT SPLIT -- do not tune against these results ***")
        print("this calls a local model repeatedly and will take a while.\n")

        result = runner.run_suite(suite, splits=splits)  # type: ignore[arg-type]
        # History is shown by DEFAULT, not behind a flag. A flag that has to be
        # remembered is precisely what failed twice (ADR-043). `exclude` keeps
        # this run out of its own history when it has already been saved.
        history = load_history(
            results_dir,
            suite.suite,
            result.model,
            include_holdout=args.split == "holdout",
            exclude=result.filename(),
        )
        print(render(result, history if history else None))

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

    plans_p = sub.add_parser("plans", help="what runs did, and resume an interrupted one")
    plans_p.set_defaults(func=cmd_plans, plan_command="list")
    plans_sub = plans_p.add_subparsers(dest="plan_command")
    plans_p.add_argument(
        "--status", choices=["running", "done", "failed", "abandoned"]
    )
    plans_p.add_argument("--limit", type=int, default=50)

    plans_show = plans_sub.add_parser("show", help="one plan and every step it recorded")
    plans_show.add_argument("id", type=int)
    plans_show.set_defaults(func=cmd_plans, plan_command="show")

    plans_resume = plans_sub.add_parser(
        "resume", help="continue a plan a crash left running"
    )
    plans_resume.add_argument("id", type=int)
    plans_resume.set_defaults(func=cmd_plans, plan_command="resume")

    plans_abandon = plans_sub.add_parser(
        "abandon", help="mark a running plan abandoned; the only route to that state"
    )
    plans_abandon.add_argument("id", type=int)
    plans_abandon.set_defaults(func=cmd_plans, plan_command="abandon")

    finance_p = sub.add_parser("finance", help="inspect the ledger, no model involved")
    finance_p.add_argument(
        "amount",
        nargs="?",
        default=None,
        help="optional: check whether this amount is affordable, e.g. 5000",
    )
    finance_p.set_defaults(func=cmd_finance)

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
    eval_run.add_argument(
        "--split",
        choices=["default", "holdout"],
        default="default",
        help="'default' runs train+validation; 'holdout' runs ONLY the holdout cases",
    )
    eval_run.add_argument("--no-save", action="store_true", help="do not write a result file")
    eval_run.add_argument(
        "--trace-dir",
        type=Path,
        default=None,
        help=(
            "write one JSONL trace per repetition to "
            "DIR/<suite>/<case>/run-NN_<run_id>.jsonl, for counting failure "
            "mechanisms. Off by default. Use a path under runs/ -- it is "
            "gitignored, and traces are large"
        ),
    )
    eval_run.set_defaults(func=cmd_eval, eval_command="run")

    eval_hist = eval_sub.add_parser(
        "history", help="every recorded result for a suite, oldest first"
    )
    eval_hist.add_argument("suite")
    eval_hist.add_argument("--model", help="required: history is per suite and model")
    eval_hist.add_argument(
        "--include-holdout",
        action="store_true",
        help="include holdout results (ADR-027: do not browse these casually)",
    )
    eval_hist.set_defaults(func=cmd_eval, eval_command="history")

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
