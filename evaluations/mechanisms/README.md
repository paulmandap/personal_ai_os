# Mechanism counts

Derived numbers from traced evaluation arms (ADR-045's `--trace-dir`).

**These files exist because the traces do not.** Raw traces are written under
`runs/`, which is gitignored and gets overwritten by the next experiment. A
mechanism count that lives only in terminal scrollback is not a record — and
comparing arms is the entire point of taking one. So the derived numbers are
committed here, each carrying its own provenance (model, runtime version, code
version, result file, per-run rows) and its own **definitions**, so a future
reader can tell what was counted without rerunning anything.

One file per arm:

```
adr046-before__qwen2.5-3b-instruct.json
adr046-after__qwen2.5-3b-instruct.json
```

`count_mechanisms.py` produces them; `test_count_mechanisms.py` is what makes
them believable. **Verify a detector before believing its number** — this project
has recorded seven detector errors, six false alarms and one blind spot — so the
detector's controls run as part of `pytest`.

## The blind-spot rule, learned again here

ADR-042 defined its H1 as *"a zero-match refusal, then a wrong write naming a
title that refusal enumerated"*, and its fix removed the enumeration. So that
definition **can only report zero afterwards, whatever the model does** — not
because the defect stopped, but because the evidence it keyed on was the thing
that was deleted.

Measured on current code, the wrong write still happens 4 times in 15. It now
selects the task **by id** instead of by title, so the strict definition scores a
clean 0 over a live defect.

Both counts are therefore kept and reported side by side:

| count | meaning |
|---|---|
| `h1` | refusal, then a wrong write by **any** selector — the defect |
| `h1_strict` | refusal, then a wrong write **by title** — what ADR-042 could see |

The general rule, which is ADR-040's stated the other way round: **ask what a
detector cannot see, not only what it reports** — and when a fix removes the
signal a detector keys on, the detector must be re-derived before its next
reading means anything.
