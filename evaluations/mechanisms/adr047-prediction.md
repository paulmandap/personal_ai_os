# ADR-047 — what is predicted, written before the change exists

Written after ADR-046's after-arm and before a line of ADR-047. Same discipline as
`adr046-prediction.md`: a prediction written afterwards explains any result
equally well and is worth nothing.

## The trigger, and why it is met

The plan gated ADR-047 on **direct trace evidence from the ADR-046 experiment**
that the redundant-selector pathway is causally active — explicitly *not* on
ADR-042's historical 4/15. That evidence exists.

In `adr046-after__qwen2.5-3b-instruct.json`, **all four remaining wrong writes**
have this shape, and none of them contains a lookup refusal of any kind:

```
list_tasks()
complete_task(id=1, 'Buy oat milk')    !invalid arguments   <- both selectors, same task
complete_task(id=2, 'Renew passport')  !invalid arguments   <- both selectors, same task
...loop, up to six rejections...
complete_task('oat milk') ; complete_task('Renew passport')    both land
```

Every rejected call names **one task consistently** — `id=1` with
`'Buy oat milk'`, `id=2` with `'Renew passport'`. The model is not confused about
which task it means. It is being told that saying so twice is an error.

- `runs_with_both_selectors`: **12 of 15**
- invalid-argument refusals: **27**
- one before-arm run (`run-15`) spent its whole budget on this and **completed
  nothing at all** — the correct write lost, not just an extra one gained.

## The baseline

`adr046-after__qwen2.5-3b-instruct.json` is ADR-047's before-arm: same code
(HEAD + ADR-045 + ADR-046), same day, same Ollama 0.33.2, same repeat, already
traced. No re-run needed, and it is contemporaneous by construction rather than
by argument.

| | before (= ADR-046 after) |
|---|---|
| wrong write landed | 4/15 |
| runs supplying both selectors | 12/15 |
| invalid-argument refusals | 27 |
| correct write landed | 15/15 |
| case pass | 11/15 |

## Prediction

1. **Invalid-argument refusals fall sharply** — most of the 27 are calls whose two
   selectors agree, and agreement will proceed instead of being refused. This is
   the mechanism, and it is nearly definitional; if it does *not* fall, the
   selectors were disagreeing and the whole premise is wrong.
2. **`wrong_write_landed` falls from 4/15.** This is the real claim and the
   falsifiable one. If the model completes both tasks anyway — just faster,
   without the loop — then the loop was never the cause, the intent to complete
   every listed task was, and ADR-047 is a negative result no matter how clean
   the traces look.
3. **`correct_write_landed` stays at 15/15**, and the run that previously
   completed nothing now completes something.
4. **`h1` stays at 0/15.** ADR-047 does not touch ADR-046's branch.
5. **Tool calls per run fall.** 70 across 15 runs before; the loop is most of the
   excess.

## What would make this a negative result

- `wrong_write_landed` unchanged at 4/15 while invalid arguments fall. That would
  say the redundant-selector rejection was a *delay*, not a cause — the same
  lesson ADR-046 just taught, and it must be recorded the same way rather than
  explained away a second time.
- `correct_write_landed` dropping below 15/15.
- `authorization::completion_resolved_by_id` or
  `tool_calling::survives_a_bad_start` moving outside their historical bands.

## Scope, recorded so the result is not overstated

The both-selectors stumble is **not general**. Measured across both ADR-046 arms,
`tool_calling::completes_the_right_task` — a single-instruction completion, 15
runs — shows **zero** invalid-argument calls in either arm. The pathway is
provoked by the *two-instruction* objective, where the model is tracking two
referents at once.

So ADR-047 should be expected to help multi-instruction cases and do nothing
visible elsewhere. A flat result on the other suites is the prediction, not a
disappointment — and a large move on a single-instruction case would be a reason
to distrust the measurement rather than to celebrate it.
