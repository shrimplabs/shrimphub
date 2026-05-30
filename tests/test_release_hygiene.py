import subprocess
import json
from pathlib import Path




def test_required_godot_template_files_exist():
    repo = Path(__file__).resolve().parents[1]
    required = [
        "templates/godot/autoload/state_server.gd",
        "templates/godot/autoload/test_harness.gd",
        "templates/godot/check_scripts.gd",
        "templates/godot/icon.svg",
        "templates/godot/test/unit/test_placeholder.gd",
        "swarm/godot_bootstrap.py",
    ]

    missing = [path for path in required if not (repo / path).exists()]
    assert missing == []


def _tracked_files(repo: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.splitlines()


def test_no_runtime_or_session_artifacts_are_tracked():
    repo = Path(__file__).resolve().parents[1]
    forbidden_exact = {
        ".env",
        "config.json",
        "SKILL.md",
        "SKILLS.md",
        "RAG_PLAN.md",
        "RAG_IMPLEMENTATION.md",
        "memory/MEMORY.md",
    }
    forbidden_prefixes = (
        ".claude/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".venv/",
        "workspace/",
    )
    forbidden_suffixes = (
        ".db",
        ".sqlite",
        ".swp",
        ".log",
    )

    # Cartographer survey outputs are intentionally versioned alongside the map
    allowed_in_data = {"data/PROJECT_MAP.md", "data/SWARM_SUMMARY.json"}
    offenders = []
    for path in _tracked_files(repo):
        if path in forbidden_exact:
            offenders.append(path)
        if path.startswith(forbidden_prefixes):
            offenders.append(path)
        if path.startswith("data/") and path != "data/.gitkeep" and path not in allowed_in_data:
            offenders.append(path)
        if path.endswith(forbidden_suffixes):
            offenders.append(path)

    assert sorted(set(offenders)) == []


def test_public_sample_config_is_safe_by_default():
    repo = Path(__file__).resolve().parents[1]
    config = json.loads((repo / "config.example.json").read_text())

    assert config.get("managed_projects") == []
    assert config.get("paused_projects") == []
    assert config.get("disable_remote_repo") is True
    assert "gitea_user" not in config
    assert "gitea_pass" not in config


def test_agent_ops_docs_are_not_at_repo_root():
    repo = Path(__file__).resolve().parents[1]

    assert not (repo / "SKILL.md").exists()
    assert not (repo / "SKILLS.md").exists()
    assert not (repo / "skills").exists()
    assert (repo / "docs" / "agent-ops" / "SKILLS.md").exists()
