from pathlib import Path

from swarm import project_graph_policy as policy


def test_quality_errors_reject_redundant_godot_setup_task():
    tasks = [
        {"id": "p-t1", "description": "Project Setup", "dependencies": []},
        {"id": "p-t2", "description": "Grid System", "dependencies": ["p-t1"]},
        {"id": "p-t3", "description": "Tower Data Model", "dependencies": ["p-t1"]},
    ]

    errors = policy.task_graph_quality_errors(tasks, project_type="godot")

    assert any("Do not create generic Project Setup" in error for error in errors)


def test_quality_errors_allow_setup_title_for_python_project():
    tasks = [
        {"id": "p-t1", "description": "Project Setup", "dependencies": []},
        {"id": "p-t2", "description": "CLI Parser", "dependencies": ["p-t1"]},
    ]

    errors = policy.task_graph_quality_errors(tasks, project_type="python")

    assert not any("Do not create generic Project Setup" in error for error in errors)


def test_semantic_warnings_require_hud_to_display_multiple_state_categories():
    tasks = [
        {"id": "p-t1", "description": "Player Control", "dependencies": []},
        {"id": "p-t2", "description": "Wave Progression", "dependencies": []},
        {"id": "p-t3", "description": "HUD Display", "dependencies": ["p-t1"]},
    ]

    warnings = policy.task_graph_semantic_warnings(tasks)

    assert any("HUD/Feedback tasks should depend on multiple gameplay state categories" in w for w in warnings)


def test_repair_context_summarizes_graph_shape():
    tasks = [
        {"id": "proj-t1", "description": "Foundation", "dependencies": []},
        {"id": "proj-t2", "description": "State", "dependencies": ["proj-t1"]},
        {"id": "proj-t3", "description": "Layout", "dependencies": ["proj-t2"]},
        {"id": "proj-t4", "description": "HUD", "dependencies": ["proj-genesis"]},
        {"id": "proj-t5", "description": "Menu", "dependencies": ["proj-genesis"]},
    ]

    text = policy.render_graph_repair_context(
        tasks,
        ["Generated dependency graph is too flat"],
        allowed_external_roots=["proj-genesis"],
    )

    assert "Root-like tasks: proj-t1, proj-t4, proj-t5" in text
    assert "Tasks depending only on external anchors: proj-t4, proj-t5" in text
    assert "Validation errors:" in text
    assert "- Generated dependency graph is too flat" in text


def test_repair_prompt_requires_task_identity_preservation():
    prompt = policy.build_graph_repair_prompt(
        "repair-proj",
        "overview",
        [
            {"id": "repair-proj-t1", "description": "Foundation", "type": "feature", "priority": 50, "dependencies": []},
            {"id": "repair-proj-t2", "description": "System B", "type": "feature", "priority": 50, "dependencies": []},
        ],
        ["Generated dependency graph is too flat"],
        allowed_external_roots=["repair-proj-genesis"],
    )

    assert "Do not rename, remove, merge, or split tasks." in prompt
    assert "Current graph diagnostics:" in prompt
    assert "Tasks depending only on external anchors" in prompt


def test_project_creation_validation_checks_repo_and_graph(tmp_path: Path):
    project_path = tmp_path / "game"
    project_path.mkdir()
    (project_path / ".git").mkdir()
    (project_path / "README.md").write_text("# game\n")
    (project_path / ".gitignore").write_text(".godot/\n")
    tasks = [
        {"id": "game-t1", "description": "Foundation", "dependencies": []},
        {"id": "game-t2", "description": "HUD", "dependencies": ["missing"]},
    ]

    errors = policy.project_creation_validation_errors(project_path, tasks)

    assert any("unknown dependencies: missing" in error for error in errors)
