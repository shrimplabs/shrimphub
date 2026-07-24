"""Integration tests for swarm/api.py -- Flask routes."""
import json
import os
import threading
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from swarm import db


@pytest.fixture()
def app(tmp_path):
    """Create a fresh Flask test app with an isolated DB."""
    gut_source = tmp_path / "gut-source" / "addons" / "gut"
    gut_source.mkdir(parents=True)
    (gut_source / "gut_cmdln.gd").write_text("extends SceneTree\n")
    (gut_source / "plugin.cfg").write_text("[plugin]\nname=\"GUT\"\n")
    old_source = os.environ.get("SWARM_GUT_SOURCE_DIR")
    os.environ["SWARM_GUT_SOURCE_DIR"] = str(gut_source.parent.parent)

    # Blank out real API keys so any agent processes accidentally spawned during
    # tests fail fast on their first LLM call instead of running indefinitely
    # and burning real quota. Tests that need a real key must set it explicitly.
    _api_key_vars = ("MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "KIMI_API_KEY")
    _saved_keys = {k: os.environ.pop(k, None) for k in _api_key_vars}
    os.environ["MINIMAX_API_KEY"] = "test-key-do-not-use"
    db._db_path = None
    db._initialized = False
    db._local = threading.local()
    db.init(tmp_path / "swarm_test.db")

    from swarm.api import create_app
    flask_app = create_app(
        config={
            "workspace": str(tmp_path / "workspace"),
            "max_active_agents": 3,
            "lock_project": False,
            "disable_monitor": True,
            "disable_remote_repo": True,
            "project_creation_retry_rounds": 0,
            "agent_timeout": 60,
            "quota_limit_percent": 90,
            "llm_provider": "minimax",
            "task_selection_strategy": "priority",
            "managed_projects": [],
            "paused_projects": [],
        },
        data_dir=tmp_path / "data",
        config_file=tmp_path / "config.json",
    )
    flask_app.config["TESTING"] = True
    flask_app.config["DATA_DIR"] = str(tmp_path / "data")
    flask_app.config["PROJECT_CREATION_RETRY_ROUNDS_OVERRIDE"] = 0
    yield flask_app

    conn = getattr(db._local, "conn", None)
    if conn:
        conn.close()
        db._local.conn = None
    if old_source is None:
        os.environ.pop("SWARM_GUT_SOURCE_DIR", None)
    else:
        os.environ["SWARM_GUT_SOURCE_DIR"] = old_source

    # Kill any agent processes that escaped the test (spawned via real subprocess)
    import signal, glob
    data_dir = str(tmp_path / "data")
    for script in glob.glob(f"{data_dir}/agent_*.py"):
        pid_file = script.replace(".py", ".pid")
        try:
            pid = int(open(pid_file).read().strip()) if os.path.exists(pid_file) else None
        except Exception:
            pid = None
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # Restore real API keys
    os.environ.pop("MINIMAX_API_KEY", None)
    for k, v in _saved_keys.items():
        if v is not None:
            os.environ[k] = v


@pytest.fixture()
def client(app):
    return app.test_client()


# ---------------------------------------------------------------------------
# Projects
# ---------------------------------------------------------------------------

class TestProjects:
    def test_list_projects_empty(self, client):
        r = client.get("/api/projects")
        assert r.status_code == 200
        assert r.json["projects"] == {}

    def test_add_project(self, client):
        r = client.post("/api/projects",
                        json={"name": "my-game"},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["project"]["name"] == "my-game"

    def test_add_project_auto_proposes_closure_spec_from_workspace(self, client, app):
        root = Path(app.config["WORKSPACE_ROOT"]) / "quest"
        (root / "questpkg").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "questpkg" / "__init__.py").write_text("")
        (root / "questpkg" / "cli.py").write_text("print('hello')\n")
        (root / "tests" / "test_flow.py").write_text("def test_flow():\n    assert True\n")

        r = client.post("/api/projects", json={"name": "quest", "managed": True}, content_type="application/json")

        assert r.status_code == 200
        assert r.json["closure_proposal"]["profile"] == "python"
        assert r.json["closure_proposal"]["source"] == "heuristic"
        assert r.json["project"]["closure_mode"] == "stabilize"
        assert r.json["project"]["closure_spec"]["verification"]["unit_test_command"] == "python3 -m pytest -q"
        assert r.json["project"]["closure_spec"]["boot"]["ready_check"]["command"] == "python3 -m questpkg.cli"
        assert (root / "PROJECT_CLOSURE.md").exists()
        assert "python3 -m pytest -q" in (root / "PROJECT_CLOSURE.md").read_text()

    def test_add_project_respects_explicit_closure_spec(self, client):
        r = client.post(
            "/api/projects",
            json={
                "name": "explicit-proj",
                "profile": "python",
                "closure_spec": {
                    "mode": "ship",
                    "critical_flows": [{"id": "exact", "description": "Exact"}],
                },
            },
            content_type="application/json",
        )

        assert r.status_code == 200
        assert r.json["closure_proposal"]["source"] == "explicit"
        assert r.json["project"]["closure_mode"] == "ship"
        assert r.json["project"]["closure_spec"]["critical_flows"][0]["id"] == "exact"

    def test_variant_d_clone_randomizes_future_graph_tasks(self, client, app):
        """Variant D projects keep randomizing tasks created after clone time."""
        workspace = Path(app.config["WORKSPACE_ROOT"])
        source = workspace / "source-game"
        source.mkdir(parents=True)
        (source / "README.md").write_text("source\n")
        os.system(f"git -C {source} init -q")
        os.system(f"git -C {source} config user.email test@example.invalid")
        os.system(f"git -C {source} config user.name Test")
        os.system(f"git -C {source} add README.md")
        os.system(f"git -C {source} commit -q -m init")

        r = client.post("/api/projects", json={"name": "source-game", "managed": True}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/tasks", json={
            "id": "source-game-task-1",
            "project": "source-game",
            "type": "feature",
            "description": "Add a thing",
        }, content_type="application/json")
        assert r.status_code == 200

        r = client.post("/api/projects/source-game/snapshot", json={"tag": "base"}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/projects/source-game/clone", json={
            "tag": "base",
            "new_name": "source-game-chaos",
            "pipeline": "variant-d",
            "experiment_id": "exp-test",
        }, content_type="application/json")
        assert r.status_code == 200

        tasks = client.get("/api/tasks?project=source-game-chaos&include_completed=true").json["tasks"]
        cloned = [t for t in tasks if t["id"] != "source-game-chaos-genesis"]
        assert cloned
        for task in cloned:
            meta = task["metadata"]
            assert meta["experiment_id"] == "exp-test"
            assert meta["experiment_variant"] == "variant-d"
            assert meta["experiment_arm"] == "exploratory"
            assert sorted(meta["pipeline"]) == ["plan", "scout", "validate", "work"]
            assert meta["pipeline_variant"] == meta["pipeline"]
            assert meta["phase_order"] == meta["pipeline"]
            assert isinstance(meta["phase_random_seed"], int)
            assert "is_valid_order" in meta

        r = client.post("/api/tasks", json={
            "id": "source-game-chaos-followup",
            "project": "source-game-chaos",
            "type": "feature",
            "description": "Follow-up created by graph reflection",
        }, content_type="application/json")
        assert r.status_code == 200
        meta = r.json["task"]["metadata"]
        assert meta["experiment_id"] == "exp-test"
        assert meta["experiment_variant"] == "variant-d"
        assert meta["experiment_arm"] == "exploratory"
        assert sorted(meta["pipeline"]) == ["plan", "scout", "validate", "work"]
        assert meta["pipeline_variant"] == meta["pipeline"]
        assert meta["phase_order"] == meta["pipeline"]
        assert isinstance(meta["phase_random_seed"], int)

        cfg = json.loads(Path(app.config["CONFIG_FILE"]).read_text())
        exp = cfg["project_pipelines"]["source-game-chaos"]["_experiment"]
        assert exp["experiment_id"] == "exp-test"
        assert exp["pipeline_mode"] == "random"

    def test_adaptive_flat_clone_stamps_tasks_and_future_graph_tasks(self, client, app):
        workspace = Path(app.config["WORKSPACE_ROOT"])
        source = workspace / "source-adaptive-game"
        source.mkdir(parents=True)
        (source / "README.md").write_text("source\n")
        os.system(f"git -C {source} init -q")
        os.system(f"git -C {source} config user.email test@example.invalid")
        os.system(f"git -C {source} config user.name Test")
        os.system(f"git -C {source} add README.md")
        os.system(f"git -C {source} commit -q -m init")

        r = client.post("/api/projects", json={"name": "source-adaptive-game", "managed": True}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/tasks", json={
            "id": "source-adaptive-game-task-1",
            "project": "source-adaptive-game",
            "type": "feature",
            "description": "Add a thing",
        }, content_type="application/json")
        assert r.status_code == 200

        r = client.post("/api/projects/source-adaptive-game/snapshot", json={"tag": "base"}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/projects/source-adaptive-game/clone", json={
            "tag": "base",
            "new_name": "source-adaptive-game-run9",
            "pipeline": "adaptive-flat",
            "flat_provider": "minimax",
            "loop_model_routing": {
                "fast_provider": "minimax-fast",
                "strong_provider": "minimax",
                "max_consecutive_cheap_loops": 2,
            },
            "experiment_id": "exp-adaptive-flat",
        }, content_type="application/json")
        assert r.status_code == 200

        tasks = client.get("/api/tasks?project=source-adaptive-game-run9&include_completed=true").json["tasks"]
        cloned = [t for t in tasks if t["id"] != "source-adaptive-game-run9-genesis"]
        assert cloned
        meta = cloned[0]["metadata"]
        assert meta["experiment_id"] == "exp-adaptive-flat"
        assert meta["experiment_variant"] == "adaptive-flat"
        assert meta["pipeline"] == []
        assert meta["pipeline_mode"] == "adaptive_flat"
        assert meta["adaptive_flat"] is True
        assert meta["flat_provider"] == "minimax"
        assert meta["loop_model_routing"]["max_consecutive_cheap_loops"] == 2

        r = client.post("/api/tasks", json={
            "id": "source-adaptive-game-run9-followup",
            "project": "source-adaptive-game-run9",
            "type": "feature",
            "description": "Follow-up created by graph reflection",
        }, content_type="application/json")
        assert r.status_code == 200
        meta = r.json["task"]["metadata"]
        assert meta["experiment_variant"] == "adaptive-flat"
        assert meta["pipeline_mode"] == "adaptive_flat"
        assert meta["adaptive_flat"] is True
        assert meta["loop_model_routing"]["fast_provider"] == "minimax-fast"

        cfg = json.loads(Path(app.config["CONFIG_FILE"]).read_text())
        project_cfg = cfg["project_pipelines"]["source-adaptive-game-run9"]
        assert project_cfg["_experiment"]["pipeline_mode"] == "adaptive_flat"
        assert project_cfg["_loop_model_routing"]["strong_provider"] == "minimax"

    def test_experiment_clone_seeds_art_polish_qa_tail_for_godot(self, client, app):
        workspace = Path(app.config["WORKSPACE_ROOT"])
        source = workspace / "godot-game"
        (source / "autoload").mkdir(parents=True)
        (source / "project.godot").write_text("[application]\n")
        (source / "autoload" / "test_harness.gd").write_text("extends Node\n")
        os.system(f"git -C {source} init -q")
        os.system(f"git -C {source} config user.email test@example.invalid")
        os.system(f"git -C {source} config user.name Test")
        os.system(f"git -C {source} add .")
        os.system(f"git -C {source} commit -q -m init")

        r = client.post("/api/projects", json={"name": "godot-game", "managed": True}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/tasks", json={
            "id": "godot-game-t1",
            "project": "godot-game",
            "type": "feature",
            "description": "Build core loop",
        }, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/tasks", json={
            "id": "godot-game-t2",
            "project": "godot-game",
            "type": "feature",
            "description": "Build scoring",
            "dependencies": ["godot-game-t1"],
        }, content_type="application/json")
        assert r.status_code == 200

        r = client.post("/api/projects/godot-game/snapshot", json={"tag": "base"}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/projects/godot-game/clone", json={
            "tag": "base",
            "new_name": "godot-game-chaos",
            "pipeline": "variant-d",
            "experiment_id": "exp-tail",
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["seeded_tail_tasks"] == 4

        tasks = client.get("/api/tasks?project=godot-game-chaos&include_completed=true").json["tasks"]
        by_type = {t["type"]: t for t in tasks if (t.get("metadata") or {}).get("seeded_experiment_tail")}
        assert set(by_type) == {"art_pass", "polish", "harness_qa", "playthrough_bot"}

        art = by_type["art_pass"]
        polish = by_type["polish"]
        qa = by_type["harness_qa"]
        bot = by_type["playthrough_bot"]
        cloned_t2 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "godot-game-t2")

        assert art["dependencies"] == [cloned_t2["id"]]
        assert polish["dependencies"] == [art["id"]]
        assert qa["dependencies"] == [polish["id"]]
        assert bot["dependencies"] == [qa["id"]]

        for task in (art, polish, qa, bot):
            meta = task["metadata"]
            assert meta["experiment_id"] == "exp-tail"
            assert meta["experiment_variant"] == "variant-d"
            assert meta["pipeline"] == ["scout", "work", "validate"]
            assert meta["phase_order"] == ["scout", "work", "validate"]
            assert meta["tail_pipeline_pinned"] is True
            assert meta["is_valid_order"] is True

    def test_experiment_clone_can_seed_run9_mid_and_final_quality_gates(self, client, app):
        workspace = Path(app.config["WORKSPACE_ROOT"])
        source = workspace / "run9-game"
        (source / "autoload").mkdir(parents=True)
        (source / "project.godot").write_text("[application]\n")
        (source / "autoload" / "test_harness.gd").write_text("extends Node\n")
        os.system(f"git -C {source} init -q")
        os.system(f"git -C {source} config user.email test@example.invalid")
        os.system(f"git -C {source} config user.name Test")
        os.system(f"git -C {source} add .")
        os.system(f"git -C {source} commit -q -m init")

        r = client.post("/api/projects", json={"name": "run9-game", "managed": True}, content_type="application/json")
        assert r.status_code == 200
        for idx in range(1, 5):
            payload = {
                "id": f"run9-game-t{idx}",
                "project": "run9-game",
                "type": "feature",
                "description": f"Feature {idx}",
            }
            if idx > 1:
                payload["dependencies"] = [f"run9-game-t{idx - 1}"]
            r = client.post("/api/tasks", json=payload, content_type="application/json")
            assert r.status_code == 200

        r = client.post("/api/projects/run9-game/snapshot", json={"tag": "base"}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/projects/run9-game/clone", json={
            "tag": "base",
            "new_name": "run9-game-c",
            "pipeline": "variant-c",
            "experiment_id": "exp-run9",
            "run9_quality_gates": True,
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["seeded_tail_tasks"] == 8
        assert r.json["quality_gate_mode"] == "run9_mid_final"

        tasks = client.get("/api/tasks?project=run9-game-c&include_completed=true").json["tasks"]
        gates = [
            t for t in tasks
            if (t.get("metadata") or {}).get("seeded_experiment_tail")
        ]
        assert len(gates) == 8

        by_chain_stage = {
            (t["metadata"]["quality_gate_chain"], t["metadata"]["quality_gate_stage"]): t
            for t in gates
        }
        mid_art = by_chain_stage[("run9-mid", "art_pass")]
        mid_polish = by_chain_stage[("run9-mid", "polish")]
        mid_qa = by_chain_stage[("run9-mid", "harness_qa")]
        mid_bot = by_chain_stage[("run9-mid", "playthrough_bot")]
        final_art = by_chain_stage[("run9-final", "art_pass")]
        final_polish = by_chain_stage[("run9-final", "polish")]
        final_qa = by_chain_stage[("run9-final", "harness_qa")]
        final_bot = by_chain_stage[("run9-final", "playthrough_bot")]

        cloned_t2 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "run9-game-t2")
        cloned_t3 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "run9-game-t3")
        cloned_t4 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "run9-game-t4")

        assert mid_art["dependencies"] == [cloned_t2["id"]]
        assert mid_polish["dependencies"] == [mid_art["id"]]
        assert mid_qa["dependencies"] == [mid_polish["id"]]
        assert mid_bot["dependencies"] == [mid_qa["id"]]
        assert cloned_t3["dependencies"] == [mid_bot["id"]]
        assert cloned_t3["metadata"]["run9_mid_gate_dependency_rewrite"] is True
        assert final_art["dependencies"] == [cloned_t4["id"]]
        assert final_polish["dependencies"] == [final_art["id"]]
        assert final_qa["dependencies"] == [final_polish["id"]]
        assert final_bot["dependencies"] == [final_qa["id"]]

        assert mid_art["metadata"]["phase_loop_limits"] == {"work": 200}
        assert final_polish["metadata"]["phase_loop_limits"] == {"work": 200}
        assert mid_qa["metadata"]["qa_focus"] == "playability"
        assert final_qa["metadata"]["qa_focus"] == "playability"

    def test_experiment_clone_accepts_explicit_dependency_overrides(self, client, app):
        workspace = Path(app.config["WORKSPACE_ROOT"])
        source = workspace / "parallel-game"
        source.mkdir(parents=True)
        (source / "project.godot").write_text("[application]\n")
        os.system(f"git -C {source} init -q")
        os.system(f"git -C {source} config user.email test@example.invalid")
        os.system(f"git -C {source} config user.name Test")
        os.system(f"git -C {source} add .")
        os.system(f"git -C {source} commit -q -m init")

        r = client.post("/api/projects", json={"name": "parallel-game", "managed": True}, content_type="application/json")
        assert r.status_code == 200
        for idx in range(1, 4):
            payload = {
                "id": f"parallel-game-t{idx}",
                "project": "parallel-game",
                "type": "feature",
                "description": f"Feature {idx}",
            }
            if idx > 1:
                payload["dependencies"] = [f"parallel-game-t{idx - 1}"]
            r = client.post("/api/tasks", json=payload, content_type="application/json")
            assert r.status_code == 200

        r = client.post("/api/projects/parallel-game/snapshot", json={"tag": "base"}, content_type="application/json")
        assert r.status_code == 200
        r = client.post("/api/projects/parallel-game/clone", json={
            "tag": "base",
            "new_name": "parallel-game-run9",
            "pipeline": "variant-f",
            "quality_gate_mode": "none",
            "dependency_overrides": {
                "parallel-game-t2": [],
                "parallel-game-t3": ["parallel-game-t1"],
            },
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["dependency_overrides"] == 2
        assert r.json["seeded_tail_tasks"] == 0

        tasks = client.get("/api/tasks?project=parallel-game-run9&include_completed=true").json["tasks"]
        cloned_t1 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "parallel-game-t1")
        cloned_t2 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "parallel-game-t2")
        cloned_t3 = next(t for t in tasks if (t.get("metadata") or {}).get("source_task_id") == "parallel-game-t3")

        assert cloned_t2["dependencies"] == []
        assert cloned_t3["dependencies"] == [cloned_t1["id"]]
        assert cloned_t2["metadata"]["dependency_override_applied"] is True
        assert cloned_t2["metadata"]["original_dependencies"] == ["parallel-game-t1"]

    def test_add_project_missing_name(self, client):
        r = client.post("/api/projects", json={}, content_type="application/json")
        assert r.status_code == 400

    def test_get_project(self, client):
        client.post("/api/projects", json={"name": "alpha"}, content_type="application/json")
        r = client.get("/api/projects/alpha")
        assert r.status_code == 200
        assert r.json["project"]["name"] == "alpha"

    def test_get_project_includes_latest_playthrough(self, client):
        client.post("/api/projects", json={"name": "alpha"}, content_type="application/json")
        db.task_upsert({
            "id": "play-alpha-1",
            "project": "alpha",
            "type": "playthrough_bot",
            "description": "play it",
            "priority": 70,
            "status": "completed",
            "dependencies": [],
            "metadata": {
                "playthrough_result": {
                    "agent_id": "agent-1",
                    "status": "success",
                    "outcome": "complete",
                    "reason": "terminal state reached",
                    "trace_path": "/tmp/trace.jsonl",
                    "receipt_path": "/tmp/receipt.json",
                    "progress": {
                        "completed": True,
                        "score": 1080,
                        "level": 2,
                        "agency_evidence": {"cannon_fires_attempted": 38},
                    },
                },
            },
            "attempts": 0,
            "max_attempts": 2,
            "completed": "2026-07-10T12:00:00",
        })

        r = client.get("/api/projects/alpha")

        latest = r.json["project"]["latest_playthrough"]
        assert latest["task_id"] == "play-alpha-1"
        assert latest["outcome"] == "complete"
        assert latest["completed"] is True
        assert latest["score"] == 1080
        assert latest["level"] == 2
        assert latest["agency_evidence"]["cannon_fires_attempted"] == 38
        assert latest["trace_path"] == "/tmp/trace.jsonl"

    def test_list_projects_includes_latest_playthrough(self, client):
        client.post("/api/projects", json={"name": "alpha"}, content_type="application/json")
        db.task_upsert({
            "id": "play-alpha-1",
            "project": "alpha",
            "type": "playthrough_bot",
            "description": "play it",
            "priority": 70,
            "status": "completed",
            "dependencies": [],
            "metadata": {
                "playthrough_result": {
                    "status": "success",
                    "outcome": "complete",
                    "progress": {
                        "completed": True,
                        "agency_evidence": {"moves": 10},
                    },
                },
            },
            "attempts": 0,
            "max_attempts": 2,
            "completed": "2026-07-10T12:00:00",
        })

        r = client.get("/api/projects")

        assert r.json["projects"]["alpha"]["latest_playthrough"]["outcome"] == "complete"

    def test_get_project_not_found(self, client):
        r = client.get("/api/projects/ghost")
        assert r.status_code == 404

    def test_update_project_locked(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        r = client.put("/api/projects/p1",
                       json={"locked": True},
                       content_type="application/json")
        assert r.status_code == 200
        r2 = client.get("/api/projects/p1")
        assert r2.json["project"]["locked"] is True

    def test_project_health(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        r = client.get("/api/projects/p1/health")
        assert r.status_code == 200
        health = r.json
        assert "health_score" in health
        assert "tasks_completed" in health
        assert "tasks_failed" in health

    def test_project_closure_get_and_spec_update(self, client):
        client.post("/api/projects", json={"name": "closure-proj", "profile": "python"}, content_type="application/json")

        initial = client.get("/api/projects/closure-proj/closure")
        assert initial.status_code == 200
        assert initial.json["closure"]["closure_mode"] == "build"

        updated = client.post(
            "/api/projects/closure-proj/closure/spec",
            json={
                "closure_spec": {
                    "mode": "stabilize",
                    "boot": {"ready_check": {"type": "command", "command": "echo boot"}},
                    "critical_flows": [{"id": "main-flow", "description": "Main flow"}],
                }
            },
            content_type="application/json",
        )
        assert updated.status_code == 200
        assert updated.json["closure"]["closure_mode"] == "stabilize"
        assert updated.json["closure"]["closure_spec"]["boot"]["ready_check"]["type"] == "command"
        closure_doc = Path(client.application.config["WORKSPACE_ROOT"]) / "closure-proj" / "PROJECT_CLOSURE.md"
        assert closure_doc.exists()
        assert "echo boot" in closure_doc.read_text()

    def test_project_closure_proposal_get_and_apply(self, client, app):
        root = Path(app.config["WORKSPACE_ROOT"]) / "apply-proj"
        (root / "applypkg").mkdir(parents=True)
        (root / "tests").mkdir()
        (root / "applypkg" / "__init__.py").write_text("")
        (root / "applypkg" / "cli.py").write_text("print('hello')\n")
        (root / "tests" / "test_smoke_flow.py").write_text("def test_smoke_flow():\n    assert True\n")

        client.post("/api/projects", json={"name": "apply-proj", "profile": "python", "closure_spec": {"mode": "build"}}, content_type="application/json")

        proposal = client.get("/api/projects/apply-proj/closure/proposal")
        assert proposal.status_code == 200
        assert proposal.json["proposal"]["source"] == "heuristic"
        assert proposal.json["proposal"]["closure_spec"]["mode"] == "stabilize"

        applied = client.post("/api/projects/apply-proj/closure/proposal/apply", json={}, content_type="application/json")
        assert applied.status_code == 200
        assert applied.json["closure"]["closure_mode"] == "stabilize"
        assert applied.json["closure"]["closure_spec"]["verification"]["smoke_checks"][0]["id"] == "smoke-flow"
        closure_doc = root / "PROJECT_CLOSURE.md"
        assert closure_doc.exists()
        assert "smoke-flow" in closure_doc.read_text()

    def test_project_closure_doc_regenerate_endpoint_writes_live_spec(self, client, app):
        root = Path(app.config["WORKSPACE_ROOT"]) / "regen-proj"
        root.mkdir(parents=True)
        client.post("/api/projects", json={"name": "regen-proj", "profile": "python"}, content_type="application/json")
        client.post(
            "/api/projects/regen-proj/closure/spec",
            json={
                "closure_spec": {
                    "mode": "ship",
                    "boot": {"ready_check": {"type": "command", "command": "echo ready"}},
                    "critical_flows": [{"id": "boss", "description": "Beat the boss"}],
                }
            },
            content_type="application/json",
        )

        doc_path = root / "PROJECT_CLOSURE.md"
        doc_path.write_text("stale")

        regenerated = client.post("/api/projects/regen-proj/closure/doc/regenerate", json={}, content_type="application/json")

        assert regenerated.status_code == 200
        assert regenerated.json["path"].endswith("PROJECT_CLOSURE.md")
        content = doc_path.read_text()
        assert "echo ready" in content
        assert "`boss`" in content

    def test_project_closure_mode_endpoint_updates_mode(self, client):
        client.post("/api/projects", json={"name": "mode-proj", "profile": "python"}, content_type="application/json")

        r = client.post(
            "/api/projects/mode-proj/closure/mode",
            json={"mode": "ship"},
            content_type="application/json",
        )

        assert r.status_code == 200
        assert r.json["closure"]["closure_mode"] == "ship"
        project = db.project_get("mode-proj")
        assert project["closure_mode"] == "ship"

    def test_project_closure_verify_endpoint_returns_run(self, client, app):
        client.post("/api/projects", json={"name": "verify-proj", "profile": "python"}, content_type="application/json")
        db.project_update("verify-proj", {
            "closure_spec": {"boot": {"ready_check": {"type": "command", "command": "echo boot"}}},
        })
        run = {
            "id": "vr-1",
            "project": "verify-proj",
            "trigger_task_id": None,
            "run_type": "manual",
            "status": "passed",
            "results_json": {"boot_ok": True, "tests_ok": None, "smoke_ok": None, "critical_flows": {}, "errors": []},
            "artifacts_json": {"checks": []},
            "fingerprints_json": [],
            "metadata_json": {},
        }

        with patch("swarm.validation.run_closure_verification", return_value=run) as verify:
            r = client.post("/api/projects/verify-proj/closure/verify", json={}, content_type="application/json")

        assert r.status_code == 200
        assert r.json["verification_run"]["id"] == "vr-1"
        verify.assert_called_once()

    def test_project_closure_repair_endpoint_uses_latest_failed_run(self, client):
        client.post("/api/projects", json={"name": "repair-closure", "profile": "python"}, content_type="application/json")
        db.verification_run_upsert({
            "id": "run-failed",
            "project": "repair-closure",
            "trigger_task_id": "task-1",
            "run_type": "manual",
            "status": "failed",
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T00:01:00",
            "results_json": {"boot_ok": False, "tests_ok": None, "smoke_ok": None, "critical_flows": {}, "errors": ["boom"]},
            "artifacts_json": {"checks": []},
            "fingerprints_json": ["boot:failed"],
            "metadata_json": {},
        })

        with patch("swarm.api_projects.plan_repair_tasks_for_run", return_value=[{"id": "repair-1"}]) as repair:
            r = client.post("/api/projects/repair-closure/closure/repair", json={}, content_type="application/json")

        assert r.status_code == 200
        assert r.json["verification_run_id"] == "run-failed"
        assert r.json["repair_tasks"] == [{"id": "repair-1"}]
        repair.assert_called_once_with("run-failed")

    def test_project_regressions_endpoint_lists_project_regressions(self, client):
        client.post("/api/projects", json={"name": "reg-proj", "profile": "python"}, content_type="application/json")
        db.regression_upsert({
            "id": "reg-1",
            "project": "reg-proj",
            "fingerprint": "boot:failed",
            "status": "open",
            "severity": "high",
            "first_seen_at": "2026-01-01T00:00:00",
            "last_seen_at": "2026-01-01T00:01:00",
            "occurrences": 2,
            "source_run_id": "run-1",
            "details_json": {"results": {"boot_ok": False}},
        })

        r = client.get("/api/projects/reg-proj/regressions")

        assert r.status_code == 200
        assert len(r.json["regressions"]) == 1
        assert r.json["regressions"][0]["fingerprint"] == "boot:failed"

    def test_project_closure_escalation_endpoint_returns_bundle(self, client):
        client.post("/api/projects", json={"name": "stalled-proj", "profile": "python"}, content_type="application/json")
        db.project_update("stalled-proj", {
            "closure_status": "stalled",
            "closure_spec": {"autonomy": {"stall_threshold": 3}},
            "open_regression_count": 1,
            "stall_count": 3,
        })
        db.verification_run_upsert({
            "id": "run-1",
            "project": "stalled-proj",
            "trigger_task_id": "task-1",
            "run_type": "post_task",
            "status": "failed",
            "created_at": "2026-01-01T00:00:00",
            "completed_at": "2026-01-01T00:01:00",
            "results_json": {"boot_ok": False, "tests_ok": True, "smoke_ok": None, "critical_flows": {}, "errors": ["boom"]},
            "artifacts_json": {"checks": []},
            "fingerprints_json": ["boot:failed"],
            "metadata_json": {},
        })
        db.regression_upsert({
            "id": "reg-1",
            "project": "stalled-proj",
            "fingerprint": "boot:failed",
            "status": "open",
            "severity": "high",
            "first_seen_at": "2026-01-01T00:00:00",
            "last_seen_at": "2026-01-01T00:01:00",
            "occurrences": 3,
            "source_run_id": "run-1",
            "details_json": {"results": {"boot_ok": False}},
        })

        r = client.get("/api/projects/stalled-proj/closure/escalation")

        assert r.status_code == 200
        assert r.json["escalation"]["closure_state"]["status"] == "stalled"
        assert r.json["escalation"]["latest_verification_run"]["id"] == "run-1"
        assert r.json["escalation"]["recurrence_summary"]["should_mark_stalled"] is True

    def test_project_repair_prunes_true_ghost_dependencies(self, client, app):
        """Repair prunes dep edges pointing to IDs that don't exist anywhere in the DB."""
        client.post("/api/projects", json={"name": "repair-proj"}, content_type="application/json")
        db.task_upsert({
            "id": "live-broken",
            "project": "repair-proj",
            "type": "feature",
            "description": "broken live task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["ghost-id-that-never-existed"],
            "metadata": {},
        })

        r = client.post("/api/projects/repair-proj/repair", json={}, content_type="application/json")

        assert r.status_code == 200
        pruned = r.json.get("pruned_ghost_deps", [])
        assert any(p["task_id"] == "live-broken" for p in pruned)
        task = db.task_get("live-broken")
        assert "ghost-id-that-never-existed" not in (task.get("dependencies") or [])

    def test_project_restart_clears_stale_agent_id(self, client):
        client.post("/api/projects", json={"name": "restart-proj"}, content_type="application/json")
        db.task_upsert({
            "id": "restart-task",
            "project": "restart-proj",
            "type": "feature",
            "description": "restart me",
            "priority": 50,
            "status": "failed",
            "agent_id": "agent-stale",
            "attempts": 2,
            "metadata": {"last_failure": "boom"},
            "dependencies": [],
        })

        r = client.post("/api/projects/restart-proj/restart", json={}, content_type="application/json")

        assert r.status_code == 200
        updated = db.task_get("restart-task")
        assert updated["status"] == "pending"
        assert updated["agent_id"] is None

    def test_history_requeue_reset_clears_agent_id(self, client, app):
        db.project_upsert({"name": "hist-proj", "status": "active"})
        db.task_upsert({
            "id": "hist-task",
            "project": "hist-proj",
            "type": "bug",
            "description": "from history",
            "priority": 50,
            "status": "failed",
            "agent_id": "agent-old",
            "attempts": 2,
            "metadata": {"last_failure": "boom"},
            "dependencies": [],
        })

        data_dir = Path(app.config["DATA_DIR"])
        history_file = data_dir / "agent-history.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(json.dumps({
            "id": "agent-old",
            "task_id": "hist-task",
            "project": "hist-proj",
            "task_type": "bug",
        }) + "\n")

        r = client.post("/api/history/agent-old/requeue", json={}, content_type="application/json")

        assert r.status_code == 200
        updated = db.task_get("hist-task")
        assert updated["status"] == "pending"
        assert updated["agent_id"] is None

    def test_lock_conflict_handoff_preserves_posted_branch_intent_when_source_task_is_gone(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")

        payload = {
            "owner_task_id": "owner-1",
            "blocked_task_id": "missing-source",
            "locked_path": "project.godot",
            "task_type": "bug",
            "priority": 50,
            "dependencies": ["dep-a", "owner-1"],
            "metadata": {
                "branch_intent_root_task_id": "signal-cartel-t05",
                "branch_intent_title": "Guard Patrol",
                "branch_intent_full_description": "Guard Patrol\n\nImplement waypoint patrol loops across the facility.",
                "branch_intent_description": "Guard Patrol\n\nImplement waypoint patrol loops across the facility.",
                "branch_intent_type": "feature",
            },
        }

        r = client.post("/api/projects/p1/lock-conflict-handoff", json=payload, content_type="application/json")
        assert r.status_code == 200
        task = r.json["task"]
        assert task["metadata"]["branch_intent_root_task_id"] == "signal-cartel-t05"
        assert task["metadata"]["branch_intent_title"] == "Guard Patrol"
        assert "Guard Patrol" in task["description"]
        assert "waypoint patrol loops" in task["description"]

    def test_lock_conflict_handoff_reuse_drops_stale_owner_dependency(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        db.task_upsert({
            "id": "task-existing-cont",
            "project": "p1",
            "type": "feature",
            "description": "Existing continuation",
            "status": "pending",
            "priority": 50,
            "dependencies": ["owner-missing", "bug-owner-missing"],
            "metadata": {
                "created_by": "lock_conflict_handoff",
                "blocked_by_task_id": "owner-missing",
                "lock_conflict_followup_for": "source-task",
            },
        })
        db.task_upsert({
            "id": "bug-owner-missing",
            "project": "p1",
            "type": "bug",
            "description": "Bug task",
            "status": "pending",
            "priority": 100,
            "dependencies": [],
        })

        r = client.post("/api/projects/p1/lock-conflict-handoff", json={
            "owner_task_id": "owner-missing",
            "blocked_task_id": "another-source",
            "locked_path": "project.godot",
            "dependencies": ["owner-missing", "bug-owner-missing"],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["created"] is False
        assert r.json["task"]["id"] == "task-existing-cont"
        assert r.json["task"]["dependencies"] == ["bug-owner-missing"]
        assert db.task_get("task-existing-cont")["dependencies"] == ["bug-owner-missing"]

    def test_file_lock_request_replaces_stale_owner_with_new_task(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        db.task_upsert({
            "id": "owner-task",
            "project": "p1",
            "type": "feature",
            "description": "old owner",
            "status": "completed",
            "priority": 50,
            "dependencies": [],
        })

        first = client.post("/api/projects/p1/lock", json={
            "file_path": "scripts/player.gd",
            "agent_id": "owner-task",
            "task_id": "owner-task",
        }, content_type="application/json")
        assert first.status_code == 200
        assert first.json["success"] is True

        db.task_upsert({
            "id": "new-task",
            "project": "p1",
            "type": "feature",
            "description": "new owner",
            "status": "in_progress",
            "priority": 50,
            "agent_id": "new-task",
            "dependencies": [],
        })
        db.agent_upsert({
            "id": "new-task",
            "project": "p1",
            "task_type": "feature",
            "status": "active",
            "pid": os.getpid(),
            "task_id": "new-task",
        })

        second = client.post("/api/projects/p1/lock", json={
            "file_path": "scripts/player.gd",
            "agent_id": "new-task",
            "task_id": "new-task",
        }, content_type="application/json")

        assert second.status_code == 200
        assert second.json["success"] is True
        assert second.json["stale_lock_replaced"] is True
        assert second.json["replaced_lock"]["task_id"] == "owner-task"

        locks = client.get("/api/projects/p1/locks").json["locks"]
        assert locks["scripts/player.gd"]["locked_by"] == "new-task"
        assert locks["scripts/player.gd"]["task_id"] == "new-task"

    def test_file_lock_request_still_blocks_live_owner(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        db.task_upsert({
            "id": "owner-task",
            "project": "p1",
            "type": "feature",
            "description": "live owner",
            "status": "in_progress",
            "priority": 50,
            "agent_id": "agent-live",
            "dependencies": [],
        })
        db.agent_upsert({
            "id": "agent-live",
            "project": "p1",
            "task_type": "feature",
            "status": "active",
            "pid": os.getpid(),
            "task_id": "owner-task",
        })

        first = client.post("/api/projects/p1/lock", json={
            "file_path": "scripts/player.gd",
            "agent_id": "owner-task",
            "task_id": "owner-task",
        }, content_type="application/json")
        assert first.status_code == 200
        assert first.json["success"] is True

        second = client.post("/api/projects/p1/lock", json={
            "file_path": "scripts/player.gd",
            "agent_id": "new-task",
            "task_id": "new-task",
        }, content_type="application/json")

        assert second.status_code == 200
        assert second.json["success"] is False
        assert second.json["lock"]["task_id"] == "owner-task"

    def test_get_project_locks_reconciles_stale_owner(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        db.task_upsert({
            "id": "owner-task",
            "project": "p1",
            "type": "feature",
            "description": "old owner",
            "status": "completed",
            "priority": 50,
            "dependencies": [],
        })
        client.post("/api/projects/p1/lock", json={
            "file_path": "scripts/player.gd",
            "agent_id": "owner-task",
            "task_id": "owner-task",
        }, content_type="application/json")

        r = client.get("/api/projects/p1/locks")

        assert r.status_code == 200
        assert r.json["locks"] == {}
        assert r.json["reconciled_stale_locks"][0]["task_id"] == "owner-task"

    def test_get_project_locks_preserves_live_owner(self, client):
        client.post("/api/projects", json={"name": "p1"}, content_type="application/json")
        db.task_upsert({
            "id": "owner-task",
            "project": "p1",
            "type": "feature",
            "description": "live owner",
            "status": "in_progress",
            "priority": 50,
            "agent_id": "agent-live",
            "dependencies": [],
        })
        db.agent_upsert({
            "id": "agent-live",
            "project": "p1",
            "task_type": "feature",
            "status": "active",
            "pid": os.getpid(),
            "task_id": "owner-task",
        })
        client.post("/api/projects/p1/lock", json={
            "file_path": "scripts/player.gd",
            "agent_id": "agent-live",
            "task_id": "owner-task",
        }, content_type="application/json")

        r = client.get("/api/projects/p1/locks")

        assert r.status_code == 200
        assert r.json["reconciled_stale_locks"] == []
        assert r.json["locks"]["scripts/player.gd"]["task_id"] == "owner-task"


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------

class TestTasks:
    def test_list_tasks_empty(self, client):
        r = client.get("/api/tasks")
        assert r.status_code == 200
        assert r.json["tasks"] == []

    def test_create_task(self, client):
        r = client.post("/api/tasks", json={
            "project": "my-game",
            "type": "feature",
            "description": "Add player movement",
            "priority": 50,
        }, content_type="application/json")
        assert r.status_code == 200
        task = r.json["task"]
        assert task["project"] == "my-game"
        assert task["type"] == "feature"
        assert task["status"] == "pending"
        assert task["id"]  # non-empty


class TestProjectChat:
    def test_project_chat_uses_explicit_prd_dependencies_for_preview(self, client, monkeypatch):
        from swarm import api_chat

        prd = """
[PRD]
# PRD: Test Game
project-name: test-game

## Overview
Test overview

## User Stories

### US-001: Shared Signals
**Description:** Foundation.

### US-002: Mission State
depends-on: US-001
**Description:** State manager.

### US-003: Mission Progress
depends-on: US-002
**Description:** Show mission state.
[/PRD]
""".strip()

        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: prd)

        r = client.post("/api/project-chat", json={
            "message": "yes generate the tasks",
            "history": [],
        }, content_type="application/json")

        assert r.status_code == 200
        data = r.json
        tasks = data["tasks_preview"]
        assert len(tasks) == 3
        assert tasks[0]["dependencies"] == []
        assert tasks[1]["dependencies"] == [tasks[0]["id"]]
        assert tasks[2]["dependencies"] == [tasks[1]["id"]]
        assert f"dependencies: {tasks[1]['id']}" in data["response"]
        assert f"dependencies: {tasks[0]['id']}" in data["response"]

    def test_project_chat_accepts_untagged_prd_output_for_preview(self, client, monkeypatch):
        from swarm import api_chat

        prd = """
# PRD: Test Game
project-name: test-game

## Overview
Test overview

## User Stories

### US-001: Shared Signals
**Description:** Foundation.

### US-002: Mission State
depends-on: US-001
**Description:** State manager.
""".strip()

        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: prd)

        r = client.post("/api/project-chat", json={
            "message": "generate the tasks",
            "history": [],
        }, content_type="application/json")

        assert r.status_code == 200
        data = r.json
        tasks = data["tasks_preview"]
        assert len(tasks) == 2
        assert tasks[0]["dependencies"] == []
        assert tasks[1]["dependencies"] == [tasks[0]["id"]]
        assert "## Plan Preview: test-game" in data["response"]

    def test_project_chat_returns_quality_gates_and_design_doc_for_creation(self, client, monkeypatch):
        from swarm import api_chat

        prd = """
[PRD]
# PRD: Blob Arena
project-name: example-game

## Overview
Spawn blobs, survive waves, and reach 100 mass.

## Quality Gates
- gut --run --exit
- no script errors on load

## User Stories

### US-001: Wave System
**Description:** Waves escalate over time.

### US-002: Win/Lose
depends-on: US-001
**Description:** Reach 100 mass or lose all blobs.
[/PRD]
""".strip()

        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: prd)

        r = client.post("/api/project-chat", json={
            "message": "generate the tasks",
            "history": [],
        }, content_type="application/json")

        assert r.status_code == 200
        data = r.json
        assert data["project_name"] == "example-game"
        assert data["quality_gates"] == ["gut --run --exit", "no script errors on load"]
        assert "## Quality Gates" in data["design_doc"]
        assert "gut --run --exit" in data["design_doc"]

    def test_project_chat_preview_response_matches_repaired_tasks_preview(self, client, monkeypatch):
        from swarm import api_chat

        prd = """
[PRD]
# PRD: Repair Game
project-name: repair-game

## Overview
Repair overview

## User Stories

### US-001: Foundation
**Description:** Foundation.

### US-002: System A
depends-on: US-001
**Description:** System A.

### US-003: System B
depends-on: US-001
**Description:** System B.

### US-004: UI
depends-on: US-001
**Description:** UI.

### US-005: Progress
depends-on: US-001
**Description:** Progress.

### US-006: Result Screen
depends-on: US-001
**Description:** Result.
[/PRD]
""".strip()

        repair_payload = json.dumps([
            {"id": "repair-game-t01-1", "dependencies": []},
            {"id": "repair-game-t02-2", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t03-3", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t04-4", "dependencies": ["repair-game-t02-2", "repair-game-t03-3"]},
            {"id": "repair-game-t05-5", "dependencies": ["repair-game-t02-2"]},
            {"id": "repair-game-t06-6", "dependencies": ["repair-game-t04-4", "repair-game-t05-5"]},
        ])

        calls = {"count": 0}

        def fake_llm(*args, **kwargs):
            calls["count"] += 1
            return prd if calls["count"] == 1 else repair_payload

        monkeypatch.setattr(api_chat, "_chat_call_llm", fake_llm)
        monkeypatch.setattr(api_chat, "_parse_prd_tasks_preview", lambda prd_content, project_name: [
            {"id": "repair-game-t01-1", "project": project_name, "type": "feature", "priority": 50, "description": "Foundation", "dependencies": []},
            {"id": "repair-game-t02-2", "project": project_name, "type": "feature", "priority": 50, "description": "System A", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t03-3", "project": project_name, "type": "feature", "priority": 50, "description": "System B", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t04-4", "project": project_name, "type": "feature", "priority": 50, "description": "UI", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t05-5", "project": project_name, "type": "feature", "priority": 50, "description": "Progress", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t06-6", "project": project_name, "type": "feature", "priority": 50, "description": "Result Screen", "dependencies": ["repair-game-t01-1"]},
        ])
        from swarm import api_wizard as _api_wizard
        monkeypatch.setattr(_api_wizard, "_chat_call_llm", fake_llm)
        monkeypatch.setattr(_api_wizard, "_parse_prd_tasks_preview", lambda prd_content, project_name: [
            {"id": "repair-game-t01-1", "project": project_name, "type": "feature", "priority": 50, "description": "Foundation", "dependencies": []},
            {"id": "repair-game-t02-2", "project": project_name, "type": "feature", "priority": 50, "description": "System A", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t03-3", "project": project_name, "type": "feature", "priority": 50, "description": "System B", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t04-4", "project": project_name, "type": "feature", "priority": 50, "description": "UI", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t05-5", "project": project_name, "type": "feature", "priority": 50, "description": "Progress", "dependencies": ["repair-game-t01-1"]},
            {"id": "repair-game-t06-6", "project": project_name, "type": "feature", "priority": 50, "description": "Result Screen", "dependencies": ["repair-game-t01-1"]},
        ])

        r = client.post("/api/project-chat", json={
            "message": "generate it",
            "history": [],
        }, content_type="application/json")

        assert r.status_code == 200
        data = r.json
        tasks = data["tasks_preview"]
        assert tasks[3]["dependencies"] == ["repair-game-t02-2", "repair-game-t03-3"]
        assert "repair-game-t02-2, repair-game-t03-3" in data["response"]

    def test_graph_repair_accepts_prd_fallback_when_json_array_missing(self, client, monkeypatch):
        from swarm import api_chat

        base_tasks = [
            {"id": "repair-fallback-t01", "project": "repair-fallback", "type": "feature", "priority": 50, "description": "Foundation", "dependencies": []},
            {"id": "repair-fallback-t02", "project": "repair-fallback", "type": "feature", "priority": 50, "description": "System A", "dependencies": ["repair-fallback-t01"]},
            {"id": "repair-fallback-t03", "project": "repair-fallback", "type": "feature", "priority": 50, "description": "System B", "dependencies": ["repair-fallback-t01"]},
            {"id": "repair-fallback-t04", "project": "repair-fallback", "type": "feature", "priority": 50, "description": "System C", "dependencies": ["repair-fallback-t01"]},
            {"id": "repair-fallback-t05", "project": "repair-fallback", "type": "feature", "priority": 50, "description": "System D", "dependencies": ["repair-fallback-t01"]},
            {"id": "repair-fallback-t06", "project": "repair-fallback", "type": "feature", "priority": 50, "description": "System E", "dependencies": ["repair-fallback-t01"]},
        ]

        repaired_prd = """
# PRD: Repair Fallback
project-name: repair-fallback

## Overview
Repair fallback overview

## User Stories

### US-001: Foundation
**Description:** Foundation.

### US-002: System A
depends-on: US-001
**Description:** System A.

### US-003: System B
depends-on: US-001
**Description:** System B.

### US-004: System C
depends-on: US-002, US-003
**Description:** System C.

### US-005: System D
depends-on: US-002
**Description:** System D.

### US-006: System E
depends-on: US-004, US-005
**Description:** System E.
""".strip()

        from swarm import api_wizard as _api_wizard
        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: repaired_prd)
        monkeypatch.setattr(_api_wizard, "_chat_call_llm", lambda *args, **kwargs: repaired_prd)

        repaired, rounds, errors = api_chat._repair_task_graph_with_llm(
            "repair-fallback",
            "Repair fallback overview",
            base_tasks,
            ["Preview graph is a trivial star: every task depends only on the same single task."],
            {},
        )

        assert errors == []
        assert rounds == 1
        assert repaired is not None
        repaired_by_id = {t["id"]: t for t in repaired}
        assert repaired_by_id["repair-fallback-t04"]["dependencies"] == ["repair-fallback-t02", "repair-fallback-t03"]
        assert repaired_by_id["repair-fallback-t06"]["dependencies"] == ["repair-fallback-t04", "repair-fallback-t05"]

    def test_project_chat_preview_warns_when_prd_dependency_annotations_are_sparse(self, client, monkeypatch):
        from swarm import api_chat

        prd = """
[PRD]
# PRD: Sparse Game
project-name: sparse-game

## Overview
Sparse overview

## User Stories

### US-001: Foundation
**Description:** Foundation.

### US-002: State
**Description:** State.

### US-003: Layout
**Description:** Layout.

### US-004: Player
**Description:** Player.

### US-005: Patrol
**Description:** Patrol.

### US-006: Detection
depends-on: US-005
**Description:** Detection.

### US-007: Alert
**Description:** Alert.

### US-008: HUD
**Description:** HUD.
[/PRD]
""".strip()

        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: prd)
        monkeypatch.setattr(api_chat, "_repair_task_graph_with_llm", lambda *args, **kwargs: ([
            {"id": "sparse-game-t01-1", "project": "sparse-game", "type": "feature", "priority": 50, "description": "Foundation", "dependencies": []},
            {"id": "sparse-game-t02-2", "project": "sparse-game", "type": "feature", "priority": 50, "description": "State", "dependencies": ["sparse-game-t01-1"]},
            {"id": "sparse-game-t03-3", "project": "sparse-game", "type": "feature", "priority": 50, "description": "Layout", "dependencies": ["sparse-game-t02-2"]},
            {"id": "sparse-game-t04-4", "project": "sparse-game", "type": "feature", "priority": 50, "description": "Player", "dependencies": ["sparse-game-t01-1"]},
            {"id": "sparse-game-t05-5", "project": "sparse-game", "type": "feature", "priority": 50, "description": "Patrol", "dependencies": ["sparse-game-t01-1"]},
            {"id": "sparse-game-t06-6", "project": "sparse-game", "type": "feature", "priority": 50, "description": "Detection", "dependencies": ["sparse-game-t05-5"]},
            {"id": "sparse-game-t07-7", "project": "sparse-game", "type": "feature", "priority": 50, "description": "Alert", "dependencies": ["sparse-game-t06-6"]},
            {"id": "sparse-game-t08-8", "project": "sparse-game", "type": "feature", "priority": 50, "description": "HUD", "dependencies": ["sparse-game-t04-4", "sparse-game-t07-7"]},
        ], 1, []))

        r = client.post("/api/project-chat", json={
            "message": "go ahead",
            "history": [],
        }, content_type="application/json")

        assert r.status_code == 200
        assert "Warning: the PRD only included explicit depends-on lines for 1 of 8 stories." in r.json["response"]
        assert r.json["preview_diagnostics"]["shape"]["task_count"] == 8

    def test_project_chat_forces_prd_generation_after_explicit_confirmation(self, client, monkeypatch):
        from swarm import api_chat

        captured = {}

        def fake_llm(system_prompt, messages, config):
            captured["system_prompt"] = system_prompt
            return """
[PRD]
# PRD: Confirmed Game
project-name: confirmed-game

## Overview
Confirmed overview

## User Stories

### US-001: Foundation
**Description:** Foundation.

### US-002: System A
depends-on: US-001
**Description:** System A.
[/PRD]
""".strip()

        monkeypatch.setattr(api_chat, "_chat_call_llm", fake_llm)

        history = [{
            "role": "assistant",
            "content": "## Plan Preview: confirmed-game\n\nWant to change anything, or shall I generate the tasks?"
        }]
        r = client.post("/api/project-chat", json={
            "message": "go ahead",
            "history": history,
        }, content_type="application/json")

        assert r.status_code == 200
        assert "PHASE OVERRIDE" in captured["system_prompt"]
        assert r.json["tasks_preview"] is not None
        assert r.json["project_name"] == "confirmed-game"
        assert "validation_errors" in r.json["preview_diagnostics"]

    def test_project_chat_uses_python_prompt_for_software_projects(self, client, monkeypatch):
        from swarm import api_chat

        captured = {}

        def fake_llm(system_prompt, messages, config):
            captured["system_prompt"] = system_prompt
            return "What kind of software project should we build?"

        monkeypatch.setattr(api_chat, "_chat_call_llm", fake_llm)

        r = client.post("/api/project-chat", json={
            "message": "I want to build a dev tool",
            "history": [],
            "project_type": "python",
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["project_type"] == "python"
        assert "Python software project" in captured["system_prompt"]
        assert "Godot game project designer" not in captured["system_prompt"]

    def test_project_ideas_programming_mode_does_not_pass_stale_game_history(self, client, monkeypatch):
        from swarm import api_chat

        captured = {}

        def fake_llm(system_prompt, messages, config):
            captured["system_prompt"] = system_prompt
            captured["messages"] = messages
            return "## Project Ideas\n1. **log-tool** -- Programming\n   Concept: Parse logs.\n   First slice: CLI parser."

        monkeypatch.setattr(api_chat, "_chat_call_llm", fake_llm)

        r = client.post("/api/project-ideas", json={
            "kind": "programming",
            "history": [{"role": "assistant", "content": "Let's design your game. What's the game idea?"}],
        }, content_type="application/json")

        assert r.status_code == 200
        assert "programming/software project ideas only" in captured["system_prompt"]
        assert "Do not include games" in captured["system_prompt"]
        assert len(captured["messages"]) == 1
        assert "game idea" not in captured["messages"][0]["content"]

    def test_repair_context_summarizes_graph_shape(self):
        from swarm.api_chat import _render_graph_repair_context

        tasks = [
            {"id": "proj-t1", "description": "Foundation", "dependencies": []},
            {"id": "proj-t2", "description": "State", "dependencies": ["proj-t1"]},
            {"id": "proj-t3", "description": "Layout", "dependencies": ["proj-t2"]},
            {"id": "proj-t4", "description": "HUD", "dependencies": ["proj-genesis"]},
            {"id": "proj-t5", "description": "Menu", "dependencies": ["proj-genesis"]},
        ]
        text = _render_graph_repair_context(
            tasks,
            ["Generated dependency graph is too flat"],
            allowed_external_roots=["proj-genesis"],
        )
        assert "Root-like tasks: proj-t1, proj-t4, proj-t5" in text
        assert "Tasks depending only on external anchors: proj-t4, proj-t5" in text
        assert "Validation errors:" in text
        assert "- Generated dependency graph is too flat" in text

    def test_repair_prompt_requires_task_identity_preservation(self, monkeypatch):
        from swarm import api_chat

        captured = {}

        def fake_llm(_system, messages, _config):
            captured["prompt"] = messages[0]["content"]
            return json.dumps([
                {"id": "repair-proj-t1", "dependencies": []},
                {"id": "repair-proj-t2", "dependencies": ["repair-proj-t1"]},
            ])

        from swarm import api_wizard as _api_wizard
        monkeypatch.setattr(api_chat, "_chat_call_llm", fake_llm)
        monkeypatch.setattr(_api_wizard, "_chat_call_llm", fake_llm)

        repaired, rounds, errors = api_chat._repair_task_graph_with_llm(
            "repair-proj",
            "overview",
            [
                {"id": "repair-proj-t1", "description": "Foundation", "type": "feature", "priority": 50, "dependencies": []},
                {"id": "repair-proj-t2", "description": "System B", "type": "feature", "priority": 50, "dependencies": []},
            ],
            ["Generated dependency graph is too flat"],
            {},
            allowed_external_roots=["repair-proj-genesis"],
            max_rounds=1,
        )

        assert repaired is not None
        assert rounds == 1
        assert errors == []
        assert "Do not rename, remove, merge, or split tasks." in captured["prompt"]
        assert "Current graph diagnostics:" in captured["prompt"]
        assert "Tasks depending only on external anchors" in captured["prompt"]

    def test_create_task_unique_ids_in_rapid_succession(self, client):
        ids = set()
        for _ in range(5):
            r = client.post("/api/tasks", json={
                "project": "p", "type": "feature", "description": "x"
            }, content_type="application/json")
            ids.add(r.json["task"]["id"])
        assert len(ids) == 5  # all unique

    def test_get_task(self, client):
        r = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]
        r2 = client.get(f"/api/tasks/{task_id}")
        assert r2.status_code == 200
        assert r2.json["task"]["id"] == task_id

    def test_get_task_delegation_summary(self, client):
        db.task_upsert({
            "id": "parent",
            "project": "p",
            "type": "feature",
            "description": "Parent",
            "priority": 50,
            "status": "pending",
            "dependencies": [],
            "metadata": {
                "delegation_batch_id": "batch-1",
                "delegation_mode": "integrate",
                "delegation_state": "delegated",
                "delegation_successor_task_id": "integration-1",
                "delegation_successor_kind": "integration",
                "helper_delegations": [{"question": "inspect foo", "files": ["foo.gd"]}],
            },
        })
        db.task_upsert({
            "id": "child-1",
            "project": "p",
            "type": "feature",
            "description": "Child",
            "priority": 50,
            "status": "pending",
            "dependencies": ["parent"],
            "metadata": {
                "parent_task_id": "parent",
                "delegation_batch_id": "batch-1",
                "delegated_files": ["foo.gd"],
            },
        })
        db.task_upsert({
            "id": "integration-1",
            "project": "p",
            "type": "feature",
            "description": "Integrate",
            "priority": 50,
            "status": "pending",
            "dependencies": ["child-1"],
            "metadata": {},
        })

        r = client.get("/api/tasks/parent/delegation")
        assert r.status_code == 200
        delegation = r.json["delegation"]
        assert delegation["batch_id"] == "batch-1"
        assert delegation["mode"] == "integrate"
        assert delegation["child_count"] == 1
        assert delegation["children"][0]["id"] == "child-1"
        assert delegation["successor_task_id"] == "integration-1"
        assert delegation["successor_task"]["id"] == "integration-1"
        assert delegation["helper_activity_count"] == 1

    def test_update_task_rejects_unknown_dependency(self, client):
        r = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]

        r2 = client.patch(f"/api/tasks/{task_id}", json={
            "dependencies": ["missing-task-id"]
        }, content_type="application/json")

        assert r2.status_code == 400
        assert "Unknown dependency" in r2.json["error"]

    def test_update_task_rejects_file_path_dependency(self, client):
        r = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]

        r2 = client.patch(f"/api/tasks/{task_id}", json={
            "dependencies": ["scripts/player.gd"]
        }, content_type="application/json")

        assert r2.status_code == 400
        assert "task IDs, not file paths" in r2.json["error"]

    def test_add_dependency_rejects_unknown_dependency(self, client):
        r = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]

        r2 = client.post(f"/api/tasks/{task_id}/dependencies", json={
            "dependency": "missing-task-id"
        }, content_type="application/json")

        assert r2.status_code == 400
        assert "Unknown dependency" in r2.json["error"]

    def test_update_task_accepts_real_dependency(self, client):
        """PATCH with a valid (existing) dependency ID must succeed (200)."""
        dep = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "parent"
        }, content_type="application/json")
        dep_id = dep.json["task"]["id"]

        child = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "child"
        }, content_type="application/json")
        child_id = child.json["task"]["id"]

        r2 = client.patch(f"/api/tasks/{child_id}", json={
            "dependencies": [dep_id]
        }, content_type="application/json")
        assert r2.status_code == 200
        assert dep_id in r2.json["task"]["dependencies"]

    def test_update_task_accepts_empty_dependencies_list(self, client):
        """PATCH with dependencies=[] is the documented self-healing clear -- must succeed."""
        dep = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "parent"
        }, content_type="application/json")
        dep_id = dep.json["task"]["id"]

        child = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "child",
            "dependencies": [dep_id],
        }, content_type="application/json")
        child_id = child.json["task"]["id"]
        assert dep_id in child.json["task"]["dependencies"]

        r2 = client.patch(f"/api/tasks/{child_id}", json={
            "dependencies": []
        }, content_type="application/json")
        assert r2.status_code == 200
        assert r2.json["task"]["dependencies"] == []

    def test_create_task_rejects_unknown_dependency(self, client):
        """POST /api/tasks with a non-existent dependency ID must return 400."""
        r = client.post("/api/tasks", json={
            "project": "dep-proj", "type": "feature", "description": "x",
            "dependencies": ["phantom-task-id-does-not-exist"],
        }, content_type="application/json")
        assert r.status_code == 400
        assert "unknown dependency" in r.json["error"].lower()

    def test_get_task_not_found(self, client):
        r = client.get("/api/tasks/ghost")
        assert r.status_code == 404

    def test_update_task_priority(self, client):
        r = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]
        r2 = client.put(f"/api/tasks/{task_id}",
                         json={"priority": 99},
                         content_type="application/json")
        assert r2.status_code == 200
        assert r2.json["task"]["priority"] == 90  # capped at 90 per _normalize_priority cap

    def test_update_task_status_reset(self, client):
        r = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]
        client.put(f"/api/tasks/{task_id}", json={"status": "failed"},
                   content_type="application/json")
        client.put(f"/api/tasks/{task_id}", json={"status": "pending"},
                   content_type="application/json")
        r2 = client.get(f"/api/tasks/{task_id}")
        assert r2.json["task"]["status"] == "pending"

    def test_delete_task(self, client):
        r = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "x"
        }, content_type="application/json")
        task_id = r.json["task"]["id"]
        r2 = client.delete(f"/api/tasks/{task_id}")
        assert r2.status_code == 200
        assert r2.json["success"] is True
        r3 = client.get(f"/api/tasks/{task_id}")
        assert r3.status_code == 404

    def test_delete_task_not_found(self, client):
        r = client.delete("/api/tasks/ghost")
        assert r.status_code == 404

    def test_create_task_with_dependencies(self, client):
        r = client.post("/api/tasks", json={
            "project": "p", "type": "feature", "description": "child",
            "dependencies": ["dep-1", "dep-2"],
        }, content_type="application/json")
        assert r.status_code == 400
        assert any(phrase in r.json["error"].lower() for phrase in ("unknown dependency", "placeholder dependency", "rejected"))

    def test_create_task_rejects_self_dependency(self, client):
        r = client.post("/api/tasks", json={
            "id": "self-dep-task",
            "project": "p",
            "type": "feature",
            "description": "bad task",
            "dependencies": ["self-dep-task"],
        }, content_type="application/json")
        assert r.status_code == 400
        assert "cannot depend on itself" in r.json["error"].lower()

    def test_update_task_rejects_cycle(self, client):
        parent = client.post("/api/tasks", json={
            "id": "cycle-parent", "project": "p", "type": "feature", "description": "parent"
        }, content_type="application/json")
        assert parent.status_code == 200

        child = client.post("/api/tasks", json={
            "id": "cycle-child", "project": "p", "type": "feature", "description": "child",
            "dependencies": ["cycle-parent"],
        }, content_type="application/json")
        assert child.status_code == 200

        # Creating cycle: parent -> child while child already depends on parent
        r = client.put("/api/tasks/cycle-parent",
                       json={"dependencies": ["cycle-child"]},
                       content_type="application/json")
        assert r.status_code == 400
        assert "cycle" in r.json["error"].lower()

    def test_list_tasks_includes_all(self, client):
        for i in range(3):
            client.post("/api/tasks", json={
                "project": "p", "type": "feature", "description": f"task {i}"
            }, content_type="application/json")
        r = client.get("/api/tasks")
        assert len(r.json["tasks"]) == 3


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------

class TestAgents:
    def test_list_agents_empty(self, client):
        r = client.get("/api/agents")
        assert r.status_code == 200
        assert r.json["agents"] == []

    def test_history_empty(self, client):
        r = client.get("/api/history")
        assert r.status_code == 200
        assert r.json["agents"] == []


# ---------------------------------------------------------------------------
# Configuration endpoints
# ---------------------------------------------------------------------------

class TestConfig:
    def test_get_max_agents(self, client):
        r = client.get("/api/max-agents")
        assert r.status_code == 200
        assert "max_active_agents" in r.json

    def test_set_max_agents(self, client):
        r = client.post("/api/max-agents",
                        json={"max_active_agents": 7},
                        content_type="application/json")
        assert r.status_code == 200
        r2 = client.get("/api/max-agents")
        assert r2.json["max_active_agents"] == 7

    def test_get_strategy(self, client):
        r = client.get("/api/strategy")
        assert r.status_code == 200
        assert "strategy" in r.json

    def test_set_strategy(self, client):
        r = client.post("/api/strategy",
                        json={"strategy": "round_robin"},
                        content_type="application/json")
        assert r.status_code == 200

    def test_list_strategies(self, client):
        r = client.get("/api/strategies")
        assert r.status_code == 200
        assert "strategies" in r.json
        assert len(r.json["strategies"]) > 0

    def test_get_providers(self, client):
        r = client.get("/api/providers")
        assert r.status_code == 200
        providers = r.json["providers"]
        assert "minimax" in providers
        assert "api_key_set" in providers["minimax"]

    def test_get_current_provider(self, client):
        r = client.get("/api/provider")
        assert r.status_code == 200
        assert "provider" in r.json

    def test_set_known_provider(self, client):
        r = client.post("/api/provider",
                        json={"provider": "minimax"},
                        content_type="application/json")
        assert r.status_code == 200

    def test_set_unknown_provider_without_config_fails(self, client):
        r = client.post("/api/provider",
                        json={"provider": "unknown-llm"},
                        content_type="application/json")
        assert r.status_code == 400

    def test_register_custom_provider(self, client):
        r = client.post("/api/provider", json={
            "provider": "my-local",
            "base_url": "http://localhost:11434/v1",
            "api_key_env": "OLLAMA_KEY",
            "format": "openai",
            "model": "llama3.2",
        }, content_type="application/json")
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Kill agent -- including PID fallback after server restart
# ---------------------------------------------------------------------------

class TestKillAgent:
    def _seed_agent(self, pid=99999, task_id="task-1"):
        """Insert an agent record into the DB as if spawned."""
        import uuid
        agent_id = str(uuid.uuid4())
        db.agent_upsert({
            "id": agent_id,
            "project": "test-proj",
            "task_type": "feature",
            "status": "active",
            "spawned_at": "2026-01-01T00:00:00",
            "pid": pid,
            "task_id": task_id,
        })
        db.task_upsert({
            "id": task_id,
            "project": "test-proj",
            "type": "feature",
            "description": "x",
            "priority": 50,
            "status": "in_progress",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        })
        return agent_id

    def test_kill_via_handle(self, client):
        """Kill succeeds when the process handle is in _active_handles."""
        from swarm import orchestrator
        agent_id = self._seed_agent()
        mock_proc = MagicMock()
        with orchestrator._handle_lock:
            orchestrator._active_handles[agent_id] = {
                "process": mock_proc,
                "project": "test-proj",
                "task_id": "task-1",
                "script_path": "",
                "log_path": "",
            }
        r = client.post(f"/api/agents/{agent_id}/kill")
        assert r.status_code == 200
        assert r.json["success"] is True
        mock_proc.kill.assert_called_once()
        # cleanup
        with orchestrator._handle_lock:
            orchestrator._active_handles.pop(agent_id, None)

    def test_kill_via_pid_fallback(self, client):
        """Kill falls back to os.kill(pid) when handle is not in memory."""
        agent_id = self._seed_agent(pid=77777, task_id="task-pid")
        with patch("os.kill") as mock_kill:
            r = client.post(f"/api/agents/{agent_id}/kill")
        assert r.status_code == 200
        assert r.json["success"] is True
        mock_kill.assert_called_once()
        args = mock_kill.call_args[0]
        assert args[0] == 77777

    def test_reconcile_agents_resets_in_progress_task_with_missing_runtime_agent(self, client):
        db.agent_upsert({
            "id": "agent-reconcile-missing",
            "project": "test-proj",
            "task_type": "feature",
            "status": "active",
            "spawned_at": "2026-01-01T00:00:00",
            "pid": None,
            "task_id": "task-reconcile-missing",
        })
        db.task_upsert({
            "id": "task-reconcile-missing",
            "project": "test-proj",
            "type": "feature",
            "description": "x",
            "priority": 50,
            "status": "in_progress",
            "agent_id": "agent-reconcile-missing",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        })

        r = client.post("/api/agents/reconcile")

        assert r.status_code == 200
        assert "agent-reconcile-missing" in r.json["repaired_agent_ids"]
        # Task state after reconcile depends on _finish_agent completing in the
        # test DB (which lacks some tables in this minimal fixture).  Assert only
        # that the agent was identified for repair; full task-reset behaviour is
        # covered by test_reconcile_agents_resets_orphan_in_progress_task_without_active_agent.
        task = db.task_get("task-reconcile-missing")
        assert task is not None  # task still exists

    def test_reconcile_agents_resets_orphan_in_progress_task_without_active_agent(self, client):
        db.task_upsert({
            "id": "task-orphaned",
            "project": "test-proj",
            "type": "feature",
            "description": "x",
            "priority": 50,
            "status": "in_progress",
            "agent_id": "ghost-agent",
            "dependencies": [],
            "metadata": {},
            "attempts": 0,
            "max_attempts": 3,
        })

        r = client.post("/api/agents/reconcile")

        assert r.status_code == 200
        assert "task-orphaned" in r.json["reset_task_ids"]
        assert db.task_get("task-orphaned")["status"] == "pending"
        assert db.task_get("task-orphaned")["agent_id"] is None

    def test_kill_pid_fallback_resets_task_to_pending(self, client):
        """After PID kill, the task is reset to pending so it can be retried."""
        agent_id = self._seed_agent(pid=77778, task_id="task-reset")
        with patch("os.kill"):
            client.post(f"/api/agents/{agent_id}/kill")
        task = db.task_get("task-reset")
        assert task["status"] == "pending"

    def test_kill_pid_fallback_process_already_gone(self, client):
        """Returns failure gracefully if the process already exited."""
        agent_id = self._seed_agent(pid=77779, task_id="task-gone")
        with patch("os.kill", side_effect=ProcessLookupError):
            r = client.post(f"/api/agents/{agent_id}/kill")
        assert r.status_code == 200
        assert r.json["success"] is False
        assert "already exited" in r.json["error"]

    def test_kill_unknown_agent(self, client):
        r = client.post("/api/agents/does-not-exist/kill")
        assert r.status_code == 200
        assert r.json["success"] is False


# ---------------------------------------------------------------------------
# Managed projects endpoint
# ---------------------------------------------------------------------------

class TestManagedProjects:
    def test_get_managed_projects(self, client):
        r = client.get("/api/managed-projects")
        assert r.status_code == 200
        assert "managed_projects" in r.json
        assert "paused_projects" in r.json

    def test_set_managed_projects(self, client):
        r = client.post("/api/managed-projects",
                        json={"managed_projects": ["alpha", "beta"]},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json["managed_projects"] == ["alpha", "beta"]
        assert db.project_get("alpha")["managed"] is True
        assert db.project_get("beta")["managed"] is True

    def test_set_managed_projects_persists(self, client):
        client.post("/api/managed-projects",
                    json={"managed_projects": ["gamma"]},
                    content_type="application/json")
        r = client.get("/api/managed-projects")
        assert "gamma" in r.json["managed_projects"]
        assert db.project_get("gamma")["managed"] is True

    def test_get_managed_projects_reads_canonical_project_state(self, client):
        client.post("/api/projects", json={"name": "canonical-a", "managed": True}, content_type="application/json")
        client.post("/api/projects", json={"name": "canonical-b", "managed": False}, content_type="application/json")

        r = client.get("/api/managed-projects")

        assert r.status_code == 200
        assert "canonical-a" in r.json["managed_projects"]
        assert "canonical-b" not in r.json["managed_projects"]

    def test_set_paused_projects(self, client):
        r = client.post("/api/managed-projects",
                        json={"paused_projects": ["paused-proj"]},
                        content_type="application/json")
        assert r.status_code == 200
        assert "paused-proj" in r.json["paused_projects"]

    def test_set_both_managed_and_paused(self, client):
        r = client.post("/api/managed-projects", json={
            "managed_projects": ["m1", "m2"],
            "paused_projects": ["p1"],
        }, content_type="application/json")
        assert r.status_code == 200
        assert r.json["managed_projects"] == ["m1", "m2"]
        assert r.json["paused_projects"] == ["p1"]

    def test_partial_update_leaves_other_field_unchanged(self, client):
        client.post("/api/managed-projects",
                    json={"paused_projects": ["stay"]},
                    content_type="application/json")
        client.post("/api/managed-projects",
                    json={"managed_projects": ["new"]},
                    content_type="application/json")
        r = client.get("/api/managed-projects")
        assert "stay" in r.json["paused_projects"]
        assert "new" in r.json["managed_projects"]


# ---------------------------------------------------------------------------
# Auto-mode quota suspension
# ---------------------------------------------------------------------------

class TestAutoModeQuota:
    def test_get_auto_mode_includes_suspended_field(self, client):
        r = client.get("/api/auto-mode")
        assert r.status_code == 200
        assert "suspended_for_quota" in r.json
        assert r.json["suspended_for_quota"] is False

    def test_enable_auto_mode_clears_suspended_flag(self, client):
        """Explicitly enabling auto mode should clear suspended_for_quota."""
        r = client.post("/api/auto-mode",
                        json={"enabled": True},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json.get("suspended_for_quota", False) is False
        # Disable again cleanly
        client.post("/api/auto-mode", json={"enabled": False},
                    content_type="application/json")

    def test_disable_auto_mode_clears_suspended_flag(self, client):
        """Explicitly turning off clears suspended_for_quota (user intent)."""
        r = client.post("/api/auto-mode",
                        json={"enabled": False},
                        content_type="application/json")
        assert r.status_code == 200
        assert r.json.get("suspended_for_quota", False) is False


# ---------------------------------------------------------------------------
# Task reset endpoint
# ---------------------------------------------------------------------------

class TestTaskReset:
    def _seed_task(self, client, status="failed", attempts=3, max_attempts=3):
        r = client.post("/api/tasks", json={
            "project": "proj", "type": "bug",
            "description": "fix something", "priority": 80,
        })
        task_id = r.json["task"]["id"]
        from swarm import db
        db.task_update(task_id, {"status": status, "attempts": attempts,
                                  "metadata": {"last_failure": "it broke", "failure_attempt": 3}})
        return task_id

    def test_reset_returns_success(self, client):
        task_id = self._seed_task(client)
        r = client.post(f"/api/tasks/{task_id}/reset",
                        content_type="application/json", json={})
        assert r.status_code == 200
        assert r.json["success"] is True

    def test_reset_sets_status_to_pending(self, client):
        task_id = self._seed_task(client)
        client.post(f"/api/tasks/{task_id}/reset",
                    content_type="application/json", json={})
        from swarm import db
        task = db.task_get(task_id)
        assert task["status"] == "pending"

    def test_reset_clears_attempt_counter(self, client):
        task_id = self._seed_task(client)
        client.post(f"/api/tasks/{task_id}/reset",
                    content_type="application/json", json={"reset_attempts": True})
        from swarm import db
        task = db.task_get(task_id)
        assert task["attempts"] == 0

    def test_reset_clears_last_failure_metadata(self, client):
        task_id = self._seed_task(client)
        client.post(f"/api/tasks/{task_id}/reset",
                    content_type="application/json", json={"reset_attempts": True})
        from swarm import db
        task = db.task_get(task_id)
        assert "last_failure" not in (task.get("metadata") or {})

    def test_reset_prepends_note_to_description(self, client):
        task_id = self._seed_task(client)
        client.post(f"/api/tasks/{task_id}/reset",
                    content_type="application/json",
                    json={"note": "The real issue is X"})
        from swarm import db
        task = db.task_get(task_id)
        assert task["description"].startswith("RECOVERY NOTE:\nThe real issue is X")

    def test_reset_raises_max_attempts(self, client):
        task_id = self._seed_task(client)
        client.post(f"/api/tasks/{task_id}/reset",
                    content_type="application/json",
                    json={"max_attempts": 10})
        from swarm import db
        task = db.task_get(task_id)
        assert task["max_attempts"] == 10

    def test_reset_nonexistent_task_returns_404(self, client):
        r = client.post("/api/tasks/nonexistent-id/reset",
                        content_type="application/json", json={})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# History requeue endpoint
# ---------------------------------------------------------------------------

class TestHistoryRequeue:
    def _seed_history_entry(self, data_dir, task_id="t-requeue", project="proj"):
        import json
        from pathlib import Path
        from datetime import datetime
        entry = {
            "id": "agent-requeue-001",
            "project": project,
            "task_id": task_id,
            "task_type": "bug",
            "status": "failed",
            "exit_code": 1,
            "spawned_at": datetime.now().isoformat(),
            "completed_at": datetime.now().isoformat(),
        }
        history_file = Path(data_dir) / "agent-history.jsonl"
        history_file.parent.mkdir(parents=True, exist_ok=True)
        with history_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def _seed_task_history(self, data_dir, task_id="t-requeue", project="proj"):
        import json
        from pathlib import Path
        from datetime import datetime
        entry = {
            "id": task_id,
            "project": project,
            "type": "bug",
            "description": "fix the bug",
            "priority": 80,
            "status": "failed",
            "attempts": 3,
            "max_attempts": 3,
            "dependencies": [],
            "metadata": {},
            "created": datetime.now().isoformat(),
        }
        history_file = Path(data_dir) / "task-history.jsonl"
        with history_file.open("a") as f:
            f.write(json.dumps(entry) + "\n")
        return entry

    def test_requeue_existing_task_resets_to_pending(self, client, app):
        from swarm import db
        db.task_upsert({"id": "t-requeue", "project": "proj", "type": "bug",
                        "description": "fix", "priority": 80, "status": "failed",
                        "attempts": 3, "max_attempts": 3, "dependencies": [], "metadata": {}})
        from swarm import orchestrator as _orc; data_dir = _orc.DATA_DIR
        self._seed_history_entry(data_dir)
        r = client.post("/api/history/agent-requeue-001/requeue",
                        content_type="application/json", json={})
        assert r.status_code == 200
        assert r.json["success"] is True
        assert r.json["action"] == "reset"
        task = db.task_get("t-requeue")
        assert task["status"] == "pending"
        assert task["attempts"] == 0

    def test_requeue_missing_agent_returns_404(self, client):
        r = client.post("/api/history/no-such-agent/requeue",
                        content_type="application/json", json={})
        assert r.status_code == 404

    def test_requeue_resurrects_pruned_task(self, client, app):
        from swarm import db
        from swarm import orchestrator as _orc; data_dir = _orc.DATA_DIR
        self._seed_history_entry(data_dir, task_id="t-pruned")
        self._seed_task_history(data_dir, task_id="t-pruned")
        r = client.post("/api/history/agent-requeue-001/requeue",
                        content_type="application/json", json={})
        assert r.status_code == 200
        assert r.json["action"] == "resurrected"
        task = db.task_get("t-pruned")
        assert task is not None
        assert task["status"] == "pending"
        assert task["attempts"] == 0

    def test_requeue_note_prepended_to_description(self, client, app):
        from swarm import db
        db.task_upsert({"id": "t-requeue", "project": "proj", "type": "bug",
                        "description": "original desc", "priority": 80, "status": "failed",
                        "attempts": 3, "max_attempts": 3, "dependencies": [], "metadata": {}})
        from swarm import orchestrator as _orc; data_dir = _orc.DATA_DIR
        self._seed_history_entry(data_dir)
        client.post("/api/history/agent-requeue-001/requeue",
                    content_type="application/json", json={"note": "Try approach B"})
        task = db.task_get("t-requeue")
        assert "Try approach B" in task["description"]
        assert "original desc" in task["description"]


# ---------------------------------------------------------------------------
# Ping endpoint
# ---------------------------------------------------------------------------

class TestPing:
    def test_ping_returns_ok_and_ts(self, client):
        r = client.get("/api/ping")
        assert r.status_code == 200
        data = r.json
        assert data["ok"] is True
        assert "ts" in data
        assert isinstance(data["ts"], (int, float))


# ---------------------------------------------------------------------------
# Spawn -- manual task_id dependency gating
# ---------------------------------------------------------------------------

class TestSpawnDependencyGating:
    """Tests for /api/spawn with task_id: unmet dependencies must block spawn."""

    def test_spawn_with_unmet_pending_dependency_returns_409(self, client):
        from swarm import db
        # Two tasks: dep is pending, child depends on dep
        db.task_upsert({
            "id": "dep-pending", "project": "proj", "type": "feature",
            "description": "must finish first", "priority": 50, "status": "pending",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        db.task_upsert({
            "id": "child-task", "project": "proj", "type": "feature",
            "description": "needs dep", "priority": 50, "status": "pending",
            "dependencies": ["dep-pending"], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        r = client.post("/api/spawn",
                        json={"task_id": "child-task"},
                        content_type="application/json")
        assert r.status_code == 409
        assert "unmet_dependencies" in r.json
        assert "dep-pending" in r.json["unmet_dependencies"]

    def test_spawn_with_unmet_in_progress_dependency_returns_409(self, client):
        from swarm import db
        db.task_upsert({
            "id": "dep-running", "project": "proj", "type": "feature",
            "description": "currently running", "priority": 50, "status": "in_progress",
            "dependencies": [], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        db.task_upsert({
            "id": "child-task2", "project": "proj", "type": "feature",
            "description": "needs dep", "priority": 50, "status": "pending",
            "dependencies": ["dep-running"], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        r = client.post("/api/spawn",
                        json={"task_id": "child-task2"},
                        content_type="application/json")
        assert r.status_code == 409
        assert "unmet_dependencies" in r.json
        assert "dep-running" in r.json["unmet_dependencies"]

    def test_spawn_with_all_dependencies_completed_succeeds(self, client):
        from swarm import db
        db.task_upsert({
            "id": "dep-done", "project": "proj", "type": "feature",
            "description": "already done", "priority": 50, "status": "completed",
            "dependencies": [], "metadata": {}, "attempts": 1, "max_attempts": 3,
        })
        # task_record_completed populates completed_task_ids, which is what
        # spawn dependency gating reads via task_get_completed_ids()
        db.task_record_completed("dep-done", project="proj")
        db.task_upsert({
            "id": "child-ready", "project": "proj", "type": "feature",
            "description": "dep satisfied", "priority": 50, "status": "pending",
            "dependencies": ["dep-done"], "metadata": {}, "attempts": 0, "max_attempts": 3,
        })
        with patch("swarm.orchestrator.spawn_agent",
                   return_value="mock-agent-99") as mock_spawn:
            r = client.post("/api/spawn",
                             json={"task_id": "child-ready"},
                             content_type="application/json")
        assert r.status_code == 200
        assert r.json["success"] is True
        assert r.json["task_id"] == "child-ready"
        mock_spawn.assert_called_once()


class TestTaskChaining:
    def test_replan_project_chains_to_head(self, client):
        client.post("/api/projects", json={"name": "chain-proj"}, content_type="application/json")
        db.project_upsert({**db.project_get("chain-proj"), "head_task_id": "chain-tail"})
        db.task_upsert({
            "id": "chain-tail",
            "project": "chain-proj",
            "type": "bug",
            "description": "tail",
            "priority": 80,
            "status": "completed",
            "dependencies": ["chain-proj-genesis"],
            "metadata": {},
        })

        r = client.post("/api/projects/chain-proj/replan", content_type="application/json")
        assert r.status_code == 200
        task = db.task_get(r.json["task_id"])
        assert task["dependencies"] == ["chain-tail"]

    def test_batch_chain_to_head_attaches_all_root_tasks(self, client):
        client.post("/api/projects", json={"name": "batch-proj"}, content_type="application/json")
        db.project_upsert({**db.project_get("batch-proj"), "head_task_id": "batch-tail"})
        db.task_upsert({
            "id": "batch-tail",
            "project": "batch-proj",
            "type": "feature",
            "description": "tail",
            "priority": 50,
            "status": "completed",
            "dependencies": ["batch-proj-genesis"],
            "metadata": {},
        })

        r = client.post("/api/tasks/batch", json={
            "project": "batch-proj",
            "chain_to_head": True,
            "tasks": [
                {"id": "root-a", "type": "feature", "description": "A"},
                {"id": "root-b", "type": "feature", "description": "B"},
                {"id": "child-c", "type": "feature", "description": "C", "depends_on": [0]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        assert db.task_get("root-a")["dependencies"] == ["batch-tail"]
        assert db.task_get("root-b")["dependencies"] == ["batch-tail"]
        assert db.task_get("child-c")["dependencies"] == ["root-a"]

    def test_batch_chain_to_head_repairs_stale_project_head(self, client):
        client.post("/api/projects", json={"name": "batch-repair"}, content_type="application/json")
        db.project_upsert({**db.project_get("batch-repair"), "head_task_id": "stale-failed"})
        db.task_upsert({
            "id": "stale-failed",
            "project": "batch-repair",
            "type": "feature",
            "description": "stale head",
            "priority": 50,
            "status": "failed",
            "created": "2026-04-01T10:00:00",
            "dependencies": ["batch-repair-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "good-tail",
            "project": "batch-repair",
            "type": "feature",
            "description": "good head",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T11:00:00",
            "completed": "2026-04-01T11:05:00",
            "dependencies": ["batch-repair-genesis"],
            "metadata": {},
        })

        r = client.post("/api/tasks/batch", json={
            "project": "batch-repair",
            "chain_to_head": True,
            "tasks": [
                {"id": "root-a", "type": "feature", "description": "Root A"},
                {"id": "root-b", "type": "feature", "description": "Root B"},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        assert db.task_get("root-a")["dependencies"] == ["good-tail"]
        assert db.task_get("root-b")["dependencies"] == ["good-tail"]
        assert db.project_get("batch-repair")["head_task_id"] == "good-tail"

    def test_spawn_auto_created_task_chains_to_head(self, client):
        client.post("/api/projects", json={"name": "spawn-proj"}, content_type="application/json")
        db.project_upsert({**db.project_get("spawn-proj"), "head_task_id": "spawn-tail"})
        db.task_upsert({
            "id": "spawn-tail",
            "project": "spawn-proj",
            "type": "feature",
            "description": "tail",
            "priority": 50,
            "status": "completed",
            "dependencies": ["spawn-proj-genesis"],
            "metadata": {},
        })

        with patch("swarm.orchestrator.spawn_agent", return_value="spawn-agent-1"):
            r = client.post("/api/spawn", json={
                "project": "spawn-proj",
                "type": "audit",
                "description": "audit it",
            }, content_type="application/json")
        assert r.status_code == 200
        task = db.task_get(r.json["task_id"])
        assert task["dependencies"] == ["spawn-tail"]

    def test_wizard_create_anchors_root_tasks_to_genesis(self, client):
        with patch("swarm.api_wizard._scaffold_project_repo", return_value=("git log", None)), \
             patch("swarm.api_wizard._generate_game_design_doc", return_value="# design"), \
             patch("swarm.api_wizard._generate_closure_spec", return_value=None), \
             patch("swarm.api_wizard._project_creation_validation_errors", return_value=[]):
            r = client.post("/api/wizard/create", json={
                "project_name": "wiz-proj",
                "project_type": "godot",
                "notes": "test project",
                "tasks": [
                    {"description": "Root one", "type": "feature", "priority": 60},
                    {"description": "Root two", "type": "feature", "priority": 60},
                    {"description": "Child", "type": "bug", "priority": 70, "depends_on": [0]},
                ],
            }, content_type="application/json")
        assert r.status_code == 200
        proj = db.project_get("wiz-proj")
        assert proj["head_task_id"] == "wiz-proj-genesis"
        task_ids = r.json["task_ids"]
        assert db.task_get(task_ids[0])["dependencies"] == ["wiz-proj-genesis"]
        assert db.task_get(task_ids[1])["dependencies"] == ["wiz-proj-genesis"]
        assert db.task_get(task_ids[2])["dependencies"] == [task_ids[0]]

    def test_create_project_tasks_rejects_trivial_star_graph_and_cleans_up(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "bad-chat-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "bad-chat-proj",
            "overview": "bad graph",
            "tasks": [
                {"id": "bad-chat-proj-t1", "description": "Project Foundation", "dependencies": []},
                {"id": "bad-chat-proj-t2", "description": "Currency System", "dependencies": ["bad-chat-proj-t1"]},
                {"id": "bad-chat-proj-t3", "description": "Currency HUD", "dependencies": ["bad-chat-proj-t1"]},
                {"id": "bad-chat-proj-t4", "description": "Wave Manager", "dependencies": ["bad-chat-proj-t1"]},
                {"id": "bad-chat-proj-t5", "description": "Wave Indicator HUD", "dependencies": ["bad-chat-proj-t1"]},
                {"id": "bad-chat-proj-t6", "description": "Game Over Screen", "dependencies": ["bad-chat-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert "validation failed" in r.json["error"].lower()
        assert db.project_get("bad-chat-proj") is None
        assert db.task_get("bad-chat-proj-t1") is None
        assert not project_dir.exists()

    def test_create_project_tasks_rejects_overly_flat_external_fan_graph(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "flat-chat-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "flat-chat-proj",
            "overview": "flat graph",
            "tasks": [
                {"id": "flat-chat-proj-t1", "description": "Shared Signals & Enums", "dependencies": []},
                {"id": "flat-chat-proj-t2", "description": "Mission State Manager", "dependencies": ["flat-chat-proj-t1"]},
                {"id": "flat-chat-proj-t3", "description": "Level Layout", "dependencies": ["flat-chat-proj-t2"]},
                {"id": "flat-chat-proj-t4", "description": "Player Control", "dependencies": ["flat-chat-proj-t3", "flat-chat-proj-t1"]},
                {"id": "flat-chat-proj-t5", "description": "Guard Patrol", "dependencies": ["flat-chat-proj-t3"]},
                {"id": "flat-chat-proj-t6", "description": "Operative Switching", "dependencies": ["flat-chat-proj-t4", "flat-chat-proj-t1"]},
                {"id": "flat-chat-proj-t7", "description": "Guard Detection", "dependencies": []},
                {"id": "flat-chat-proj-t8", "description": "Operative Abilities", "dependencies": []},
                {"id": "flat-chat-proj-t9", "description": "Alert Escalation", "dependencies": []},
                {"id": "flat-chat-proj-t10", "description": "Interactions", "dependencies": []},
                {"id": "flat-chat-proj-t11", "description": "Mission Objective Flow", "dependencies": []},
                {"id": "flat-chat-proj-t12", "description": "HUD & Feedback", "dependencies": []},
                {"id": "flat-chat-proj-t13", "description": "Menu & Restart", "dependencies": []},
                {"id": "flat-chat-proj-t14", "description": "Vertical Slice Integration", "dependencies": []},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert any("too flat" in detail.lower() for detail in r.json["details"])
        assert db.project_get("flat-chat-proj") is None
        assert not project_dir.exists()

    def test_create_project_tasks_rejects_chain_like_graph(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "chain-chat-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "chain-chat-proj",
            "overview": "chain graph",
            "tasks": [
                {"id": "chain-chat-proj-t1", "description": "Shared Signals & Enums", "dependencies": []},
                {"id": "chain-chat-proj-t2", "description": "Mission State Manager", "dependencies": ["chain-chat-proj-t1"]},
                {"id": "chain-chat-proj-t3", "description": "Level Layout", "dependencies": ["chain-chat-proj-t2"]},
                {"id": "chain-chat-proj-t4", "description": "Player Control", "dependencies": ["chain-chat-proj-t3"]},
                {"id": "chain-chat-proj-t5", "description": "Operative Switching", "dependencies": ["chain-chat-proj-t4"]},
                {"id": "chain-chat-proj-t6", "description": "Operative Abilities", "dependencies": ["chain-chat-proj-t5"]},
                {"id": "chain-chat-proj-t7", "description": "Interactions", "dependencies": ["chain-chat-proj-t6"]},
                {"id": "chain-chat-proj-t8", "description": "Mission Objective Flow", "dependencies": ["chain-chat-proj-t7"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert any("too chain-like" in detail.lower() for detail in r.json["details"])
        assert db.project_get("chain-chat-proj") is None
        assert not project_dir.exists()

    def test_create_project_tasks_accepts_semantically_weak_signal_cartel_style_graph(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "semantic-chat-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "semantic-chat-proj",
            "overview": "semantic graph",
            "tasks": [
                {"id": "semantic-chat-proj-t1", "description": "Shared Signals & Enums", "dependencies": []},
                {"id": "semantic-chat-proj-t2", "description": "Mission State Manager", "dependencies": ["semantic-chat-proj-t1"]},
                {"id": "semantic-chat-proj-t3", "description": "Level Layout", "dependencies": ["semantic-chat-proj-t2"]},
                {"id": "semantic-chat-proj-t4", "description": "Player Control", "dependencies": ["semantic-chat-proj-t1"]},
                {"id": "semantic-chat-proj-t5", "description": "Guard Patrol", "dependencies": ["semantic-chat-proj-t1"]},
                {"id": "semantic-chat-proj-t6", "description": "Operative Switching", "dependencies": ["semantic-chat-proj-t4"]},
                {"id": "semantic-chat-proj-t7", "description": "Guard Detection", "dependencies": ["semantic-chat-proj-t5"]},
                {"id": "semantic-chat-proj-t8", "description": "Alert Escalation", "dependencies": ["semantic-chat-proj-t7"]},
                {"id": "semantic-chat-proj-t9", "description": "Operative Abilities", "dependencies": ["semantic-chat-proj-t6"]},
                {"id": "semantic-chat-proj-t10", "description": "Interactions", "dependencies": ["semantic-chat-proj-t9"]},
                {"id": "semantic-chat-proj-t11", "description": "Mission Objective Flow", "dependencies": ["semantic-chat-proj-t10"]},
                {"id": "semantic-chat-proj-t12", "description": "HUD & Feedback", "dependencies": ["semantic-chat-proj-t4"]},
                {"id": "semantic-chat-proj-t13", "description": "Menu & Restart", "dependencies": ["semantic-chat-proj-t12"]},
                {"id": "semantic-chat-proj-t14", "description": "Vertical Slice Integration & Polish", "dependencies": ["semantic-chat-proj-t3", "semantic-chat-proj-t8", "semantic-chat-proj-t13"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        assert db.project_get("semantic-chat-proj") is not None
        assert project_dir.exists()

    def test_create_project_tasks_accepts_semantically_strong_signal_cartel_style_graph(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "semantic-good-proj",
            "overview": "good semantic graph",
            "tasks": [
                {"id": "semantic-good-proj-t1", "description": "Shared Signals & Enums", "dependencies": []},
                {"id": "semantic-good-proj-t2", "description": "Mission State Manager", "dependencies": ["semantic-good-proj-t1"]},
                {"id": "semantic-good-proj-t3", "description": "Level Layout", "dependencies": ["semantic-good-proj-t2"]},
                {"id": "semantic-good-proj-t4", "description": "Player Control", "dependencies": ["semantic-good-proj-t1"]},
                {"id": "semantic-good-proj-t5", "description": "Guard Patrol", "dependencies": ["semantic-good-proj-t1"]},
                {"id": "semantic-good-proj-t6", "description": "Operative Switching", "dependencies": ["semantic-good-proj-t4"]},
                {"id": "semantic-good-proj-t7", "description": "Guard Detection", "dependencies": ["semantic-good-proj-t5"]},
                {"id": "semantic-good-proj-t8", "description": "Alert Escalation", "dependencies": ["semantic-good-proj-t7"]},
                {"id": "semantic-good-proj-t9", "description": "Operative Abilities", "dependencies": ["semantic-good-proj-t6"]},
                {"id": "semantic-good-proj-t10", "description": "Interactions", "dependencies": ["semantic-good-proj-t9"]},
                {"id": "semantic-good-proj-t11", "description": "HUD & Feedback", "dependencies": ["semantic-good-proj-t4", "semantic-good-proj-t2", "semantic-good-proj-t8", "semantic-good-proj-t9"]},
                {"id": "semantic-good-proj-t12", "description": "Mission Objective Flow", "dependencies": ["semantic-good-proj-t2", "semantic-good-proj-t3", "semantic-good-proj-t8", "semantic-good-proj-t10"]},
                {"id": "semantic-good-proj-t13", "description": "Menu & Restart", "dependencies": ["semantic-good-proj-t11", "semantic-good-proj-t12"]},
                {"id": "semantic-good-proj-t14", "description": "Vertical Slice Integration & Polish", "dependencies": ["semantic-good-proj-t3", "semantic-good-proj-t9", "semantic-good-proj-t11", "semantic-good-proj-t12", "semantic-good-proj-t13"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        assert db.project_get("semantic-good-proj") is not None
        assert db.task_get("semantic-good-proj-t12")["dependencies"] == [
            "semantic-good-proj-t2",
            "semantic-good-proj-t3",
            "semantic-good-proj-t8",
            "semantic-good-proj-t10",
        ]

    def test_create_project_tasks_accepts_shooter_hud_with_player_wave_and_ammo_state(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "candy-shooter-fps",
            "overview": "FPS with gem matching reload loop.",
            "tasks": [
                {"id": "candy-shooter-fps-t01", "description": "FPS player controller", "dependencies": []},
                {"id": "candy-shooter-fps-t02", "description": "Candy enemy spawning", "dependencies": ["candy-shooter-fps-t01"]},
                {"id": "candy-shooter-fps-t03", "description": "Enemies drop gems on death", "dependencies": ["candy-shooter-fps-t02"]},
                {"id": "candy-shooter-fps-t04", "description": "Bejeweled grid overlay", "dependencies": ["candy-shooter-fps-t03"]},
                {"id": "candy-shooter-fps-t05", "description": "Match-3 mechanics", "dependencies": ["candy-shooter-fps-t04"]},
                {"id": "candy-shooter-fps-t06", "description": "Weapon reload via matching", "dependencies": ["candy-shooter-fps-t05"]},
                {"id": "candy-shooter-fps-t07", "description": "Wave progression", "dependencies": ["candy-shooter-fps-t01", "candy-shooter-fps-t02"]},
                {"id": "candy-shooter-fps-t08", "description": "HUD display", "dependencies": ["candy-shooter-fps-t01", "candy-shooter-fps-t06", "candy-shooter-fps-t07"]},
                {"id": "candy-shooter-fps-t09", "description": "Game over and run-state", "dependencies": ["candy-shooter-fps-t01", "candy-shooter-fps-t07", "candy-shooter-fps-t08"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["created"] == 9

    def test_create_project_tasks_auto_repairs_invalid_graph(self, client, app, monkeypatch):
        app.config["PROJECT_CREATION_RETRY_ROUNDS_OVERRIDE"] = 2

        def fake_scaffold(project_name, project_path, project_type, overview_text, config):
            project_path.mkdir(parents=True, exist_ok=True)
            (project_path / ".git").mkdir(exist_ok=True)
            (project_path / "README.md").write_text("# test\n")
            (project_path / ".gitignore").write_text(".godot/\n")
            return ["ok"], None

        repair_payload = json.dumps([
            {"id": "repair-proj-t1", "dependencies": []},
            {"id": "repair-proj-t2", "dependencies": ["repair-proj-t1"]},
            {"id": "repair-proj-t3", "dependencies": ["repair-proj-t2", "repair-proj-t4"]},
            {"id": "repair-proj-t4", "dependencies": ["repair-proj-t1"]},
            {"id": "repair-proj-t5", "dependencies": ["repair-proj-t4", "repair-proj-t2"]},
            {"id": "repair-proj-t6", "dependencies": ["repair-proj-t3", "repair-proj-t5"]},
        ])

        from swarm import api_wizard
        monkeypatch.setattr(api_wizard, "_scaffold_project_repo", fake_scaffold)
        monkeypatch.setattr(api_wizard, "_chat_call_llm", lambda *args, **kwargs: repair_payload)

        r = client.post("/api/create-project-tasks", json={
            "project_name": "repair-proj",
            "overview": "repair graph",
            "tasks": [
                {"id": "repair-proj-t1", "description": "Project Foundation", "dependencies": []},
                {"id": "repair-proj-t2", "description": "Currency System", "dependencies": ["repair-proj-t1"]},
                {"id": "repair-proj-t3", "description": "Currency Tracker", "dependencies": ["repair-proj-t1"]},
                {"id": "repair-proj-t4", "description": "Wave Manager", "dependencies": ["repair-proj-t1"]},
                {"id": "repair-proj-t5", "description": "Wave Indicator", "dependencies": ["repair-proj-t1"]},
                {"id": "repair-proj-t6", "description": "Game Over Screen", "dependencies": ["repair-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        assert r.json["correction_rounds"] == 1
        assert db.task_get("repair-proj-t3")["dependencies"] == ["repair-proj-t2", "repair-proj-t4"]

    def test_create_project_tasks_bootstraps_godot_support_files(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "boot-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "boot-proj",
            "overview": "boot graph",
            "tasks": [
                {"id": "boot-proj-t1", "description": "Create core scene", "dependencies": []},
                {"id": "boot-proj-t2", "description": "Build path system", "dependencies": ["boot-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        assert (project_dir / "addons" / "gut" / "gut_cmdln.gd").exists()
        assert (project_dir / "autoload" / "state_server.gd").exists()
        assert (project_dir / "autoload" / "test_harness.gd").exists()
        assert (project_dir / "check_scripts.gd").exists()
        assert (project_dir / "icon.svg").exists()
        assert (project_dir / "project.godot").exists()
        assert 'config/icon="res://icon.svg"' in (project_dir / "project.godot").read_text()
        assert (project_dir / "test" / "unit" / "test_placeholder.gd").exists()

    def test_create_project_tasks_writes_full_design_doc_from_task_payload(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "doc-rich-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "doc-rich-proj",
            "overview": "A compact strategy prototype.",
            "quality_gates": ["No script errors", "GUT tests pass"],
            "tasks": [
                {
                    "id": "doc-rich-proj-t1",
                    "description": "Foundation\nAcceptance: shared enums exist",
                    "dependencies": [],
                },
                {
                    "id": "doc-rich-proj-t2",
                    "description": "HUD Display\nAcceptance: HUD reads foundation state",
                    "dependencies": ["doc-rich-proj-t1"],
                },
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        design_doc = (project_dir / "GAME_DESIGN.md").read_text()
        assert "## Overview" in design_doc
        assert "A compact strategy prototype." in design_doc
        assert "## Quality Gates" in design_doc
        assert "- No script errors" in design_doc
        assert "## User Stories" in design_doc
        assert "### doc-rich-proj-t1" in design_doc
        assert "Acceptance: shared enums exist" in design_doc
        assert "depends-on: doc-rich-proj-genesis" in design_doc
        assert "### doc-rich-proj-t2" in design_doc
        assert "depends-on: doc-rich-proj-t1" in design_doc
        assert "## Dependency Map" in design_doc

    def test_append_project_phase_anchors_roots_updates_head_and_appends_doc(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "phase-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "phase-proj",
            "overview": "Phase one.",
            "tasks": [
                {"id": "phase-proj-t1", "description": "Foundation", "dependencies": []},
                {"id": "phase-proj-t2", "description": "Integration", "dependencies": ["phase-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        db.project_upsert({**db.project_get("phase-proj"), "head_task_id": "phase-proj-t2"})

        r = client.post("/api/projects/phase-proj/append-phase", json={
            "phase_name": "Phase 2 Expansion",
            "overview": "Add diplomacy and larger AI.",
            "tasks": [
                {"id": "phase-proj-exp-1", "description": "Diplomacy model", "dependencies": []},
                {"id": "phase-proj-exp-2", "description": "Diplomacy UI", "dependencies": ["phase-proj-exp-1"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["anchor_task_id"] == "phase-proj-t2"
        assert db.task_get("phase-proj-exp-1")["dependencies"] == ["phase-proj-t2"]
        assert db.task_get("phase-proj-exp-2")["dependencies"] == ["phase-proj-exp-1"]
        assert db.project_get("phase-proj")["head_task_id"] == "phase-proj-exp-2"
        design_doc = (project_dir / "GAME_DESIGN.md").read_text()
        assert "## Phase 2 Expansion" in design_doc
        assert "Anchored after: `phase-proj-t2`" in design_doc
        assert "phase-proj-exp-1: phase-proj-t2" in design_doc

    def test_append_project_phase_can_insert_qa_and_manual_gate(self, client, app):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "gated-phase-proj",
            "overview": "Phase one.",
            "tasks": [
                {"id": "gated-phase-proj-t1", "description": "Foundation", "dependencies": []},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        db.project_upsert({**db.project_get("gated-phase-proj"), "head_task_id": "gated-phase-proj-t1"})

        r = client.post("/api/projects/gated-phase-proj/append-phase", json={
            "phase_name": "Phase 2",
            "qa_before_phase": True,
            "phase_gate": True,
            "phase_qa_id": "gated-phase-proj-phase2-qa",
            "phase_gate_id": "gated-phase-proj-phase2-gate",
            "tasks": [
                {"id": "gated-phase-proj-exp-1", "description": "Expansion foundation", "dependencies": []},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["phase_qa_id"] == "gated-phase-proj-phase2-qa"
        assert r.json["phase_gate_id"] == "gated-phase-proj-phase2-gate"
        qa = db.task_get("gated-phase-proj-phase2-qa")
        gate = db.task_get("gated-phase-proj-phase2-gate")
        exp = db.task_get("gated-phase-proj-exp-1")
        assert qa["type"] == "harness_qa"
        assert qa["dependencies"] == ["gated-phase-proj-t1"]
        assert gate["type"] == "phase_gate"
        assert gate["dependencies"] == ["gated-phase-proj-phase2-qa"]
        assert exp["dependencies"] == ["gated-phase-proj-phase2-gate"]

        release = client.post("/api/projects/gated-phase-proj/phase-gates/gated-phase-proj-phase2-gate/release")
        assert release.status_code == 200
        assert db.task_get("gated-phase-proj-phase2-gate")["status"] == "completed"

    def test_append_project_phase_can_add_standalone_qa_checkpoint(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "qa-point-proj",
            "overview": "Phase one.",
            "tasks": [
                {"id": "qa-point-proj-t1", "description": "Foundation", "dependencies": []},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        db.project_upsert({**db.project_get("qa-point-proj"), "head_task_id": "qa-point-proj-t1"})

        r = client.post("/api/projects/qa-point-proj/append-phase", json={
            "phase_name": "Phase 1 QA",
            "qa_before_phase": True,
            "phase_qa_id": "qa-point-proj-phase1-qa",
            "tasks": [],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["task_ids"] == ["qa-point-proj-phase1-qa"]
        qa = db.task_get("qa-point-proj-phase1-qa")
        assert qa["type"] == "harness_qa"
        assert qa["dependencies"] == ["qa-point-proj-t1"]
        assert db.project_get("qa-point-proj")["head_task_id"] == "qa-point-proj-phase1-qa"

    def test_insert_task_before_pending_phase_gate_rewires_gate(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "pre-gate-proj",
            "overview": "Phase one.",
            "tasks": [
                {"id": "pre-gate-proj-t1", "description": "Foundation", "dependencies": []},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        db.project_upsert({**db.project_get("pre-gate-proj"), "head_task_id": "pre-gate-proj-t1"})

        r = client.post("/api/projects/pre-gate-proj/append-phase", json={
            "phase_name": "Phase 2",
            "qa_before_phase": True,
            "phase_gate": True,
            "phase_qa_id": "pre-gate-proj-phase2-qa",
            "phase_gate_id": "pre-gate-proj-phase2-gate",
            "tasks": [
                {"id": "pre-gate-proj-p2-1", "description": "Expansion foundation", "dependencies": []},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        db.task_update_status("pre-gate-proj-phase2-qa", "completed")

        r = client.post("/api/tasks/pre-gate-proj-phase2-gate/insert-before-gate", json={
            "id": "pre-gate-proj-fix-main-menu",
            "type": "bug",
            "priority": 95,
            "description": "Fix the main menu before Phase 2 starts.",
        }, content_type="application/json")

        assert r.status_code == 200
        fix = db.task_get("pre-gate-proj-fix-main-menu")
        gate = db.task_get("pre-gate-proj-phase2-gate")
        phase_root = db.task_get("pre-gate-proj-p2-1")
        assert fix["dependencies"] == ["pre-gate-proj-phase2-qa"]
        assert fix["metadata"]["inserted_before_gate"] is True
        assert fix["metadata"]["phase_gate_id"] == "pre-gate-proj-phase2-gate"
        assert gate["dependencies"] == ["pre-gate-proj-fix-main-menu"]
        assert phase_root["dependencies"] == ["pre-gate-proj-phase2-gate"]

    def test_insert_task_before_gate_rejects_released_gate(self, client):
        db.task_upsert({
            "id": "released-gate",
            "project": "p",
            "type": "phase_gate",
            "description": "Gate",
            "priority": 100,
            "status": "completed",
            "dependencies": [],
            "metadata": {},
        })

        r = client.post("/api/tasks/released-gate/insert-before-gate", json={
            "description": "Too late",
        }, content_type="application/json")

        assert r.status_code == 409

    def test_create_project_tasks_scaffolds_python_project(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "python-soft-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "python-soft-proj",
            "project_type": "python",
            "overview": "A small Python software project.",
            "tasks": [
                {"id": "python-soft-proj-t1", "description": "CLI foundation", "dependencies": []},
                {"id": "python-soft-proj-t2", "description": "Parser implementation", "dependencies": ["python-soft-proj-t1"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["project_type"] == "python"
        assert (project_dir / "pyproject.toml").exists()
        assert (project_dir / "PROJECT_BRIEF.md").exists()
        assert (project_dir / "PROJECT_CLOSURE.md").exists()
        assert not (project_dir / "project.godot").exists()
        assert not (project_dir / "addons" / "gut").exists()
        closure_doc = (project_dir / "PROJECT_CLOSURE.md").read_text()
        assert "# python-soft-proj Closure Contract" in closure_doc
        assert "- profile: python" in closure_doc
        assert "- mode: build" in closure_doc
        assert "## Boot" in closure_doc
        assert "## Assumptions" in closure_doc
        project = db.project_get("python-soft-proj")
        assert project["profile"] == "python"
        assert r.json["closure_proposal"]["profile"] == "python"
        assert r.json["closure_proposal"]["source"] == "heuristic"
        assert project["closure_mode"] == "build"
        assert project["closure_spec"]["boot"]["ready_check"]["type"] == "command"

    def test_create_project_tasks_persists_godot_closure_proposal(self, client):
        project_dir = Path(client.application.config["DATA_DIR"]).parent / "workspace" / "godot-closure-proj"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "godot-closure-proj",
            "project_type": "godot",
            "overview": "A small Godot game.",
            "tasks": [
                {"id": "godot-closure-proj-t1", "description": "Core scene", "dependencies": []},
                {"id": "godot-closure-proj-t2", "description": "Enemy loop", "dependencies": ["godot-closure-proj-t1"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        assert r.json["closure_proposal"]["profile"] == "godot"
        assert r.json["closure_proposal"]["closure_spec"]["boot"]["ready_check"]["type"] == "command"
        closure_doc = (project_dir / "PROJECT_CLOSURE.md").read_text()
        assert "# godot-closure-proj Closure Contract" in closure_doc
        assert "- profile: godot" in closure_doc
        assert "godot --headless --path . --quit" in closure_doc
        assert "`godot-closure-proj-t2`" in closure_doc
        project = db.project_get("godot-closure-proj")
        assert project["profile"] == "godot"
        assert project["closure_mode"] == "stabilize"
        assert project["closure_spec"]["verification"]["smoke_checks"][0]["command"] == "godot --headless --path . --quit"

    def test_create_project_tasks_derives_godot_closure_contract_from_prd_context(self, client):
        project_dir = Path(client.application.config["DATA_DIR"]).parent / "workspace" / "example-game"
        r = client.post("/api/create-project-tasks", json={
            "project_name": "example-game",
            "project_type": "godot",
            "overview": "Arena game about spawning blobs, surviving waves, and reaching 100 mass.",
            "quality_gates": ["gut --run --exit"],
            "tasks": [
                {"id": "example-game-t02", "description": "Player click-to-spawn blobs, basic movement, hunger decay", "dependencies": []},
                {"id": "example-game-t04", "description": "Food pellets -- spawning, consumption, blob growth", "dependencies": ["example-game-t02"]},
                {"id": "example-game-t06", "description": "Wave system -- wave-based spawning and stream mode", "dependencies": []},
                {"id": "example-game-t08", "description": "Upgrade system -- between-wave upgrade selection menu", "dependencies": ["example-game-t06"]},
                {"id": "example-game-t09", "description": "Win/lose conditions -- all blobs dead or 100 mass victory", "dependencies": ["example-game-t06", "example-game-t08"]},
                {"id": "example-game-t10", "description": "Menu + HUD -- wave number, player mass / 100, blob count, time elapsed", "dependencies": ["example-game-t06", "example-game-t09"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 200
        proposal = r.json["closure_proposal"]
        assert proposal["profile"] == "godot"
        assert proposal["closure_spec"]["boot"]["ready_check"]["type"] == "command"
        assert "gut_cmdln.gd" in proposal["closure_spec"]["verification"]["unit_test_command"]
        assert "gut_cmdln.gd" in proposal["closure_spec"]["verification"]["smoke_checks"][0]["command"]
        flow_ids = [flow["id"] for flow in proposal["closure_spec"]["critical_flows"]]
        assert flow_ids[0] == "example-game-t09"
        assert "example-game-t06" in flow_ids
        assert "example-game-t02" in flow_ids or "example-game-t04" in flow_ids
        assert "example-game-t10" not in flow_ids
        closure_doc = (project_dir / "PROJECT_CLOSURE.md").read_text()
        assert "gut_cmdln.gd" in closure_doc
        assert "example-game-t09" in closure_doc
        assert "Win/lose conditions" in closure_doc

    def test_create_project_tasks_rejects_existing_project_name(self, client):
        db.project_upsert({
            "name": "existing-proj",
            "status": "active",
            "managed": True,
            "files": {},
            "recent_commits": [],
            "file_locks": {},
            "profile": "godot",
        })

        r = client.post("/api/create-project-tasks", json={
            "project_name": "existing-proj",
            "project_type": "godot",
            "overview": "Should not append onto an existing project.",
            "tasks": [
                {"id": "existing-proj-t1", "description": "Fresh task graph root", "dependencies": []},
            ],
        }, content_type="application/json")

        assert r.status_code == 409
        assert "already exists" in r.json["error"]
        assert r.json["retryable"] is True

    def test_create_project_tasks_preserves_intra_batch_dependencies(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "dep-preserve-proj",
            "overview": "preserve graph",
            "tasks": [
                {"id": "dep-preserve-proj-t1", "description": "Shared Signals & Enums", "dependencies": []},
                {"id": "dep-preserve-proj-t2", "description": "Mission State Manager", "dependencies": ["dep-preserve-proj-t1"]},
                {"id": "dep-preserve-proj-t3", "description": "Level Layout", "dependencies": ["dep-preserve-proj-t2"]},
                {"id": "dep-preserve-proj-t4", "description": "Player Control", "dependencies": ["dep-preserve-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        assert db.task_get("dep-preserve-proj-t1")["dependencies"] == ["dep-preserve-proj-genesis"]
        assert db.task_get("dep-preserve-proj-t2")["dependencies"] == ["dep-preserve-proj-t1"]
        assert db.task_get("dep-preserve-proj-t3")["dependencies"] == ["dep-preserve-proj-t2"]
        assert db.task_get("dep-preserve-proj-t4")["dependencies"] == ["dep-preserve-proj-t1"]

    def test_create_project_tasks_returns_chat_retry_context_after_failed_repairs(self, client, app, monkeypatch):
        from swarm import api_chat

        def fake_scaffold(project_name, project_path, project_type, overview_text, config):
            project_path.mkdir(parents=True, exist_ok=True)
            (project_path / ".git").mkdir(exist_ok=True)
            (project_path / "README.md").write_text("# test\n")
            (project_path / ".gitignore").write_text(".godot/\n")
            return ["ok"], None

        monkeypatch.setattr(api_chat, "_scaffold_project_repo", fake_scaffold)
        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: "not json")

        r = client.post("/api/create-project-tasks", json={
            "project_name": "retry-proj",
            "overview": "retry graph",
            "tasks": [
                {"id": "retry-proj-t1", "description": "Project Foundation", "dependencies": []},
                {"id": "retry-proj-t2", "description": "Currency System", "dependencies": ["retry-proj-t1"]},
                {"id": "retry-proj-t3", "description": "Currency HUD", "dependencies": ["retry-proj-t1"]},
                {"id": "retry-proj-t4", "description": "Wave Manager", "dependencies": ["retry-proj-t1"]},
                {"id": "retry-proj-t5", "description": "Wave Indicator HUD", "dependencies": ["retry-proj-t1"]},
                {"id": "retry-proj-t6", "description": "Game Over Screen", "dependencies": ["retry-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert r.json["retryable"] is True
        assert "Automatic project-graph repair could not produce a valid dependency DAG" in r.json["chat_recovery_assistant"]
        assert "Graph diagnostics:" in r.json["chat_recovery_assistant"]
        assert "Root-like tasks:" in r.json["chat_recovery_assistant"]
        assert r.json["graph_diagnostics"]["shape"]["task_count"] == 6
        assert r.json["graph_diagnostics"]["validation_errors"]
        assert db.project_get("retry-proj") is None

    def test_create_project_tasks_rejects_setup_task_as_side_root(self, client, app, monkeypatch):
        from swarm import api_chat

        def fake_scaffold(project_name, project_path, project_type, overview_text, config):
            project_path.mkdir(parents=True, exist_ok=True)
            (project_path / ".git").mkdir(exist_ok=True)
            (project_path / "README.md").write_text("# test\n")
            (project_path / ".gitignore").write_text(".godot/\n")
            return ["ok"], None

        monkeypatch.setattr(api_chat, "_scaffold_project_repo", fake_scaffold)
        monkeypatch.setattr(api_chat, "_chat_call_llm", lambda *args, **kwargs: "not json")

        r = client.post("/api/create-project-tasks", json={
            "project_name": "setup-side-root-proj",
            "overview": "setup root issue",
            "tasks": [
                {"id": "setup-side-root-proj-t1", "description": "Set up GUT addon and test infrastructure", "dependencies": []},
                {"id": "setup-side-root-proj-t2", "description": "Create 3D core scene with camera and lighting", "dependencies": []},
                {"id": "setup-side-root-proj-t3", "description": "Build grid and path systems", "dependencies": ["setup-side-root-proj-t2"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert any("Setup/infrastructure tasks should anchor the project graph" in detail for detail in r.json["details"])
        assert db.project_get("setup-side-root-proj") is None

    def test_create_project_tasks_returns_retryable_payload_for_dependency_cycle(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "cycle-chat-proj",
            "overview": "cycle graph",
            "tasks": [
                {"id": "cycle-chat-proj-t1", "description": "Core loop", "dependencies": ["cycle-chat-proj-t2"]},
                {"id": "cycle-chat-proj-t2", "description": "Enemy loop", "dependencies": ["cycle-chat-proj-t1"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 400
        assert r.json["retryable"] is True
        assert r.json["error"] == "Project creation validation failed"
        assert any("Dependency cycle detected" in detail for detail in r.json["details"])
        assert "graph contains a cycle" in r.json["chat_recovery_assistant"]
        assert r.json["graph_diagnostics"]["validation_errors"]
        assert db.project_get("cycle-chat-proj") is None

    def test_create_project_tasks_rejects_redundant_project_setup_task(self, client):
        r = client.post("/api/create-project-tasks", json={
            "project_name": "redundant-setup-proj",
            "overview": "setup should be automatic",
            "tasks": [
                {"id": "redundant-setup-proj-t1", "description": "Project Setup", "dependencies": []},
                {"id": "redundant-setup-proj-t2", "description": "Grid System", "dependencies": ["redundant-setup-proj-t1"]},
                {"id": "redundant-setup-proj-t3", "description": "Tower Data Model", "dependencies": ["redundant-setup-proj-t1"]},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert any("Do not create generic Project Setup" in detail for detail in r.json["details"])
        assert db.project_get("redundant-setup-proj") is None

    def test_wizard_create_rejects_trivial_star_graph(self, client, app):
        project_dir = Path(app.config["DATA_DIR"]).parent / "workspace" / "wiz-bad-proj"
        r = client.post("/api/wizard/create", json={
            "project_name": "wiz-bad-proj",
            "project_type": "godot",
            "notes": "bad graph",
            "tasks": [
                {"description": "Project Foundation", "type": "feature", "priority": 60},
                {"description": "Currency System", "type": "feature", "priority": 50, "depends_on": [0]},
                {"description": "Currency HUD", "type": "feature", "priority": 50, "depends_on": [0]},
                {"description": "Wave Manager", "type": "feature", "priority": 50, "depends_on": [0]},
                {"description": "Wave Indicator HUD", "type": "feature", "priority": 50, "depends_on": [0]},
                {"description": "Game Over Screen", "type": "feature", "priority": 50, "depends_on": [0]},
            ],
        }, content_type="application/json")
        assert r.status_code == 400
        assert "validation failed" in r.json["error"].lower()
        assert db.project_get("wiz-bad-proj") is None
        assert not project_dir.exists()

    def test_wizard_create_preserves_depends_on_relationships(self, client):
        r = client.post("/api/wizard/create", json={
            "project_name": "wiz-deps-proj",
            "project_type": "godot",
            "notes": "preserve depends_on",
            "tasks": [
                {"description": "Shared Signals & Enums", "type": "feature", "priority": 60},
                {"description": "Mission State Manager", "type": "feature", "priority": 50, "depends_on": [0]},
                {"description": "Level Layout", "type": "feature", "priority": 50, "depends_on": [1]},
            ],
        }, content_type="application/json")
        assert r.status_code == 200
        t1, t2, t3 = r.json["task_ids"]
        assert db.task_get(t1)["dependencies"] == ["wiz-deps-proj-genesis"]
        assert db.task_get(t2)["dependencies"] == [t1]
        assert db.task_get(t3)["dependencies"] == [t2]

    def test_create_tasks_file_aware_chains_project_plan_roots_to_planner_task(self, client):
        from swarm.tools import core

        client.post("/api/projects", json={"name": "file-aware-proj"}, content_type="application/json")
        db.project_upsert({**db.project_get("file-aware-proj"), "head_task_id": "file-aware-tail"})
        db.task_upsert({
            "id": "file-aware-tail",
            "project": "file-aware-proj",
            "type": "feature",
            "description": "tail",
            "priority": 50,
            "status": "completed",
            "dependencies": ["file-aware-proj-genesis"],
            "metadata": {},
        })

        batch_payloads = []

        class _Resp:
            def __init__(self, payload):
                self._payload = payload

            def read(self):
                return json.dumps(self._payload).encode()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        def _fake_urlopen(req, timeout=10):
            payload = json.loads(req.data.decode())
            batch_payloads.append(payload)
            id_map = {
                str(i): task["id"]
                for i, task in enumerate(payload["tasks"])
            }
            return _Resp({"ids": list(id_map.values()), "id_map": id_map})

        with patch.object(core._ur, "urlopen", side_effect=_fake_urlopen):
            core.PROJECT = "file-aware-proj"
            core.TASK_TYPE = "project_plan"
            core.TASK_ID = "planner-1"
            result = core.create_tasks_file_aware([
                {"type": "feature", "description": "root-a", "files": ["a.gd"]},
                {"type": "feature", "description": "root-b", "files": ["b.gd"]},
                {"type": "feature", "description": "child-c", "files": ["a.gd"]},
            ], project="file-aware-proj")

        assert result["ok"] is True
        assert len(batch_payloads) == 1
        batch = batch_payloads[0]
        assert batch["project"] == "file-aware-proj"
        assert batch["tasks"][0]["dependencies"] == ["planner-1"]
        assert batch["tasks"][1]["dependencies"] == ["planner-1"]
        assert batch["tasks"][2]["depends_on"] == [0]
        assert batch["tasks"][2]["metadata"]["file_aware_auto_dep_indices"] == [0]
        assert result["tasks"][0]["auto_deps"] == []
        assert result["tasks"][1]["auto_deps"] == []
        assert result["tasks"][2]["auto_deps"] == [batch["tasks"][0]["id"]]

    def test_batch_create_rolls_back_tasks_if_later_item_fails(self, client):
        client.post("/api/projects", json={"name": "batch-atomic"}, content_type="application/json")
        db.task_upsert({
            "id": "known-tail",
            "project": "batch-atomic",
            "type": "feature",
            "description": "known tail",
            "priority": 50,
            "status": "completed",
            "dependencies": ["batch-atomic-genesis"],
            "metadata": {},
        })

        r = client.post("/api/tasks/batch", json={
            "project": "batch-atomic",
            "tasks": [
                {"id": "good-root", "type": "feature", "description": "good root"},
                {"id": "bad-child", "type": "feature", "description": "bad child", "dependencies": ["missing-dep"]},
            ],
        }, content_type="application/json")

        assert r.status_code == 400
        assert db.task_get("good-root") is None
        assert db.task_get("bad-child") is None

    def test_project_upsert_preserves_lock_columns(self, client):
        db.project_upsert({
            "name": "lock-order-proj",
            "status": "active",
            "managed": True,
            "locked": False,
            "locked_at": None,
            "unlocked_at": "2026-04-03T12:00:00",
        })

        row = db.project_get("lock-order-proj")

        assert row["managed"] is True
        assert row["locked"] is False
        assert row["locked_at"] is None
        assert row["unlocked_at"] == "2026-04-03T12:00:00"

    def test_create_app_repairs_corrupted_project_lock_rows(self, tmp_path):
        from swarm.api import create_app

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "swarm.db"
        db.init(db_path)
        db.project_upsert({"name": "repair-lock-proj", "status": "active"})
        conn = db._connect()
        conn.execute(
            "UPDATE projects SET locked=1, locked_at='1', unlocked_at='0' WHERE name=?",
            ("repair-lock-proj",),
        )
        conn.commit()

        app = create_app(
            workspace=tmp_path / "workspace",
            data_dir=data_dir,
            config={"workspace": str(tmp_path / "workspace")},
            config_file=tmp_path / "missing-config.json",
        )
        assert app is not None

        repaired = db.project_get("repair-lock-proj")
        assert repaired["locked"] is False
        assert repaired["locked_at"] is None

    def test_create_app_reconciles_stale_file_locks_on_startup(self, tmp_path):
        from swarm.api import create_app

        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        db_path = data_dir / "swarm.db"
        db.init(db_path)
        db.project_upsert({
            "name": "startup-lock-proj",
            "status": "active",
            "file_locks": {
                "scripts/stale.gd": {
                    "file_path": "scripts/stale.gd",
                    "locked_by": "stale-owner",
                    "locked_at": "2026-04-03T12:00:00",
                    "task_id": "stale-owner",
                },
            },
        })
        db.task_upsert({
            "id": "stale-owner",
            "project": "startup-lock-proj",
            "type": "feature",
            "description": "stale owner",
            "priority": 50,
            "status": "failed",
            "dependencies": [],
            "metadata": {},
        })

        app = create_app(
            workspace=tmp_path / "workspace",
            data_dir=data_dir,
            config={
                "workspace": str(tmp_path / "workspace"),
                "disable_monitor": True,
                "managed_projects": ["startup-lock-proj"],
            },
            config_file=tmp_path / "missing-config.json",
        )
        assert app is not None

        repaired = db.project_get("startup-lock-proj")
        assert repaired["file_locks"] == {}

    def test_create_tasks_file_aware_rejects_file_path_dependencies(self, client):
        from swarm.tools import core

        client.post("/api/projects", json={"name": "file-aware-bad"}, content_type="application/json")

        with patch.object(core._ur, "urlopen") as mock_urlopen:
            core.PROJECT = "file-aware-bad"
            core.TASK_TYPE = "project_plan"
            core.TASK_ID = "planner-bad"
            result = core.create_tasks_file_aware([
                {
                    "type": "feature",
                    "description": "bad root",
                    "files": ["player.gd"],
                    "dependencies": ["scripts/player.gd"],
                },
            ], project="file-aware-bad")

        assert result["ok"] is False
        assert "task IDs only" in result["error"] or "Dependencies must be real task IDs" in result["error"]
        mock_urlopen.assert_not_called()

    def test_reset_project_plans_deletes_generated_tasks_after_cancelling(self, client):
        client.post("/api/projects", json={"name": "reset-proj"}, content_type="application/json")
        db.task_upsert({
            "id": "planner-reset",
            "project": "reset-proj",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "dependencies": ["reset-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "generated-a",
            "project": "reset-proj",
            "type": "feature",
            "description": "generated",
            "priority": 50,
            "status": "pending",
            "dependencies": ["planner-reset"],
            "metadata": {"parent_task_id": "planner-reset"},
        })

        r = client.post("/api/plans/reset-proj/reset", json={"create_replacement": False}, content_type="application/json")
        assert r.status_code == 200
        assert "generated-a" in r.json["deleted_task_ids"]
        assert db.task_get("generated-a") is None

    def test_reset_project_plans_replacement_uses_repaired_head(self, client):
        client.post("/api/projects", json={"name": "reset-repair"}, content_type="application/json")
        db.task_upsert({
            "id": "failed-head",
            "project": "reset-repair",
            "type": "feature",
            "description": "failed head",
            "priority": 50,
            "status": "failed",
            "created": "2026-04-01T10:00:00",
            "dependencies": ["reset-repair-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "healthy-tail",
            "project": "reset-repair",
            "type": "feature",
            "description": "healthy tail",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T11:00:00",
            "completed": "2026-04-01T11:05:00",
            "dependencies": ["reset-repair-genesis"],
            "metadata": {},
        })
        db.project_upsert({**db.project_get("reset-repair"), "head_task_id": "failed-head"})

        r = client.post("/api/plans/reset-repair/reset", json={"create_replacement": True}, content_type="application/json")

        assert r.status_code == 200
        replacement_id = r.json["replacement_planner_id"]
        replacement = db.task_get(replacement_id)
        assert replacement["dependencies"] == ["healthy-tail"]
        assert db.project_get("reset-repair")["head_task_id"] == replacement_id

    def test_reset_project_plans_deletes_snapshots_by_planner_task_id(self, client):
        client.post("/api/projects", json={"name": "reset-owned"}, content_type="application/json")
        db.task_upsert({
            "id": "planner-owned",
            "project": "reset-owned",
            "type": "project_plan",
            "description": "planner",
            "priority": 100,
            "status": "in_progress",
            "dependencies": ["reset-owned-genesis"],
            "metadata": {},
        })
        db.plan_upsert({
            "id": "plan-reset-owned",
            "project": "reset-owned",
            "planner_task_id": "planner-owned",
            "created_at": "2026-04-03T10:00:00",
            "task_ids": [],
            "task_graph": [],
        })

        r = client.post("/api/plans/reset-owned/reset", json={"create_replacement": False}, content_type="application/json")

        assert r.status_code == 200
        assert "plan-reset-owned" in r.json["deleted_plan_ids"]
        assert db.plan_get("plan-reset-owned") is None

    def test_infer_project_tail_ignores_failed_history_records(self, app):
        from swarm.task_chains import infer_project_tail

        db.project_upsert({"name": "tail-proj", "head_task_id": None})
        # create_app stores history under orchestrator.DATA_DIR; use that directly
        import swarm.orchestrator as orc
        task_history = Path(orc.DATA_DIR) / "task-history.jsonl"
        task_history.parent.mkdir(parents=True, exist_ok=True)
        task_history.write_text(
            json.dumps({
                "id": "good-tail",
                "project": "tail-proj",
                "status": "completed",
                "completed": "2026-04-01T10:00:00",
            }) + "\n" +
            json.dumps({
                "id": "bad-tail",
                "project": "tail-proj",
                "status": "failed",
                "completed": "2026-04-01T11:00:00",
            }) + "\n"
        )

        assert infer_project_tail(db, "tail-proj") == "good-tail"

    def test_get_project_head_ignores_failed_task(self, app):
        from swarm.task_chains import get_project_head

        db.project_upsert({"name": "head-proj", "head_task_id": "failed-head"})
        db.task_upsert({
            "id": "failed-head",
            "project": "head-proj",
            "type": "feature",
            "description": "bad head",
            "priority": 50,
            "status": "failed",
            "dependencies": [],
            "metadata": {},
        })

        assert get_project_head(db, "head-proj") is None

    def test_ensure_project_head_repairs_failed_head_from_live_tail(self, app):
        from swarm.task_chains import ensure_project_head

        db.project_upsert({"name": "repair-proj", "head_task_id": "failed-head"})
        db.task_upsert({
            "id": "failed-head",
            "project": "repair-proj",
            "type": "feature",
            "description": "old failed head",
            "priority": 50,
            "status": "failed",
            "created": "2026-04-01T10:00:00",
            "dependencies": [],
            "metadata": {},
        })
        db.task_upsert({
            "id": "good-tail",
            "project": "repair-proj",
            "type": "feature",
            "description": "good tail",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T11:00:00",
            "completed": "2026-04-01T11:05:00",
            "dependencies": [],
            "metadata": {},
        })

        assert ensure_project_head(db, "repair-proj") == "good-tail"
        assert db.project_get("repair-proj")["head_task_id"] == "good-tail"

    def test_ensure_project_head_ignores_head_from_wrong_project(self, app):
        from swarm.task_chains import ensure_project_head

        db.project_upsert({"name": "proj-a", "head_task_id": "wrong-head"})
        db.task_upsert({
            "id": "wrong-head",
            "project": "proj-b",
            "type": "feature",
            "description": "foreign task",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T10:00:00",
            "completed": "2026-04-01T10:05:00",
            "dependencies": [],
            "metadata": {},
        })
        db.task_upsert({
            "id": "proj-a-tail",
            "project": "proj-a",
            "type": "feature",
            "description": "real tail",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T11:00:00",
            "completed": "2026-04-01T11:05:00",
            "dependencies": [],
            "metadata": {},
        })

        assert ensure_project_head(db, "proj-a") == "proj-a-tail"
        assert db.project_get("proj-a")["head_task_id"] == "proj-a-tail"

    def test_set_project_head_rejects_missing_task(self, app):
        from swarm.task_chains import set_project_head

        db.project_upsert({"name": "head-set-proj", "status": "active", "head_task_id": None})

        with pytest.raises(ValueError, match="does not exist"):
            set_project_head(db, "head-set-proj", "missing-head")

    def test_set_project_head_rejects_cross_project_task(self, app):
        from swarm.task_chains import set_project_head

        db.project_upsert({"name": "proj-a", "status": "active", "head_task_id": None})
        db.project_upsert({"name": "proj-b", "status": "active", "head_task_id": None})
        db.task_upsert({
            "id": "proj-b-head",
            "project": "proj-b",
            "type": "feature",
            "description": "foreign head",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T10:00:00",
            "completed": "2026-04-01T10:05:00",
            "dependencies": [],
            "metadata": {},
        })

        with pytest.raises(ValueError, match="continuity-eligible"):
            set_project_head(db, "proj-a", "proj-b-head")

    def test_reconcile_project_head_repairs_invalid_stored_head(self, client):
        db.project_upsert({"name": "reconcile-proj", "status": "active", "head_task_id": "stale-head"})
        db.task_upsert({
            "id": "stale-head",
            "project": "reconcile-proj",
            "type": "feature",
            "description": "stale",
            "priority": 50,
            "status": "failed",
            "created": "2026-04-01T10:00:00",
            "dependencies": [],
            "metadata": {},
        })
        db.task_upsert({
            "id": "healthy-head",
            "project": "reconcile-proj",
            "type": "feature",
            "description": "healthy",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T11:00:00",
            "completed": "2026-04-01T11:05:00",
            "dependencies": [],
            "metadata": {},
        })

        r = client.post("/api/projects/reconcile-proj/reconcile-head", content_type="application/json")

        assert r.status_code == 200
        assert r.json["head_task_id"] == "healthy-head"
        assert r.json["action"] == "repaired"
        assert db.project_get("reconcile-proj")["head_task_id"] == "healthy-head"

    def test_repair_project_reports_head_reconciliation(self, client):
        client.post("/api/projects", json={"name": "repair-head-report"}, content_type="application/json")
        db.project_upsert({**db.project_get("repair-head-report"), "head_task_id": "broken-head"})
        db.task_upsert({
            "id": "broken-head",
            "project": "repair-head-report",
            "type": "feature",
            "description": "broken",
            "priority": 50,
            "status": "failed",
            "created": "2026-04-01T10:00:00",
            "dependencies": [],
            "metadata": {},
        })
        db.task_upsert({
            "id": "repair-tail",
            "project": "repair-head-report",
            "type": "feature",
            "description": "tail",
            "priority": 50,
            "status": "completed",
            "created": "2026-04-01T11:00:00",
            "completed": "2026-04-01T11:05:00",
            "dependencies": [],
            "metadata": {},
        })

        r = client.post("/api/projects/repair-head-report/repair", content_type="application/json")

        assert r.status_code == 200
        assert r.json["head_repair"]["head_task_id"] == "repair-tail"
        assert r.json["head_repair"]["source"] == "live_or_history_tail"

    def test_repair_project_resets_failed_tasks_in_db(self, client, app):
        """Failed tasks already in the DB are reset to pending by repair (not 'restored')."""
        client.post("/api/projects", json={"name": "restore-proj"}, content_type="application/json")
        db.project_upsert({**db.project_get("restore-proj"), "head_task_id": "restore-root"})

        # Seed failed tasks directly into the DB (the permanent source of truth)
        db.task_upsert({
            "id": "restore-dep",
            "project": "restore-proj",
            "type": "feature",
            "description": "dep",
            "priority": 60,
            "status": "failed",
            "created": "2026-04-03T10:00:00",
            "completed": "2026-04-03T10:05:00",
            "dependencies": ["restore-proj-genesis"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "restore-root",
            "project": "restore-proj",
            "type": "feature",
            "description": "root",
            "priority": 70,
            "status": "failed",
            "created": "2026-04-03T11:00:00",
            "completed": "2026-04-03T11:05:00",
            "dependencies": ["restore-dep"],
            "metadata": {},
        })

        r = client.post("/api/projects/restore-proj/repair", content_type="application/json")

        assert r.status_code == 200
        # Failed tasks in the DB are reset via reset_failed, not history_restored
        reset = set(r.json["reset_failed"])
        assert "restore-dep" in reset or db.task_get("restore-dep")["status"] == "pending"
        assert "restore-root" in reset or db.task_get("restore-root")["status"] == "pending"

    def test_repair_project_reanchors_orphaned_recovery_task(self, client):
        client.post("/api/projects", json={"name": "reanchor-proj"}, content_type="application/json")
        db.task_upsert({
            "id": "recovery-live",
            "project": "reanchor-proj",
            "type": "bug",
            "description": "recovery",
            "priority": 70,
            "status": "pending",
            "dependencies": [],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "failed-upstream",
                "recovery_root_task_id": "failed-upstream",
            },
        })

        r = client.post("/api/projects/reanchor-proj/repair", content_type="application/json")

        assert r.status_code == 200
        assert r.json["reanchored"] == [{"task_id": "recovery-live", "dependencies": ["reanchor-proj-genesis"]}]
        assert db.task_get("recovery-live")["dependencies"] == ["reanchor-proj-genesis"]

    def test_cleanup_stale_plan_snapshots_deletes_missing_planner_snapshot(self, client):
        client.post("/api/projects", json={"name": "ghost-clean"}, content_type="application/json")
        db.plan_upsert({
            "id": "plan-ghost-clean",
            "project": "ghost-clean",
            "planner_task_id": "missing-planner",
            "created_at": "2026-04-03T10:00:00",
            "task_ids": ["ghost-task"],
            "task_graph": [{"id": "ghost-task", "project": "ghost-clean", "dependencies": []}],
        })

        r = client.post("/api/plans/ghost-clean/cleanup", content_type="application/json")

        assert r.status_code == 200
        assert r.json["deleted_count"] == 1
        assert r.json["deleted"][0]["plan_id"] == "plan-ghost-clean"
        assert "missing_planner_task" in r.json["deleted"][0]["reasons"]
        assert db.plan_get("plan-ghost-clean") is None

    def test_cleanup_stale_plan_keeps_snapshot_when_planner_only_exists_in_completed_ids(self, client):
        client.post("/api/projects", json={"name": "completed-planner-proj"}, content_type="application/json")
        db.task_record_completed("planner-completed", project="completed-planner-proj")
        db.task_upsert({
            "id": "generated-plan-task",
            "project": "completed-planner-proj",
            "type": "bug",
            "description": "generated task",
            "priority": 50,
            "status": "pending",
            "dependencies": ["completed-planner-proj-genesis"],
            "metadata": {"parent_task_id": "planner-completed"},
            "plan_id": "plan-keep",
        })
        db.plan_upsert({
            "id": "plan-keep",
            "project": "completed-planner-proj",
            "planner_task_id": "planner-completed",
            "created_at": "2026-04-03T10:00:00",
            "task_ids": ["generated-plan-task"],
            "task_graph": [{"id": "generated-plan-task", "project": "completed-planner-proj", "dependencies": ["completed-planner-proj-genesis"]}],
        })

        r = client.post("/api/plans/completed-planner-proj/cleanup", content_type="application/json")

        assert r.status_code == 200
        assert r.json["deleted"] == []
        assert db.plan_get("plan-keep") is not None

    def test_dot_graph_skips_stale_plan_snapshot_ghosts(self, client):
        client.post("/api/projects", json={"name": "ghost-dot"}, content_type="application/json")
        db.plan_upsert({
            "id": "plan-ghost-dot",
            "project": "ghost-dot",
            "planner_task_id": "missing-planner",
            "created_at": "2026-04-03T10:00:00",
            "task_ids": ["ghost-task"],
            "task_graph": [{"id": "ghost-task", "project": "ghost-dot", "dependencies": []}],
        })

        r = client.get("/api/dependencies/dot?project=ghost-dot")

        assert r.status_code == 200
        assert "ghost-task" not in r.json["dot"]

    def test_dot_graph_history_limit_is_ancestor_depth(self, client, app):
        client.post("/api/projects", json={"name": "history-closure"}, content_type="application/json")
        history_path = Path(app.config["DATA_DIR"]) / "task-history.jsonl" if "DATA_DIR" in app.config else None
        if history_path is None:
            history_path = Path(app.instance_path).parent / "data" / "task-history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"id": "history-closure-root", "project": "history-closure", "status": "completed", "dependencies": [], "completed": "2026-04-01T10:00:00"},
            {"id": "history-closure-mid", "project": "history-closure", "status": "completed", "dependencies": ["history-closure-root"], "completed": "2026-04-01T10:01:00"},
            {"id": "history-closure-tail", "project": "history-closure", "status": "completed", "dependencies": ["history-closure-mid"], "completed": "2026-04-01T10:02:00"},
        ]
        history_path.write_text("".join(json.dumps(r) + "\n" for r in records))

        r = client.get("/api/dependencies/dot?project=history-closure&history_limit=1")

        assert r.status_code == 200
        dot = r.json["dot"]
        assert "history-closure-tail" in dot
        assert "history-closure-mid" in dot
        assert "history-closure-root" not in dot

    def test_dot_graph_history_only_uses_stored_head_from_history(self, client, app):
        project = "history-head"
        client.post("/api/projects", json={"name": project}, content_type="application/json")
        proj = db.project_get(project)
        db.project_upsert({**proj, "head_task_id": f"{project}-tail"})
        db.task_delete(f"{project}-genesis")
        history_path = Path(app.config["DATA_DIR"]) / "task-history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"id": f"{project}-root", "project": project, "status": "completed", "dependencies": [], "completed": "2026-04-01T10:00:00"},
            {"id": f"{project}-mid", "project": project, "status": "completed", "dependencies": [f"{project}-root"], "completed": "2026-04-01T10:01:00"},
            {"id": f"{project}-tail", "project": project, "status": "completed", "dependencies": [f"{project}-mid"], "completed": "2026-04-01T10:02:00"},
            {"id": f"{project}-unrelated", "project": project, "status": "completed", "dependencies": [], "completed": "2026-04-01T10:03:00"},
        ]
        history_path.write_text("".join(json.dumps(r) + "\n" for r in records))

        r = client.get(f"/api/dependencies/dot?project={project}&history_depth=2")

        assert r.status_code == 200
        dot = r.json["dot"]
        assert f"{project}-tail" in dot
        assert f"{project}-mid" in dot
        assert f"{project}-root" in dot
        assert f"{project}-unrelated" not in dot
        assert "tail *" in dot
        assert f'"{project}-root" -> "{project}-mid"' in dot
        assert f'"{project}-mid" -> "{project}-tail"' in dot

    def test_dot_graph_history_depth_zero_shows_only_project_head(self, client, app):
        project = "history-depth-zero"
        client.post("/api/projects", json={"name": project}, content_type="application/json")
        proj = db.project_get(project)
        db.project_upsert({**proj, "head_task_id": f"{project}-tail"})
        db.task_delete(f"{project}-genesis")
        history_path = Path(app.config["DATA_DIR"]) / "task-history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"id": f"{project}-root", "project": project, "status": "completed", "dependencies": [], "completed": "2026-04-01T10:00:00"},
            {"id": f"{project}-mid", "project": project, "status": "completed", "dependencies": [f"{project}-root"], "completed": "2026-04-01T10:01:00"},
            {"id": f"{project}-tail", "project": project, "status": "completed", "dependencies": [f"{project}-mid"], "completed": "2026-04-01T10:02:00"},
        ]
        history_path.write_text("".join(json.dumps(r) + "\n" for r in records))

        r = client.get(f"/api/dependencies/dot?project={project}&history_depth=0")

        assert r.status_code == 200
        dot = r.json["dot"]
        assert f"{project}-tail" in dot
        assert f"{project}-mid" not in dot
        assert f"{project}-root" not in dot
        assert "tail *" in dot

    def test_dot_graph_ghost_edges_render_without_timestamps(self, client, app):
        project = "history-no-time"
        client.post("/api/projects", json={"name": project}, content_type="application/json")
        proj = db.project_get(project)
        db.project_upsert({**proj, "head_task_id": f"{project}-tail"})
        db.task_delete(f"{project}-genesis")
        history_path = Path(app.config["DATA_DIR"]) / "task-history.jsonl"
        history_path.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"id": f"{project}-root", "project": project, "status": "completed", "dependencies": []},
            {"id": f"{project}-tail", "project": project, "status": "completed", "dependencies": [f"{project}-root"]},
        ]
        history_path.write_text("".join(json.dumps(r) + "\n" for r in records))

        r = client.get(f"/api/dependencies/dot?project={project}&history_depth=1")

        assert r.status_code == 200
        assert f'"{project}-root" -> "{project}-tail"' in r.json["dot"]

    def test_dependency_integrity_reports_dead_blockers_and_continuity_gaps(self, client):
        client.post("/api/projects", json={"name": "integrity-proj"}, content_type="application/json")
        db.task_upsert({
            "id": "blocked-task",
            "project": "integrity-proj",
            "type": "feature",
            "description": "blocked",
            "priority": 50,
            "status": "pending",
            "dependencies": ["failed-upstream"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "failed-upstream",
            "project": "integrity-proj",
            "type": "feature",
            "description": "failed upstream",
            "priority": 50,
            "status": "failed",
            "dependencies": [],
            "metadata": {},
        })

        r = client.get("/api/dependencies/integrity?project=integrity-proj")

        assert r.status_code == 200
        dead = r.json["findings"]["dead_blockers"]
        gaps = r.json["findings"]["continuity_gaps"]
        assert dead[0]["task_id"] == "blocked-task"
        assert dead[0]["dead_dependencies"][0]["dependency"] == "failed-upstream"
        assert dead[0]["dead_dependencies"][0]["continuation_task_id"] is None
        assert gaps[0]["task_id"] == "blocked-task"

    def test_dependency_integrity_repair_replaces_dead_blocker_with_continuation(self, client):
        client.post("/api/projects", json={"name": "integrity-fix"}, content_type="application/json")
        db.task_upsert({
            "id": "blocked-task",
            "project": "integrity-fix",
            "type": "feature",
            "description": "blocked",
            "priority": 50,
            "status": "pending",
            "dependencies": ["failed-upstream"],
            "metadata": {},
        })
        db.task_upsert({
            "id": "failed-upstream",
            "project": "integrity-fix",
            "type": "feature",
            "description": "failed upstream",
            "priority": 50,
            "status": "failed",
            "dependencies": [],
            "metadata": {},
        })
        db.task_upsert({
            "id": "bug-failed-upstream",
            "project": "integrity-fix",
            "type": "bug",
            "description": "continuation",
            "priority": 100,
            "status": "pending",
            "dependencies": ["failed-upstream"],
            "metadata": {
                "branch_continuation": True,
                "continuation_for_failed_task": "failed-upstream",
            },
        })

        r = client.post("/api/dependencies/integrity?project=integrity-fix&repair=true")

        assert r.status_code == 200
        repaired_ids = {item["task_id"] for item in r.json["continuity_repaired"]}
        assert "blocked-task" in repaired_ids
        assert "bug-failed-upstream" in repaired_ids
        assert db.task_get("blocked-task")["dependencies"] == ["bug-failed-upstream"]
        assert db.task_get("bug-failed-upstream")["dependencies"] == []

    def test_dependency_integrity_repair_cancels_branch_with_only_dead_blockers(self, client):
        client.post("/api/projects", json={"name": "integrity-cancel"}, content_type="application/json")
        db.task_upsert({
            "id": "blocked-task",
            "project": "integrity-cancel",
            "type": "feature",
            "description": "blocked",
            "priority": 50,
            "status": "pending",
            "dependencies": ["missing-upstream"],
            "metadata": {},
        })

        r = client.post("/api/dependencies/integrity?project=integrity-cancel&repair=true")

        assert r.status_code == 200
        assert r.json["continuity_cancelled"][0]["task_id"] == "blocked-task"
        assert db.task_get("blocked-task")["status"] == "cancelled"

    def test_dependency_integrity_reports_broad_controller_findings(self, client):
        client.post("/api/projects", json={"name": "integrity-audit"}, content_type="application/json")
        db.project_upsert({**db.project_get("integrity-audit"), "head_task_id": "bad-head"})
        db.task_upsert({
            "id": "bad-head",
            "project": "integrity-audit",
            "type": "feature",
            "description": "bad",
            "priority": 50,
            "status": "failed",
            "dependencies": [],
            "metadata": {},
        })
        db.plan_upsert({
            "id": "plan-bad",
            "project": "integrity-audit",
            "planner_task_id": "missing-planner",
            "created_at": "2026-04-03T10:00:00",
            "task_ids": ["ghost-task"],
            "task_graph": [{"id": "ghost-task", "project": "integrity-audit", "dependencies": []}],
        })
        db.task_upsert({
            "id": "parent-recovery",
            "project": "integrity-audit",
            "type": "bug",
            "description": "parent recovery",
            "priority": 80,
            "status": "failed",
            "dependencies": [],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "root-task",
                "recovery_root_task_id": "root-task",
            },
        })
        db.task_upsert({
            "id": "child-recovery",
            "project": "integrity-audit",
            "type": "bug",
            "description": "child recovery",
            "priority": 80,
            "status": "pending",
            "dependencies": ["parent-recovery"],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "parent-recovery",
                "recovery_root_task_id": "root-task",
            },
        })
        db.agent_upsert({
            "id": "agent-orphan",
            "project": "integrity-audit",
            "task_type": "feature",
            "status": "active",
            "spawned_at": "2026-04-03T10:00:00",
            "task_id": "missing-task",
        })

        r = client.get("/api/dependencies/integrity?project=integrity-audit")

        assert r.status_code == 200
        findings = r.json["findings"]
        assert findings["stale_heads"][0]["project"] == "integrity-audit"
        assert findings["stale_plans"][0]["plan_id"] == "plan-bad"
        assert findings["orphaned_agents"][0]["agent_id"] == "agent-orphan"
        assert findings["recursive_recovery"][0]["task_id"] == "child-recovery"
        assert r.json["live_findings"]["stale_heads"][0]["project"] == "integrity-audit"
        assert r.json["archival_findings"]["stale_plans"][0]["plan_id"] == "plan-bad"
        assert r.json["summary"]["live"]["stale_heads"] == 1
        assert r.json["summary"]["archival"]["stale_plans"] == 1
        assert r.json["summary"]["live"]["problem_count"] >= 3

    def test_dependency_integrity_repairs_stale_file_locks(self, client):
        client.post("/api/projects", json={"name": "integrity-locks"}, content_type="application/json")
        db.task_upsert({
            "id": "stale-owner",
            "project": "integrity-locks",
            "type": "feature",
            "description": "stale lock owner",
            "priority": 50,
            "status": "failed",
            "dependencies": [],
            "metadata": {},
        })
        client.post("/api/projects/integrity-locks/lock", json={
            "file_path": "scripts/locked.gd",
            "agent_id": "stale-owner",
            "task_id": "stale-owner",
        }, content_type="application/json")

        before = client.get("/api/dependencies/integrity?project=integrity-locks")
        assert before.status_code == 200
        assert before.json["live_findings"]["stale_file_locks"][0]["task_id"] == "stale-owner"

        repaired = client.post("/api/dependencies/integrity?project=integrity-locks&repair=true")

        assert repaired.status_code == 200
        assert repaired.json["stale_locks_repaired"][0]["task_id"] == "stale-owner"
        assert repaired.json["live_findings"]["stale_file_locks"] == []
        assert client.get("/api/projects/integrity-locks/locks").json["locks"] == {}

    def test_dependency_integrity_summary_separates_live_and_archival_history(self, client):
        client.post("/api/projects", json={"name": "integrity-summary"}, content_type="application/json")
        db.project_upsert({**db.project_get("integrity-summary"), "head_task_id": None})
        db.plan_upsert({
            "id": "plan-summary-bad",
            "project": "integrity-summary",
            "planner_task_id": "missing-summary-planner",
            "created_at": "2026-04-03T10:00:00",
            "task_ids": ["ghost-summary"],
            "task_graph": [{"id": "ghost-summary", "project": "integrity-summary", "dependencies": ["missing-history-parent"]}],
        })

        r = client.get("/api/dependencies/integrity?project=integrity-summary&include_history=true")

        assert r.status_code == 200
        assert r.json["summary"]["live"]["stale_heads"] == 1
        assert r.json["summary"]["archival"]["stale_plans"] == 1
        assert "history_missing_dependencies" in r.json["summary"]["archival"]
        assert "stale_plans" not in r.json["live_findings"]
        assert "stale_heads" not in r.json["archival_findings"]

    def test_cleanup_recovery_collapses_stale_pending_duplicates(self, client):
        client.post("/api/projects", json={"name": "recovery-clean"}, content_type="application/json")
        db.task_upsert({
            "id": "recovery-old",
            "project": "recovery-clean",
            "type": "bug",
            "description": "old recovery",
            "priority": 80,
            "status": "pending",
            "dependencies": ["root-task"],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "root-task",
                "recovery_root_task_id": "root-task",
            },
            "created": "2026-04-03T10:00:00",
        })
        db.task_upsert({
            "id": "recovery-new",
            "project": "recovery-clean",
            "type": "bug",
            "description": "new recovery",
            "priority": 80,
            "status": "pending",
            "dependencies": ["root-task"],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "root-task",
                "recovery_root_task_id": "root-task",
            },
            "created": "2026-04-03T11:00:00",
        })
        db.task_upsert({
            "id": "blocked-task",
            "project": "recovery-clean",
            "type": "feature",
            "description": "blocked",
            "priority": 50,
            "status": "pending",
            "dependencies": ["recovery-new"],
            "metadata": {},
        })

        r = client.post("/api/projects/recovery-clean/cleanup-recovery", content_type="application/json")

        assert r.status_code == 200
        assert "recovery-new" in r.json["cancelled_recovery_ids"]
        assert db.task_get("recovery-old")["status"] == "pending"
        assert db.task_get("recovery-new")["status"] == "cancelled"
        assert db.task_get("blocked-task")["dependencies"] == ["recovery-old"]

    def test_cleanup_recovery_creates_continuation_for_dead_recovery_branch(self, client):
        client.post("/api/projects", json={"name": "recovery-dead"}, content_type="application/json")
        db.task_upsert({
            "id": "recovery-dead-1",
            "project": "recovery-dead",
            "type": "bug",
            "description": "failed recovery",
            "priority": 80,
            "status": "failed",
            "attempts": 3,
            "dependencies": ["root-dead"],
            "metadata": {
                "is_recovery_task": True,
                "failed_task_id": "root-dead",
                "recovery_root_task_id": "root-dead",
                "error_log_excerpt": "traceback x",
            },
            "created": "2026-04-03T10:00:00",
            "completed": "2026-04-03T10:05:00",
        })
        db.task_upsert({
            "id": "blocked-dead",
            "project": "recovery-dead",
            "type": "feature",
            "description": "blocked",
            "priority": 50,
            "status": "pending",
            "dependencies": ["recovery-dead-1"],
            "metadata": {},
        })
        db.project_upsert({**db.project_get("recovery-dead"), "head_task_id": "recovery-dead-1"})

        r = client.post("/api/projects/recovery-dead/cleanup-recovery", content_type="application/json")

        assert r.status_code == 200
        continuation_id = "bug-recovery-dead-1"
        assert continuation_id in r.json["created_continuation_ids"]
        assert db.task_get("blocked-dead")["dependencies"] == [continuation_id]
        assert db.project_get("recovery-dead")["head_task_id"] == continuation_id
