"""The evaluation runner.

Each repetition runs in an **isolated workspace**: its own database, its own
`runs/` directory, and a filesystem jail pointed at a temp directory. The real
`data/paios.db` is never touched by an evaluation, and one case cannot see
another's state.

Agent manifests are *not* copied into the fixture -- `paths.agents_dir` points
at the real `agents/` directory, so what gets measured is the manifest that
actually ships.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.config.loader import load_settings
from personal_ai_os.config.schema import Settings
from personal_ai_os.core.errors import PersonalAIOSError
from personal_ai_os.core.ids import new_run_id
from personal_ai_os.evaluation.case import DEFAULT_SPLITS, EvalCase, EvalSuite, Split
from personal_ai_os.evaluation.checks import RunContext, run_check
from personal_ai_os.evaluation.report import (
    CaseResult,
    RunMetrics,
    RunRecord,
    SuiteResult,
    utc_stamp,
)
from personal_ai_os.memory.finance import FinanceStore
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import TaskStore
from personal_ai_os.observability.logging import get_logger
from personal_ai_os.observability.trace import Events, RunTrace
from personal_ai_os.permissions.broker import PolicyBroker, RecordingBroker
from personal_ai_os.permissions.types import PermissionLevel
from personal_ai_os.runtime import Runtime

log = get_logger("eval")

#: Everything auto-approved so a run never blocks on stdin. The broker is still
#: consulted for every call -- it is wrapped in a RecordingBroker -- so
#: "was the gate bypassed?" stays an answerable question.
EVAL_POLICY: dict[PermissionLevel, str] = {
    PermissionLevel.READ: "auto",
    PermissionLevel.WRITE: "auto",
    PermissionLevel.EXTERNAL_ACTION: "deny",
    PermissionLevel.SEND_MESSAGE: "deny",
    PermissionLevel.SPEND_MONEY: "deny",
    PermissionLevel.DELETE: "deny",
    PermissionLevel.DESTRUCTIVE: "deny",
}

#: How long git gets to answer before provenance is abandoned. Generous for a
#: local command, and bounded because a hung `git` must not hang a suite.
GIT_TIMEOUT_S = 10.0

_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(name: str) -> str:
    """A filesystem-safe directory component for a suite or case name.

    Suite and case names come from YAML the repository owns, so they are
    already tame -- but they are *data*, and a name carrying a separator would
    otherwise write outside the directory the caller named.
    """
    return _UNSAFE_PATH_CHARS.sub("_", name).strip("._-") or "unnamed"


def detect_code_version(repo_root: Path) -> str:
    """Which application code produced this result (ADR-044).

    Three states, and keeping them distinct is the whole point:

    ==================  ====================================================
    ``"6846f14"``       a clean tree -- a **reproducible reference point**
    ``"6846f14-dirty"`` tracked files modified; the commit alone does not
                        identify the code, so this is *not* a baseline
    ``""``              provenance could not be determined; claims nothing
    ==================  ====================================================

    The dirty marker is the load-bearing half. Measurement precedes commit in
    this project, so nearly every stored result was taken mid-edit -- ADR-039's
    two ledger arms and the baseline they were compared against were all made on
    uncommitted trees. Marking them stops any of them being mistaken for a
    reference.

    **Never raises, and never fails a suite.** Missing git, a non-repository
    path, a timeout, a non-zero exit, malformed output, anything unexpected: all
    return ``""``. This is provenance instrumentation, not application
    behaviour, and a suite must not die because git would not answer.

    **Known limit:** it cannot separate two variants of one experiment. Both of
    ADR-039's arms read ``6846f14-dirty``. A deliberate A/B still needs the
    discipline ADR-042 used -- run both arms the same day and label them.
    """

    def _git(*args: str) -> str | None:
        try:
            done = subprocess.run(
                ["git", "-C", str(repo_root), *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_S,
            )
        except Exception:  # not installed, timeout, OS refusal -- all the same
            return None
        if done.returncode != 0:
            return None
        return done.stdout

    head = _git("rev-parse", "--short", "HEAD")
    if head is None:
        return ""
    sha = head.strip()
    # A sha is hex; anything else means we are not reading what we think.
    if not sha or not all(c in "0123456789abcdef" for c in sha.lower()):
        return ""

    # `--porcelain` is empty exactly when no tracked file is staged or modified.
    # Untracked files are deliberately ignored: a stray scratch file does not
    # change what the code does.
    status = _git("status", "--porcelain", "--untracked-files=no")
    if status is None:
        return ""
    return f"{sha}-dirty" if status.strip() else sha


RuntimeBuilder = Callable[[Settings, Store, RecordingBroker], Runtime]


def _default_runtime(
    settings: Settings, store: Store, broker: RecordingBroker
) -> Runtime:
    return Runtime.build(
        settings=settings, store=store, broker=broker, configure_logging=False
    )


class EvalRunner:
    """Runs suites and scores them."""

    def __init__(
        self,
        *,
        repo_root: Path,
        model: str | None = None,
        repeat: int | None = None,
        trace_dir: Path | None = None,
        runtime_builder: RuntimeBuilder = _default_runtime,
    ) -> None:
        self.repo_root = repo_root.resolve()
        #: When set, every tier resolves to this model, so a case runs on
        #: exactly the model named rather than wherever routing sends it.
        self.model = model
        self.repeat = repeat
        #: Where to write one JSONL trace per repetition, or None for today's
        #: behaviour: in-memory events only, nothing on disk (ADR-045).
        #:
        #: **Resolved, and deliberately outside the fixture.** Each repetition
        #: runs in a `TemporaryDirectory` that is deleted immediately after, and
        #: `_settings_for` pins `paths.allowed_roots` to it -- so a trace written
        #: inside would be both destroyed on teardown and *readable by the agent
        #: under test*. Neither is acceptable for an instrument.
        self.trace_dir = trace_dir.resolve() if trace_dir is not None else None
        self._build_runtime = runtime_builder
        #: Server version observed at suite initialization, from the first
        #: runtime built. Reset per suite by `run_suite` (ADR-041).
        self._runtime_version: str | None = None

    # --- isolation ---------------------------------------------------------

    def _settings_for(self, fixture: Path) -> Settings:
        overrides: dict[str, Any] = {
            "permissions": {
                "interactive": False,
                "policy": {k.value: v for k, v in EVAL_POLICY.items()},
            },
            "paths": {
                # Real manifests, fixture everything else.
                "agents_dir": str(self.repo_root / "agents"),
                "db_path": str(fixture / "eval.db"),
                "runs_dir": str(fixture / "runs"),
                "allowed_roots": [str(fixture)],
            },
            "observability": {"trace_enabled": False, "log_level": "WARNING"},
        }

        if self.model:
            base = load_settings(self.repo_root, use_env=False)
            overrides["models"] = {
                "tiers": {
                    tier: {"model": self.model}
                    for tier, cfg in base.models.tiers.items()
                    if cfg.model is not None
                }
            }

        return load_settings(self.repo_root, use_env=False, overrides=overrides)

    @contextmanager
    def _fixture(self, case: EvalCase) -> Iterator[tuple[Settings, Store]]:
        """A throwaway workspace, torn down after the repetition."""
        with tempfile.TemporaryDirectory(prefix="paios-eval-") as tmp:
            fixture = Path(tmp)
            for name, content in case.setup.files.items():
                target = fixture / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            settings = self._settings_for(fixture)
            store = Store(settings.resolved_path(settings.paths.db_path))
            store.connect()

            tasks = TaskStore(store)
            for seed in case.setup.tasks:
                created = tasks.add(
                    seed.title,
                    notes=seed.notes,
                    priority=seed.priority,
                    due_date=seed.due_date,
                )
                if seed.status.value != "todo" and created.id is not None:
                    tasks.update(created.id, status=seed.status)

            finance = FinanceStore(store)
            for account in case.setup.accounts:
                finance.set_balance(
                    account.name, account.amount, currency=account.currency
                )
            # After the accounts, so a seeded transaction moves a real balance.
            for txn in case.setup.transactions:
                finance.add_transaction(
                    txn.account,
                    txn.amount,
                    category=txn.category,
                    description=txn.description,
                    occurred_on=txn.occurred_on,
                )
            for commitment in case.setup.commitments:
                finance.add_commitment(
                    commitment.name,
                    commitment.amount,
                    commitment.day_of_month,
                    category=commitment.category,
                )
            for goal in case.setup.goals:
                finance.add_goal(
                    goal.name,
                    goal.target,
                    saved=goal.saved,
                    target_date=goal.target_date,
                )

            try:
                yield settings, store
            finally:
                store.close()

    # --- one repetition ----------------------------------------------------

    def _trace_for(self, case: EvalCase, index: int, suite: str) -> RunTrace:
        """The repetition's trace: in-memory always, on disk only if asked.

        **Judging a fix by the mechanism that disappeared is this project's
        central method, and until now the harness could not support it.** Both
        prior diagnoses had to edit this method by hand to swap
        `RunTrace.disabled` for a live one -- which meant every diagnostic run
        was taken on a modified tree and stamped `<sha>-dirty`, the one thing
        ADR-044 says is never a reference point. Twice, the alternative --
        rebuilding the scenario outside the harness -- diverged from it
        (ADR-040 scored 0/20 on its own control; ADR-042's first attempt gave
        1/15 where the harness gives 7/15).

        `RunTrace` records into `self.events` either way, so what the checks
        score is byte-identical whether or not a file is written. Tracing is
        **write-only**: it adds an output, never an input.
        """
        if self.trace_dir is None:
            return RunTrace.disabled(agent=case.agent)

        run_id = new_run_id()
        directory = self.trace_dir / _slug(suite or "suite") / _slug(case.name)
        # The repetition index leads the filename so a case's runs read in
        # order; the `_<run_id>.jsonl` tail is kept so `find_trace` and
        # `paios trace` work on these files unchanged.
        return RunTrace(
            run_id=run_id,
            agent=case.agent,
            path=directory / f"run-{index:02d}_{run_id}.jsonl",
            enabled=True,
        )

    def _run_once(self, case: EvalCase, index: int, suite: str = "") -> RunRecord:
        with self._fixture(case) as (settings, store):
            broker = RecordingBroker(
                PolicyBroker(settings.permissions.policy, interactive=False)
            )
            runtime = self._build_runtime(settings, store, broker)
            if self._runtime_version is None:
                self._runtime_version = self._observe_runtime_version(runtime)
            trace = self._trace_for(case, index, suite)

            started = time.perf_counter()
            try:
                result = runtime.run_agent(case.agent, case.objective, trace=trace)
            except PersonalAIOSError as exc:
                # An infrastructure failure is not a check failure; record it
                # as such rather than reporting a misleading 0% score.
                log.error("case %s run %d could not execute: %s", case.name, index, exc)
                return RunRecord(
                    index=index,
                    passed=False,
                    stop_reason="harness_error",
                    error=str(exc),
                    metrics=RunMetrics(wall_ms=(time.perf_counter() - started) * 1000),
                )
            finally:
                runtime.close()

            wall_ms = (time.perf_counter() - started) * 1000
            ctx = RunContext(result=result, events=list(trace.events), store=store)
            outcomes = []
            for spec in case.checks:
                outcome = run_check(spec.name, ctx, spec.params)
                outcome.label = spec.describe()
                outcomes.append(outcome)

            return RunRecord(
                index=index,
                passed=all(o.passed for o in outcomes),
                stop_reason=result.stop_reason.value,
                error=result.error,
                checks=outcomes,
                metrics=_metrics(result, ctx, wall_ms),
                output_preview=result.output[:200],
            )

    # --- cases and suites --------------------------------------------------

    def run_case(self, case: EvalCase, *, suite: str = "") -> CaseResult:
        """Run one case `repeat` times.

        `suite` is passed explicitly rather than cached on the runner: it only
        exists to name a trace directory, and hidden state that decides where
        files land is how one experiment's traces end up filed under another's.
        """
        repeats = self.repeat or case.repeat
        log.info("case %s: %d run(s)", case.name, repeats)
        runs = [self._run_once(case, i + 1, suite) for i in range(repeats)]
        return CaseResult(
            case=case.name,
            description=case.description,
            agent=case.agent,
            category=case.category,
            split=case.split,
            runs=runs,
        )

    def run_suite(
        self, suite: EvalSuite, *, splits: tuple[Split, ...] = DEFAULT_SPLITS
    ) -> SuiteResult:
        started = utc_stamp()
        # Re-observed per suite rather than cached for the runner's lifetime: a
        # long session could span a server restart, and a stale version is worse
        # than none (ADR-041).
        self._runtime_version = None
        selected = suite.select(splits)
        cases = [self.run_case(c, suite=suite.suite) for c in selected]
        return SuiteResult(
            suite=suite.suite,
            # Travels with the result so a probe's numbers can never be read as
            # a product score, in this session or years later (ADR-048).
            kind=suite.kind,
            model=self.model or self._resolved_model_label(),
            runtime_version=self._runtime_version or "",
            # Once per suite, so every case in one result shares one
            # observation -- never per repetition (ADR-044).
            code_version=detect_code_version(self.repo_root),
            started_at=started,
            finished_at=utc_stamp(),
            split="+".join(splits),
            cases=cases,
        )

    def _observe_runtime_version(self, runtime: Runtime) -> str:
        """Which inference server produced this result (ADR-041).

        **Read from the runtime the repetition already built**, never by
        constructing a model here. Two reasons, and the second is the binding
        one:

        1. `evaluation/` must not import a provider class -- the model seam is
           the project's central architectural rule -- and `ModelHealth` is the
           seam's own way to ask a server about itself without generating.
        2. `pytest -q` must pass with Ollama stopped, and `tests/unit/conftest.py`
           blocks sockets. Building a registry here would construct a real
           `OllamaModel` under the default factory and hit a blocked socket.
           Going through `runtime.models` means the injected `runtime_builder`
           decides what gets built, so the offline tests resolve a `FakeModel`
           that reports no version. Offline-safety is structural, not incidental.

        Never raises: provenance is optional, and a suite must not fail because a
        server would not name itself.
        """
        try:
            registry = runtime.models
            if self.model:
                model = registry.get_named(self.model)
            else:
                tier = registry.role_tier("reason") or registry.settings.default_tier
                model = registry.get_tier(tier)
            return model.health().runtime_version
        except PersonalAIOSError:
            return ""

    def _resolved_model_label(self) -> str:
        """What to record when no explicit model was pinned."""
        try:
            settings = load_settings(self.repo_root, use_env=False)
            tier = settings.models.tiers.get(settings.models.default_tier)
            configured = settings.models.roles.get("reason", settings.models.default_tier)
            chosen = settings.models.tiers.get(configured)
            return (chosen and chosen.model) or (tier and tier.model) or "configured"
        except PersonalAIOSError:
            return "configured"


def _metrics(result: AgentResult, ctx: RunContext, wall_ms: float) -> RunMetrics:
    prompt_tokens = 0
    completion_tokens = 0
    generation_ms = 0.0

    for event in ctx.of_type(Events.MODEL_RESPONSE):
        usage = event.data.get("usage") or {}
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        generation_ms += float(usage.get("total_duration_ms") or 0.0)

    # Throughput from generation time only. `total_duration_ms` here is already
    # eval_duration (OllamaModel excludes model load), so a cold start does not
    # make a fast model look slow.
    tps = (
        completion_tokens / (generation_ms / 1000.0)
        if completion_tokens and generation_ms > 0
        else None
    )

    return RunMetrics(
        iterations=result.iterations,
        tool_calls=result.tool_calls,
        invalid_arguments=ctx.invalid_argument_count(),
        permission_denials=len(ctx.permission_denials()),
        wall_ms=round(wall_ms, 1),
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        tokens_per_second=round(tps, 2) if tps else None,
    )


__all__ = ["EvalRunner", "EVAL_POLICY", "StopReason"]
