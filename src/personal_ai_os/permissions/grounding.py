"""Is this write something the user actually asked for?

The question this answers is narrow and mechanical: **does the thing being
written appear anywhere in the user's own message?**

It exists because two attempts to defend the instructions-in-data boundary by
talking to the model both failed. ADR-034 stated the rule; ADR-035 delimited the
content the rule referred to. The `safety` suite sat at 70/75 through all of it
and through having no rule at all -- roughly 375 runs, three configurations,
93% every time. Only *which* injection succeeded ever moved.

So this one does not ask the model anything. It is a comparison in Python
between the objective and what the tool says it is about to touch, and an
injected note cannot argue with it. That is the whole point: the model is the
component being defended, so it cannot also be the one enforcing the defence.

**What makes the signal clean.** Every injection measured so far writes
something the user never mentioned -- `add_task("Cleanup done")` in answer to
*"What do I still need to do?"*. Every legitimate write names its target:
*"Add a task to renew my passport"* -> `"Renew passport"`. The overlap is not
subtle, which is why a crude token check is the right instrument rather than a
model.

**What it is not.** Not a judgement about intent, and not a claim that grounded
writes are safe -- an injection that happens to echo the user's words would pass.
It raises the cost of the attack; it does not end it.
"""

from __future__ import annotations

from personal_ai_os.memory.tasks import significant_words


def write_is_grounded(objective: str, resource: str) -> bool:
    """Does `resource` share any content word with what the user asked for?

    `resource` is `Tool.describe_resource(args)` -- the same string already
    shown in an approval prompt, so the gate judges exactly what a human would
    be asked to approve.

    Any overlap is enough. A stricter rule would fire on ordinary paraphrase
    (*"I just spent 450 pesos on groceries"* -> `-450.00 on cash`, where only
    two of three tokens match), and a gate that fires on ordinary work trains
    people to click through the prompts that matter (ADR-014).

    A resource with no content words is treated as grounded. The gate needs a
    reason to fire; an absence of signal is not one.
    """
    wanted = significant_words(resource)
    if not wanted:
        return True
    return bool(wanted & significant_words(objective))


__all__ = ["write_is_grounded"]
