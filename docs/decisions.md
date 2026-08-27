# Architecture Decision Records

Decisions are appended, never rewritten. If one is reversed, add a new record
that supersedes it and say why — the reasoning that turned out to be wrong is
more useful to a future reader than a clean history.

Format: Context · Options · Decision · Reason · Consequences.

---

## ADR-001 — Local-first: no cloud model provider, ever

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** The system is being built with Claude Code as the engineering
assistant, and that access is expected to end on 2026-09-07. A system that
grows a dependency on the assistant that built it dies with it.

**Options.**
1. Cloud provider now, local later ("we'll swap it out")
2. Cloud provider as a fallback when local inference fails
3. Local-only runtime, with no cloud code path at all

**Decision.** Option 3. The runtime speaks to Ollama and nothing else. No
`anthropic` or `openai` package may be added to `pyproject.toml`; no API-key
setting exists; no fallback path routes off the local ladder.

**Reason.** "Swap it out later" fails because a cloud model quietly absorbs
the difficulty a local model would have exposed. Every place a 7B struggles is
a place the architecture needs to do more work — routing, retries, better tool
schemas, tighter prompts. Hiding those with a stronger model means discovering
all of them at once on the day the strong model is gone. A *fallback* is worse
still: it fails open, so the system looks healthy right up until the bill or
the subscription stops.

**Consequences.** Development is harder now and the system is honest about its
capability. The router degrades *down* the local ladder and raises when
nothing is mapped, rather than reaching outward. Model quality is a
configuration concern, not an architectural one.

---

## ADR-002 — Synchronous execution core

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** Agent frameworks are usually async. This machine has one RTX 3050
with 8 GB of VRAM.

**Options.** (1) async core from the start; (2) sync core, async later where it
pays.

**Decision.** Synchronous. `AgentModel.generate` is a blocking call.

**Reason.** One GPU holds one model at a time. Running two agents concurrently
would not parallelise — it would thrash, swapping models in and out of VRAM,
and finish slower than running them in sequence. Async would buy nothing while
costing real complexity in stack traces, testing and readability. Measured
evidence for the constraint arrived the same day: running the 3B and 7B
integration tests back-to-back crashed Ollama's runner mid-request
(`wsarecv: connection forcibly closed`) purely from the swap. Concurrency on
this hardware is not a missed optimisation; it is a hazard.

**Revisit when.** IO-bound tools arrive (email, web, GitHub) where waiting
dominates. That is a tool-layer concern and can be solved there — with a
thread pool inside the tool — without inverting the whole system.

**Consequences.** Simple code and simple tests today. A future Master Agent
fanning out to several sub-agents runs them in sequence, which on this hardware
is what would have happened anyway.

---

## ADR-003 — Talk to Ollama over HTTP directly, not through an SDK

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** The `ollama` Python package exists and works.

**Decision.** Use `httpx` against the REST API. `models/ollama.py` owns the
translation.

**Reason.** The adapter is ~250 lines, most of which would be needed anyway to
map into our own types. Owning it means the provider seam is real rather than
nominal: llama.cpp's server, vLLM and LM Studio all speak a near-identical
dialect, so a second backend is a new adapter rather than a new dependency
with its own idea of what a message is. It also means the wire format is
something this repository *knows*, which paid off immediately — see the
empirical findings recorded in ADR-010.

**Consequences.** We own the format, including its surprises. The integration
suite is what protects us from an upstream change, which is why it exists.

---

## ADR-004 — Pydantic as the single typing layer

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Decision.** One dependency does four jobs: conversation types, configuration
validation, tool argument schemas, and (later) structured output constraints.

**Reason.** These four could easily have been four libraries — dataclasses, a
settings library, a JSON-schema builder, and a parser. Using one means a
tool's advertised schema is *generated from* the model it validates against,
so the two cannot drift. That single property removes an entire category of
bug where a tool accepts something it never advertised, or advertises
something it rejects.

**Consequences.** Pydantic v2 is load-bearing. Acceptable: it is stable,
widely used, and the alternative is more code doing the same thing worse.

---

## ADR-005 — JSONL run traces as both observability and trajectory data

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** §26 needs execution logs for debugging. §31 needs high-quality
execution records as candidate distillation data, much later.

**Decision.** One append-only JSONL file per run, in `runs/`, with a fixed
event vocabulary (`observability/trace.py`, class `Events`).

**Reason.** These are the same artifact viewed twice. Designing the trace
properly now means Phase 8 collects its training data for free, from runs that
already happened, rather than needing instrumentation retrofitted onto a system
whose interesting runs are already in the past. Append-and-flush per event
also means a crashed process leaves a readable partial trace — which is the
trace you most want.

**Consequences.** The event vocabulary is a compatibility surface: renaming an
event type invalidates historical traces. Add new types rather than renaming
old ones.

---

## ADR-006 — One permission gate, in the agent loop

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Options.** (1) each tool checks its own permission; (2) a decorator on tool
methods; (3) a single gate in the loop that owns every path to execution.

**Decision.** Option 3. `BaseAgent._execute_tool_call` is the only code that
calls `Tool.execute`, and the only way past the broker there is a granted
decision.

**Reason.** A check that appears in nine places is a check that is missing from
the tenth, and the tenth will be the tool that sends email. Centralising means
the safety property is stated once and testable once: *a denied tool is never
executed*. Tools stay unaware of policy, which also keeps them trivial to test.

**Consequences.** Any future execution path — a Master Agent invoking a
sub-agent, a scheduled run — must route through the same gate. Adding a second
call site to `Tool.execute` is a design error, not a shortcut.

---

## ADR-007 — `src/` layout, pip + venv, PEP 621

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Decision.** `src/personal_ai_os/`, standard `venv`, `pip install -e .`,
metadata in `pyproject.toml`.

**Reason.** `src/` prevents the classic accident where tests import the source
directory instead of the installed package and mask packaging bugs. `uv` was
not installed on this machine; PEP 621 metadata means adopting it later is a
drop-in with no file changes.

---

## ADR-008 — Recoverable failures are observations, not exceptions

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** A model will emit invalid tool arguments, call tools it does not
have, and request things policy refuses.

**Decision.** Invalid arguments, unknown tools, execution errors and denied
permissions are written back into the conversation as tool results. Only
failures the model cannot act on — the inference server being unreachable —
end a run.

**Reason.** The whole premise of the project is that a small local model can be
made reliable through architecture. Small models make more mistakes, so the
loop must be able to absorb them; raising on the first would guarantee the
opposite of the goal. This was validated on the first live run: the 7B asked
for `max_bytes: 1048576`, exceeded the bound, received the validation error as
a tool result, corrected itself to `524288`, and completed the task.

**Consequences.** Runs are more robust and more expensive — a confused model
can burn its whole iteration budget. `max_iterations` is the backstop, and
exhausting it is reported honestly as a failure rather than dressed up as an
answer.

---

## ADR-009 — A deterministic router, not a model-driven one

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Decision.** `ModelRouter` is a lookup table with documented precedence:
explicit model → role → complexity → default, degrading down the ladder.

**Reason.** A model that picks the model is a second thing to debug when the
first misbehaves, and it spends inference to make a decision that is usually
obvious. More importantly there is no evidence yet that a learned router would
do better — and gathering that evidence is what the Phase 4 evaluation
framework is for. Choosing the clever option before the measurement exists is
how a system acquires complexity it can never justify removing.

**Revisit when.** Evaluation data shows the fixed mapping making bad calls.

---

## ADR-010 — Ollama wire-format findings (empirical)

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** The adapter was written against Ollama 0.33.0. Rather than assume
the format, it was probed directly before any code depended on it.

**Findings, all verified against a live server:**

| Question | Answer |
|---|---|
| Does `tool_calls[]` carry an `id`? | Yes (`call_vbgkcqae`). We still synthesise one if absent — it is template behaviour, not a guarantee |
| Type of `function.arguments`? | A **dict**, not the JSON *string* the OpenAI API returns. Both are handled |
| Tool-result echo format? | Both `tool_name` and `tool_call_id`+`name` are accepted. We send both |
| `done_reason` with tool calls? | `"stop"`, not `"tool_calls"` — so finish reason is derived from the presence of calls |
| Duration units? | Nanoseconds. `total_duration` **includes** model load (18.4 s cold start), so throughput is computed from `eval_duration` alone |

**Consequences.** These are pinned by unit tests using captured payloads
(`tests/unit/test_ollama_adapter.py`) and guarded against upstream drift by
`tests/integration/`. Probing first cost about ten minutes and removed the
single largest source of rework in the phase.

---

## ADR-011 — Trace redaction matches key segments, not substrings

**Date:** 2026-08-27 · **Status:** accepted · **Phase:** 1

**Context.** Trace redaction initially used case-insensitive substring
matching against markers like `token` and `password`.

**Problem, found in a real trace.** `prompt_tokens` and `completion_tokens`
contain the substring `token`, so token counts were being written as `***`.
The redaction was silently destroying exactly the throughput telemetry that
local-model comparison depends on — and it would have gone unnoticed until
someone tried to evaluate two models and found the numbers missing.

**Decision.** Match markers against whole *segments* of a key, splitting on
non-alphanumerics and camelCase boundaries.

**Reason.** `access_token` → `[access, token]` still matches `token`;
`prompt_tokens` → `[prompt, tokens]` does not. `OPENAI_API_KEY` and
`accessToken` are still caught. The rule is narrow enough to keep the data and
wide enough to keep the secrets.

**Consequences.** A secret named without separators and without a marker
segment (e.g. `mysecretishere`) would slip through. Acceptable: redaction is a
safety net against accidental logging, not a substitute for not putting
credentials in trace payloads.
