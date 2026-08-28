"""Evaluation harness.

Measures whether the system works *well*, not merely whether it runs.

Two properties shape everything here:

**Scores come from the trace, not from a judge model** (ADR-020). Which tool was
called first, whether arguments validated, whether a permission was violated --
all of it is already ground truth in ``runs/*.jsonl``. A judge would add a second
unmeasured model grading an unmeasured agent, competing for the same VRAM, with
scores that differ run to run.

**A case result is a pass rate over N runs, not a boolean** (ADR-021). Local
models are stochastic even at ``temperature=0``; one run is an anecdote.
"""
