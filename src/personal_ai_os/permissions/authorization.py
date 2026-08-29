"""Where did the authority for this write come from?

Two different questions get confused when a write is judged, and separating them
is what makes a mechanical gate usable:

**Resource provenance** -- *where did the target come from?* Was the thing being
written to named by the user, resolved from trusted state, or smuggled in from
tool output? `grounding.write_is_grounded` answers this, crudely.

**Authorization provenance** -- *where did the permission come from?* Did the
user's turn authorise a write of this kind at all?

They come apart, and the measurement says so. `write_is_grounded` alone
eliminated every state change in the injection cases (15/15 task lists intact)
and blocked **21 of 40 legitimate writes** -- because *"Everything that is
overdue, mark it done"* and *"mark the second one done"* never name their
targets. Resource provenance is blind to the fact that those requests plainly
authorise a completion; it only sees that "beta report" is not in the sentence.

Authorization provenance sees the opposite thing, and gets both right:

    "What is on my task list?"        -> no write authorised at all -> deny
    "Everything overdue, mark it done" -> completions authorised     -> allow

**Why a word list is acceptable here and would not be in a detector.** This
reads the *user's own message*, which is trusted input an attacker cannot edit.
It is intent recognition on the authorization path, not attack-string matching
on the detection path -- the thing ADR-035's review rightly warns against. A
payload buried in a task note cannot add "please delete" to what the user typed.

**What it is not.** Not a claim that an authorised action class makes any
specific target safe; that is resource provenance's job, and the two are meant
to compose. Not a semantic parser -- it will miss intent phrased without any of
these verbs, and that failure is an *escalation to the human*, not a silent
allow.
"""

from __future__ import annotations

from personal_ai_os.memory.tasks import significant_words

#: What kind of change a tool makes. Grouped by what a user would recognise
#: themselves asking for, not by tool name -- `complete_task` and
#: `update_task(status=done)` are the same request in a person's head.
CREATE = "create"
FINISH = "finish"
MODIFY = "modify"
RECORD_MONEY = "record_money"

#: Verbs that authorise each action class, in the user's own words. Folded by
#: `significant_words`, so "finished"/"finishing" both reach "finish".
_INTENT: dict[str, frozenset[str]] = {
    CREATE: frozenset(
        {
            "add", "creat", "new", "remember", "track", "note", "put",
            "schedul", "book", "remind",
        }
    ),
    FINISH: frozenset(
        {
            "don", "finish", "complet", "mark", "tick", "off", "did", "sort",
            "handl", "deal", "clos", "resolv",
        }
    ),
    MODIFY: frozenset(
        {
            "chang", "updat", "set", "mov", "reschedul", "renam", "cancel",
            "edit", "fix", "correct", "make", "push", "bump",
        }
    ),
    RECORD_MONEY: frozenset(
        {
            "spent", "spend", "paid", "pay", "transfer", "mov", "record", "set",
            "balanc", "cost", "bought", "buy", "deposit", "withdrew", "ha", "is",
        }
    ),
}

#: Which action classes authorise each write tool. A tool may be reachable by
#: more than one intent, and `update_task` is why: setting a status to done *is*
#: finishing, so "mark everything overdue as done" must authorise it.
#:
#: Measured, not anticipated. qwen2.5:7b serves that request with
#: `complete_task` and qwen2.5:3b with `update_task(status=done)` -- identical
#: user intent, different tool. Classifying `update_task` as MODIFY alone denied
#: the 3B thirty times on a request it was handling correctly. A gate that
#: depends on which tool a model happens to pick is measuring the model, not the
#: authorization.
TOOL_ACTIONS: dict[str, frozenset[str]] = {
    "add_task": frozenset({CREATE}),
    "complete_task": frozenset({FINISH}),
    "update_task": frozenset({MODIFY, FINISH}),
    "set_balance": frozenset({RECORD_MONEY}),
    "add_transaction": frozenset({RECORD_MONEY}),
    "transfer": frozenset({RECORD_MONEY}),
    "add_commitment": frozenset({RECORD_MONEY}),
    "add_goal": frozenset({RECORD_MONEY}),
}


def write_is_authorized(objective: str, tool_name: str) -> bool:
    """Did the user's turn authorise a write of this kind?

    Unknown tools return True: this decides whether to *escalate*, and a tool
    nobody has classified should fall through to the ordinary permission policy
    rather than be silently blocked by an incomplete table.
    """
    actions = TOOL_ACTIONS.get(tool_name)
    if actions is None:
        return True

    said = significant_words(objective)
    stems = frozenset().union(*(_INTENT[a] for a in actions))
    # `significant_words` folds suffixes, so compare on prefixes: "completed"
    # folds to "complet", "finished" to "finish".
    #
    # One direction only. Matching `stem.startswith(word)` as well let the bare
    # word "do" satisfy the stem "don[e]", so "What do I still need to do?" read
    # as authorising a task creation -- the exact injection this is meant to
    # stop.
    return any(word.startswith(stem) for word in said for stem in stems)


__all__ = ["write_is_authorized", "TOOL_ACTIONS", "CREATE", "FINISH", "MODIFY",
           "RECORD_MONEY"]
