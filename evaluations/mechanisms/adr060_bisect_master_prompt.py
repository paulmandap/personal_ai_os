r"""ADR-060: which part of MASTER_SYSTEM_PROMPT suppresses the 3B's tool calling?

**This is the open task of Phase 7 Increment 0.** ADR-060 established that the
3B emits a structurally valid `delegate` call under a *trivial* system prompt and
nothing at all under the Master's, with no harness, runtime or agent loop
involved. This bisects the prompt to find which part is responsible.

Run it directly -- it needs Ollama and a free GPU, so it is a script rather than
a test::

    & .\.venv\Scripts\python.exe evaluations\mechanisms\adr060_bisect_master_prompt.py

~260 calls, roughly 10 GPU-minutes. Nothing else may hold VRAM while it runs: the
7B takes ~5.8 GB of 8 GB.

**Read the arms, not a total.** The question is *which* arm restores `CALL`, and
a summed pass rate across arms means nothing -- the same mistake ADR-057 caught
in a safety number and PROJECT_STATE caught in the `delegation` headline.

Three outcomes are counted per arm:

``CALL``   a parsed tool call came back -- what the agent loop needs
``TEXT``   content, no tool call -- the model answered in words
``EMPTY``  no content and no tool call -- the failure under investigation

An arm that turns `EMPTY` into `TEXT` has **not** fixed anything: the Master
cannot delegate by describing a delegation. Only `CALL` counts as recovery.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import httpx

from personal_ai_os.agents.builtin.master import MASTER_SYSTEM_PROMPT
from personal_ai_os.tools.delegate import DelegateTool

OLLAMA_URL = "http://localhost:11434/api/chat"
OBJECTIVE = "Add a task to renew my passport."
N = 10

#: The shipped roster, verbatim from the manifests, so the arms differ from the
#: real Master's prompt only where this file says they do.
ROSTER_4 = """\
  - finance: Analyses the user's own financial data: balances, spending, recurring bills, savings goals, and whether a purchase is affordable. Use this for any question about money, budgets, or what the user can spend.
  - ping: Verifies that local tool calling works end to end. Reads and lists files inside the workspace to answer simple questions about it.
  - research: Reads web pages the user asks about and answers from their contents. Use this when the user gives a URL or asks what a page says.
  - task_agent: Creates, updates, completes and reports on the user's tasks. Use this for anything about what the user has to do, deadlines, or things to remember."""

ROSTER_3 = "\n".join(
    line for line in ROSTER_4.splitlines() if not line.startswith("  - research:")
)
ROSTER_1 = "  - task_agent: Creates, updates, completes and reports on the user's tasks."

HEAD, _, TAIL = MASTER_SYSTEM_PROMPT.partition("How to work:")
BULLETS = [b.rstrip() for b in TAIL.split("\n- ") if b.strip()]


def classify(message: dict[str, Any]) -> str:
    """Label one Ollama `message` object. Pure -- no network, so it is testable.

    `tool_calls` wins over content: a turn that both speaks and calls a tool is
    still a delegation, and that is what the agent loop acts on.
    """
    if message.get("tool_calls"):
        return "CALL"
    return "TEXT" if (message.get("content") or "").strip() else "EMPTY"


def ask(model: str, system: str, schema: dict[str, Any]) -> dict[str, Any]:
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": OBJECTIVE},
        ],
        "stream": False,
        "tools": [schema],
        # Matches config/default.yaml, so an arm differs from the shipped
        # Master only in its system prompt.
        "options": {"temperature": 0.3, "num_ctx": 8192},
    }
    response = httpx.post(OLLAMA_URL, json=body, timeout=180)
    response.raise_for_status()
    return response.json().get("message", {})


def arm(model: str, label: str, system: str, schema: dict[str, Any]) -> dict[str, int]:
    counts = Counter(classify(ask(model, system, schema)) for _ in range(N))
    print(
        f"  {label:<48} CALL {counts['CALL']:2d}  "
        f"TEXT {counts['TEXT']:2d}  EMPTY {counts['EMPTY']:2d}",
        flush=True,
    )
    return dict(counts)


def variants() -> list[tuple[str, str]]:
    """(label, system prompt) for every arm, roster placeholder still present."""
    out: list[tuple[str, str]] = [
        ("trivial prompt, no roster, no rules", "You are a helpful assistant."),
        ("MASTER, 4 agents (shipped)", MASTER_SYSTEM_PROMPT),
        ("MASTER, 3 agents (pre-Phase-6)", MASTER_SYSTEM_PROMPT.replace(ROSTER_4, ROSTER_4)),
        ("HEAD only (no 'How to work' bullets)", HEAD),
    ]
    for i, bullet in enumerate(BULLETS):
        out.append(
            (f"HEAD + bullet {i + 1}: {bullet.strip()[:32]}",
             HEAD + "How to work:\n- " + bullet)
        )
    for i in range(len(BULLETS)):
        kept = [b for j, b in enumerate(BULLETS) if j != i]
        out.append(
            (f"FULL minus bullet {i + 1}",
             HEAD + "How to work:\n- " + "\n- ".join(kept))
        )
    return out


def main() -> None:
    schema = DelegateTool().schema().to_openai_format()
    results: dict[str, dict[str, dict[str, int]]] = {}

    for model in ("qwen2.5:3b-instruct", "qwen2.5:7b-instruct"):
        print(f"\n{'=' * 82}\n{model}   (n={N} per arm)\n{'=' * 82}", flush=True)
        per_model: dict[str, dict[str, int]] = {}

        # Roster size, on the full prompt.
        for label, roster in (
            ("MASTER, 4 agents (shipped)", ROSTER_4),
            ("MASTER, 3 agents (pre-Phase-6)", ROSTER_3),
            ("MASTER, 1 agent", ROSTER_1),
        ):
            per_model[label] = arm(
                model, label, MASTER_SYSTEM_PROMPT.replace("{roster}", roster), schema
            )

        # Prompt structure, roster held at the shipped four.
        for label, system in variants():
            if label.startswith("MASTER, "):
                continue
            per_model[label] = arm(
                model, label, system.replace("{roster}", ROSTER_4), schema
            )

        results[model] = per_model

    out = "evaluations/mechanisms/adr060-bisection.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, sort_keys=True)
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
