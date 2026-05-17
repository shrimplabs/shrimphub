from pathlib import Path

from swarm.closure.specs import GODOT_GUT_COMMAND
from swarm.closure.proposals import propose_project_closure


def test_python_project_with_cli_and_tests_gets_stabilize_proposal(tmp_path):
    root = tmp_path / "corridor-quest"
    (root / "corridor_quest").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "corridor_quest" / "__init__.py").write_text("")
    (root / "corridor_quest" / "cli.py").write_text("print('hi')\n")
    (root / "tests" / "test_boss_flow.py").write_text("def test_ok():\n    assert True\n")

    proposal = propose_project_closure("corridor-quest", root, "python")

    assert proposal["profile"] == "python"
    assert proposal["source"] == "heuristic"
    assert proposal["closure_spec"]["mode"] == "stabilize"
    assert proposal["closure_spec"]["boot"]["ready_check"]["command"] == "python3 -m corridor_quest.cli"
    assert proposal["closure_spec"]["verification"]["unit_test_command"] == "python3 -m pytest -q"
    assert proposal["closure_spec"]["verification"]["smoke_checks"][0]["command"] == "python3 -m pytest -q tests/test_boss_flow.py"


def test_typescript_project_prefers_existing_scripts_and_playwright_specs(tmp_path):
    root = tmp_path / "example-web-app"
    (root / "e2e").mkdir(parents=True)
    (root / "package.json").write_text(
        '{"scripts":{"dev":"vite","test":"vitest","test:e2e":"playwright test"},"devDependencies":{"vite":"1.0.0"}}'
    )
    (root / "e2e" / "toolGeneration.spec.ts").write_text("test('ok', async () => {})\n")

    proposal = propose_project_closure("example-web-app", root, None)

    assert proposal["profile"] == "typescript"
    assert proposal["closure_spec"]["boot"]["command"] == "npm run dev"
    assert proposal["closure_spec"]["boot"]["ready_check"]["url"] == "http://127.0.0.1:5173"
    assert proposal["closure_spec"]["verification"]["unit_test_command"] == "npm test"
    assert proposal["closure_spec"]["verification"]["integration_test_command"] == "npm run test:e2e"
    assert "toolGeneration.spec.ts" in proposal["closure_spec"]["verification"]["smoke_checks"][0]["command"]


def test_godot_project_falls_through_to_heuristic_proposal(tmp_path):
    root = tmp_path / "example-godot-game"
    root.mkdir(parents=True)
    (root / "project.godot").write_text("[application]\nconfig/name=\"example-godot-game\"\n")
    proposal = propose_project_closure("example-godot-game", root, None)

    assert proposal["source"] == "heuristic"
    assert proposal["profile"] == "godot"
    assert proposal["closure_spec"]["mode"] in ("stabilize", "build")


def test_godot_project_with_prd_context_uses_quality_gates_and_task_flows(tmp_path):
    root = tmp_path / "example-game"
    root.mkdir(parents=True)
    (root / "project.godot").write_text("[application]\nconfig/name=\"example-game\"\n")

    proposal = propose_project_closure(
        "example-game",
        root,
        "godot",
        context={
            "quality_gates": ["gut --run --exit"],
            "tasks": [
                {"id": "example-game-t06", "description": "Wave system — structured waves and stream mode", "dependencies": ["example-game-t04"]},
                {"id": "example-game-t08", "description": "Upgrade system — between-wave upgrade selection menu", "dependencies": ["example-game-t06"]},
                {"id": "example-game-t09", "description": "Win/lose conditions — all blobs dead, 100 total mass to win", "dependencies": ["example-game-t06", "example-game-t08"]},
            ],
        },
    )

    assert proposal["profile"] == "godot"
    assert proposal["closure_spec"]["boot"]["ready_check"]["type"] == "command"
    assert proposal["closure_spec"]["boot"]["ready_check"]["command"] == "godot --headless --path . --quit"
    assert proposal["closure_spec"]["verification"]["unit_test_command"] == GODOT_GUT_COMMAND
    assert proposal["closure_spec"]["verification"]["smoke_checks"][0]["command"] == GODOT_GUT_COMMAND
    flow_ids = [flow["id"] for flow in proposal["closure_spec"]["critical_flows"]]
    assert "example-game-t09" in flow_ids
    assert proposal["closure_spec"]["mode"] == "stabilize"


def test_godot_project_context_prefers_gameplay_loops_over_hud_tasks(tmp_path):
    root = tmp_path / "example-game"
    root.mkdir(parents=True)
    (root / "project.godot").write_text("[application]\nconfig/name=\"example-game\"\n")

    proposal = propose_project_closure(
        "example-game",
        root,
        "godot",
        context={
            "quality_gates": ["gut --run --exit"],
            "tasks": [
                {"id": "example-game-t02", "description": "Player click-to-spawn blobs, basic movement, hunger decay", "dependencies": ["example-game-t01"]},
                {"id": "example-game-t04", "description": "Food pellets — spawning, consumption, blob growth", "dependencies": ["example-game-t02"]},
                {"id": "example-game-t06", "description": "Wave system — wave-based spawning, difficulty scaling", "dependencies": ["example-game-t04"]},
                {"id": "example-game-t08", "description": "Upgrade system — between-wave upgrade selection menu", "dependencies": ["example-game-t06"]},
                {"id": "example-game-t09", "description": "Win/lose conditions — all blobs dead, 100 total mass to win", "dependencies": ["example-game-t06", "example-game-t08"]},
                {"id": "example-game-t10", "description": "Menu + HUD — wave, mass, blob count, time elapsed", "dependencies": ["example-game-t06", "example-game-t09"]},
            ],
        },
    )

    flow_ids = [flow["id"] for flow in proposal["closure_spec"]["critical_flows"]]
    assert flow_ids[0] == "example-game-t09"
    assert "example-game-t06" in flow_ids
    assert "example-game-t02" in flow_ids or "example-game-t04" in flow_ids
    assert "example-game-t10" not in flow_ids


def test_godot_project_context_does_not_promote_hud_restart_story_over_core_loops(tmp_path):
    root = tmp_path / "example-game"
    root.mkdir(parents=True)
    (root / "project.godot").write_text("[application]\nconfig/name=\"example-game\"\n")

    proposal = propose_project_closure(
        "example-game",
        root,
        "godot",
        context={
            "quality_gates": ["gut --run --exit"],
            "tasks": [
                {"id": "example-game-t06", "description": "Enemy Wave Spawning", "dependencies": ["example-game-t01"]},
                {"id": "example-game-t08", "description": "Win and Lose Conditions", "dependencies": ["example-game-t06", "example-game-t04"]},
                {"id": "example-game-t09", "description": "Difficulty Scaling", "dependencies": ["example-game-t06", "example-game-t07"]},
                {"id": "example-game-t10", "description": "HUD — Mass, Wave, and Restart", "dependencies": ["example-game-t02", "example-game-t04", "example-game-t06", "example-game-t08"]},
            ],
        },
    )

    flow_ids = [flow["id"] for flow in proposal["closure_spec"]["critical_flows"]]
    assert flow_ids[0] == "example-game-t08"
    assert "example-game-t06" in flow_ids
    assert flow_ids[:2] == ["example-game-t08", "example-game-t06"]
