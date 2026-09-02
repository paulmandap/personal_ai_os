# ADR-062 prediction — written BEFORE the experiment was run

**Date:** 2026-09-01 · **Author:** Claude Code, reviewed by Paul

Pre-registered because ADR-053 set a bar from n=1 and disqualified its own
intervention, and ADR-060 published a mechanism drawn from a single draw. The
gates, the pairing, the outcome table and — importantly — **what each outcome
does not prove** are fixed here while the result is unknown.

---

## The question

> **Does the silent turn arise BEFORE model generation (no tool-call-like
> completion is produced) or AFTER it (a completion is produced but does not
> appear in the parsed chat response)?**

The path under test spans four layers:

```
template rendering → model generation → tool-call parsing → chat serialization
```

`/api/generate` with `raw: true` supplies a fully-rendered prompt and returns the
unparsed completion, so it splits the chain **at generation and no further**.

**This is not a test of the template.** The template is an input to the method,
not the hypothesis.

## Scope — fixed, and repeated in the ADR

One model (`qwen2.5:3b-instruct`), one objective (*"Add a task to renew my
passport."*), one roster, one tool schema, one first-turn configuration, one
machine, one regime. **Nothing generalises past that.**

---

## Gate 1 — the failing regime must be present

15 `/api/chat` calls, unseeded (as production sends none). Require
`CALL_ANY ≤ 3` — the `SUPPRESSED` band from ADR-061.

**If the system is in the calling regime, ABORT.** There would be no silent
turns to inspect and RAW-vs-CHAT would measure nothing.

**Gate 1 is not part of the measurement population.** Its calls are recorded
separately and never pooled with the paired CHAT arm.

**State is NOT reset between Gate 1 and the measurement — deliberately.**
`ollama stop` risks losing the perishable regime, and ADR-061 showed a fresh load
does not restore the calling mode. Recorded in the result JSON with its reason,
because unrecorded runtime state has misled this project twice.

## Gate 2 — prompt identity

**Checked on this machine, not assumed from upstream source:** the installed
Ollama 0.33.2 binary contains `DebugRenderOnly` and `_debug_render_only`, and
`POST /api/chat` with `"_debug_render_only": true` returns
`{"_debug_info": {"rendered_template": …}}` while generating nothing. The
spellings `debug_render_only` and `DebugRenderOnly` are ignored and fall through
to normal generation.

So the RAW prompt is **fetched, not reconstructed**:

1. take `_debug_info.rendered_template` verbatim; record it and its SHA-256;
2. send **that exact string** to `/api/generate` with `raw: true`;
3. require RAW `prompt_eval_count` == CHAT `prompt_eval_count`.

**Step 3 is corroboration, not proof.** Identity between the CHAT prompt and the
RAW prompt is by construction. What the token match checks is the remaining
assumption — that `rendered_template` is what actually gets tokenised.

**If Gate 2 fails, ABORT and record it**: that would mean `rendered_template` is
not what the model is fed, which invalidates the comparison and is worth knowing.

---

## Pairs, not windows

> A **pair** is one CHAT execution and one RAW execution sharing the same model
> digest, prompt hash, generation options, **seed**, and `pair_id`.

15 pairs; `seed = 20260901 + pair_id`; CHAT/RAW order randomised within each pair
by `ORDER_SEED = 20260902`, with the executed order stored.

**Causal claims may be drawn ONLY from within-pair disagreement.** A difference
between pairs is the ADR-061 bimodality, not a mechanism.

---

## Outcome table

Primary class of the RAW completion, verbatim text always retained.

| | condition (within a pair) | reading |
|---|---|---|
| **T1** | RAW `TOOL_CALL_LIKE`, CHAT `content:''` and no `tool_calls` | **RAW exposes a tool-call-like completion absent from CHAT.** Consistent with a completion being generated but not surfaced by the CHAT path. **Endpoint-specific generation differences must be ruled out before calling this parser loss** |
| **T2** | RAW `EMPTY` | **No tool-call-like completion was observed in RAW under this prompt and configuration.** Nothing broader |
| **T3** | RAW `PROSE`, CHAT shows no content | CHAT should have surfaced it; a **different** defect in the chat path |
| **T4** | anything else, or CHAT and RAW agree | record verbatim, **name nothing** |

## What each outcome does NOT prove

**T1 does not prove parser loss.** Two unseparated causes remain:

1. the completion is generated and discarded by the chat parsing/serialization path;
2. the two endpoints generate differently (constrained sampling under `tools`,
   different sampler init), so CHAT never produced it.

Separating them needs a further experiment — `/api/chat` **without** `tools`
against RAW on the same rendered prompt — **named here and not run.**

**T2 does not prove the model is silent.** It proves no tool-call-like text
appeared on the RAW endpoint under this configuration. If the endpoints generate
differently, CHAT's 15–28 tokens could still be something else.

**T3** is the only outcome implicating the chat path in a way tool parsing cannot
explain, since plain content needs no tool handling.

**No outcome establishes anything about the template.**

---

## Guards

- **`eval_count` similarity is recorded as description only.** It would **not**
  establish that the two endpoints sample identically, and must never be written
  up as evidence of equivalence.
- **`TEXT`/`PROSE` is not a tool call.** Prose naming `delegate` is `PROSE`;
  pinned by a control test.
- **Diagnostic sub-flags are secondary** — malformed/truncated, valid JSON,
  wrong tool name, valid delegate call. They never change the primary endpoint;
  they exist so a discarded detail is not a lost mechanism (ADR-059's lesson).
- **ADR-062 is diagnostic.** No `src/` change, no fix in the same commit.

## An observation already recorded, and deliberately not chased

The rendered tools block is **not valid JSON**:

```
{"type": "function", "function": {delegate Hand a piece of work to a specialised
agent... {object <nil> <nil> [agent objective] {"agent":{…},"objective":{…}}}}}
```

A Go struct printed with `%v` — `<nil>` placeholders, `[agent objective]` as a
slice — where the template promises a JSON function signature. **The model reads
a malformed tool definition on every call.**

It cannot by itself be the silent regime: the 7B is 45/45 through the same
prompt, and the 3B was 13/15 in the calling window. It is Ollama's defect rather
than this repository's. **Recorded; a separate experiment; not run here.**
