"""Verify the mechanism detector before believing its numbers.

This project has recorded seven detector errors, six of them false alarms and
one a blind spot. The rule that came out of it: **verify a detector against real
transcripts, and ask what it cannot see, not only what it reports.**

The real-transcript half is done by eye against `runs/probe`. This is the other
half -- synthetic traces are the right instrument *here* because the subject
under test is the detector, not the agent. Every count it reports must be shown
to fire when it should and stay silent when it should not; a detector that has
never fired is not a verified detector.

    pytest evaluations/mechanisms -q
"""

from __future__ import annotations

import json
from pathlib import Path

import count_mechanisms as mechanisms

NAMED = {"Buy oat milk"}
UNNAMED = {"Renew passport"}


def task_json(title: str, status: str = "done", tid: int = 1) -> str:
    return json.dumps({"id": tid, "title": title, "status": status})


def trace(tmp_path: Path, *events) -> Path:
    """Write a JSONL trace of (tool, args, ok, error_or_result) tuples."""
    path = tmp_path / "run-01_deadbeef.jsonl"
    lines = []
    seq = 0
    for tool, args, ok, payload in events:
        seq += 1
        lines.append(
            {"seq": seq, "ts": "t", "run_id": "deadbeef", "type": "tool.requested",
             "data": {"tool": tool, "arguments": args}}
        )
        seq += 1
        data = {"tool": tool, "ok": ok}
        data["result" if ok else "error"] = payload
        lines.append({"seq": seq, "ts": "t", "run_id": "deadbeef",
                      "type": "tool.result", "data": data})
    path.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    return path


def analyse(tmp_path, *events):
    return mechanisms.analyse(trace(tmp_path, *events), NAMED, UNNAMED)


REFUSE_MISS = (False, "no open task matches 'dentist appointment'.")
REFUSE_DONE = (False, "nothing to change: 'Buy oat milk' is already marked done.")
REFUSE_BOTH = (False, "invalid arguments for complete_task: give exactly one of "
                      "'title' or 'id'")


class TestH1:
    def test_it_fires_on_the_adr_042_signature(self, tmp_path):
        """The exact five-step shape ADR-042 traced, ending in the wrong write."""
        out = analyse(
            tmp_path,
            ("complete_task", {"title": "dentist appointment"}, *REFUSE_MISS),
            ("complete_task", {"title": "oat milk"}, True, task_json("Buy oat milk")),
            ("complete_task", {"id": 1, "title": "Buy oat milk"}, *REFUSE_BOTH),
            ("complete_task", {"title": "oat milk"}, *REFUSE_MISS),
            ("complete_task", {"title": "Renew passport"}, True,
             task_json("Renew passport", tid=2)),
        )
        assert out["h1"] is True
        assert out["h1_strict"] is True   # the wrong write names a title
        assert out["wrong_write_landed"] is True
        assert out["correct_write_landed"] is True

    def test_a_wrong_write_by_id_is_h1_but_not_adr_042_strict(self, tmp_path):
        """The distinction the baseline turned up, and the reason both are kept.

        ADR-042's H1 required the wrong write to name a title its refusal had
        enumerated. Removing the enumeration removed the *title*, not the wrong
        write: the observed shape now selects by id. Reporting only the strict
        count would show a clean zero over a defect happening 4 times in 15.
        """
        out = analyse(
            tmp_path,
            ("list_tasks", {}, True, "[]"),
            ("complete_task", {"id": 1}, True, task_json("Buy oat milk")),
            ("complete_task", {"title": "Buy oat milk"}, *REFUSE_MISS),
            ("complete_task", {"id": 2}, True, task_json("Renew passport", tid=2)),
        )
        assert out["h1"] is True
        assert out["h1_strict"] is False

    def test_it_fires_through_the_new_already_done_refusal_too(self, tmp_path):
        """ADR-046 replaces the refusal text; H1 must not go blind because of it.

        This is the ADR-040 lesson inverted: a detector keyed to the *old*
        wording would report zero after the change and the fall would be an
        artefact of the instrument, not a result.
        """
        out = analyse(
            tmp_path,
            ("complete_task", {"title": "oat milk"}, True, task_json("Buy oat milk")),
            ("complete_task", {"title": "oat milk"}, *REFUSE_DONE),
            ("complete_task", {"title": "Renew passport"}, True,
             task_json("Renew passport", tid=2)),
        )
        assert out["h1"] is True

    def test_it_stays_silent_when_no_wrong_write_lands(self, tmp_path):
        out = analyse(
            tmp_path,
            ("complete_task", {"title": "dentist appointment"}, *REFUSE_MISS),
            ("complete_task", {"title": "oat milk"}, True, task_json("Buy oat milk")),
        )
        assert out["h1"] is False

    def test_a_wrong_write_before_any_refusal_is_not_h1(self, tmp_path):
        """Order is the claim. A wrong write with no refusal preceding it is a
        different defect, and counting it here would inflate the mechanism."""
        out = analyse(
            tmp_path,
            ("complete_task", {"title": "Renew passport"}, True,
             task_json("Renew passport", tid=2)),
            ("complete_task", {"title": "dentist"}, *REFUSE_MISS),
        )
        assert out["h1"] is False
        assert out["wrong_write_landed"] is True   # still recorded, separately

    def test_an_unsuccessful_wrong_write_is_not_h1(self, tmp_path):
        out = analyse(
            tmp_path,
            ("complete_task", {"title": "dentist"}, *REFUSE_MISS),
            ("complete_task", {"title": "Renew passport"}, False, "DENIED"),
        )
        assert out["h1"] is False


class TestSubstitute:
    def test_it_fires_on_failed_lookup_then_list_then_id(self, tmp_path):
        out = analyse(
            tmp_path,
            ("complete_task", {"title": "dentist"}, *REFUSE_MISS),
            ("list_tasks", {}, True, "[]"),
            ("complete_task", {"id": 2}, True, task_json("Renew passport", tid=2)),
        )
        assert out["substitute"] is True

    def test_list_then_id_without_a_failed_lookup_is_not_the_substitute(
        self, tmp_path
    ):
        """The false positive that would matter most.

        `authorization::completion_selected_by_filter` legitimately calls
        list_tasks and then completes by id -- 'everything overdue, mark it
        done' names no title. Counting that as the substitute pathway would
        manufacture a defect out of correct behaviour.
        """
        out = analyse(
            tmp_path,
            ("list_tasks", {}, True, "[]"),
            ("complete_task", {"id": 2}, True, task_json("Renew passport", tid=2)),
        )
        assert out["substitute"] is False

    def test_a_failed_id_lookup_does_not_arm_it(self, tmp_path):
        """`survives_a_bad_start` fails on the id path; that is not this."""
        out = analyse(
            tmp_path,
            ("complete_task", {"id": 999}, False, "no task with id 999"),
            ("list_tasks", {}, True, "[]"),
            ("complete_task", {"id": 1}, True, task_json("Buy oat milk")),
        )
        assert out["substitute"] is False


class TestRefusalTaxonomy:
    def test_each_refusal_class_is_recognised(self, tmp_path):
        cases = {
            "no open task matches 'x'.": "no_open_match",
            "nothing to change: 'Buy oat milk' is already marked done.":
                "already_closed",
            "'Buy oat milk' is cancelled, not open, so it cannot be completed.":
                "already_closed",
            "'Buy oat milk' is done, not open, and update_task matches open "
            "tasks by title.": "already_closed",
            "no open task matches 'milk'. 2 closed tasks do; use a more "
            "specific title.": "closed_ambiguous",
            "2 open tasks match 'oat milk': ['a', 'b']": "open_ambiguous",
            "invalid arguments for complete_task: give exactly one":
                "invalid_arguments",
            "no task with id 999": "unknown_id",
            "nothing was created: 'x' looks like a task the user already has":
                "duplicate_guard",
            "something else entirely": "other",
        }
        for text, expected in cases.items():
            assert mechanisms.classify(text) == expected, text

    def test_closed_ambiguity_is_not_swallowed_by_the_plain_miss(self, tmp_path):
        """The multiple-closed message *begins* with ADR-042's wording, so the
        general pattern would match it first and the taxonomy would report a
        plain miss where a closed-side ambiguity happened. Ordering in
        REFUSAL_PATTERNS is what prevents that, so it is asserted."""
        assert mechanisms.classify(
            "no open task matches 'milk'. 3 closed tasks do; use a more "
            "specific title."
        ) == "closed_ambiguous"
        assert mechanisms.classify("no open task matches 'milk'.") == "no_open_match"
        assert mechanisms.classify("3 open tasks match 'milk'.") == "open_ambiguous"


class TestSelectorsAndWrites:
    def test_both_selectors_supplied_is_visible(self, tmp_path):
        """ADR-047's signature. An earlier renderer printed only the id and hid
        it completely."""
        out = analyse(
            tmp_path,
            ("complete_task", {"id": 1, "title": "Buy oat milk"}, *REFUSE_BOTH),
        )
        assert out["both_selectors"] == 1
        assert "id=1" in out["sequence"][0] and "Buy oat milk" in out["sequence"][0]

    def test_a_write_that_did_not_reach_done_is_not_counted_as_landed(
        self, tmp_path
    ):
        out = analyse(
            tmp_path,
            ("update_task", {"find": "oat milk", "notes": "x"}, True,
             task_json("Buy oat milk", status="todo")),
        )
        assert out["correct_write_landed"] is False

    def test_update_task_reaching_done_counts_as_a_finish(self, tmp_path):
        """qwen2.5:3b serves 'mark it done' with update_task(status=done);
        counting only complete_task would miss half the writes."""
        out = analyse(
            tmp_path,
            ("update_task", {"find": "oat milk", "status": "done"}, True,
             task_json("Buy oat milk")),
        )
        assert out["correct_write_landed"] is True
