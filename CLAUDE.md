# CLAUDE.md — rules for AI assistants working in this repository

Read this file and `PROJECT_STATE.md` before doing anything.

---

## What this project is

A **Personal AI OS**: a local-first, model-agnostic personal agent platform.
High-level objectives go in; the system decomposes them, routes them to
specialised agents, uses tools, asks permission for consequential actions,
remembers what matters, and records everything it did.

**The organising principle:**

> The agent system owns the intelligence workflow, not the model.

## The rule that overrides everything

### No cloud model provider. Ever.

The runtime speaks to **Ollama and local models only**.

**Never:**
- add `anthropic`, `openai`, or any hosted-inference SDK to `pyproject.toml`
- create an API-key setting, env var, or config field for a hosted model
- write a provider that calls a hosted API
- add a fallback that routes to a hosted model when local inference fails
- suggest a hosted model as a fix for a local model's poor performance

Claude Code is the **temporary development assistant**, not part of the
runtime. Access is expected to end **2026-09-07**. The system must keep working
after that date without modification.

If a design would create such a dependency, stop and redesign it. Say so
explicitly rather than working around it quietly.

**Why a fallback is worse than no support at all:** it fails *open*. The system
looks healthy right up until the subscription lapses, and every weakness the
local model had was masked instead of fixed. See ADR-001.

## Git: you do not commit

Paul is the only person who commits to this repository.

**Never run:** `git commit`, `git push`, `git tag`, `git reset`, `git rebase`,
`git merge`, `git checkout`, or anything else that rewrites history or touches
a remote.

**Read-only is fine:** `git status`, `git diff`, `git log`, `git show`,
`git branch`.

Before stopping after a meaningful unit of work, provide:

```
Files changed
What changed
Tests performed
Known issues
Suggested commit message
Next recommended task
```

Then stop. Do not create the commit. Never describe a change as approved unless
Paul explicitly approved it.

## Filesystem safety

- Work inside `c:\paul\AI_SYSTEM`. Other directories under `c:\paul\` are
  unrelated projects — do not touch them.
- Never delete data to tidy up. Explain and get approval before any destructive
  operation.
- `config/local.yaml` and `runs/` are gitignored and machine-specific.

## Architecture rules

**Never break the model seam.** Everything reaches models through
`core/model.py::AgentModel`. No agent, tool, or memory component may import a
provider class.

**Never add a second path to tool execution.** `BaseAgent._execute_tool_call`
is the only code that calls `Tool.execute`, and the only way past the broker
there is a granted decision (ADR-006). A second call site is a design error.

**Never let a unit test touch the network.** `pytest -q` must pass with Ollama
stopped. `tests/unit/conftest.py` enforces this by blocking sockets. If a test
needs a live model it belongs in `tests/integration/` with the `integration`
marker.

**Never widen the filesystem jail without being asked.** `paths.allowed_roots`
is the boundary keeping an agent inside the project.

**Prefer configuration over code.** Changing which model an agent uses, or what
a role maps to, must be a config edit. If it requires a code change, the
abstraction is wrong. Likewise, adding an agent must be a new
`agents/*.yaml` and nothing else — no edit to the Master, its manifest, or any
registry.

**Never let a tool reach for a global.** Capabilities (`store`, `delegate`)
arrive on `ToolContext` and are `None` when unavailable; a tool that needs one
reports that clearly instead of crashing. This is what keeps tools testable
without a wired process.

**Never let delegation escape its guards.** `depth` and `call_stack` are
injected by `Runtime` inside a closure, so a tool chooses *which* agent to call
and nothing else. Do not add a code path that lets a caller supply its own
depth.

**Gate where the consequence is.** `delegate` is `read`-level and does not
prompt; the sub-agent's own tools carry the real levels. Adding prompts that
protect nothing trains the user to click through the ones that matter.

## Layout

```
src/personal_ai_os/
  core/          types, AgentModel ABC, errors      <- contracts
  models/        ollama, fake, registry, router
  memory/        store (SQLite), tasks              <- persistence
  tools/         base (+ path jail), registry, delegate, builtin/
  permissions/   levels, brokers                    <- the gate
  agents/        spec, registry, base loop, builtin/{ping,master,task_agent}
  evaluation/    case, checks, runner, report       <- measurement
  observability/ logging, JSONL traces
  config/        schema, loader
  runtime.py     composition root — the only place things are wired
  cli.py         doctor | models | agents | run | tasks | trace

agents/*.yaml    agent manifests (data, not code)
evaluations/     cases/*.yaml (data), results/*.json (committed history)
config/          default.yaml (committed), local.yaml (gitignored)
docs/            architecture, agents, tools, permissions, model-routing,
                 memory, evaluation, development-workflow, decisions
runs/            JSONL traces (gitignored)
data/            paios.db (gitignored)
```

## Commands

```powershell
paios doctor                  # does this machine work?
paios models                  # the ladder + availability
paios agents                  # registered agents
paios run master "..."        # delegate an objective
paios tasks                   # the task list, no model involved
paios trace <run_id> -v       # replay a run, sub-agents indented
paios eval run <suite>        # measure agent behaviour
paios eval compare a.json b.json

pytest -q                     # unit — must pass with Ollama STOPPED
pytest -m integration         # live — needs Ollama
```

## Measuring, not guessing

`pytest` says the code is correct. `paios eval` says the agent behaves well.
They are different questions.

**Run an evaluation after changing a prompt, a tool schema, or a model.** None
of those are judged by unit tests. Phase 4 found a defect every test passed
through: both models guessed a task id, completed the wrong task, and described
it fluently. Only checking the database caught it.

- **Never assert a score in a test.** Pinning a score turns a finding into a
  fixture. Integration tests assert that a scored result comes back, not what
  it is.
- **A prompt fix is unproven until measured.** If a fix cannot be measured, say
  so rather than calling it done.
- **Identical failure across two different models means the design is wrong,
  not the model** (ADR-022). Reach for structure before prompt wording.
- **When an argument names something the user never said, expect a model to
  invent it.**

## Conventions

- Python 3.11+, type hints everywhere, `from __future__ import annotations`
- Pydantic v2 for anything crossing a boundary
- Typed errors from `core/errors.py`, never bare `Exception`
- Constructor injection; no global state, no singletons
- Comments explain **why**. The code already says what.
- Line length 90

## Documentation duties

Update **`PROJECT_STATE.md`** whenever a meaningful milestone lands. It is the
handoff document: a future local agent must be able to read it and know exactly
where work stopped, with no access to any previous conversation.

Add an ADR to **`docs/decisions.md`** for any architectural decision. Record
options considered and consequences. Never rewrite an old decision — supersede
it and explain why. A wrong decision with honest reasoning is more useful to a
future reader than a tidy history.

Update the relevant `docs/*.md` when behaviour changes.

## Honesty

- Do not mark a feature complete when it partially works. Say which part works.
- Do not weaken or delete a failing test to get green. An inconvenient test has
  usually found something.
- Report failures with: what failed, why it likely failed, what was tried, what
  is unresolved, recommended next step.
- Do not claim something was verified unless it was actually run.

## Working style

Paul is learning agent engineering by building this. Explain the important
decision through the code and structure — briefly. Implement, explain the one
thing worth understanding, let him inspect it, test, iterate. Do not lecture.

For meaningful changes: inspect the repository first, produce a plan, identify
risks and tests, then execute. Do not start editing many files before
understanding what is there.

## Future architecture: Health & Wellness

A Health & Wellness domain is **recorded, not built**. Full design in
`docs/health-wellness.md`; decisions in ADR-017/018/019.

**Do not implement any of it until Paul explicitly asks.** No agents, no tools,
no tables. It is recorded so the system can grow into it without a rewrite.

When it is eventually built, these are not negotiable:

- **Never present the system as a psychiatrist, psychologist, therapist, or
  physician.** The term is "Health & Wellness Support".
- **No diagnostic or clinical functionality, ever.** No diagnosing, no treating,
  no interpreting symptoms, no risk scores. Out of scope for this project
  permanently — not "later".
- **Never claim to assess mental-health risk.** A local 7B is not a clinical
  instrument and must not imply it is.
- **Crisis resources are static, human-reviewed data shipped in the repo,
  rendered verbatim.** Never model-generated. A hallucinated helpline number
  fails at the moment it matters most, while looking like it worked.
- **Escalation suggests; it never acts.** Nothing contacts anyone on the user's
  behalf.
- **Never design for engagement or emotional dependency.** Support the user's
  human relationships instead of substituting for them. Never frame the system
  as their only safe space, and never discourage contacting real people.
- **No sycophancy.** Do not affirm unsupported inferences to seem supportive.
- **Sensitive data needs stricter memory and sharing rules than severity alone
  implies** (ADR-019), and emotional conversation is not persisted by default.
- **Evaluation comes first.** The harness is a prerequisite for this domain, not
  a follow-up — it is the one place where being wrong costs something outside
  the repository.

## Phase

See `PROJECT_STATE.md` for current status. Phases: 1 Foundation · 2 Master +
Task agent + memory · 3 Finance + Research agents · 4 Reliability & evaluation ·
5 Routing · 6 Integrations · 7 Local Master · 8 Distillation.

Do not build ahead of the current phase. Each one exists to settle contracts
the next depends on.
