r"""ADR-062 — does the silent turn arise BEFORE or AFTER model generation?

ADR-059 localised the loss: on a turn scored `empty_response`, `/api/chat`
returns `content: ""` with **no `tool_calls` key**, while reporting 15-28
evaluated tokens. ADR-061 eliminated every explanation living in this repository
-- prompt bytes, roster, seed, interleaving, model load order, runner freshness
-- and found the 3B bimodal, with nothing here selecting the mode.

`/api/generate` with `raw: true` returns the completion verbatim. Comparing it
against `/api/chat` splits the chain at generation:

    template rendering -> model generation -> tool-call parsing -> chat serialization

**This does not test "the template".** The template is an input to the method,
not the hypothesis. `raw: true` bypasses prompt formatting *and* the chat
tool-parsing path, so a RAW/CHAT difference localises the discrepancy to the span
between raw generation and parsed chat output -- not to any one component in it.

Run it directly; it needs Ollama and a free GPU::

    & .\.venv\Scripts\python.exe evaluations\mechanisms\adr062_template_layer.py

~48 calls, ~2 minutes, one model, no swaps.

SCOPE -- repeated in the ADR, not buried here
---------------------------------------------
One model (`qwen2.5:3b-instruct`), one objective, one roster, one tool schema,
one first-turn configuration, one machine, one regime. Nothing generalises past
that.

PROMPT IDENTITY
---------------
The installed Ollama 0.33.2 supports `_debug_render_only`, verified on this
machine: `/api/chat` with that field returns
`{"_debug_info": {"rendered_template": ...}}` and generates nothing. **So the RAW
prompt is fetched, not reconstructed** -- the exact string `/api/chat` reports
rendering is what `/api/generate` receives. Gate 2's token-count check then
corroborates that `rendered_template` is what actually gets tokenised; it is
corroboration, not proof.

PAIRS, NOT WINDOWS
------------------
A **pair** is one CHAT and one RAW execution sharing model digest, prompt hash,
options, **seed** and `pair_id`. Causal claims may only be drawn from
**within-pair** disagreement -- a difference between pairs is the ADR-061
bimodality, not a mechanism.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from adr060_bisect_master_prompt import (
    AGENTS_4,
    BULLETS,
    KEEP_ALIVE,
    NUM_CTX,
    OBJECTIVE,
    ROSTER_4,
    SMALL,
    SUPPRESSED_MAX,
    TEMPERATURE,
    build,
)
from adr060_bisect_master_prompt import classify as classify_chat
from personal_ai_os.tools.delegate import DelegateTool

OLLAMA = "http://localhost:11434"
N_PAIRS = 15
GATE1_N = 15
BASE_SEED = 20260901
ORDER_SEED = 20260902

OUT_PATH = "evaluations/mechanisms/adr062-template-layer.json"

END_TOKENS = ("<|im_end|>", "<|endoftext|>")
TOOL_OPEN = "<tool_call>"
TOOL_CLOSE = "</tool_call>"


# --- pure classification (offline-testable; the HTTP lives further down) ---


def strip_end_tokens(text: str) -> str:
    out = text
    for token in END_TOKENS:
        out = out.replace(token, "")
    return out


def classify_raw(text: str, agents: frozenset[str] | None = AGENTS_4) -> dict[str, Any]:
    """Label a verbatim RAW completion, keeping the diagnostic detail.

    Primary class is `TOOL_CALL_LIKE` / `PROSE` / `EMPTY`. The sub-flags exist so
    that a malformed or truncated call is not silently collapsed into the same
    bucket as a clean one -- ADR-059's whole finding was that a discarded detail
    is a lost mechanism.
    """
    stripped = strip_end_tokens(text).strip()
    has_tag = TOOL_OPEN in text
    closed = has_tag and TOOL_CLOSE in text

    flags: dict[str, Any] = {
        "has_tool_call_tag": has_tag,
        "closed_tool_call": closed,
        "malformed_or_truncated": False,
        "valid_json_call": False,
        "tool_name": None,
        "wrong_tool_name": False,
        "valid_delegate_call": False,
    }

    payload: str | None = None
    if has_tag:
        after = text.split(TOOL_OPEN, 1)[1]
        payload = after.split(TOOL_CLOSE, 1)[0] if closed else after
        flags["malformed_or_truncated"] = not closed
    elif stripped.startswith("{"):
        payload = stripped

    if payload is not None:
        try:
            obj: Any = json.loads(strip_end_tokens(payload).strip())
        except json.JSONDecodeError:
            obj = None
            flags["malformed_or_truncated"] = True
        if isinstance(obj, dict):
            name = obj.get("name")
            flags["tool_name"] = name if isinstance(name, str) else None
            flags["valid_json_call"] = "name" in obj and "arguments" in obj
            flags["wrong_tool_name"] = bool(flags["tool_name"]) and (
                flags["tool_name"] != "delegate"
            )
            args = obj.get("arguments")
            if (
                flags["valid_json_call"]
                and flags["tool_name"] == "delegate"
                and isinstance(args, dict)
            ):
                objective = args.get("objective")
                agent = args.get("agent")
                flags["valid_delegate_call"] = bool(
                    isinstance(objective, str)
                    and objective.strip()
                    and (agents is None or agent in agents)
                )

    looks_like_call = has_tag or (stripped.startswith("{") and '"name"' in stripped)
    if looks_like_call:
        primary = "TOOL_CALL_LIKE"
    elif stripped:
        primary = "PROSE"
    else:
        primary = "EMPTY"

    return {"primary": primary, **flags}


def gate1_verdict(call_any: int) -> bool:
    """The failing regime must be present; otherwise there is nothing to inspect."""
    return call_any <= SUPPRESSED_MAX


def gate2_verdict(chat_prompt_tokens: int | None, raw_prompt_tokens: int | None) -> bool:
    """Corroborates that `rendered_template` is what actually gets tokenised."""
    return (
        chat_prompt_tokens is not None
        and raw_prompt_tokens is not None
        and chat_prompt_tokens == raw_prompt_tokens
    )


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- transport ------------------------------------------------------------

MASTER_PROMPT = build(BULLETS, ROSTER_4)
SCHEMA = DelegateTool().schema().to_openai_format()


def _options(seed: int | None) -> dict[str, Any]:
    options: dict[str, Any] = {"temperature": TEMPERATURE, "num_ctx": NUM_CTX}
    if seed is not None:
        options["seed"] = seed
    return options


def chat_body(seed: int | None) -> dict[str, Any]:
    return {
        "model": SMALL,
        "messages": [
            {"role": "system", "content": MASTER_PROMPT},
            {"role": "user", "content": OBJECTIVE},
        ],
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "tools": [SCHEMA],
        "options": _options(seed),
    }


def call_chat(seed: int | None) -> dict[str, Any]:
    started = time.perf_counter()
    payload = httpx.post(f"{OLLAMA}/api/chat", json=chat_body(seed), timeout=300).json()
    message = payload.get("message") or {}
    result = classify_chat(message, AGENTS_4)
    return {
        "endpoint": "CHAT",
        "seed": seed,
        "content": message.get("content") or "",
        "tool_calls": message.get("tool_calls"),
        "primary": "TOOL_CALL_LIKE" if result["call_any"] else result["label"],
        "call_any": result["call_any"],
        "valid_delegation": result["valid_delegation"],
        "prompt_eval_count": payload.get("prompt_eval_count"),
        "eval_count": payload.get("eval_count"),
        "done_reason": payload.get("done_reason"),
        "wall_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def call_raw(prompt: str, seed: int | None) -> dict[str, Any]:
    body = {
        "model": SMALL,
        "prompt": prompt,
        "raw": True,
        "stream": False,
        "keep_alive": KEEP_ALIVE,
        "options": _options(seed),
    }
    started = time.perf_counter()
    payload = httpx.post(f"{OLLAMA}/api/generate", json=body, timeout=300).json()
    text = payload.get("response") or ""
    return {
        "endpoint": "RAW",
        "seed": seed,
        "response": text,
        **classify_raw(text),
        "prompt_eval_count": payload.get("prompt_eval_count"),
        "eval_count": payload.get("eval_count"),
        "done_reason": payload.get("done_reason"),
        "wall_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def fetch_rendered_prompt() -> str:
    """The prompt `/api/chat` itself reports rendering. Generates nothing."""
    body = dict(chat_body(None))
    body["_debug_render_only"] = True
    payload = httpx.post(f"{OLLAMA}/api/chat", json=body, timeout=120).json()
    info = payload.get("_debug_info")
    if not isinstance(info, dict) or "rendered_template" not in info:
        raise SystemExit(
            "ABORT: this Ollama build did not return _debug_info.rendered_template. "
            f"Keys returned: {sorted(payload)}. Gate 2 cannot be established; "
            "fall back to a reconstruction design before proceeding."
        )
    return info["rendered_template"]


def conditions(rendered: str) -> dict[str, Any]:
    version = httpx.get(f"{OLLAMA}/api/version", timeout=30).json().get("version")
    tags = httpx.get(f"{OLLAMA}/api/tags", timeout=30).json().get("models", [])
    digests = {m["name"]: (m.get("digest") or "")[:12] for m in tags if m.get("name") == SMALL}
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=30
        ).stdout.strip()
    except Exception:  # pragma: no cover - diagnostic only
        head = ""
    return {
        "utc": datetime.now(timezone.utc).isoformat(),
        "git_head": head,
        "server_version": version,
        "model": SMALL,
        "model_digests": digests,
        "debug_render_only": {
            "supported": True,
            "field": "_debug_render_only",
            "ignored_spellings": ["debug_render_only", "DebugRenderOnly"],
            "verified_on": "installed build, 2026-09-01",
        },
        "rendered_template": rendered,
        "rendered_template_sha256": sha256(rendered),
        "rendered_template_len": len(rendered),
        "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX, "keep_alive": KEEP_ALIVE},
        "base_seed": BASE_SEED,
        "order_seed": ORDER_SEED,
        "objective": OBJECTIVE,
        "n_pairs": N_PAIRS,
        "gate1_n": GATE1_N,
        "suppressed_max": SUPPRESSED_MAX,
        "unpinned_sampling_defaults": ["top_p", "top_k", "repeat_penalty"],
        "state_reset_between_gate1_and_measurement": False,
        "state_reset_reason": (
            "`ollama stop` risks losing the perishable failing regime, and ADR-061 "
            "showed a fresh load does not restore the calling mode. Recorded because "
            "unrecorded runtime state has misled this project twice."
        ),
    }


# --- run ------------------------------------------------------------------


def save(out: dict[str, Any]) -> None:
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    print(f"\nwritten: {OUT_PATH}", flush=True)


def main() -> None:
    rendered = fetch_rendered_prompt()
    out: dict[str, Any] = {"conditions": conditions(rendered)}
    print(
        f"rendered prompt: {len(rendered)} chars, "
        f"sha256 {out['conditions']['rendered_template_sha256'][:16]}...",
        flush=True,
    )

    # --- Gate 1: is the failing regime present? ---------------------------
    print(f"\nGate 1 -- regime check, {GATE1_N} CHAT calls (unseeded, as production)",
          flush=True)
    gate1 = [call_chat(None) for _ in range(GATE1_N)]
    gate1_calls = sum(1 for t in gate1 if t["call_any"])
    chat_ptok = next((t["prompt_eval_count"] for t in gate1 if t["prompt_eval_count"]), None)
    out["gate1"] = {
        "trials": gate1,
        "call_any": gate1_calls,
        "n": GATE1_N,
        "chat_prompt_eval_count": chat_ptok,
        "passed": gate1_verdict(gate1_calls),
        "note": "Regime check only. NEVER pooled with the paired CHAT arm.",
    }
    print(f"  CALL_ANY {gate1_calls}/{GATE1_N}  (need <= {SUPPRESSED_MAX})", flush=True)
    if not out["gate1"]["passed"]:
        out["verdict"] = "ABORTED -- calling regime present, no silent turns to inspect"
        save(out)
        raise SystemExit(
            f"\nABORT: {gate1_calls}/{GATE1_N} CALL means the system is in the CALLING "
            f"regime. There are no silent turns to examine, so RAW-vs-CHAT would "
            f"measure nothing. Re-run when the failing regime returns."
        )

    # --- Gate 2: is `rendered_template` what gets tokenised? --------------
    print("\nGate 2 -- prompt identity", flush=True)
    probe = call_raw(rendered, None)
    out["gate2"] = {
        "chat_prompt_eval_count": chat_ptok,
        "raw_prompt_eval_count": probe["prompt_eval_count"],
        "passed": gate2_verdict(chat_ptok, probe["prompt_eval_count"]),
        "probe": probe,
        "note": (
            "The RAW prompt IS the string /api/chat reports rendering, so identity is "
            "by construction. This check corroborates that rendered_template is what "
            "actually gets tokenised -- corroboration, not proof."
        ),
    }
    print(
        f"  CHAT prompt_eval_count={chat_ptok}  RAW={probe['prompt_eval_count']}  "
        f"passed={out['gate2']['passed']}",
        flush=True,
    )
    if not out["gate2"]["passed"]:
        out["verdict"] = "ABORTED -- rendered_template is not what gets tokenised"
        save(out)
        raise SystemExit(
            f"\nABORT: CHAT tokenised {chat_ptok} prompt tokens, RAW tokenised "
            f"{probe['prompt_eval_count']} on the string /api/chat says it rendered. "
            f"`rendered_template` is therefore not what the model is fed, which "
            f"invalidates the comparison and is worth recording on its own."
        )

    # --- Measurement: 15 pairs, order randomised within each pair ---------
    print(f"\nMeasurement -- {N_PAIRS} pairs, CHAT/RAW order randomised", flush=True)
    rng = random.Random(ORDER_SEED)
    pairs: list[dict[str, Any]] = []
    for pair_id in range(1, N_PAIRS + 1):
        seed = BASE_SEED + pair_id
        order = ["CHAT", "RAW"]
        rng.shuffle(order)
        trials: dict[str, Any] = {}
        for position, endpoint in enumerate(order):
            trial = call_chat(seed) if endpoint == "CHAT" else call_raw(rendered, seed)
            trial["executed_position"] = position
            trials[endpoint] = trial
        pairs.append({"pair_id": pair_id, "seed": seed, "order": order, **trials})
        print(
            f"  pair {pair_id:2d}  order={'/'.join(order):9s}  "
            f"CHAT={trials['CHAT']['primary']:<15} RAW={trials['RAW']['primary']}",
            flush=True,
        )
    out["pairs"] = pairs

    # --- summary ----------------------------------------------------------
    disagree = [
        p for p in pairs if p["CHAT"]["primary"] != p["RAW"]["primary"]
    ]
    raw_classes: dict[str, int] = {}
    for p in pairs:
        raw_classes[p["RAW"]["primary"]] = raw_classes.get(p["RAW"]["primary"], 0) + 1
    out["summary"] = {
        "raw_classes": raw_classes,
        "chat_call_any": sum(1 for p in pairs if p["CHAT"]["call_any"]),
        "raw_tool_call_like": raw_classes.get("TOOL_CALL_LIKE", 0),
        "within_pair_disagreements": len(disagree),
        "raw_valid_delegate_calls": sum(1 for p in pairs if p["RAW"]["valid_delegate_call"]),
        "raw_malformed_or_truncated": sum(
            1 for p in pairs if p["RAW"]["malformed_or_truncated"]
        ),
        "chat_eval_counts": [p["CHAT"]["eval_count"] for p in pairs],
        "raw_eval_counts": [p["RAW"]["eval_count"] for p in pairs],
    }
    out["verdict"] = "COMPLETE -- both gates passed"
    save(out)

    print(f"\n{'=' * 74}\nRAW classes: {raw_classes}")
    print(f"CHAT call_any: {out['summary']['chat_call_any']}/{N_PAIRS}")
    print(f"within-pair disagreements: {len(disagree)}/{N_PAIRS}")
    print(
        "\neval_count is recorded as DESCRIPTION only -- similarity would not "
        "establish that the two endpoints sample identically."
    )
    print("Read the pairs against adr062-prediction.md. Causal claims come from")
    print("within-pair disagreement only; differences between pairs are ADR-061's")
    print("bimodality, not a mechanism.")


if __name__ == "__main__":
    main()
