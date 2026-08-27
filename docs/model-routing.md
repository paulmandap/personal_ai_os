# Model routing

## The ladder

Sized for the machine this was built on: RTX 3050, **8 GB VRAM**, 16 GB RAM,
Ryzen 5 3600.

| Tier | Model | Size | Fits GPU? | Use |
|---|---|---|---|---|
| `small` | `qwen2.5:3b-instruct` | ~2.2 GB | yes, easily | Classification, extraction, routing |
| `medium` | `qwen2.5:7b-instruct` | ~4.7 GB (5.1 GB resident) | yes | **The workhorse.** Reliable tool calling |
| `large` | *unmapped* | — | — | Declared so the router has a rung to degrade from |

### Why `large` is empty

A 14B at Q4 is roughly 9 GB. That exceeds 8 GB of VRAM, so it spills to CPU and
runs at perhaps 5–10 tok/s — slow enough that an agent loop making six calls
becomes a minutes-long wait.

That is a trade worth making rarely, if ever, and it should be a deliberate
choice rather than a default. The tier is declared but unmapped, and the router
degrades past it with a logged warning. Mapping it later is one line in
`config/local.yaml` — no code change:

```yaml
models:
  tiers:
    large:
      model: "qwen2.5:14b-instruct-q4_K_M"
```

### One model at a time

The 7B occupies 5.1 GB resident. The 3B and 7B do not co-reside comfortably,
and forcing a swap under load is not merely slow — during integration testing
it crashed Ollama's runner mid-request (`wsarecv: connection forcibly closed`).

This is why the core is synchronous (ADR-002) and why the integration suite
evicts models explicitly between size changes rather than letting Ollama swap
them under pressure.

## Resolution precedence

Highest first:

1. **explicit model** on the agent spec — *"use exactly this"*
2. **role** — *"this is planning work"*
3. **complexity** — `simple` → small, `normal` → medium, `complex` → large
4. **default tier** from config

```yaml
models:
  roles:
    classify: small
    extract:  small
    plan:     medium
    reason:   medium
    code:     medium
```

Prefer roles in manifests. A role is a statement about the *work*; a model name
is a statement about one file on one machine.

## Degradation

An unmapped or unavailable tier degrades **down** the ladder — `large` →
`medium` → `small` — logging a warning and recording `degraded: true` in the
trace.

It never degrades *upward*: silently spending more compute than was asked for
is its own kind of surprise.

If nothing is mapped at the requested tier or below, the router raises
`ModelNotConfiguredError`. It does not reach for a cloud model, because there
is no cloud code path in this repository to reach for (ADR-001).

## Why the router is dumb on purpose

`ModelRouter` is a lookup table (ADR-009). A model that picks the model is a
second thing to debug when the first misbehaves, and it spends inference on a
decision that is usually obvious.

More to the point: there is no evidence yet that a learned router would do
better, and gathering that evidence is what the Phase 4 evaluation framework is
for. Building the clever version before the measurement exists is how a system
acquires complexity nobody can later justify removing.

Every selection records its reasoning into the trace, so *"which model answered
this, and why that one?"* — the first question asked of any surprising output —
is always answerable:

```json
{"type": "router.select", "data": {
  "model": "qwen2.5:7b-instruct", "tier": "medium",
  "requested_tier": "medium", "reason": "role 'reason' -> tier 'medium'",
  "degraded": false}}
```

## Inspecting

```
paios models     # the ladder, resolved models, live availability
paios agents     # what each agent's preference resolves to
paios doctor     # whether any of it actually works right now
```

## Changing models

Edit `config/local.yaml` (gitignored, machine-specific). No code changes:

```yaml
models:
  tiers:
    medium:
      model: "llama3.1:8b-instruct-q4_K_M"
      num_ctx: 16384
```

Then `paios doctor` to confirm it is installed and reachable, and
`pytest -m integration` to confirm it can actually call tools — a model that
generates fluently but calls tools badly is worse than useless in this system,
and only the integration suite will tell you.
