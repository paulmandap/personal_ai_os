# Memory

> **Status: not implemented.** Phase 2. This document records the intended
> design so the decision is not re-litigated from scratch later.

## What exists today

Nothing persistent. `BaseAgent.run()` builds a fresh message list each call and
accepts an optional `history` argument. Conversation state lives only for the
duration of a run.

The one durable artifact is the **run trace** (`runs/*.jsonl`), which records
what happened but is not read back as memory.

## Why it is not built yet

Memory is the layer most likely to be built wrong if built early. Every design
decision in it depends on questions Phase 1 could not answer:

- What does a Master Agent actually need to pass to a sub-agent?
- Which facts recur often enough to be worth storing?
- Does semantic retrieval beat plain SQL lookup at this scale? (At the volume
  one person generates, it very often does not.)

Guessing now would mean building a vector store because it is fashionable,
rather than because retrieval was measured to be the bottleneck.

## Intended structure

Six kinds, distinguished by lifetime and purpose:

| Kind | Lifetime | Example | Likely storage |
|---|---|---|---|
| Short-term | One run | Current message list | In memory |
| Project state | Weeks | "Building Phase 2" | SQLite / `PROJECT_STATE.md` |
| Long-term | Indefinite | "Prefers direct answers" | SQLite |
| Episodic | Indefinite | "Last Tuesday's budget analysis" | SQLite + trace refs |
| Semantic | Indefinite | "Rent is ₱12,000/month" | SQLite, maybe vectors |
| Procedural | Indefinite | "How to run the weekly review" | Files / skills |

## Planned approach

**Start with SQLite.** One file, no server, transactional, trivially
inspectable with any SQL client, and it survives a machine restart. It is very
likely sufficient for a single person's data by several orders of magnitude.

**Add vectors only when retrieval is measurably the bottleneck.** The test is
concrete: an agent fails a task because it could not *find* a fact it had
stored. Until that happens, semantic search is infrastructure without a
problem — and it brings an embedding model that competes for the same 8 GB of
VRAM the reasoning model needs.

**Traces are already the episodic substrate.** `runs/*.jsonl` records every
run in full. Episodic memory is largely a matter of indexing what is already
being written, not of capturing something new.

## Open questions

- Who writes memory — the agent mid-run, or a separate consolidation step?
  (A separate step is more controllable and easier to evaluate.)
- What stops memory filling with low-value facts? Some quality gate is needed,
  the same problem Phase 8 has with trajectories.
- How does memory interact with permissions? Storing a fact from a `read` is
  not obviously a `read` itself.
- Retention: does anything expire?

## When this gets built

Phase 2, alongside the Master Agent, because that is the point at which
something concrete needs to pass context between agents and the requirements
stop being hypothetical.
