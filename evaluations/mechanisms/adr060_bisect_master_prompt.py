r"""ADR-060 follow-up: which part of MASTER_SYSTEM_PROMPT suppresses the 3B's
tool calling?

**The open task of Phase 7 Increment 0.** ADR-060 established that
`qwen2.5:3b-instruct` emits a structurally valid `delegate` call under a
*trivial* system prompt and nothing at all under the Master's, with no harness,
runtime or agent loop involved. This bisects the prompt.

Run it directly -- it needs Ollama and a free GPU, so it is a script, not a test::

    & .\.venv\Scripts\python.exe evaluations\mechanisms\adr060_bisect_master_prompt.py

~300 calls, roughly 8 GPU-minutes. Nothing else may hold VRAM: the 7B takes
~5.8 GB of 8 GB.

WHAT THIS CAN AND CANNOT CONCLUDE
---------------------------------
Deletion alone does not identify a cause. Necessity (`MINUS_k` restores) and
sufficiency (`ONLY_k` suppresses) are measured separately, and naming a
mechanism requires **both** plus the Stage-2 arm that separates instruction
*content* from instruction *count*. Everything less is suggestive.

Any finding is scoped to **first-turn `delegate` emission, under the tested
objective, roster, tool schema and generation configuration** -- one objective
is tested, so this is not "the mechanism behind the 3B Master failure".

**Read the arms, never a total.** Summing heterogeneous arms is the error
ADR-057 caught in a safety number and ADR-060 caught in the `delegation`
headline.

METRICS
-------
``CALL_ANY``          any parsed tool call. **Primary endpoint** -- the
                      hypothesis is tool-call *suppression*.
``VALID_DELEGATION``  a `delegate` call naming an agent this arm actually
                      advertises, with a non-empty objective. Recorded because
                      ADR-060 saw the 3B invent `passportRenewalAgent`; routing
                      *correctness* remains the `delegation` suite's job.
``TEXT``              content, no tool call. **Never recovery** -- a Master that
                      describes a delegation has not delegated.
``EMPTY``             neither. The failure under investigation.

Full pre-registration (arms, thresholds, decision table, control failure rules)
is in `adr060-prediction.md`, fixed before this was run.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from typing import Any

import httpx

from personal_ai_os.agents.builtin.master import MASTER_SYSTEM_PROMPT
from personal_ai_os.tools.delegate import DelegateTool

# --- fixed experimental parameters ---------------------------------------

OLLAMA = "http://localhost:11434"
OBJECTIVE = "Add a task to renew my passport."
N = 15

#: Seeds are distinct per replicate, so the sample is a real sample and the run
#: is still reproducible. (An earlier draft claimed a fixed seed would collapse
#: n=15 into one draw -- that is only true if ONE seed is reused throughout.)
#: Production sends no seed, so `FULL_UNSEEDED` checks that seeding did not move
#: the regime.
BASE_SEED = 20260901
#: Shuffles arm order *within* each replicate. Fixed arm order would leave every
#: arm in the same slot every replicate, making a slot effect (cache warmth,
#: scheduler) indistinguishable from an arm effect.
ARM_ORDER_SEED = 20260901

#: Matches `ollama.py`, which sends only these two. The 3B runs at **0.3**, not
#: `small`'s 0.2: `--model` overrides each tier's `model` field only, and the
#: Master is `role: reason` -> tier `medium`. Running it at 0.2 would measure a
#: configuration the suite never runs.
TEMPERATURE = 0.3
NUM_CTX = 8192
KEEP_ALIVE = "5m"

SMALL = "qwen2.5:3b-instruct"
MEDIUM = "qwen2.5:7b-instruct"

#: Decision thresholds on CALL_ANY, n=15. Verified against the binomial rather
#: than chosen because they sound reasonable:
#:   RESTORED   >=12: P=0.00 at true rate 0.2, 0.018 at 0.5, 0.65 at 0.8
#:   SUPPRESSED <= 3: mirror image
#: The wide MIXED band forces "no claim" on a marginal arm. Note the cost: only
#: ~65% power at a true rate of 0.8, so absence of RESTORED is NOT evidence of
#: absence.
RESTORED_MIN = 12
SUPPRESSED_MAX = 3

#: Control floor. Reference: 7B `delegated_to=task_agent` is 55/55 across 11
#: stored runs. Rule of three -> 95% upper bound on failure 3/55 = 0.0545, so
#: p >= 0.9455. At that worst case P(X>=13 | n=15) = 0.955, a 4.5% false-void
#: rate; >=14 would void 19.6% of valid runs. Not delicate: a broken instrument
#: presents at ~0/15, thirteen runs away.
CONTROL_MIN = 13

# --- rosters --------------------------------------------------------------

ROSTER_4 = """\
  - finance: Analyses the user's own financial data: balances, spending, recurring bills, savings goals, and whether a purchase is affordable. Use this for any question about money, budgets, or what the user can spend.
  - ping: Verifies that local tool calling works end to end. Reads and lists files inside the workspace to answer simple questions about it.
  - research: Reads web pages the user asks about and answers from their contents. Use this when the user gives a URL or asks what a page says.
  - task_agent: Creates, updates, completes and reports on the user's tasks. Use this for anything about what the user has to do, deadlines, or things to remember."""

ROSTER_3 = "\n".join(
    line for line in ROSTER_4.splitlines() if not line.startswith("  - research:")
)
ROSTER_1 = "  - task_agent: Creates, updates, completes and reports on the user's tasks."

AGENTS_4 = frozenset({"finance", "ping", "research", "task_agent"})
AGENTS_3 = frozenset({"finance", "ping", "task_agent"})
AGENTS_1 = frozenset({"task_agent"})

# --- deterministic prompt construction ------------------------------------

HEAD, SEP, TAIL = MASTER_SYSTEM_PROMPT.partition("How to work:")
BULLETS = [b.rstrip() for b in TAIL.split("\n- ") if b.strip()]
#: Trailing newline(s) after the last bullet, preserved so a rebuilt FULL is
#: byte-identical to the shipped prompt.
TRAILER = TAIL[len(TAIL.rstrip("\n")) :]

#: Inert bullets: no instruction about tools, turns or delegation. Written to
#: approximate the real bullets' length, but **length is measured, not assumed**
#: -- `prompt_eval_count` is recorded per call, so any length claim is checked
#: against observed tokens rather than asserted here.
FILLER = [
    "The user's local time zone is Asia/Manila, and dates in this system are "
    "written year-first as ISO strings.",
    "Monetary amounts in this system are Philippine pesos, stored as integer "
    "minor units rather than as decimals.",
    "This workspace is a personal project repository maintained by a single "
    "person on a Windows machine.",
    "Records in this system are kept indefinitely and are never removed "
    "automatically by any scheduled process.",
]


def build(bullets: list[str], roster: str) -> str:
    """The one construction rule every arm goes through.

    `FULL` is not a special case: `build(BULLETS, ROSTER_4)` reproduces the
    shipped prompt byte for byte, pinned by a test. Without that, `FULL` as the
    raw constant and the other arms as rebuilds would differ in whitespace and
    confound the comparison.

    An empty bullet list drops the "How to work:" header too. Leaving a header
    with nothing under it would be a prompt artifact of its own -- a strange
    string no shipped configuration produces -- and that is a confound, not a
    control.
    """
    if bullets:
        body = HEAD + SEP + "".join("\n- " + b for b in bullets) + TRAILER
    else:
        body = HEAD.rstrip("\n") + TRAILER
    return body.replace("{roster}", roster)


# --- arms -----------------------------------------------------------------


@dataclass(frozen=True)
class Arm:
    name: str
    system: str
    #: Agents this arm advertises, for VALID_DELEGATION. `None` where no roster
    #: is shown (TRIVIAL), which makes the metric not-applicable rather than 0.
    agents: frozenset[str] | None
    #: False only for the fidelity arm that omits `seed`, matching production.
    seeded: bool = True


def stage1_arms() -> list[Arm]:
    """The 17 Stage-1 arms, in declaration order (execution order is shuffled)."""
    arms = [
        Arm("FULL", build(BULLETS, ROSTER_4), AGENTS_4),
        Arm("FULL_UNSEEDED", build(BULLETS, ROSTER_4), AGENTS_4, seeded=False),
        Arm("HEAD_ONLY", build([], ROSTER_4), AGENTS_4),
        Arm("TRIVIAL", "You are a helpful assistant.", None),
    ]
    for k in range(len(BULLETS)):
        kept = [b for j, b in enumerate(BULLETS) if j != k]
        arms.append(Arm(f"MINUS_{k + 1}", build(kept, ROSTER_4), AGENTS_4))
    for k in range(len(BULLETS)):
        arms.append(Arm(f"ONLY_{k + 1}", build([BULLETS[k]], ROSTER_4), AGENTS_4))
    arms.append(Arm("STRUCT_4", build(FILLER, ROSTER_4), AGENTS_4))
    arms.append(Arm("ROSTER_3", build(BULLETS, ROSTER_3), AGENTS_3))
    arms.append(Arm("ROSTER_1", build(BULLETS, ROSTER_1), AGENTS_1))
    return arms


def control_arms() -> list[Arm]:
    """7B controls. FULL validates the instrument; TRIVIAL is exploratory.

    TRIVIAL is deliberately NOT a validity control: ADR-060 observed the 7B
    EMPTY there on n=1, so it cannot serve as a positive control. It is measured
    to confirm or correct that single observation.
    """
    return [
        Arm("FULL", build(BULLETS, ROSTER_4), AGENTS_4),
        Arm("TRIVIAL", "You are a helpful assistant.", None),
    ]


# --- measurement ----------------------------------------------------------


def classify(message: dict[str, Any], agents: frozenset[str] | None) -> dict[str, Any]:
    """Label one Ollama `message`. Pure -- no network, so it is testable offline.

    `tool_calls` wins over content: a turn that both speaks and calls a tool is
    still a delegation, and the call is what the agent loop acts on.
    """
    calls = message.get("tool_calls") or []
    content = (message.get("content") or "").strip()

    valid = False
    for call in calls:
        fn = call.get("function") or {}
        if fn.get("name") != "delegate":
            continue
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                continue
        if not isinstance(args, dict):
            continue
        objective = args.get("objective")
        if not (isinstance(objective, str) and objective.strip()):
            continue
        if agents is not None and args.get("agent") not in agents:
            continue
        valid = True
        break

    return {
        "call_any": bool(calls),
        "valid_delegation": None if agents is None else valid,
        "label": "CALL" if calls else ("TEXT" if content else "EMPTY"),
    }


def ask(model: str, arm: Arm, seed: int | None, schema: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": TEMPERATURE, "num_ctx": NUM_CTX}
    if seed is not None:
        options["seed"] = seed
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": arm.system},
            {"role": "user", "content": OBJECTIVE},
        ],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "tools": [schema],
        "options": options,
    }
    payload = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=180).json()
    observation = classify(payload.get("message", {}), arm.agents)
    observation.update(
        seed=seed,
        prompt_eval_count=payload.get("prompt_eval_count"),
        eval_count=payload.get("eval_count"),
        done_reason=payload.get("done_reason"),
    )
    return observation


def run_block(model: str, arms: list[Arm], schema: dict[str, Any]) -> dict[str, Any]:
    """Round-robin: one call per arm per replicate, arm order shuffled."""
    rng = random.Random(ARM_ORDER_SEED)
    observations: dict[str, list[dict[str, Any]]] = {a.name: [] for a in arms}
    for rep in range(1, N + 1):
        order = list(arms)
        rng.shuffle(order)
        for arm in order:
            seed = BASE_SEED + rep if arm.seeded else None
            observations[arm.name].append(ask(model, arm, seed, schema))
        print(f"    replicate {rep}/{N} done", flush=True)
    return observations


def summarise(observations: list[dict[str, Any]]) -> dict[str, Any]:
    calls = sum(1 for o in observations if o["call_any"])
    tokens = [o["prompt_eval_count"] for o in observations if o["prompt_eval_count"]]
    valid = [o["valid_delegation"] for o in observations if o["valid_delegation"] is not None]
    return {
        "n": len(observations),
        "call_any": calls,
        "valid_delegation": sum(1 for v in valid if v) if valid else None,
        "text": sum(1 for o in observations if o["label"] == "TEXT"),
        "empty": sum(1 for o in observations if o["label"] == "EMPTY"),
        "label": (
            "RESTORED" if calls >= RESTORED_MIN
            else "SUPPRESSED" if calls <= SUPPRESSED_MAX
            else "MIXED"
        ),
        "prompt_tokens_median": sorted(tokens)[len(tokens) // 2] if tokens else None,
    }


def report(title: str, summaries: dict[str, dict[str, Any]]) -> None:
    print(f"\n  {title}")
    print(
        f"    {'arm':<18} {'CALL':>5} {'VALID':>6} {'TEXT':>5} {'EMPTY':>6} "
        f"{'ptok':>6}  label"
    )
    for name, s in summaries.items():
        valid = "-" if s["valid_delegation"] is None else str(s["valid_delegation"])
        print(
            f"    {name:<18} {s['call_any']:>5} {valid:>6} {s['text']:>5} "
            f"{s['empty']:>6} {s['prompt_tokens_median'] or 0:>6}  {s['label']}",
            flush=True,
        )


def conditions() -> dict[str, Any]:
    version = httpx.get(f"{OLLAMA}/api/version", timeout=30).json().get("version")
    tags = httpx.get(f"{OLLAMA}/api/tags", timeout=30).json().get("models", [])
    digests = {
        m["name"]: (m.get("digest") or "")[:12]
        for m in tags
        if m.get("name") in {SMALL, MEDIUM}
    }
    return {
        "server_version": version,
        "model_digests": digests,
        "temperature": TEMPERATURE,
        "num_ctx": NUM_CTX,
        "keep_alive": KEEP_ALIVE,
        "base_seed": BASE_SEED,
        "arm_order_seed": ARM_ORDER_SEED,
        "unpinned_sampling_defaults": ["top_p", "top_k", "repeat_penalty"],
        "objective": OBJECTIVE,
        "n_per_arm": N,
        "thresholds": {
            "restored_min": RESTORED_MIN,
            "suppressed_max": SUPPRESSED_MAX,
            "control_min": CONTROL_MIN,
        },
    }


def main() -> None:
    schema = DelegateTool().schema().to_openai_format()
    out: dict[str, Any] = {"conditions": conditions(), "blocks": {}}
    print(json.dumps(out["conditions"], indent=2), flush=True)

    def block(label: str, model: str, arms: list[Arm]) -> dict[str, dict[str, Any]]:
        print(f"\n{'=' * 78}\n{label}  ({model}, n={N})\n{'=' * 78}", flush=True)
        raw = run_block(model, arms, schema)
        summaries = {name: summarise(obs) for name, obs in raw.items()}
        out["blocks"][label] = {"model": model, "raw": raw, "summary": summaries}
        report(label, summaries)
        return summaries

    def save() -> None:
        path = "evaluations/mechanisms/adr060-bisection.json"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(out, fh, indent=2, sort_keys=True)
        print(f"\nwritten: {path}", flush=True)

    opening = block("control_opening", MEDIUM, control_arms())
    if opening["FULL"]["call_any"] < CONTROL_MIN:
        out["verdict"] = "VOID -- opening 7B control failed"
        save()
        raise SystemExit(
            f"\nRUN VOID: opening 7B FULL control returned "
            f"{opening['FULL']['call_any']}/{N} CALL, below the pre-registered "
            f"floor of {CONTROL_MIN}. No 3B arm may be interpreted. Investigate "
            f"the instrument or the environment first."
        )

    block("stage1_3b", SMALL, stage1_arms())

    closing = block("control_closing", MEDIUM, [control_arms()[0]])
    if closing["FULL"]["call_any"] < CONTROL_MIN:
        out["verdict"] = "VOID -- closing 7B control failed"
        save()
        raise SystemExit(
            f"\nRUN VOID: closing 7B FULL control returned "
            f"{closing['FULL']['call_any']}/{N} CALL, below {CONTROL_MIN}. The "
            f"environment drifted during the 3B block, so those results are NOT "
            f"interpretable and the complete run must be repeated. No per-arm "
            f"salvage is permitted."
        )

    out["verdict"] = "VALID -- both controls passed"
    save()
    print("\nBoth controls passed. Read the arms against adr060-prediction.md.")
    print("Remember: TEXT is not recovery, and arms are never summed.")


if __name__ == "__main__":
    main()
