"""Tests for the dashboard Telemetry panel (feature-395920652-0421).

Verifies the frontend surface that backs the per-project Telemetry view:
- dashboard.html contains the Telemetry panel section + heatmap section
- dashboard-config.js exposes toggleTelemetry / loadTelemetry / renderTelemetry
  / loadTelemetryHeatmap and the heatmap canvas renderer
- The panel references the GET /api/telemetry/<game>/summary and
  /heatmap endpoints documented in internal/docs/telemetry-spec.md
"""

from pathlib import Path


def _root() -> Path:
    return Path(__file__).parent.parent


def _html() -> str:
    return (_root() / "dashboard.html").read_text(encoding="utf-8")


def _config_js() -> str:
    return (_root() / "dashboard-config.js").read_text(encoding="utf-8")


class TestTelemetryPanelMarkup:
    """dashboard.html must include the Telemetry panel structure."""

    def test_telemetry_panel_exists(self):
        assert 'id="telemetry-panel"' in _html()

    def test_telemetry_toggle_hint_exists(self):
        assert 'id="telemetry-toggle-hint"' in _html()

    def test_telemetry_sessions_grid_exists(self):
        assert 'id="telemetry-sessions-grid"' in _html()

    def test_telemetry_victory_grid_exists(self):
        assert 'id="telemetry-victory-grid"' in _html()

    def test_telemetry_funnel_container_exists(self):
        assert 'id="telemetry-funnel"' in _html()

    def test_telemetry_quit_scenes_container_exists(self):
        assert 'id="telemetry-quit-scenes"' in _html()

    def test_telemetry_death_causes_container_exists(self):
        assert 'id="telemetry-death-causes"' in _html()

    def test_telemetry_heatmap_section_exists(self):
        assert 'id="telemetry-heatmap-section"' in _html()

    def test_telemetry_heatmap_canvas_exists(self):
        assert 'id="telemetry-heatmap-canvas"' in _html()

    def test_telemetry_panel_inside_main_content(self):
        html = _html()
        main_start = html.index('id="main-content"')
        main_end = html.index('<div class="quota-bar">')
        telemetry_pos = html.index('id="telemetry-panel"')
        assert main_start < telemetry_pos < main_end, \
            "telemetry-panel must live inside #main-content"


class TestTelemetryPanelWiring:
    """The panel must trigger loadTelemetry via toggleTelemetry()."""

    def test_toggle_telemetry_handler(self):
        assert "toggleTelemetry()" in _html()

    def test_heatmap_scene_change_handler(self):
        assert "loadTelemetryHeatmap()" in _html()


class TestTelemetryPanelJS:
    """dashboard-config.js must define the four panel functions."""

    def test_defines_toggle_telemetry(self):
        assert "function toggleTelemetry()" in _config_js()

    def test_defines_load_telemetry(self):
        assert "async function loadTelemetry()" in _config_js()

    def test_defines_render_telemetry(self):
        assert "function renderTelemetry(" in _config_js()

    def test_defines_load_telemetry_heatmap(self):
        assert "async function loadTelemetryHeatmap()" in _config_js()

    def test_defines_render_heatmap_canvas(self):
        assert "function _renderHeatmap(" in _config_js()

    def test_calls_summary_endpoint(self):
        js = _config_js()
        assert "/api/telemetry/" in js
        assert "/summary" in js

    def test_calls_heatmap_endpoint_with_scene_query(self):
        js = _config_js()
        assert "/heatmap" in js
        assert "scene=" in js

    def test_selects_per_project(self):
        """The panel must drive the <game> path param from the sidebar selection."""
        js = _config_js()
        assert "_selectedProject" in js
        assert "encodeURIComponent(game)" in js

    def test_panel_shows_empty_state_when_no_project(self):
        """When the sidebar has no selection, the panel must hide content + show hint."""
        js = _config_js()
        assert "telemetry-empty-project" in js
