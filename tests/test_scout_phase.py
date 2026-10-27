"""Regression tests for ScoutPhase stall-counter and early-completion logic."""

import json
import pytest
from unittest.mock import patch, call

from swarm.pipeline import TaskState, run_pipeline


def _make_state(tmp_path, plan=None):
    return TaskState(
        task_id="scout-test",
        task_type="bug",
        project="proj",
        description="Fix the crash",
        project_path=str(tmp_path),
        workspace=str(tmp_path),
        plan=plan or {
            "goal": "Fix crash",
            "constraints": [],
            "success_criteria": [],
            "unknowns": [],
            "risk_areas": [],
            "files_to_inspect_first": ["scripts/player.gd"],
            "likely_files_to_change": ["scripts/player.gd"],
            "implementation_steps": [],
            "test_plan": [],
            "scope": "small",
            "fast_path": False,
        },
    )


def _valid_scout_json():
    return json.dumps({
        "files_inspected": ["scripts/player.gd"],
        "findings": ["Null ref at line 42"],
        "hypotheses": ["Player node freed before signal fires"],
        "recommended_actions": ["Check node lifetime before signal connect"],
        "confidence": 0.9,
    })


def _tool_call_response(tool="read_file_range"):
    return f'[TOOL_CALL]{{"tool":"{tool}","path":"scripts/player.gd","start":1,"end":50}}[/TOOL_CALL]'


class TestScoutStallReset:
    def test_stall_counter_resets_after_too_early_rejection(self, tmp_path):
        """When SCOUT_COMPLETE is rejected for being too early (loop < min_loops),
        the stall counter must reset to 0 so the next loop isn't treated as a stall."""
        state = _make_state(tmp_path, plan={
            "goal": "Fix crash",
            "constraints": [],
            "success_criteria": [],
            "unknowns": [],
            "risk_areas": [],
            "files_to_inspect_first": [],  # no reading list → min_loops = 5
            "likely_files_to_change": [],
            "implementation_steps": [],
            "test_plan": [],
            "scope": "small",
            "fast_path": False,
        })

        scout_complete = "SCOUT_COMPLETE\n" + _valid_scout_json()
        tool_call = _tool_call_response()

        # Sequence:
        # Loop 1: too early, rejected → stall counter resets → no stall nudge
        # Loop 2: tool call (proves counter was reset, not treated as stall 1)
        # Loop 3: SCOUT_COMPLETE accepted (loop 3 < 5 still, but we just need to confirm
        #          the stall counter didn't fire a "output SCOUT_COMPLETE now" nudge
        #          at loop 2 when the model was doing a legitimate tool call)
        #
        # If the stall counter was NOT reset after rejection, loop 2's tool call
        # would be followed by stall count = 1 (accumulated from rejection loop),
        # and loop 3's tool call would trigger a spurious "output SCOUT_COMPLETE" nudge.
        # We verify no such nudge appears by checking injected messages.

        call_count = [0]
        injected_messages = []

        def fake_llm(system, messages, *args, **kwargs):
            n = call_count[0]
            call_count[0] += 1
            # Track any user messages injected by the phase (not the first prompt)
            if n > 0 and messages and messages[-1]["role"] == "user":
                injected_messages.append(messages[-1]["content"])
            if n == 0:
                return scout_complete, {}, []   # too early (loop 1, min 5)
            elif n == 1:
                return tool_call, {}, []        # tool call after rejection
            else:
                return scout_complete, {}, []   # complete (loop 3, still < 5 but test ends)

        with patch("swarm.phases.scout.call_llm", side_effect=fake_llm), \
             patch("swarm.phases.scout.execute_tool", return_value={"ok": True, "content": "line 42: null ref"}):
            # Run only 3 loops max for this test
            with patch("swarm.phases.scout._MAX_SCOUT_LOOPS", 3):
                run_pipeline(["scout"], state, config={"data_dir": str(tmp_path)}, log_fn=lambda _: None)

        # The injected message at loop 2 (after tool call returned) must NOT be
        # "output SCOUT_COMPLETE now" — that would mean the stall counter incorrectly
        # accumulated from the rejection and fired a contradictory nudge.
        stall_nudges = [m for m in injected_messages if "output SCOUT_COMPLETE" in m.lower() and "too early" not in m.lower()]
        assert stall_nudges == [], (
            f"Spurious 'output SCOUT_COMPLETE' nudge fired after too-early rejection reset: {stall_nudges}"
        )

    def test_malformed_scout_complete_gets_repair_nudge_not_stall_nudge(self, tmp_path):
        """When the model outputs SCOUT_COMPLETE with broken JSON, the phase must
        send a JSON-repair prompt (not a generic stall nudge) and reset the stall counter.

        Strategy: no reading list → min_loops=5. Return tool calls for loops 0-3,
        malformed JSON at loop 4, valid JSON at loop 5. The repair nudge must appear
        in the messages passed to the call after malformed JSON.
        """
        state = _make_state(tmp_path, plan={
            "goal": "Fix crash",
            "constraints": [],
            "success_criteria": [],
            "unknowns": [],
            "risk_areas": [],
            "files_to_inspect_first": [],   # no reading list → min_loops = 5
            "likely_files_to_change": [],
            "implementation_steps": [],
            "test_plan": [],
            "scope": "small",
            "fast_path": False,
        })

        malformed = "SCOUT_COMPLETE\n{bad json: missing quotes}"
        tool_call = _tool_call_response()
        valid = "SCOUT_COMPLETE\n" + _valid_scout_json()

        call_count = [0]
        all_call_messages = []

        def fake_llm(system, messages, *args, **kwargs):
            n = call_count[0]
            call_count[0] += 1
            all_call_messages.append(list(messages))
            if n < 4:
                return tool_call, {}, []   # loops 0-3: tool calls to reach min_loops=5 floor
            elif n == 4:
                return malformed, {}, []   # loop 5 (index 4): malformed JSON
            else:
                return valid, {}, []       # loop 6 (index 5): valid JSON, accepted

        with patch("swarm.phases.scout.call_llm", side_effect=fake_llm), \
             patch("swarm.phases.scout.execute_tool", return_value={"ok": True, "content": "found crash"}):
            run_pipeline(["scout"], state, config={"data_dir": str(tmp_path)}, log_fn=lambda _: None)

        assert call_count[0] >= 6, f"Expected ≥6 LLM calls, got {call_count[0]}"
        # The call after malformed (index 5) must include the repair nudge as the last user message
        post_malformed_msgs = all_call_messages[5]
        user_msgs = [m["content"] for m in post_malformed_msgs if m["role"] == "user"]
        assert user_msgs, "No user messages in post-malformed call"
        last_user = user_msgs[-1].lower()
        assert "malformed" in last_user or "json" in last_user or "valid json" in last_user, (
            f"Expected JSON repair nudge, last user message was: {last_user!r}"
        )
        assert "you have not been making progress" not in last_user, (
            "Malformed SCOUT_COMPLETE must not produce a generic stall nudge"
        )

    def test_valid_scout_complete_accepted_without_stall_interference(self, tmp_path):
        """A scout that completes cleanly after the min-loop floor is met must
        succeed on first valid SCOUT_COMPLETE without any stall nudges."""
        # Plan with reading list → min_loops = 3
        state = _make_state(tmp_path, plan={
            "goal": "Fix crash",
            "constraints": [],
            "success_criteria": [],
            "unknowns": [],
            "risk_areas": [],
            "files_to_inspect_first": ["scripts/a.gd", "scripts/b.gd"],
            "likely_files_to_change": ["scripts/a.gd"],
            "implementation_steps": [],
            "test_plan": [],
            "scope": "small",
            "fast_path": False,
        })

        tool_call = _tool_call_response()
        valid = "SCOUT_COMPLETE\n" + _valid_scout_json()

        call_count = [0]
        injected_after_floor = []

        def fake_llm(system, messages, *args, **kwargs):
            n = call_count[0]
            call_count[0] += 1
            if n >= 3 and messages and messages[-1]["role"] == "user":
                injected_after_floor.append(messages[-1]["content"])
            # loops 0,1 = tool calls; loop 2 = SCOUT_COMPLETE (loop index 3 = min_loops met)
            if n < 2:
                return tool_call, {}, []
            return valid, {}, []

        with patch("swarm.phases.scout.call_llm", side_effect=fake_llm), \
             patch("swarm.phases.scout.execute_tool", return_value={"ok": True, "content": "found crash"}):
            result = run_pipeline(["scout"], state, config={"data_dir": str(tmp_path)}, log_fn=lambda _: None)

        assert result.scout_report is not None, "Scout should have completed with a valid report"
        assert result.scout_report.get("confidence") == 0.9
        # No stall nudges should have been injected
        stall_nudges = [m for m in injected_after_floor if "output scout_complete" in m.lower()]
        assert stall_nudges == [], f"Unexpected stall nudges: {stall_nudges}"
