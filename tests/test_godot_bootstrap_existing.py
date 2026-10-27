"""Bootstrapping an existing Godot game (2026-10-03).

The swarm's QA, harness_qa, GUT validation and playthrough-bot mechanisms
need StateServer + TestHarness autoloads, GUT and check_scripts.gd.  The old
bootstrap only ran for brand-new projects, would have renamed an existing
game and repointed its main scene, and copied the template's
test/project.godot into the game (Godot then skips that folder, so the GUT
tests in it never import).
"""

import subprocess

import pytest

import swarm.api_wizard as wiz
from swarm import db
from tests.test_api_authz import OWN, _make_app, _seed, _teardown

GAME = "old-game"

PROJECT_GODOT = """config_version=5

[application]
config/name="Old Game Deluxe"
run/main_scene="res://levels/title.tscn"
config/icon="res://art/icon.png"

[autoload]
Globals="*res://core/globals.gd"
StateServer="*res://autoload/state_server.gd"

[editor_plugins]
enabled=PackedStringArray("res://addons/dialogic/plugin.cfg")
"""

TITLE_TSCN = """[gd_scene load_steps=2 format=3]

[ext_resource type="Script" path="res://levels/title.gd" id="1_t"]

[node name="Title" type="Control"]
script = ExtResource("1_t")
"""


def _fake_gut(project_path):
    addon = project_path / "addons" / "gut"
    addon.mkdir(parents=True, exist_ok=True)
    for f in ("gut_cmdln.gd", "plugin.cfg", "gut_loader.gd"):
        (addon / f).write_text("# gut\n")
    return addon


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                               "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"}).stdout


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(wiz, "install_gut_into_project", _fake_gut)
    app, saved = _make_app(tmp_path)
    _seed()
    game = tmp_path / "workspace" / GAME
    (game / "levels").mkdir(parents=True)
    (game / "core").mkdir()
    (game / "project.godot").write_text(PROJECT_GODOT)
    (game / "levels" / "title.tscn").write_text(TITLE_TSCN)
    (game / "levels" / "title.gd").write_text("extends Control\n")
    (game / "core" / "globals.gd").write_text("extends Node\n")
    (game / "tests").mkdir()
    (game / "tests" / "test_title.gd").write_text("extends GutTest\n")
    (game / ".gitignore").write_text("*.log\n")
    _git(game, "init", "-q", "-b", "main")
    _git(game, "add", ".")
    _git(game, "commit", "-qm", "init")
    yield app.test_client(), game
    _teardown(saved)


def test_readiness_before_bootstrap(env):
    client, game = env
    r = client.get(f"/api/projects/{GAME}/godot-readiness").get_json()
    assert r["ready"] is False
    assert r["checks"]["test_harness"] is False and r["checks"]["gut"] is False
    assert any("get_game_state" in item for item in r["needs_work"])
    assert any("GAME_DESIGN.md missing" in item for item in r["needs_work"])


def test_bootstrap_installs_harness_without_touching_the_game(env):
    client, game = env
    (game / "notes.txt").write_text("operator scratch, uncommitted\n")
    r = client.post(f"/api/projects/{GAME}/bootstrap").get_json()
    assert r["before"]["ready"] is False and r["after"]["ready"] is True, r
    assert r["commit"]

    pg = (game / "project.godot").read_text()
    # the game's identity and its own setup are untouched
    assert 'config/name="Old Game Deluxe"' in pg
    assert 'run/main_scene="res://levels/title.tscn"' in pg
    assert 'config/icon="res://art/icon.png"' in pg
    assert 'Globals="*res://core/globals.gd"' in pg
    assert '"res://addons/dialogic/plugin.cfg"' in pg and '"res://addons/gut/plugin.cfg"' in pg
    # harness is in place
    assert 'TestHarness="*res://autoload/test_harness.gd"' in pg
    assert (game / "autoload" / "state_server.gd").exists()
    assert (game / "check_scripts.gd").exists()
    # no skeleton scenes/scripts, no stray nested project
    assert not (game / "scenes").exists() and not (game / "scripts").exists()
    assert not list((game / "tests").rglob("project.godot")) and not (game / "test").exists()
    assert (game / "tests" / "test_title.gd").read_text() == "extends GutTest\n"
    gi = (game / ".gitignore").read_text()
    assert "*.log" in gi and ".godot/" in gi and ".import/" in gi
    # only harness paths were committed
    committed = _git(game, "show", "--name-only", "--format=", "HEAD").split()
    assert "notes.txt" not in committed and "levels/title.gd" not in committed
    assert "project.godot" in committed and "autoload/test_harness.gd" in committed
    assert "?? notes.txt" in _git(game, "status", "--porcelain")
    # what bootstrap cannot do is reported, not forced
    assert any("implements get_game_state()" in i for i in r["after"]["needs_work"])


def test_bootstrap_is_idempotent(env):
    client, game = env
    client.post(f"/api/projects/{GAME}/bootstrap")
    pg = (game / "project.godot").read_text()
    r = client.post(f"/api/projects/{GAME}/bootstrap").get_json()
    assert r["commit"] is None
    assert (game / "project.godot").read_text() == pg
    assert pg.count("res://addons/gut/plugin.cfg") == 1


def test_readiness_sees_get_game_state_and_victory_condition(env):
    client, game = env
    client.post(f"/api/projects/{GAME}/bootstrap")
    (game / "levels" / "title.gd").write_text("extends Control\nfunc get_game_state() -> Dictionary:\n\treturn {}\n")
    (game / "GAME_DESIGN.md").write_text("# Old Game\n\n## Victory Condition\nstate.won == true\n")
    r = client.get(f"/api/projects/{GAME}/godot-readiness").get_json()
    assert r["ready"] is True and r["needs_work"] == []


def test_stray_template_test_project_is_removed(env):
    client, game = env
    (game / "tests" / "project.godot").write_text("; Test project.godot for the analytics_reporter autoload.\n")
    client.post(f"/api/projects/{GAME}/bootstrap")
    assert not (game / "tests" / "project.godot").exists()


def test_refuses_while_agents_run_on_the_project(env):
    client, game = env
    db.agent_upsert({"id": "ag-run", "project": GAME, "task_type": "feature", "status": "active"})
    r = client.post(f"/api/projects/{GAME}/bootstrap")
    assert r.status_code == 409
    assert "TestHarness" not in (game / "project.godot").read_text()


def test_not_a_godot_project_or_bad_name(env):
    client, _ = env
    assert client.post(f"/api/projects/{OWN}/bootstrap").status_code == 404
    assert client.post("/api/projects/..%2F..%2Fetc/bootstrap").status_code in (400, 404)


def test_agents_cannot_bootstrap(env):
    client, game = env
    r = client.post(f"/api/projects/{OWN}/bootstrap", headers={"X-Swarm-Agent-Token": "tok-own"})
    assert r.status_code == 403


def test_new_project_bootstrap_still_builds_the_skeleton(tmp_path, monkeypatch):
    monkeypatch.setattr(wiz, "install_gut_into_project", _fake_gut)
    proj = tmp_path / "fresh"
    proj.mkdir()
    wiz._bootstrap_godot_project_support("fresh", proj)
    pg = (proj / "project.godot").read_text()
    assert 'config/name="fresh"' in pg and 'run/main_scene="res://scenes/main.tscn"' in pg
    assert "func get_game_state" in (proj / "scripts" / "main.gd").read_text()
    assert not list((proj / "test").rglob("project.godot"))
    assert (proj / "test" / "unit").is_dir()
    r = wiz.godot_readiness(proj)
    assert r["checks"] == {k: True for k in r["checks"]}, r
    assert r["needs_work"] == ["GAME_DESIGN.md missing: QA, auto-replan and the playthrough bot read it"]


def test_victory_todo_placeholder_is_reported(env):
    client, game = env
    client.post(f"/api/projects/{GAME}/bootstrap")
    (game / "levels" / "title.gd").write_text("extends Control\nfunc get_game_state() -> Dictionary:\n\treturn {}\n")
    (game / "GAME_DESIGN.md").write_text(f"## Victory Condition\n\n{wiz.VICTORY_TODO_MARKER} fill me\n")
    r = client.get(f"/api/projects/{GAME}/godot-readiness").get_json()
    assert r["needs_work"] == ["GAME_DESIGN.md Victory Condition is still a TODO placeholder"]


def test_chat_created_godot_doc_has_victory_section(tmp_path):
    doc = wiz._render_project_design_doc("g", "A game.", [{"id": "g-t1", "description": "x"}], "godot")
    assert "## Victory Condition" in doc and wiz.VICTORY_TODO_MARKER in doc
    py = wiz._render_project_design_doc("p", "A tool.", [{"id": "p-t1", "description": "x"}], "python")
    assert "Victory Condition" not in py



# ---- live-game shapes (goblinchemy, raccoon-city on 2026-10-03) ------------

def test_extended_state_server_is_never_overwritten(env):
    client, game = env
    (game / "autoload").mkdir()
    custom = "extends Node\n# game-specific input types\nfunc _handle_key(): pass\n"
    (game / "autoload" / "state_server.gd").write_text(custom)
    (game / "check_scripts.gd").write_text("# customised checker\n")
    r = client.post(f"/api/projects/{GAME}/bootstrap").get_json()
    assert (game / "autoload" / "state_server.gd").read_text() == custom
    assert (game / "check_scripts.gd").read_text() == "# customised checker\n"
    assert (game / "autoload" / "test_harness.gd").exists()          # missing piece still added
    assert r["after"]["checks"]["state_server"] is True
    assert any("StateServer (autoload/state_server.gd) differs" in d for d in r["after"]["drift"])
    assert any("check_scripts.gd differs" in d for d in r["after"]["drift"])


def test_state_server_at_custom_path_stays_registered(env):
    client, game = env
    pg = (game / "project.godot").read_text().replace(
        'StateServer="*res://autoload/state_server.gd"', 'StateServer="*res://scripts/state_server.gd"')
    (game / "project.godot").write_text(pg)
    (game / "scripts").mkdir()
    (game / "scripts" / "state_server.gd").write_text("extends Node\n# raccoon-city style\n")
    r = client.post(f"/api/projects/{GAME}/bootstrap").get_json()
    out = (game / "project.godot").read_text()
    assert 'StateServer="*res://scripts/state_server.gd"' in out
    assert "autoload/state_server.gd" not in out
    assert not (game / "autoload" / "state_server.gd").exists()
    assert 'TestHarness="*res://autoload/test_harness.gd"' in out
    assert r["after"]["checks"]["state_server"] is True


def test_get_game_state_on_an_autoload_counts(env):
    client, game = env
    pg = (game / "project.godot").read_text().replace(
        'Globals="*res://core/globals.gd"', 'Globals="*res://core/globals.gd"\nGame="*res://core/game.gd"')
    (game / "project.godot").write_text(pg)
    (game / "core" / "game.gd").write_text("extends Node\nfunc get_game_state() -> Dictionary:\n\treturn {}\n")
    r = client.get(f"/api/projects/{GAME}/godot-readiness").get_json()
    assert not any("get_game_state" in i for i in r["needs_work"])


def test_get_game_state_on_another_scene_root_counts_but_not_a_child_node(env):
    client, game = env
    (game / "levels" / "play.gd").write_text("extends Node2D\nfunc get_game_state() -> Dictionary:\n\treturn {}\n")
    (game / "levels" / "hud.gd").write_text("extends Control\nfunc get_game_state() -> Dictionary:\n\treturn {}\n")
    (game / "levels" / "with_child.tscn").write_text(
        '[gd_scene format=3]\n\n[ext_resource type="Script" path="res://levels/hud.gd" id="1_h"]\n\n'
        '[node name="Root" type="Node"]\n\n[node name="Hud" type="Control" parent="."]\nscript = ExtResource("1_h")\n')
    r = client.get(f"/api/projects/{GAME}/godot-readiness").get_json()
    assert any("get_game_state" in i for i in r["needs_work"])   # child node only: not reachable
    (game / "levels" / "play.tscn").write_text(
        '[gd_scene format=3]\n\n[ext_resource type="Script" path="res://levels/play.gd" id="1_p"]\n\n'
        '[node name="Play" type="Node2D"]\nscript = ExtResource("1_p")\n')
    r = client.get(f"/api/projects/{GAME}/godot-readiness").get_json()
    assert not any("get_game_state" in i for i in r["needs_work"])


def test_incomplete_gut_install_is_completed(env, monkeypatch):
    client, game = env
    (game / "addons" / "gut").mkdir(parents=True)
    (game / "addons" / "gut" / "gut_cmdln.gd.uid").write_text("uid://x\n")   # raccoon-city: .uid stubs only
    r = client.post(f"/api/projects/{GAME}/bootstrap").get_json()
    assert r["before"]["checks"]["gut"] is False and r["after"]["checks"]["gut"] is True


def test_template_cfg_values_are_valid_godot_literals():
    # export_presets.cfg shipped `custom_template/debug=disabled` (bare word):
    # Godot's ConfigFile parser rejects it, so every new project had broken
    # export presets.
    import re
    from pathlib import Path
    literal = re.compile(r'^(?:"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null|[A-Z]\w*\(.*\)|\[.*\]|\{.*\})$')
    root = Path(__file__).parent.parent / "templates" / "godot"
    for cfg in [root / "export_presets.cfg", root / "test" / "project.godot"]:
        for n, line in enumerate(cfg.read_text().splitlines(), 1):
            if not line or line.startswith((";", "[")) or "=" not in line:
                continue
            value = line.split("=", 1)[1].strip()
            assert literal.match(value), f"{cfg.name}:{n}: {line!r}"


def test_new_mode_for_repo_without_project_godot(env, tmp_path):
    client, _ = env
    repo = tmp_path / "workspace" / "fresh-repo"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "prd.md").write_text("# PRD\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "docs")
    assert client.post("/api/projects/fresh-repo/bootstrap").status_code == 404   # not new: refused
    r = client.post("/api/projects/fresh-repo/bootstrap", json={"new": True}).get_json()
    assert r["after"]["checks"] == {k: True for k in r["after"]["checks"]}, r
    assert 'config/name="fresh-repo"' in (repo / "project.godot").read_text()
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").split()
    assert "scripts/main.gd" in committed and "scenes/main.tscn" in committed
    assert "docs/prd.md" not in committed
    # second time: it is no longer new
    assert client.post("/api/projects/fresh-repo/bootstrap", json={"new": True}).status_code == 409


def test_new_mode_needs_a_git_repo(env, tmp_path):
    client, _ = env
    (tmp_path / "workspace" / "no-git").mkdir()
    assert client.post("/api/projects/no-git/bootstrap", json={"new": True}).status_code == 404


def test_template_export_preset_has_keys_godot_requires():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "templates" / "godot" / "export_presets.cfg").read_text()
    preset = text.split("[preset.0.options]")[0]
    for key in ("export_filter=", "include_filter=", "exclude_filter="):
        assert key in preset, key


def test_template_placeholder_test_asserts_harness():
    from pathlib import Path
    text = (Path(__file__).parent.parent / "templates" / "godot" / "test" / "unit" / "test_placeholder.gd").read_text()
    assert "assert_" in text and "/root/StateServer" in text and "/root/TestHarness" in text
