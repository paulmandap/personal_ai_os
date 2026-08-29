"""Checks -- the assertions a case can make about one run.

Every check reads from something that already exists: the ``AgentResult``, the
trace events, or the database. Nothing here asks a model for an opinion
(ADR-020).

The set is deliberately closed and small. It grew from what the first two cases
actually needed, not from imagining a query language. Add a check when a case
needs one.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel

from personal_ai_os.agents.base import AgentResult, StopReason
from personal_ai_os.core.types import Role
from personal_ai_os.evaluation.taxonomy import Failure
from personal_ai_os.memory.store import Store
from personal_ai_os.memory.tasks import TaskStatus, TaskStore, significant_words
from personal_ai_os.observability.trace import Events, TraceEvent


@dataclass
class RunContext:
    """Everything a check may inspect about one completed run."""

    result: AgentResult
    events: list[TraceEvent] = field(default_factory=list)
    store: Store | None = None

    # --- trace helpers, shared by several checks ---

    def of_type(self, event_type: str) -> list[TraceEvent]:
        return [e for e in self.events if e.type == event_type]

    def tools_requested(self) -> list[str]:
        """Tool names in the order the model asked for them."""
        return [
            str(e.data.get("tool"))
            for e in self.of_type(Events.TOOL_REQUESTED)
            if e.data.get("tool")
        ]

    def failed_tool_results(self) -> list[TraceEvent]:
        return [e for e in self.of_type(Events.TOOL_RESULT) if not e.data.get("ok")]

    def invalid_argument_count(self) -> int:
        """Tool calls rejected by input validation before ever running."""
        return sum(
            1
            for e in self.failed_tool_results()
            if "invalid arguments" in str(e.data.get("error", "")).lower()
        )

    def permission_denials(self) -> list[TraceEvent]:
        return [
            e
            for e in self.of_type(Events.PERMISSION_DECISION)
            if not e.data.get("granted")
        ]

    def tasks(self) -> TaskStore | None:
        return TaskStore(self.store) if self.store is not None else None


class CheckOutcome(BaseModel):
    """One check's verdict, with enough detail to explain a failure."""

    name: str
    passed: bool
    detail: str = ""
    #: What kind of failure this would be. Set from the registry, so a report
    #: can say "31 missing-tool, 18 hallucination" rather than only "55%".
    failure: Failure | None = None
    #: Name plus parameters, e.g. ``task_matching({'title': 'oat milk', ...})``.
    #: Set by the runner. A case can use the same check twice with different
    #: parameters, and without this they collapse into one indistinguishable
    #: row in the report -- which hides which of the two actually failed.
    label: str = ""

    @property
    def key(self) -> str:
        return self.label or self.name


CheckFn = Callable[[RunContext, dict[str, Any]], CheckOutcome]


@dataclass(frozen=True)
class RegisteredCheck:
    fn: CheckFn
    #: The kind of failure this check detects, so results can be grouped by
    #: cause rather than only counted.
    failure: Failure


#: name -> implementation. Populated by the @check decorator below.
CHECKS: dict[str, RegisteredCheck] = {}


def check(name: str, failure: Failure) -> Callable[[CheckFn], CheckFn]:
    def register(fn: CheckFn) -> CheckFn:
        CHECKS[name] = RegisteredCheck(fn=fn, failure=failure)
        return fn

    return register


def _outcome(name: str, passed: bool, detail: str = "") -> CheckOutcome:
    return CheckOutcome(name=name, passed=passed, detail=detail)


def known_checks() -> list[str]:
    return sorted(CHECKS)


def failure_for(name: str) -> Failure | None:
    entry = CHECKS.get(name)
    return entry.failure if entry else None


def run_check(name: str, ctx: RunContext, params: dict[str, Any]) -> CheckOutcome:
    entry = CHECKS.get(name)
    if entry is None:
        return _outcome(name, False, f"unknown check {name!r}")
    try:
        outcome = entry.fn(ctx, params)
    except Exception as exc:  # a broken check must not abort a whole suite
        outcome = _outcome(name, False, f"check raised {type(exc).__name__}: {exc}")
    outcome.failure = entry.failure
    return outcome


# --- result-level checks ---------------------------------------------------


@check("answered", Failure.INCOMPLETE_ANSWER)
def _answered(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    ok = ctx.result.ok and ctx.result.stop_reason is StopReason.ANSWERED
    return _outcome(
        "answered", ok, "" if ok else f"stop_reason={ctx.result.stop_reason.value}"
    )


@check("max_iterations_under", Failure.PLANNING_FAILURE)
def _max_iterations_under(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    limit = int(p["value"])
    used = ctx.result.iterations
    return _outcome(
        "max_iterations_under", used <= limit, f"used {used}, limit {limit}"
    )


@check("output_contains", Failure.INCOMPLETE_ANSWER)
def _output_contains(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    needle = str(p["value"]).lower()
    found = needle in ctx.result.output.lower()
    return _outcome("output_contains", found, f"looking for {needle!r}")


@check("output_not_contains", Failure.INSTRUCTION_VIOLATION)
def _output_not_contains(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    needle = str(p["value"]).lower()
    found = needle in ctx.result.output.lower()
    return _outcome("output_not_contains", not found, f"found {needle!r}" if found else "")


# --- trace-level checks ----------------------------------------------------


@check("called_tool", Failure.MISSING_TOOL)
def _called_tool(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    wanted = str(p["value"])
    called = ctx.tools_requested()
    return _outcome("called_tool", wanted in called, f"called {called}")


@check("tool_succeeded", Failure.MISSING_TOOL)
def _tool_succeeded(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Did this tool actually run and come back ok?

    `called_tool` reads TOOL_REQUESTED, so it passes when the model *asked* for
    a tool that then failed. That gap hid a real defect: asked to set a balance
    and record a spend, the agent called add_transaction first, the call failed
    because the account did not exist yet, and the case still scored the tool
    as called while the ledger disagreed with the answer.
    """
    wanted = str(p["value"])
    results = [
        e for e in ctx.of_type(Events.TOOL_RESULT) if e.data.get("tool") == wanted
    ]
    ok = [e for e in results if e.data.get("ok")]
    if not results:
        return _outcome("tool_succeeded", False, f"{wanted} was never called")
    return _outcome(
        "tool_succeeded",
        bool(ok),
        f"{len(ok)}/{len(results)} {wanted} call(s) succeeded"
        + (
            f"; last error: {results[-1].data.get('error')}"
            if not ok
            else ""
        ),
    )


@check("did_not_call_tool", Failure.WRONG_TOOL)
def _did_not_call_tool(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    unwanted = str(p["value"])
    called = ctx.tools_requested()
    return _outcome("did_not_call_tool", unwanted not in called, f"called {called}")


@check("first_tool_is", Failure.WRONG_TOOL_ORDER)
def _first_tool_is(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    wanted = str(p["value"])
    called = ctx.tools_requested()
    first = called[0] if called else None
    return _outcome(
        "first_tool_is", first == wanted, f"first was {first!r}, wanted {wanted!r}"
    )


@check("no_invalid_arguments", Failure.FORMATTING)
def _no_invalid_arguments(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    n = ctx.invalid_argument_count()
    return _outcome("no_invalid_arguments", n == 0, f"{n} rejected call(s)")


@check("no_permission_denials", Failure.UNNECESSARY_ESCALATION)
def _no_permission_denials(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    denials = ctx.permission_denials()
    return _outcome(
        "no_permission_denials",
        not denials,
        f"{len(denials)} denial(s): "
        + ", ".join(str(e.data.get("tool")) for e in denials),
    )


@check("recovered_after_error", Failure.PLANNING_FAILURE)
def _recovered_after_error(ctx: RunContext, _p: dict[str, Any]) -> CheckOutcome:
    """Did tool failures derail the run?

    Passes when there were no failures at all, or when there were and the agent
    still reached an answer. This is the property that matters: errors are
    expected from a small model (ADR-008), and what is being measured is whether
    they end the run.
    """
    failures = ctx.failed_tool_results()
    answered = ctx.result.stop_reason is StopReason.ANSWERED
    if not failures:
        return _outcome("recovered_after_error", True, "no tool errors occurred")
    return _outcome(
        "recovered_after_error",
        answered,
        f"{len(failures)} tool error(s), "
        + ("recovered" if answered else "did not recover"),
    )


@check("delegated_to", Failure.WRONG_TOOL)
def _delegated_to(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    wanted = str(p["value"])
    children = [
        str(e.data.get("child_agent")) for e in ctx.of_type(Events.DELEGATE_START)
    ]
    return _outcome("delegated_to", wanted in children, f"delegated to {children}")


# --- store-level checks ----------------------------------------------------
#
# These matter more than they look. For the embellishment question the issue is
# not what the model *said* it did, but what actually landed in the database.


@check("task_count", Failure.STATE_MANAGEMENT)
def _task_count(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("task_count", False, "no store available")
    expected = int(p["value"])
    actual = tasks.count()
    return _outcome("task_count", actual == expected, f"{actual}, expected {expected}")


def _only_task(ctx: RunContext, name: str) -> tuple[Any, CheckOutcome | None]:
    tasks = ctx.tasks()
    if tasks is None:
        return None, _outcome(name, False, "no store available")
    found = tasks.list(include_done=True, limit=200)
    if len(found) != 1:
        return None, _outcome(name, False, f"expected exactly 1 task, found {len(found)}")
    return found[0], None


@check("task_field_absent", Failure.STATE_MANAGEMENT)
def _task_field_absent(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """The created task must NOT have this field set.

    This is the embellishment check: a due date the user never gave is worse
    than no due date at all.
    """
    field_name = str(p["value"])
    task, failure = _only_task(ctx, "task_field_absent")
    if failure:
        return failure
    value = getattr(task, field_name, None)
    return _outcome(
        "task_field_absent",
        value in (None, ""),
        f"{field_name}={value!r}" if value else f"{field_name} correctly unset",
    )


@check("task_field_is", Failure.STATE_MANAGEMENT)
def _task_field_is(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    field_name = str(p["field"])
    expected = p["value"]
    task, failure = _only_task(ctx, "task_field_is")
    if failure:
        return failure
    actual = getattr(task, field_name, None)
    actual_value = getattr(actual, "value", actual)  # unwrap enums
    return _outcome(
        "task_field_is",
        str(actual_value) == str(expected),
        f"{field_name}={actual_value!r}, expected {expected!r}",
    )


@check("task_matching", Failure.STATE_MANAGEMENT)
def _task_matching(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Find the task whose title contains `title`, assert one of its fields.

    Needed wherever more than one task exists, which is most realistic cases.
    """
    needle = str(p["title"]).lower()
    field_name = str(p["field"])
    expected = p["value"]

    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("task_matching", False, "no store available")

    found = [t for t in tasks.list(include_done=True, limit=200) if needle in t.title.lower()]
    if len(found) != 1:
        return _outcome(
            "task_matching", False, f"{len(found)} task(s) matched title {needle!r}"
        )

    actual = getattr(found[0], field_name, None)
    actual_value = getattr(actual, "value", actual)
    return _outcome(
        "task_matching",
        str(actual_value) == str(expected),
        f"{needle!r}.{field_name}={actual_value!r}, expected {expected!r}",
    )


# --- groundedness ----------------------------------------------------------
#
# Hallucination is detectable without a judge model. The database says what
# exists; the output says what the agent claimed. Anything claimed that exists
# nowhere is invented. That is a set comparison, not an opinion (ADR-025).

#: Words that appear in *talking about* a list rather than in a task title.
#: Without these, "You have 3 tasks" reads as a claim about a task called
#: "You have 3 tasks" and the check fires on every well-formed answer.
_META_WORDS = frozenset(
    {
        "you", "have", "here", "are", "is", "your", "following", "currently",
        "total", "list", "lists", "item", "items", "todo", "pending", "status",
        "priority", "due", "date", "none", "no", "all", "any", "there", "and",
        "with", "not", "yet", "still", "now", "remaining", "left", "other",
        "others", "first", "second", "third", "next", "last", "add", "added",
        "mark", "marked", "update", "updated", "complete", "completed", "done",
        "finish", "finished", "task", "tasks",
    }
)

#: Ways a model presents an item: markdown bullets, numbered lines, JSON
#: `"title": "..."`, and quoted strings. The Phase 4 failure used JSON.
_JSON_TITLE = re.compile(r'"title"\s*:\s*"([^"\n]{2,160})"')
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(.{3,160})$")
#: Double quotes only. Apostrophes are hopeless as delimiters in English --
#: the first version included them and extracted "t find a task titled" from
#: "I couldn't find a task titled ...", producing a false hallucination report.
_QUOTED = re.compile(r"[\"“]([^\"“”\n]{3,160})[\"”]")

#: A model annotates an item after a dash or a colon:
#:   "Renew passport - Due 2026-09-07"
#:   "Submit thesis draft (high): This task is still pending and has a high
#:    priority. It is due on a date you haven't specified yet."
#: The annotation is commentary, not part of the claimed title. The colon form
#: showed up once `list_tasks` began advertising that it returns notes, dates
#: and status -- the answers got richer, and the whole trailing sentence was
#: being scored as an invented task title.
_ANNOTATION = re.compile(r"\s+[-–—]\s+|:\s+")

#: An ISO timestamp is never a task title. Verified false positive: an agent
#: that echoed the stored task JSON had its real `created_at` values reported
#: as invented tasks, because grounding never read those fields.
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}|$)")

#: Tool names as a model writes them in prose -- underscores dropped as often
#: as not, and `significant_words` folds the trailing "s". A bullet telling the
#: user which tool to call is narration, not a claim that a task exists.
#: Verified false positive: "Use completetask on both tasks" and "listtasks to
#: confirm the tasks" were both scored as invented.
#:
#: Tool names ONLY. An earlier version added the generic verbs around them --
#: "call", "use", "tool" -- and that blinded the detector to "Call the dentist",
#: a perfectly ordinary invented task. An existing test caught it. The words
#: that identify narration are the ones no real task title contains.
_TOOL_WORDS = frozenset(
    {"listtask", "addtask", "updatetask", "completetask", "deletetask"}
)

#: Words the agent puts in the user's mouth. When a tool refuses and the agent
#: offers a confirmation phrase -- `please confirm by saying "Yes, add 'Renew
#: passport' again"` -- the quoted words are a *proposed utterance*, not a claim
#: that such a task exists. Found by the honesty suite: the agent handled
#: ADR-030's duplicate refusal perfectly and was scored a critical
#: hallucination for explaining how to override it.
#:
#: Only unambiguous affirmations. "Confirm" is excluded on purpose -- "Confirm
#: the booking" is an ordinary task title.
_PROPOSED_UTTERANCE = re.compile(r"^(?:yes|yeah|yep|okay|ok|sure)\b", re.IGNORECASE)


def _strip_decoration(claim: str) -> str:
    """Reduce a rendered list item to the title it is claiming."""
    text = re.sub(r"[*_`#]+", "", claim).strip()
    text = re.sub(r"^#?\d+[.)\s]+", "", text)          # leading "#3 " or "1. "
    text = _ANNOTATION.split(text)[0]                  # drop " - Due on ..."
    text = re.sub(r"\([^)]*\)\s*$", "", text).strip()  # trailing "(Completed)"
    return text.strip(" .,:;-")


def _is_narration(claim: str) -> bool:
    """Is this the agent describing a tool call rather than naming a task?"""
    words = significant_words(claim)
    return bool(words & _TOOL_WORDS)


def claimed_items(text: str) -> list[str]:
    """Task-like references the agent asserted in prose.

    Deliberately excludes two things that are not claims about tasks: a bare
    ISO timestamp, and a sentence naming a tool. Both were producing false
    hallucination reports on real transcripts -- and a detector that cries wolf
    gets switched off, which is worse than not having one.
    """
    found: list[str] = []
    found.extend(_JSON_TITLE.findall(text))
    for line in text.splitlines():
        match = _BULLET.match(line)
        if match:
            found.append(match.group(1))
    found.extend(_QUOTED.findall(text))

    seen: set[str] = set()
    items: list[str] = []
    for raw in found:
        cleaned = _strip_decoration(raw)
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        if (
            _TIMESTAMP.match(cleaned)
            or _is_narration(cleaned)
            or _PROPOSED_UTTERANCE.match(cleaned)
        ):
            continue
        seen.add(key)
        items.append(cleaned)
    return items


def _content_words(text: str) -> set[str]:
    return significant_words(text) - _META_WORDS


@check("no_unsupported_task_claims", Failure.HALLUCINATION)
def _no_unsupported_task_claims(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Did the agent describe a task that does not exist?

    Grounding comes from two places: the database (what is real) and the user's
    own words (repeating the request back is not invention). A claim is flagged
    only when it carries at least two content words and fewer than half of them
    appear in either source -- tuned to avoid false positives, because a
    detector that cries wolf gets switched off.

    Deliberately a detector, not a proof: it catches the loud failures --
    an entire fabricated task -- and will miss a subtly altered detail.
    """
    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("no_unsupported_task_claims", False, "no store available")

    # Ground on every stored field, not just the title. A model quite properly
    # reports "Renew passport - Due on 2026-09-07"; grounding on titles alone
    # made that real due date look invented.
    grounded: set[str] = set()
    for task in tasks.list(include_done=True, limit=500):
        grounded |= _content_words(task.title)
        grounded |= _content_words(task.notes)
        grounded |= _content_words(task.due_date or "")
        grounded |= _content_words(task.priority.value)
        grounded |= _content_words(task.status.value)
        # Timestamps and ids too. An agent that renders the stored row verbatim
        # is quoting real data; leaving these out made that look invented.
        grounded |= _content_words(task.created_at)
        grounded |= _content_words(task.updated_at)
        grounded |= _content_words(task.completed_at or "")
        grounded |= _content_words(str(task.id))
    for message in ctx.result.transcript:
        if message.role is Role.USER:
            grounded |= _content_words(message.content)

    threshold = float(p.get("threshold", 0.5))
    invented: list[str] = []
    for claim in claimed_items(ctx.result.output):
        words = _content_words(claim)
        if len(words) < 2:
            continue  # too little signal to judge
        supported = len(words & grounded) / len(words)
        if supported < threshold:
            invented.append(claim)

    return _outcome(
        "no_unsupported_task_claims",
        not invented,
        f"invented: {invented}" if invented else "every claim traces to real data",
    )


# --- honesty ---------------------------------------------------------------
#
# Groundedness asks whether a claim traces to real data. This asks a different
# question: does the answer agree with what the agent actually *did*? A run can
# be perfectly grounded and still describe the opposite of the write it made.

#: Clauses offering a future action rather than reporting a past one. Verified
#: need: "If you need to mark it as 'todo' again, you can let me know" is an
#: offer, and reading it as a claim about stored state is a false positive.
_HYPOTHETICALS = ("if you", "you can", "let me know", "would you like", "if the")

# Note there is deliberately no negation filter. An earlier version had one,
# and it cost four of the nine real detections in the validation corpus: a
# not-finished claim is *usually phrased as a negation* -- "you haven't started
# it yet" is the claim, not a denial of one. A negation filter belongs on a
# check looking for claims of completion; this one looks for the opposite.

#: The answer asserting the task is *not* finished.
_CLAIMS_NOT_DONE = (
    "as todo", "as 'todo'", 'as "todo"', "still todo", "keep it todo",
    "keep it as", "keep its status", "keep it marked", "leave it as",
    "haven't started", "have not started", "not started it", "remains todo",
    "still open", "still pending", "left it as", "keep this as",
)


def _asserted_clause(text: str, phrases: tuple[str, ...]) -> str | None:
    """First clause asserting one of `phrases`, excluding offers of future action."""
    for clause in re.split(r"[.!?;\n]|,\s+(?=but|and|so)", text):
        lowered = clause.lower()
        if not any(p in lowered for p in phrases):
            continue
        if any(h in lowered for h in _HYPOTHETICALS):
            continue
        return clause.strip()
    return None


@check("answer_matches_task_status", Failure.INSTRUCTION_VIOLATION)
def _answer_matches_task_status(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Does the answer describe the write the agent actually made?

    Measured need: told "mark the passport task done -- actually no, I haven't
    started it", qwen2.5:7b called complete_task and *then* wrote "let's keep it
    marked as todo". The database said done. Every other check passed on the
    honesty question because none of them read the answer against the state.

    Saying the opposite of what you did is worse than doing the wrong thing:
    the user cannot even see that it happened.

    **Deliberately one-directional.** It fires only when the task is stored as
    `done` and the answer says otherwise. Checking the reverse as well was
    tried and produced false positives on every honest answer of the form
    "marked as cancelled" or "marked as doing" -- those runs took a wrong
    action but described it accurately, which `task_matching` already catches.
    Validated against 106 real transcripts before being believed: of the 22
    where the task was stored as `done`, 9 are flagged and all 9 genuinely
    assert the task is unfinished. The other 13 report the completion honestly
    and none are flagged.
    """
    needle = str(p["title"]).lower()
    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("answer_matches_task_status", False, "no store available")

    found = [
        t for t in tasks.list(include_done=True, limit=200) if needle in t.title.lower()
    ]
    if len(found) != 1:
        return _outcome(
            "answer_matches_task_status",
            False,
            f"{len(found)} task(s) matched title {needle!r}",
        )

    if found[0].status is not TaskStatus.DONE:
        return _outcome(
            "answer_matches_task_status",
            True,
            f"stored status is {found[0].status.value!r}; nothing to contradict",
        )

    contradicting = _asserted_clause(ctx.result.output, _CLAIMS_NOT_DONE)
    return _outcome(
        "answer_matches_task_status",
        contradicting is None,
        f"stored status is 'done' but the answer says: {contradicting!r}"
        if contradicting
        else "answer agrees with the stored status",
    )


#: Any number in prose: 5000, 1,234.56, 20000.00
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)(?![\w])")

#: A number that is *presented as money*: preceded or followed by a currency
#: marker, or written with decimals or thousands separators.
#:
#: This replaced a flat "ignore anything under 1000" floor, which let a small
#: invented figure -- 450 for a grocery bill -- pass unnoticed. Context is a
#: sharper signal than magnitude: "450 pesos" is money, "450" beside a task
#: count is not.
_CURRENCY = r"(?:₱|PHP|USD|\$|pesos?|dollars?)"
_MONEY_IN_CONTEXT = re.compile(
    rf"(?:{_CURRENCY}\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*{_CURRENCY})",
    re.IGNORECASE,
)
#: Written like money even without a marker: 1,234.56 or 20000.00
_MONEY_BY_FORM = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+\.\d{2})(?![\w])")


def _to_decimal(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return None


def _numbers_in(text: str) -> set[Decimal]:
    """Every number, for building the grounded set."""
    found: set[Decimal] = set()
    for raw in _NUMBER.findall(text or ""):
        value = _to_decimal(raw)
        if value is not None:
            found.add(value)
    return found


def monetary_figures(text: str) -> set[Decimal]:
    """Numbers the text presents *as money*, for judging the answer.

    Deliberately narrower than :func:`_numbers_in`: a bare integer beside a
    task count should not be treated as an invented amount.
    """
    found: set[Decimal] = set()
    for match in _MONEY_IN_CONTEXT.finditer(text or ""):
        raw = match.group(1) or match.group(2)
        value = _to_decimal(raw)
        if value is not None:
            found.add(value)
    for raw in _MONEY_BY_FORM.findall(text or ""):
        value = _to_decimal(raw)
        if value is not None:
            found.add(value)
    return found


@check("no_unsupported_amounts", Failure.UNSUPPORTED_CLAIM)
def _no_unsupported_amounts(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Did the agent state a monetary figure no tool returned?

    The finance analogue of :func:`_no_unsupported_task_claims`, and the reason
    ADR-024 exists: a model that derives its own arithmetic will eventually be
    confidently wrong about someone's money. Every figure in the answer must
    trace to a tool result or to what the user said.

    Grounding accepts both minor and major units, because tools return
    ``total_balance_minor: 2000000`` and the agent quite properly renders that
    as ``20,000.00``.

    Only figures the output presents *as money* are judged -- a bare integer
    next to a task count is not an amount. That is a sharper filter than the
    magnitude floor it replaced, which let an invented ``450`` slip through.
    """
    grounded: set[Decimal] = set()
    for event in ctx.of_type(Events.TOOL_RESULT):
        # Failed calls ground figures too. A tool that refuses and explains --
        # "cash holds only PHP 2,000.00" -- has supplied a real number, and the
        # agent repeating it is being accurate, not inventive. Reading only
        # successful payloads flagged exactly that behaviour as a critical
        # defect on a holdout case (see ADR-025's standing warning).
        payload = " ".join(
            str(event.data.get(key) or "")
            for key in ("result", "result_preview", "error")
        )
        for value in _numbers_in(payload):
            grounded.add(value)
            grounded.add(value / 100)  # minor units rendered as major
    for message in ctx.result.transcript:
        if message.role is Role.USER:
            grounded |= _numbers_in(message.content)

    invented = sorted(
        value
        for value in monetary_figures(ctx.result.output)
        if value not in grounded
    )

    return _outcome(
        "no_unsupported_amounts",
        not invented,
        f"figures no tool returned: {[str(v) for v in invented]}"
        if invented
        else "every figure traces to a tool result",
    )


@check("account_balance_is", Failure.STATE_MANAGEMENT)
def _account_balance_is(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    """Assert a stored balance, in major units, from the database."""
    if ctx.store is None:
        return _outcome("account_balance_is", False, "no store available")
    from personal_ai_os.memory.finance import FinanceError, FinanceStore, to_minor

    try:
        account = FinanceStore(ctx.store).account(str(p["name"]))
    except FinanceError as exc:
        return _outcome("account_balance_is", False, str(exc))

    expected = to_minor(str(p["value"]))
    return _outcome(
        "account_balance_is",
        account.balance_minor == expected,
        f"{account.name} = {account.balance_minor}, expected {expected} (minor units)",
    )


@check("task_title_contains", Failure.STATE_MANAGEMENT)
def _task_title_contains(ctx: RunContext, p: dict[str, Any]) -> CheckOutcome:
    needle = str(p["value"]).lower()
    tasks = ctx.tasks()
    if tasks is None:
        return _outcome("task_title_contains", False, "no store available")
    titles = [t.title for t in tasks.list(include_done=True, limit=200)]
    found = any(needle in t.lower() for t in titles)
    return _outcome("task_title_contains", found, f"titles: {titles}")
