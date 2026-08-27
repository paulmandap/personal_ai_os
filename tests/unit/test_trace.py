"""Run traces: ordering, durability and redaction."""

from __future__ import annotations

from pathlib import Path

from personal_ai_os.observability.trace import (
    Events,
    RunTrace,
    find_trace,
    latest_trace,
    read_trace,
    redact,
)

REDACT_KEYS = [
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
    "credential",
    "authorization",
]


class TestRedaction:
    def test_redacts_obvious_secrets(self):
        out = redact(
            {
                "api_key": "sk-live-abc",
                "db_password": "hunter2",
                "authorization": "Bearer xyz",
                "client_secret": "shhh",
            },
            REDACT_KEYS,
        )
        assert all(v == "***" for v in out.values())

    def test_does_not_redact_token_counts(self):
        """Regression: substring matching redacted `prompt_tokens`.

        This was observed in a real run -- token counts came back as '***',
        which would have quietly destroyed the throughput telemetry that model
        comparison depends on. Matching is per-segment for exactly this reason.
        """
        out = redact(
            {"prompt_tokens": 35, "completion_tokens": 21, "tokens_used": 5},
            REDACT_KEYS,
        )
        assert out == {"prompt_tokens": 35, "completion_tokens": 21, "tokens_used": 5}

    def test_matches_whole_segments_including_camel_case(self):
        out = redact(
            {"accessToken": "a", "OPENAI_API_KEY": "b", "model": "qwen"}, REDACT_KEYS
        )
        assert out["accessToken"] == "***"
        assert out["OPENAI_API_KEY"] == "***"
        assert out["model"] == "qwen"

    def test_recurses_into_nested_structures(self):
        out = redact(
            {"outer": {"items": [{"password": "p", "name": "n"}]}}, REDACT_KEYS
        )
        assert out["outer"]["items"][0]["password"] == "***"
        assert out["outer"]["items"][0]["name"] == "n"


class TestRunTrace:
    def test_events_are_written_immediately(self, tmp_path: Path):
        """A killed process must still leave a readable trace behind."""
        trace = RunTrace.create(agent="a", runs_dir=tmp_path, run_id="abc123")
        trace.event(Events.MODEL_REQUEST, model="m")

        # Read it back without closing the trace: the line is already on disk.
        assert trace.path is not None
        events = list(read_trace(trace.path))
        assert len(events) == 1
        assert events[0].type == Events.MODEL_REQUEST

    def test_sequence_numbers_are_monotonic(self, tmp_path: Path):
        trace = RunTrace.create(agent="a", runs_dir=tmp_path)
        for i in range(5):
            trace.event("tick", i=i)
        assert [e.seq for e in trace.events] == [1, 2, 3, 4, 5]

    def test_context_manager_brackets_the_run(self, tmp_path: Path):
        with RunTrace.create(agent="a", runs_dir=tmp_path) as trace:
            trace.event("middle")
        types = [e.type for e in trace.events]
        assert types == [Events.RUN_START, "middle", Events.RUN_END]
        assert trace.events[-1].data["ok"] is True

    def test_exception_is_recorded_and_reraised(self, tmp_path: Path):
        trace = RunTrace.create(agent="a", runs_dir=tmp_path)
        try:
            with trace:
                raise ValueError("boom")
        except ValueError:
            pass
        types = [e.type for e in trace.events]
        assert Events.ERROR in types
        assert trace.events[-1].data["ok"] is False

    def test_secrets_never_reach_the_file(self, tmp_path: Path):
        trace = RunTrace.create(agent="a", runs_dir=tmp_path, redact_keys=REDACT_KEYS)
        trace.event("call", api_key="sk-live-DO-NOT-LOG")
        assert trace.path is not None
        assert "sk-live-DO-NOT-LOG" not in trace.path.read_text(encoding="utf-8")

    def test_disabled_trace_records_in_memory_only(self, tmp_path: Path):
        trace = RunTrace.disabled(agent="a")
        trace.event("tick")
        assert trace.path is None
        assert len(trace.events) == 1
        assert not list(tmp_path.iterdir())

    def test_write_failure_does_not_break_the_run(self, tmp_path: Path, monkeypatch):
        """Losing observability is bad; losing the work is worse."""
        trace = RunTrace.create(agent="a", runs_dir=tmp_path)

        def explode(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", explode)
        trace.event("tick")  # must not raise
        assert trace.enabled is False


class TestTraceLookup:
    def test_finds_a_trace_by_run_id(self, tmp_path: Path):
        trace = RunTrace.create(agent="a", runs_dir=tmp_path, run_id="deadbeef")
        trace.event("tick")
        assert find_trace(tmp_path, "deadbeef") == trace.path

    def test_missing_run_id_returns_none(self, tmp_path: Path):
        assert find_trace(tmp_path, "nope") is None

    def test_latest_uses_timestamp_ordering(self, tmp_path: Path):
        (tmp_path / "20260101T000000Z_aaa.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "20260827T120000Z_bbb.jsonl").write_text("", encoding="utf-8")
        found = latest_trace(tmp_path)
        assert found is not None and found.name.endswith("_bbb.jsonl")


class TestReadTrace:
    def test_truncated_file_still_reads(self, tmp_path: Path):
        """A trace cut short by a crash is the one you most need to read."""
        path = tmp_path / "t.jsonl"
        path.write_text(
            '{"seq":1,"ts":"x","run_id":"r","type":"a","data":{}}\n'
            '{"seq":2,"ts":"x","run_id":"r","type":"b","dat',  # truncated
            encoding="utf-8",
        )
        events = list(read_trace(path))
        assert [e.type for e in events] == ["a"]
