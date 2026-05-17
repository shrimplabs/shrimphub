"""
Playwright tests for dashboard.html (sync API).

All HTTP calls to localhost:5001 are intercepted via page.route() —
no live server is needed.
"""
import json
import re

import pytest
from playwright.sync_api import Page, expect

from tests.conftest_dashboard import (
    load, setup_routes, make_mock_routes,
    DASHBOARD_PATH,
    AGENT_ACTIVE, AGENT_COMPLETED,
    TASK_PENDING, TASK_IN_PROGRESS,
    PROJECT_DATA, PROJECT_OVERFLOW,
    QUOTA_DATA,
)


# ---------------------------------------------------------------------------
# Page structure
# ---------------------------------------------------------------------------

class TestPageStructure:
    def test_title(self, page: Page):
        load(page)
        assert "Swarm Controller" in page.title()

    def test_has_spawn_bar(self, page: Page):
        load(page)
        expect(page.locator(".spawn-bar")).to_be_visible()

    def test_has_provider_bar(self, page: Page):
        load(page)
        expect(page.locator("#providerSelect")).to_be_visible()

    def test_has_quota_meter(self, page: Page):
        load(page)
        expect(page.locator("#quotaText")).to_be_visible()

    def test_has_kill_all_button(self, page: Page):
        load(page)
        expect(page.locator("#killAllBtn")).to_be_visible()

    def test_history_section_hidden_by_default(self, page: Page):
        load(page)
        expect(page.locator("#historySection")).to_be_hidden()

    def test_last_updated_timestamp_set(self, page: Page):
        load(page)
        text = page.locator("#lastUpdated").text_content()
        assert text and text.strip() != ""


# ---------------------------------------------------------------------------
# Empty state
# ---------------------------------------------------------------------------

class TestEmptyState:
    def test_no_agents_message(self, page: Page):
        load(page)
        expect(page.locator("#activeTasksGrid")).to_contain_text("No agents yet")

    def test_summary_shows_zero_projects(self, page: Page):
        load(page)
        expect(page.locator("#totalProjects")).to_have_text("0")

    def test_summary_shows_zero_active(self, page: Page):
        load(page)
        expect(page.locator("#activeTasks")).to_have_text("0")

    def test_summary_shows_zero_completed(self, page: Page):
        load(page)
        expect(page.locator("#completedTasks")).to_have_text("0")

    def test_no_pending_task_cards(self, page: Page):
        load(page)
        cards = page.locator("#pendingTasksGrid .card")
        expect(cards).to_have_count(0)


# ---------------------------------------------------------------------------
# Quota meter
# ---------------------------------------------------------------------------

class TestQuotaMeter:
    def test_quota_displays_usage(self, page: Page):
        load(page, {"/api/quota": QUOTA_DATA})
        # 800 used of 1000
        expect(page.locator("#quotaText")).to_contain_text("800")
        expect(page.locator("#quotaText")).to_contain_text("1,000")

    def test_quota_fill_width(self, page: Page):
        load(page, {"/api/quota": QUOTA_DATA})
        width = page.locator("#quotaFill").evaluate("el => el.style.width")
        assert width == "80%"

    def test_quota_fill_color_orange_at_80pct(self, page: Page):
        load(page, {"/api/quota": QUOTA_DATA})
        # 80% → orange (#f0883e = rgb(240, 136, 62))
        color = page.locator("#quotaFill").evaluate("el => el.style.background")
        assert color in ("#f0883e", "rgb(240, 136, 62)")

    def test_quota_fill_red_above_90pct(self, page: Page):
        data = {
            "model_remains": [{
                "model_name": "MiniMax-M2.5",
                "current_interval_usage_count": 50,   # 950 used of 1000 = 95%
                "current_interval_total_count": 1000,
                "remains_time": 3600000,
            }]
        }
        load(page, {"/api/quota": data})
        color = page.locator("#quotaFill").evaluate("el => el.style.background")
        assert color in ("#da3633", "rgb(218, 54, 51)")

    def test_quota_reset_time_shown(self, page: Page):
        load(page, {"/api/quota": QUOTA_DATA})
        # 3661000ms = 1h 1m
        expect(page.locator("#quotaReset")).to_contain_text("1h")

    def test_no_quota_data_shows_nothing(self, page: Page):
        load(page)
        # With no quota data the element keeps its default placeholder "-"
        text = page.locator("#quotaText").text_content()
        assert text in ("", "-") or text is None


# ---------------------------------------------------------------------------
# Agent cards
# ---------------------------------------------------------------------------

class TestAgentCards:
    def test_active_agent_card_shown(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_ACTIVE]}})
        expect(page.locator("#activeTasksGrid")).to_contain_text("my-game")
        expect(page.locator("#activeTasksGrid")).to_contain_text("Running")

    def test_completed_agent_card_shows_status(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_COMPLETED]}})
        expect(page.locator("#activeTasksGrid")).to_contain_text("completed")

    def test_agent_card_shows_task_type(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_ACTIVE]}})
        expect(page.locator("#activeTasksGrid")).to_contain_text("feature")

    def test_agent_card_shows_short_id(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_ACTIVE]}})
        expect(page.locator("#activeTasksGrid")).to_contain_text("aaaa-bbb")

    def test_completed_agent_shows_diff_stat(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_COMPLETED]}})
        expect(page.locator("#activeTasksGrid")).to_contain_text("1 file changed")

    def test_multiple_agents_all_shown(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_ACTIVE, AGENT_COMPLETED]}})
        cards = page.locator("#activeTasksGrid .agent-card")
        expect(cards).to_have_count(2)

    def test_active_agent_summary_count(self, page: Page):
        load(page, {"/api/agents": {"agents": [AGENT_ACTIVE]}})
        expect(page.locator("#activeTasks")).to_have_text("1")


# ---------------------------------------------------------------------------
# Task cards
# ---------------------------------------------------------------------------

class TestTaskCards:
    def test_pending_task_shown_in_pending_grid(self, page: Page):
        load(page, {"/api/tasks": {"tasks": [TASK_PENDING]}})
        expect(page.locator("#pendingTasksGrid")).to_contain_text("Add player movement")

    def test_pending_task_shows_project(self, page: Page):
        load(page, {"/api/tasks": {"tasks": [TASK_PENDING]}})
        expect(page.locator("#pendingTasksGrid")).to_contain_text("my-game")

    def test_in_progress_task_not_in_pending_grid(self, page: Page):
        load(page, {"/api/tasks": {"tasks": [TASK_IN_PROGRESS]}})
        cards = page.locator("#pendingTasksGrid .card")
        expect(cards).to_have_count(0)

    def test_completed_count_reflects_history(self, page: Page):
        # Completed count now reads from history agents in the last 24h
        recent_agent = {**AGENT_COMPLETED, "completed_at": "2099-01-01T12:00:00"}
        load(page, {"/api/history": {"agents": [recent_agent]}})
        expect(page.locator("#completedTasks")).to_have_text("1")

    def test_task_type_shown(self, page: Page):
        load(page, {"/api/tasks": {"tasks": [TASK_PENDING]}})
        expect(page.locator("#pendingTasksGrid")).to_contain_text("feature")

    def test_task_priority_shown(self, page: Page):
        load(page, {"/api/tasks": {"tasks": [TASK_PENDING]}})
        expect(page.locator("#pendingTasksGrid")).to_contain_text("50")

    def test_multiple_pending_tasks(self, page: Page):
        t2 = {**TASK_PENDING, "id": "task-002", "description": "Add enemy AI"}
        load(page, {"/api/tasks": {"tasks": [TASK_PENDING, t2]}})
        cards = page.locator("#pendingTasksGrid .card")
        expect(cards).to_have_count(2)


# ---------------------------------------------------------------------------
# Project cards
# ---------------------------------------------------------------------------

class TestProjectCards:
    def test_project_card_shown(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_DATA}})
        expect(page.locator("#projectsGrid")).to_contain_text("my-game")

    def test_project_total_count(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_DATA}})
        expect(page.locator("#totalProjects")).to_have_text("1")

    def test_project_shows_commit(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_DATA}})
        expect(page.locator("#projectsGrid")).to_contain_text("Add player sprite")

    def test_overflow_project_shows_warning(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_OVERFLOW}})
        expect(page.locator("#projectsGrid")).to_contain_text("⚠️")
        expect(page.locator("#overflowCount")).to_have_text("1")

    def test_locked_project_shows_badge(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_OVERFLOW}})
        expect(page.locator(".locked-badge")).to_have_count(1)

    def test_normal_project_no_overflow(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_DATA}})
        expect(page.locator("#projectsGrid")).to_contain_text("✓")
        expect(page.locator("#overflowCount")).to_have_text("0")

    def test_project_shows_largest_file(self, page: Page):
        load(page, {"/api/projects": {"projects": PROJECT_DATA}})
        expect(page.locator("#projectsGrid")).to_contain_text("500")

    def test_project_health_shown_when_available(self, page: Page):
        health = {
            "health_score": 85, "tasks_completed": 10, "tasks_failed": 1,
            "last_commit_age_seconds": 300, "avg_lines": 200, "max_file_lines": 500,
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/health": health,
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)  # health fetches happen in background
        expect(page.locator("#projectsGrid")).to_contain_text("85/100")

    def test_project_health_shows_task_counts(self, page: Page):
        health = {
            "health_score": 75, "tasks_completed": 5, "tasks_failed": 2,
            "last_commit_age_seconds": 600, "avg_lines": 300, "max_file_lines": 800,
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/health": health,
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        expect(page.locator("#projectsGrid")).to_contain_text("✓5")
        expect(page.locator("#projectsGrid")).to_contain_text("✗2")

    def test_project_closure_summary_shows_status_mode_and_regression_count(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "stabilize",
            "closure_status": "red",
            "closure_spec": {
                "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
                "critical_flows": [{"id": "main-flow", "description": "Main flow"}],
            },
            "last_verification_status": "failed",
            "open_regression_count": 2,
            "stall_count": 1,
            "last_verification_run": {
                "status": "failed",
                "results_json": {
                    "boot_ok": False,
                    "tests_ok": True,
                    "critical_flows": {"main-flow": False},
                },
            },
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        expect(page.locator("#projectsGrid")).to_contain_text("Closure")
        expect(page.locator("#projectsGrid")).to_contain_text("red")
        expect(page.locator("#projectsGrid")).to_contain_text("mode stabilize")
        expect(page.locator("#projectsGrid")).to_contain_text("2/0")

    def test_project_closure_summary_shows_gate_pills(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "ship",
            "closure_status": "green",
            "closure_spec": {
                "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
                "critical_flows": [{"id": "main-flow", "description": "Main flow"}],
            },
            "last_verification_status": "passed",
            "open_regression_count": 0,
            "stall_count": 0,
            "last_verification_run": {
                "status": "passed",
                "results_json": {
                    "boot_ok": True,
                    "tests_ok": True,
                    "critical_flows": {"main-flow": True},
                },
            },
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        expect(page.locator("#projectsGrid")).to_contain_text("boot ok")
        expect(page.locator("#projectsGrid")).to_contain_text("tests ok")
        expect(page.locator("#projectsGrid")).to_contain_text("flows 1/1 flows")

    def test_project_closure_summary_shows_live_contract_details(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "stabilize",
            "closure_status": "yellow",
            "closure_spec": {
                "boot": {
                    "command": "python3 -m my_game.cli",
                    "ready_check": {"type": "command", "command": "python3 -m my_game.cli --help"},
                },
                "verification": {
                    "unit_test_command": "pytest -q",
                    "integration_test_command": "pytest -q tests/test_flow.py",
                    "smoke_checks": [{"id": "boss-win", "type": "pytest"}],
                },
                "critical_flows": [{"id": "boss-win", "description": "Defeat the boss"}],
                "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
                "autonomy": {"repair_budget": 8, "stall_threshold": 3},
            },
            "last_verification_status": "failed",
            "open_regression_count": 1,
            "stall_count": 0,
            "last_verification_run": {"status": "failed", "results_json": {"boot_ok": True, "tests_ok": False, "critical_flows": {"boss-win": False}}},
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.locator("summary").filter(has_text="Live Contract").first.click()
        expect(page.locator("#projectsGrid")).to_contain_text("python3 -m my_game.cli")
        expect(page.locator("#projectsGrid")).to_contain_text("boss-win")
        expect(page.locator("#projectsGrid")).to_contain_text("repair_budget")
        expect(page.locator("#projectsGrid")).to_contain_text("Edit Live Contract JSON")

    def test_project_card_closure_button_opens_live_contract(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "stabilize",
            "closure_status": "yellow",
            "closure_spec": {
                "boot": {"command": "python3 -m my_game.cli", "ready_check": {"type": "command", "command": "python3 -m my_game.cli --help"}},
                "verification": {"smoke_checks": [{"id": "boss-win", "type": "pytest"}]},
                "critical_flows": [{"id": "boss-win", "description": "Defeat the boss"}],
                "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
            },
            "last_verification_status": "failed",
            "open_regression_count": 1,
            "stall_count": 0,
            "last_verification_run": {"status": "failed", "results_json": {"boot_ok": True, "tests_ok": False, "critical_flows": {"boss-win": False}}},
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.get_by_role("button", name="📄 Closure").click()
        expect(page.locator("#projectsGrid")).to_contain_text("python3 -m my_game.cli")

    def test_project_card_closure_button_fetches_when_cache_is_cold(self, page: Page):
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {
                "closure": {
                    "project": "my-game",
                    "closure_mode": "build",
                    "closure_status": "yellow",
                    "closure_spec": {
                        "boot": {"command": "python3 -m my_game.cli", "ready_check": {"type": "command", "command": "python3 -m my_game.cli --help"}},
                        "verification": {"smoke_checks": [{"id": "boss-win", "type": "pytest"}]},
                        "critical_flows": [{"id": "boss-win", "description": "Defeat the boss"}],
                        "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
                    },
                    "last_verification_status": "never",
                    "open_regression_count": 0,
                    "stall_count": 0,
                    "last_verification_run": {"status": "never", "results_json": {"critical_flows": {}}},
                }
            },
            "/api/projects/my-game/closure/proposal": {
                "proposal": {
                    "project": "my-game",
                    "profile": "python",
                    "source": "heuristic",
                    "closure_spec": {"critical_flows": [{"id": "boss-win", "description": "Defeat the boss"}]},
                }
            },
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.evaluate("() => { delete closureCache['my-game']; delete closureProposalCache['my-game']; }")
        page.wait_for_timeout(100)
        page.get_by_role("button", name="📄 Closure").click()
        expect(page.locator("#projectsGrid")).to_contain_text("python3 -m my_game.cli")

    def test_project_closure_verify_action_posts_and_shows_toast(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "build",
            "closure_status": "yellow",
            "closure_spec": {"gates": {"critical_flow_count": 0, "max_open_regressions": 0}},
            "last_verification_status": "passed",
            "open_regression_count": 0,
            "stall_count": 0,
            "last_verification_run": {"status": "passed", "results_json": {"boot_ok": True, "tests_ok": True, "critical_flows": {}}},
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
            "/api/projects/my-game/closure/verify": {
                "closure": closure,
                "verification_run": {"id": "vr-1", "status": "passed"},
            },
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.get_by_role("button", name="✓ Verify Now").click()
        expect(page.locator("body")).to_contain_text("closure verification passed")

    def test_project_closure_repair_action_posts_and_shows_toast(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "stabilize",
            "closure_status": "red",
            "closure_spec": {"gates": {"critical_flow_count": 0, "max_open_regressions": 0}},
            "last_verification_status": "failed",
            "open_regression_count": 2,
            "stall_count": 1,
            "last_verification_run": {"status": "failed", "results_json": {"boot_ok": False, "tests_ok": True, "critical_flows": {}}},
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
            "/api/projects/my-game/closure/repair": {
                "project": "my-game",
                "verification_run_id": "run-1",
                "repair_tasks": [{"id": "repair-1"}, {"id": "repair-2"}],
            },
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.get_by_role("button", name="🔧 Generate Repair").click()
        expect(page.locator("body")).to_contain_text("generated 2 closure repair tasks")

    def test_project_closure_mode_action_posts_and_shows_toast(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "build",
            "closure_status": "yellow",
            "closure_spec": {"gates": {"critical_flow_count": 0, "max_open_regressions": 0}},
            "last_verification_status": "passed",
            "open_regression_count": 0,
            "stall_count": 0,
            "last_verification_run": {"status": "passed", "results_json": {"boot_ok": True, "tests_ok": True, "critical_flows": {}}},
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
            "/api/projects/my-game/closure/mode": {
                "closure": {**closure, "closure_mode": "ship"},
            },
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.get_by_role("button", name="ship").click()
        expect(page.locator("body")).to_contain_text("closure mode → ship")

    def test_project_closure_proposal_preview_fetches_and_renders_summary(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "build",
            "closure_status": "yellow",
            "closure_spec": {"gates": {"critical_flow_count": 0, "max_open_regressions": 0}},
            "last_verification_status": "never",
            "open_regression_count": 0,
            "stall_count": 0,
            "last_verification_run": {"status": "never", "results_json": {"critical_flows": {}}},
        }
        proposal = {
            "project": "my-game",
            "profile": "python",
            "source": "heuristic",
            "closure_spec": {
                "critical_flows": [{"id": "boss-victory", "description": "Beat the boss"}],
            },
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
            "/api/projects/my-game/closure/proposal": {"proposal": proposal},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.get_by_role("button", name="♻ Preview Proposal").click()
        expect(page.locator("body")).to_contain_text("proposal ready")
        expect(page.locator("body")).to_contain_text("boss-victory")
        expect(page.locator("body")).to_contain_text("heuristic")

    def test_project_closure_proposal_apply_posts_and_shows_toast(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "build",
            "closure_status": "yellow",
            "closure_spec": {"gates": {"critical_flow_count": 0, "max_open_regressions": 0}},
            "last_verification_status": "never",
            "open_regression_count": 0,
            "stall_count": 0,
            "last_verification_run": {"status": "never", "results_json": {"critical_flows": {}}},
        }
        proposal = {
            "project": "my-game",
            "profile": "python",
            "source": "heuristic",
            "closure_spec": {
                "mode": "stabilize",
                "critical_flows": [{"id": "boss-victory", "description": "Beat the boss"}],
            },
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
            "/api/projects/my-game/closure/proposal/apply": {
                "proposal": proposal,
                "closure": {**closure, "closure_mode": "stabilize"},
            },
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.get_by_role("button", name="⤴ Apply Proposal").click()
        expect(page.locator("body")).to_contain_text("closure proposal applied")

    def test_project_closure_save_contract_posts_and_shows_toast(self, page: Page):
        closure = {
            "project": "my-game",
            "closure_mode": "build",
            "closure_status": "yellow",
            "closure_spec": {
                "mode": "build",
                "gates": {"critical_flow_count": 0, "max_open_regressions": 0},
            },
            "last_verification_status": "never",
            "open_regression_count": 0,
            "stall_count": 0,
            "last_verification_run": {"status": "never", "results_json": {"critical_flows": {}}},
        }
        updated = {
            **closure,
            "closure_spec": {
                "mode": "ship",
                "gates": {"critical_flow_count": 1, "max_open_regressions": 0},
            },
            "closure_mode": "ship",
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/projects/my-game/closure": {"closure": closure},
            "/api/projects/my-game/closure/spec": {"closure": updated},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.wait_for_timeout(600)
        page.locator("summary").filter(has_text="Edit Live Contract JSON").first.click()
        editor = page.locator("textarea[id^='closure-editor-my-game']")
        editor.fill('{\n  "mode": "ship",\n  "gates": {\n    "critical_flow_count": 1,\n    "max_open_regressions": 0\n  }\n}')
        page.get_by_role("button", name="Save Contract").click()
        expect(page.locator("body")).to_contain_text("closure contract saved")

    def test_new_project_creation_surfaces_closure_contract_in_chat_success(self, page: Page):
        proposal = {
            "project": "example-game",
            "profile": "godot",
            "source": "heuristic",
            "closure_spec": {
                "mode": "build",
                "verification": {
                    "smoke_checks": [{"id": "main-scene", "type": "godot_scene"}],
                },
                "critical_flows": [{
                    "id": "main-flow",
                    "description": "Launch the project and reach the primary playable scene.",
                }],
            },
        }
        routes = make_mock_routes({
            "/api/projects": {"projects": PROJECT_DATA},
            "/api/create-project-tasks": {
                "created": 2,
                "project": "example-game",
                "project_type": "godot",
                "closure_proposal": proposal,
            },
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")
        page.evaluate("""
            () => {
                openNewProjectPanel();
                _npProjectName = 'example-game';
                _npOverview = 'Top-down arena game';
                _npTasksPreview = [
                    {id: 'example-game-t1', description: 'Arena scene', dependencies: []},
                    {id: 'example-game-t2', description: 'Blob AI', dependencies: ['example-game-t1']},
                ];
                showNpTaskPreview(_npProjectName, _npTasksPreview);
            }
        """)
        page.evaluate("() => confirmNpTasks()")
        expect(page.locator("#npCreatedBanner")).to_contain_text("Closure Contract")
        expect(page.locator("#npCreatedBanner")).to_contain_text("PROJECT_CLOSURE.md")
        expect(page.locator("#npMessages")).to_contain_text("Closure contract created for example-game")
        expect(page.locator("#npMessages")).to_contain_text("main-flow")
        expect(page.locator("#newProjectPanel")).to_be_visible()


# ---------------------------------------------------------------------------
# Spawn bar
# ---------------------------------------------------------------------------

class TestSpawnBar:
    def test_spawn_button_exists(self, page: Page):
        load(page)
        expect(page.locator("#spawnBtn")).to_be_visible()

    def test_run_all_button_exists(self, page: Page):
        load(page)
        expect(page.get_by_text("▶▶ Run All")).to_be_visible()

    def test_spawn_shows_status_on_success(self, page: Page):
        load(page, {"/api/spawn-batch": {"count": 2, "spawned": ["a", "b"], "skipped": []}})
        page.locator("#spawnBtn").click()
        expect(page.locator("#spawnStatus")).to_contain_text("2 spawned")

    def test_spawn_nothing_message(self, page: Page):
        load(page, {"/api/spawn-batch": {"count": 0, "spawned": [], "skipped": []}})
        page.locator("#spawnBtn").click()
        expect(page.locator("#spawnStatus")).to_contain_text("Nothing to spawn")

    def test_spawn_with_skipped_shows_count(self, page: Page):
        load(page, {"/api/spawn-batch": {"count": 1, "spawned": ["a"], "skipped": ["b", "c"]}})
        page.locator("#spawnBtn").click()
        expect(page.locator("#spawnStatus")).to_contain_text("1 spawned")
        expect(page.locator("#spawnStatus")).to_contain_text("2 skipped")

    def test_max_agents_input_syncs_from_api(self, page: Page):
        load(page, {"/api/max-agents": {"max_active_agents": 7, "max_lines": 2000}})
        value = page.locator("#maxAgents").input_value()
        assert value == "7"

    def test_set_max_agents_shows_confirmation(self, page: Page):
        load(page, {"/api/max-agents": {"max_active_agents": 5}})
        page.locator("#maxAgents").fill("5")
        page.locator("#maxAgents").dispatch_event("change")
        expect(page.locator("#spawnStatus")).to_contain_text("Cap set to 5")


# ---------------------------------------------------------------------------
# Auto mode toggle
# ---------------------------------------------------------------------------

class TestAutoMode:
    def test_auto_button_shows_off_by_default(self, page: Page):
        load(page)
        expect(page.locator("#autoBtn")).to_contain_text("Auto: Off")

    def test_auto_button_shows_on_when_api_reports_enabled(self, page: Page):
        load(page, {"/api/auto-mode": {"enabled": True}})
        expect(page.locator("#autoBtn")).to_contain_text("Auto: On")

    def test_toggle_auto_sends_post_request(self, page: Page):
        captured = []
        mock_routes = make_mock_routes()
        # Override auto-mode to handle both GET (initial state) and POST (capture)
        mock_routes["/api/auto-mode"] = {"enabled": False, "suspended_for_quota": False}

        def capture(route):
            if route.request.method == "POST":
                captured.append(json.loads(route.request.post_data))
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"enabled": True, "suspended_for_quota": False}),
                )
            else:
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"enabled": False, "suspended_for_quota": False}),
                )

        setup_routes(page, mock_routes)
        page.route("http://localhost:5001/api/auto-mode", capture)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.locator("#autoBtn").click()
        page.wait_for_timeout(500)

        assert len(captured) == 1
        assert captured[0].get("enabled") is True

    def test_toggle_auto_on_updates_button_style(self, page: Page):
        load(page)
        page.route("http://localhost:5001/api/auto-mode", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"enabled": True, "spawned": 0}),
        ) if route.request.method == "POST" else route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps({"enabled": False}),
        ))
        page.locator("#autoBtn").click()
        page.wait_for_timeout(300)
        expect(page.locator("#autoBtn")).to_contain_text("Auto: On")


# ---------------------------------------------------------------------------
# Agent output modal
# ---------------------------------------------------------------------------

class TestAgentModal:
    def test_modal_hidden_by_default(self, page: Page):
        load(page)
        class_attr = page.locator("#outputModal").get_attribute("class") or ""
        assert "active" not in class_attr

    def test_clicking_agent_card_opens_modal(self, page: Page):
        routes = make_mock_routes({
            "/api/agents": {"agents": [AGENT_COMPLETED]},
            "/api/agent/1111-2222-3333-4444/output": {"output": "Agent log line 1"},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.locator(".agent-card").first.click()
        expect(page.locator("#outputModal")).to_have_class(re.compile("active"))

    def test_modal_shows_agent_output(self, page: Page):
        routes = make_mock_routes({
            "/api/agents": {"agents": [AGENT_COMPLETED]},
            "/api/agent/1111-2222-3333-4444/output": {"output": "Agent log line 1\nDone"},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.locator(".agent-card").first.click()
        expect(page.locator("#modalOutput")).to_contain_text("Agent log line 1")

    def test_close_button_hides_modal(self, page: Page):
        routes = make_mock_routes({
            "/api/agents": {"agents": [AGENT_COMPLETED]},
            "/api/agent/1111-2222-3333-4444/output": {"output": "log"},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.locator(".agent-card").first.click()
        page.locator("#outputModal .modal-close").click()
        class_attr = page.locator("#outputModal").get_attribute("class") or ""
        assert "active" not in class_attr

    def test_modal_title_shows_project_name(self, page: Page):
        routes = make_mock_routes({
            "/api/agents": {"agents": [AGENT_COMPLETED]},
            "/api/agent/1111-2222-3333-4444/output": {"output": "log"},
        })
        setup_routes(page, routes)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.locator(".agent-card").first.click()
        expect(page.locator("#modalTitle")).to_contain_text("my-game")


# ---------------------------------------------------------------------------
# History section
# ---------------------------------------------------------------------------

class TestHistory:
    def test_history_toggle_shows_section(self, page: Page):
        load(page, {"/api/history": {"agents": [AGENT_COMPLETED]}})
        page.locator("#historyToggle").click()
        expect(page.locator("#historySection")).to_be_visible()

    def test_history_shows_completed_agent(self, page: Page):
        load(page, {"/api/history": {"agents": [AGENT_COMPLETED]}})
        page.locator("#historyToggle").click()
        expect(page.locator("#historyGrid")).to_contain_text("my-game")
        expect(page.locator("#historyGrid")).to_contain_text("completed")

    def test_history_shows_diff_stat(self, page: Page):
        load(page, {"/api/history": {"agents": [AGENT_COMPLETED]}})
        page.locator("#historyToggle").click()
        expect(page.locator("#historyGrid")).to_contain_text("1 file changed")

    def test_history_toggle_hides_again(self, page: Page):
        load(page)
        page.locator("#historyToggle").click()
        page.locator("#historyToggle").click()
        expect(page.locator("#historySection")).to_be_hidden()

    def test_empty_history_message(self, page: Page):
        load(page)
        page.locator("#historyToggle").click()
        expect(page.locator("#historyGrid")).to_contain_text("No history yet")

    def test_history_toggle_button_text_changes(self, page: Page):
        load(page)
        toggle = page.locator("#historyToggle")
        initial = toggle.text_content()
        toggle.click()
        after = toggle.text_content()
        assert initial != after


# ---------------------------------------------------------------------------
# Provider bar
# ---------------------------------------------------------------------------

class TestProviderBar:
    def test_provider_select_populated(self, page: Page):
        load(page)
        options = page.locator("#providerSelect option")
        expect(options).to_have_count(2)

    def test_current_provider_selected(self, page: Page):
        load(page)
        value = page.locator("#providerSelect").input_value()
        assert value == "minimax"

    def test_key_status_shown_for_set_key(self, page: Page):
        load(page)
        expect(page.locator("#providerKeyStatus")).to_contain_text("MINIMAX_API_KEY set")
        status_class = page.locator("#providerKeyStatus").get_attribute("class") or ""
        assert "provider-key-ok" in status_class

    def test_key_status_for_missing_key_after_switch(self, page: Page):
        load(page)
        page.locator("#providerSelect").select_option("claude")
        page.locator("#providerSelect").dispatch_event("change")
        expect(page.locator("#providerKeyStatus")).to_contain_text("not set")
        status_class = page.locator("#providerKeyStatus").get_attribute("class") or ""
        assert "provider-key-missing" in status_class

    def test_model_placeholder_updates_on_switch(self, page: Page):
        load(page)
        # Switch to claude — placeholder should change to claude model
        page.locator("#providerSelect").select_option("claude")
        page.locator("#providerSelect").dispatch_event("change")
        placeholder = page.locator("#providerModel").get_attribute("placeholder") or ""
        assert "claude" in placeholder.lower()

    def test_save_provider_sends_post_request(self, page: Page):
        captured = []

        def capture(route):
            if route.request.method == "POST":
                captured.append(json.loads(route.request.post_data))
                route.fulfill(
                    status=200, content_type="application/json",
                    body=json.dumps({"provider": "claude"}),
                )
            else:
                route.continue_()

        setup_routes(page, make_mock_routes())
        page.route("http://localhost:5001/api/provider", capture)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.wait_for_function("document.getElementById('providerSelect').options.length > 1")
        page.locator("#providerSelect").select_option("claude")
        page.get_by_text("Apply").click()
        page.wait_for_timeout(300)

        assert any(r.get("provider") == "claude" for r in captured)

    def test_model_override_sent_in_request(self, page: Page):
        captured = []

        def capture(route):
            if "/api/provider" in route.request.url and route.request.method == "POST":
                captured.append(json.loads(route.request.post_data))
            route.fulfill(
                status=200, content_type="application/json",
                body=json.dumps({"provider": "minimax"}),
            )

        setup_routes(page, make_mock_routes())
        page.route("http://localhost:5001/api/provider*", capture)
        page.goto(DASHBOARD_PATH.as_uri())
        page.wait_for_function("document.getElementById('lastUpdated').textContent !== ''")

        page.locator("#providerModel").fill("custom-model-v2")
        page.get_by_text("Apply").click()
        page.wait_for_timeout(300)

        assert any(r.get("model") == "custom-model-v2" for r in captured)


# ---------------------------------------------------------------------------
# XSS safety
# ---------------------------------------------------------------------------

class TestXSSSafety:
    def test_project_name_is_escaped(self, page: Page):
        xss_project = {
            "<script>alert(1)</script>": {
                "status": "active", "locked": False, "files": {}, "recent_commits": [],
            }
        }
        load(page, {"/api/projects": {"projects": xss_project}})
        content = page.locator("#projectsGrid").inner_html()
        assert "<script>" not in content
        assert "&lt;script&gt;" in content

    def test_task_description_is_escaped(self, page: Page):
        xss_task = {**TASK_PENDING, "description": '<img src=x onerror="alert(1)">'}
        load(page, {"/api/tasks": {"tasks": [xss_task]}})
        content = page.locator("#pendingTasksGrid").inner_html()
        # Tag must be escaped — no real <img> element should exist
        assert "<img" not in content
        assert "&lt;img" in content

    def test_commit_message_is_escaped(self, page: Page):
        project_xss = {
            "safe-proj": {
                "status": "active", "locked": False, "files": {},
                "recent_commits": [
                    {"hash": "abc", "message": "<script>alert(2)</script>", "age": "1h"}
                ],
            }
        }
        load(page, {"/api/projects": {"projects": project_xss}})
        content = page.locator("#projectsGrid").inner_html()
        assert "<script>" not in content

    def test_agent_output_is_escaped(self, page: Page):
        xss_agent = {
            **AGENT_COMPLETED,
            "output": '<img src=x onerror="document.title=\'HACKED\'">'
        }
        load(page, {"/api/agents": {"agents": [xss_agent]}})
        # Title must not change to HACKED
        assert "HACKED" not in page.title()
