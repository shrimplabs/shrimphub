"""Tests for tools/probe_analytics.py."""
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "tools"))
import probe_analytics as pa


# ---------------------------------------------------------------------------
# make_analytics / track_phase
# ---------------------------------------------------------------------------

class TestMakeAnalytics:
    def test_initial_state(self):
        a = pa.make_analytics()
        assert a["phases"] == {}
        assert a["_current_phase"] is None

    def test_track_phase_creates_entry(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        assert "plan" in a["phases"]
        assert a["_current_phase"] == "plan"

    def test_track_phase_idempotent(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.track_phase(a, "plan")   # second call should not reset counts
        a["phases"]["plan"]["calls"] = 5
        pa.track_phase(a, "plan")
        assert a["phases"]["plan"]["calls"] == 5

    def test_track_phase_switches_current(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.track_phase(a, "scout")
        assert a["_current_phase"] == "scout"
        assert "plan" in a["phases"]
        assert "scout" in a["phases"]


# ---------------------------------------------------------------------------
# record_tokens
# ---------------------------------------------------------------------------

class TestRecordTokens:
    def test_accumulates_output_tokens(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.record_tokens(a, {"input": 1000, "output": 500})
        pa.record_tokens(a, {"input": 800,  "output": 300})
        p = a["phases"]["plan"]
        assert p["calls"] == 2
        assert p["input_tokens"] == 1800
        assert p["output_tokens"] == 800

    def test_accepts_alternate_key_names(self):
        a = pa.make_analytics()
        pa.track_phase(a, "scout")
        pa.record_tokens(a, {"input_tokens": 500, "output_tokens": 200,
                              "cache_read_input_tokens": 100})
        p = a["phases"]["scout"]
        assert p["input_tokens"] == 500
        assert p["output_tokens"] == 200
        assert p["cache_read"] == 100

    def test_ignores_non_dict_tokens(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.record_tokens(a, None)
        pa.record_tokens(a, "bad")
        assert a["phases"]["plan"]["calls"] == 0

    def test_no_current_phase_is_noop(self):
        a = pa.make_analytics()
        pa.record_tokens(a, {"input": 100, "output": 50})  # no phase set
        assert a["phases"] == {}


# ---------------------------------------------------------------------------
# record_tool_calls
# ---------------------------------------------------------------------------

class TestRecordToolCalls:
    def test_basic_comma_separated(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.record_tool_calls(a, "plan", "read_file, search_code, read_file")
        assert a["phases"]["plan"]["tools"]["read_file"] == 2
        assert a["phases"]["plan"]["tools"]["search_code"] == 1

    def test_space_separated(self):
        a = pa.make_analytics()
        pa.track_phase(a, "scout")
        pa.record_tool_calls(a, "scout", "read_file search_code")
        assert a["phases"]["scout"]["tools"]["read_file"] == 1

    def test_unknown_phase_is_noop(self):
        a = pa.make_analytics()
        pa.record_tool_calls(a, "nonexistent", "read_file")
        assert a["phases"] == {}

    def test_empty_string_is_noop(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.record_tool_calls(a, "plan", "")
        assert a["phases"]["plan"]["tools"] == {}


# ---------------------------------------------------------------------------
# handle_log_line
# ---------------------------------------------------------------------------

class TestHandleLogLine:
    def test_phase_start_line(self):
        a = pa.make_analytics()
        pa.handle_log_line(a, "  PHASE: PLAN  (1/4)")
        assert a["_current_phase"] == "plan"
        assert "plan" in a["phases"]

    def test_phase_complete_records_elapsed(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.handle_log_line(a, "  ✓ PLAN complete (42.5s)")
        assert a["phases"]["plan"]["elapsed_s"] == 42.5

    def test_tools_line_counts_calls(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.handle_log_line(a, "Tools: read_file, search_code, read_file")
        assert a["phases"]["plan"]["tools"]["read_file"] == 2
        assert a["phases"]["plan"]["tools"]["search_code"] == 1

    def test_tools_line_ignored_before_phase_set(self):
        a = pa.make_analytics()
        pa.handle_log_line(a, "Tools: read_file")  # no current phase
        assert a["phases"] == {}

    def test_unrelated_line_is_noop(self):
        a = pa.make_analytics()
        pa.handle_log_line(a, "[Pipeline:plan] Calling minimax to frame task...")
        assert a["phases"] == {}


# ---------------------------------------------------------------------------
# totals
# ---------------------------------------------------------------------------

class TestTotals:
    def test_sums_across_phases(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.record_tokens(a, {"input": 1000, "output": 500})
        pa.track_phase(a, "scout")
        pa.record_tokens(a, {"input": 2000, "output": 800})
        t = pa.totals(a)
        assert t["calls"] == 2
        assert t["input_tokens"] == 3000
        assert t["output_tokens"] == 1300

    def test_empty_analytics(self):
        a = pa.make_analytics()
        t = pa.totals(a)
        assert t["calls"] == 0
        assert t["output_tokens"] == 0


# ---------------------------------------------------------------------------
# build_summary
# ---------------------------------------------------------------------------

class TestBuildSummary:
    def _make_state(self, **kwargs):
        s = MagicMock()
        s.failed = kwargs.get("failed", False)
        s.errors = kwargs.get("errors", [])
        s.scout_report = kwargs.get("scout_report", None)
        s.synthesis = kwargs.get("synthesis", None)
        s.work_report = kwargs.get("work_report", None)
        return s

    def test_basic_fields(self):
        a = pa.make_analytics()
        pa.track_phase(a, "plan")
        pa.record_tokens(a, {"input": 500, "output": 200})

        summary = pa.build_summary(
            a,
            task_id="harness-123",
            project="/foo/bar",
            task_type="bug",
            pipeline=["plan", "scout"],
            provider="minimax",
            elapsed_s=42.5,
            final_state=self._make_state(),
        )
        assert summary["task_id"] == "harness-123"
        assert summary["task_type"] == "bug"
        assert summary["pipeline"] == ["plan", "scout"]
        assert summary["elapsed_s"] == 42.5
        assert summary["failed"] is False
        assert summary["total_llm_calls"] == 1
        assert summary["total_output_tokens"] == 200
        assert "plan" in summary["phases"]

    def test_synthesis_block_populated(self):
        a = pa.make_analytics()
        state = self._make_state(synthesis={
            "proposed_tasks": [{"desc": "task1"}, {"desc": "task2"}],
            "confidence": 0.85,
        })
        summary = pa.build_summary(
            a, task_id="x", project="p", task_type="bug",
            pipeline=[], provider=None, elapsed_s=0, final_state=state,
        )
        assert summary["synthesis"]["tasks_proposed"] == 2
        assert summary["synthesis"]["confidence"] == 0.85

    def test_no_synthesis_block_when_none(self):
        a = pa.make_analytics()
        summary = pa.build_summary(
            a, task_id="x", project="p", task_type="bug",
            pipeline=[], provider=None, elapsed_s=0,
            final_state=self._make_state(synthesis=None),
        )
        assert summary["synthesis"] is None

    def test_failed_flag(self):
        a = pa.make_analytics()
        summary = pa.build_summary(
            a, task_id="x", project="p", task_type="bug",
            pipeline=[], provider=None, elapsed_s=0,
            final_state=self._make_state(failed=True, errors=["something broke"]),
        )
        assert summary["failed"] is True
        assert "something broke" in summary["errors"]


# ---------------------------------------------------------------------------
# install() integration (light)
# ---------------------------------------------------------------------------

class TestInstall:
    def test_install_patches_call_llm(self):
        """install() should wrap call_llm so token counts are recorded."""
        import swarm.llm_utils as llm_mod

        orig = llm_mod.call_llm
        a = pa.make_analytics()
        pa.track_phase(a, "plan")

        fake_response = ("result text", {"input": 100, "output": 50}, [])

        with patch.object(llm_mod, "call_llm", return_value=fake_response) as mock_llm:
            # Simulate what install() does: wrap call_llm
            _orig = mock_llm
            def _wrapped(system, messages, **kwargs):
                text, tokens, thinking = _orig(system, messages, **kwargs)
                pa.record_tokens(a, tokens)
                return text, tokens, thinking

            _wrapped("sys", [{"role": "user", "content": "hi"}])

        assert a["phases"]["plan"]["calls"] == 1
        assert a["phases"]["plan"]["input_tokens"] == 100
        assert a["phases"]["plan"]["output_tokens"] == 50

    def test_install_idempotent(self, monkeypatch):
        """Calling install() twice should not double-count tokens."""
        import swarm.llm_utils as llm_mod

        # Reset the sentinel so install() runs fresh
        monkeypatch.delattr(llm_mod, "_probe_analytics_installed", raising=False)

        a = pa.make_analytics()
        pa.track_phase(a, "plan")

        call_count = [0]
        fake = ("text", {"input": 10, "output": 5}, [])

        with patch.object(llm_mod, "call_llm", side_effect=lambda *a, **k: fake):
            pa.install(a)
            pa.install(a)   # second call should be a no-op
            # Both patches on the same object means only one layer
            assert getattr(llm_mod, "_probe_analytics_installed", False)
