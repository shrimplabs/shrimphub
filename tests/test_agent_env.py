"""Unit tests for agent_lifecycle.build_agent_env -- the allowlisted subprocess env."""
from unittest.mock import patch

import pytest

from swarm.agent_lifecycle import (
    AGENT_ENV_DENY_ALWAYS,
    build_agent_env,
    routed_provider_names,
)


# A controller environment that holds every key the swarm has ever used.
FULL_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/Users/tester",
    "LANG": "en_US.UTF-8",
    "TMPDIR": "/tmp",
    "USER": "tester",
    "SHELL": "/bin/zsh",
    "GODOT_PATH": "/opt/homebrew/bin/godot",
    "SWARM_GUT_SOURCE_DIR": "/tmp/gut",
    "PIPELINE_MAX_REPAIR_ATTEMPTS": "2",
    "SSH_AUTH_SOCK": "/tmp/ssh.sock",
    # LLM keys
    "MINIMAX_API_KEY": "mm-secret",
    "MINIMAX_GROUP_ID": "grp-1",
    "ANTHROPIC_API_KEY": "anth-secret",
    "OPENROUTER_API_KEY": "or-secret",
    "KIMI_API_KEY": "kimi-secret",
    "OPENAI_API_KEY": "oai-secret",
    "MODEL_ROUTER_API_KEY": "mr-secret",
    "LOCAL_API_KEY": "local-secret",
    "OPENCODE_API_KEY": "oc-secret",
    # Search keys -- must never reach an agent
    "BRAVE_API_KEY": "brave-secret",
    "TAVILY_API_KEY": "tavily-secret",
    "SERPER_API_KEY": "serper-secret",
    # Unrelated junk that must not be forwarded
    "AWS_SECRET_ACCESS_KEY": "aws-secret",
    "SOME_RANDOM_VAR": "x",
}

ALL_KEYS = {k for k in FULL_ENV if k.endswith("_API_KEY")}


def _task(project="raccoon-city", ttype="feature", metadata=None):
    return {"id": "t-1", "project": project, "type": ttype, "metadata": metadata or {}}


class TestBaseVars:
    def test_base_vars_present_and_junk_absent(self):
        env = build_agent_env(_task(), {"llm_provider": "minimax"}, FULL_ENV)
        for v in ("PATH", "HOME", "LANG", "TMPDIR", "USER", "SHELL", "GODOT_PATH"):
            assert env[v] == FULL_ENV[v]
        assert env["SWARM_GUT_SOURCE_DIR"] == "/tmp/gut"           # SWARM_* prefix
        assert env["PIPELINE_MAX_REPAIR_ATTEMPTS"] == "2"          # PIPELINE_* prefix
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "SSH_AUTH_SOCK" not in env  # operator ssh-agent never forwarded
        assert "SOME_RANDOM_VAR" not in env
        assert env["PYTHONIOENCODING"] == "utf-8"
        assert env["SWARM_CONTROLLER_PATH"]

    def test_pure_function_does_not_mutate_inputs(self):
        base = dict(FULL_ENV)
        cfg = {"llm_provider": "minimax"}
        build_agent_env(_task(), cfg, base)
        assert base == FULL_ENV
        assert cfg == {"llm_provider": "minimax"}


class TestProviderKeys:
    def test_only_active_provider_key_present(self):
        env = build_agent_env(_task(), {"llm_provider": "kimi"}, FULL_ENV)
        assert env["KIMI_API_KEY"] == "kimi-secret"
        assert (ALL_KEYS & set(env)) == {"KIMI_API_KEY"}

    def test_minimax_brings_group_id_companion(self):
        env = build_agent_env(_task(), {"llm_provider": "minimax"}, FULL_ENV)
        assert env["MINIMAX_API_KEY"] == "mm-secret"
        assert env["MINIMAX_GROUP_ID"] == "grp-1"
        assert "ANTHROPIC_API_KEY" not in env

    def test_default_provider_is_minimax_when_unset(self):
        env = build_agent_env(_task(), {}, FULL_ENV)
        assert "MINIMAX_API_KEY" in env
        assert (ALL_KEYS & set(env)) == {"MINIMAX_API_KEY"}

    def test_search_keys_never_included(self):
        cfg = {
            "llm_provider": "minimax",
            "fallback_providers": ["claude", "kimi", "openrouter"],
            "fast_provider": "claude", "strong_provider": "kimi",
        }
        env = build_agent_env(_task(ttype="qa"), cfg, FULL_ENV)
        for k in AGENT_ENV_DENY_ALWAYS:
            assert k not in env
        assert "BRAVE_API_KEY" not in env
        assert "TAVILY_API_KEY" not in env
        assert "SERPER_API_KEY" not in env

    def test_fallback_and_phase_providers_included(self):
        cfg = {
            "llm_provider": "minimax",
            "fallback_providers": ["claude"],
            "fast_provider": "kimi",
            "plan_provider": "openrouter",
        }
        env = build_agent_env(_task(), cfg, FULL_ENV)
        assert (ALL_KEYS & set(env)) == {
            "MINIMAX_API_KEY", "ANTHROPIC_API_KEY", "KIMI_API_KEY", "OPENROUTER_API_KEY",
        }

    def test_loop_model_routing_and_meta_investigation_included(self):
        cfg = {
            "llm_provider": "minimax",
            "loop_model_routing": {"fast_provider": "kimi", "strong_provider": "minimax"},
            "meta_investigation_provider": "claude",
        }
        env = build_agent_env(_task(), cfg, FULL_ENV)
        assert (ALL_KEYS & set(env)) == {"MINIMAX_API_KEY", "KIMI_API_KEY", "ANTHROPIC_API_KEY"}

    def test_custom_provider_api_key_env_from_llm_providers(self):
        cfg = {
            "llm_provider": "athena",
            "llm_providers": {
                "athena": {"base_url": "http://gpu-box.local:8083/v1", "api_key_env": "LOCAL_API_KEY"},
            },
        }
        env = build_agent_env(_task(), cfg, FULL_ENV)
        assert (ALL_KEYS & set(env)) == {"LOCAL_API_KEY"}
        assert "MINIMAX_API_KEY" not in env

    def test_keyless_provider_forwards_no_key(self):
        cfg = {
            "llm_provider": "local",
            "llm_providers": {"local": {"base_url": "http://localhost:10098/v1", "api_key_env": ""}},
        }
        env = build_agent_env(_task(), cfg, FULL_ENV)
        assert not (ALL_KEYS & set(env))

    def test_project_pipelines_override_respected(self):
        cfg = {
            "llm_provider": "minimax",
            "project_pipelines": {
                "raccoon-city": {"_work_provider": "kimi", "_flat_provider": "claude"},
                "other-project": {"_work_provider": "openrouter"},
            },
        }
        env = build_agent_env(_task(project="raccoon-city"), cfg, FULL_ENV)
        assert (ALL_KEYS & set(env)) == {"MINIMAX_API_KEY", "KIMI_API_KEY", "ANTHROPIC_API_KEY"}
        # a different project's override must not leak in
        assert "OPENROUTER_API_KEY" not in env

    def test_project_pipelines_nested_routing_and_experiment(self):
        cfg = {
            "llm_provider": "minimax",
            "project_pipelines": {
                "p": {
                    "_loop_model_routing": {"fast_provider": "kimi"},
                    "_experiment": {"flat_provider": "claude"},
                },
            },
        }
        names = routed_provider_names(_task(project="p"), cfg)
        assert {"minimax", "kimi", "claude"} <= names

    def test_task_metadata_flat_provider_respected(self):
        env = build_agent_env(
            _task(metadata={"flat_provider": "openrouter"}), {"llm_provider": "minimax"}, FULL_ENV
        )
        assert "OPENROUTER_API_KEY" in env


class TestVisionKeys:
    VISION_CFG = {
        "llm_provider": "minimax",
        "vision_provider": "claude-vision",
        "vision_provider_fast": "local-fast",
        "vision_providers": {
            "claude-vision": {"format": "anthropic", "api_key_env": "ANTHROPIC_API_KEY"},
            "local-fast": {"format": "openai", "base_url": "http://localhost:8090/v1", "api_key_env": ""},
        },
    }

    def test_vision_keys_included_for_qa_task(self):
        env = build_agent_env(_task(ttype="qa"), self.VISION_CFG, FULL_ENV)
        assert "ANTHROPIC_API_KEY" in env
        assert "MINIMAX_API_KEY" in env

    def test_vision_keys_included_for_art_pass(self):
        env = build_agent_env(_task(ttype="art_pass"), self.VISION_CFG, FULL_ENV)
        assert "ANTHROPIC_API_KEY" in env

    def test_vision_keys_excluded_for_non_vision_task(self):
        env = build_agent_env(_task(ttype="feature"), self.VISION_CFG, FULL_ENV)
        assert "ANTHROPIC_API_KEY" not in env

    def test_unknown_vision_provider_falls_back_to_anthropic_default(self):
        cfg = {"llm_provider": "minimax", "vision_provider": "minimax-mcp", "vision_providers": {}}
        env = build_agent_env(_task(ttype="qa"), cfg, FULL_ENV)
        assert "ANTHROPIC_API_KEY" in env  # mirrors swarm/vision.py default


class TestFetchAllowlistEnv:
    def test_fetch_hosts_env_set_from_config(self):
        cfg = {"llm_provider": "minimax", "fetch_url_allowed_hosts": ["Docs.Example.org", " github.com "]}
        env = build_agent_env(_task(), cfg, FULL_ENV)
        assert env["SWARM_FETCH_ALLOWED_HOSTS"] == "docs.example.org,github.com"

    def test_fetch_hosts_env_absent_when_not_configured(self):
        env = build_agent_env(_task(), {"llm_provider": "minimax"}, FULL_ENV)
        assert "SWARM_FETCH_ALLOWED_HOSTS" not in env


class TestSpawnUsesAllowlist:
    def test_spawn_agent_passes_allowlisted_env_to_popen(self, tmp_path):
        """spawn_agent must hand Popen the build_agent_env() result, not os.environ."""
        import threading
        import swarm.agent_lifecycle as lifecycle
        from swarm import db

        db._db_path = None
        db._initialized = False
        db._local = threading.local()
        db.init(tmp_path / "swarm.db")
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        workspace = tmp_path / "ws"
        (workspace / "proj").mkdir(parents=True)
        lifecycle.configure(
            workspace=workspace, data_dir=data_dir, use_worktrees=False,
            config={"llm_provider": "kimi"},
        )
        from swarm.events import bus
        bus.reset_for_tests()
        with lifecycle._handle_lock:
            lifecycle._active_handles.clear()

        task = {"id": "task-env-1", "project": "proj", "type": "feature",
                "description": "d", "priority": 50, "status": "pending", "metadata": {}}
        db.task_upsert(task)

        captured = {}

        class _FakeProc:
            pid = 4242
            def wait(self):
                return 0
            def poll(self):
                return 0
            def kill(self):
                pass

        def _fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        with patch.dict("os.environ", {**FULL_ENV, "SWARM_AGENT_TOKEN": "inherited-token-must-not-leak"}, clear=False), \
             patch("swarm.agent_lifecycle.subprocess.Popen", side_effect=_fake_popen):
            agent_id = lifecycle.spawn_agent(task, lambda t: "import sys; sys.exit(0)\n")

        assert agent_id
        env = captured["env"]
        # Per-spawn agent token: minted, persisted, handed to Popen, not inherited.
        token = env["SWARM_AGENT_TOKEN"]
        assert token and len(token) >= 32
        assert token != "inherited-token-must-not-leak"
        row = db.agent_token_lookup(token)
        assert row and row["agent_id"] == agent_id and row["project"] == "proj" and row["task_id"] == "task-env-1"
        assert env["KIMI_API_KEY"] == "kimi-secret"
        assert "MINIMAX_API_KEY" not in env
        assert "BRAVE_API_KEY" not in env
        assert "TAVILY_API_KEY" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "SSH_AUTH_SOCK" not in env  # operator ssh-agent never forwarded
        assert "PATH" in env

        with lifecycle._handle_lock:
            lifecycle._active_handles.clear()
        conn = getattr(db._local, "conn", None)
        if conn:
            conn.close()
            db._local.conn = None


class TestAgentTokenLifecycle:
    """spawn_agent mints SWARM_AGENT_TOKEN (Popen mocked -- nothing real runs)
    and finishing / leaving 'active' revokes it."""

    def _spawn(self, tmp_path):
        import threading
        import swarm.agent_lifecycle as lifecycle
        from swarm import db

        db._db_path = None
        db._initialized = False
        db._local = threading.local()
        db.init(tmp_path / "swarm.db")
        data_dir = tmp_path / "data"
        data_dir.mkdir(exist_ok=True)
        workspace = tmp_path / "ws"
        (workspace / "proj").mkdir(parents=True, exist_ok=True)
        lifecycle.configure(workspace=workspace, data_dir=data_dir, use_worktrees=False,
                            config={"llm_provider": "minimax"})
        from swarm.events import bus
        bus.reset_for_tests()
        with lifecycle._handle_lock:
            lifecycle._active_handles.clear()
        task = {"id": "task-tok-1", "project": "proj", "type": "feature",
                "description": "d", "priority": 50, "status": "pending", "metadata": {}}
        db.task_upsert(task)

        captured = {}

        class _FakeProc:
            pid = 4343
            def wait(self):
                return 0
            def poll(self):
                return 0
            def kill(self):
                pass

        def _fake_popen(*args, **kwargs):
            captured.update(kwargs)
            return _FakeProc()

        with patch.dict("os.environ", FULL_ENV, clear=False), \
             patch("swarm.agent_lifecycle.subprocess.Popen", side_effect=_fake_popen):
            agent_id = lifecycle.spawn_agent(task, lambda t: "import sys; sys.exit(0)\n")
        assert agent_id
        return lifecycle, db, agent_id, captured["env"]["SWARM_AGENT_TOKEN"]

    def _cleanup(self, lifecycle, db):
        with lifecycle._handle_lock:
            lifecycle._active_handles.clear()
        conn = getattr(db._local, "conn", None)
        if conn:
            conn.close()
            db._local.conn = None

    def test_build_agent_env_extra_and_never_inherit(self):
        base = {**FULL_ENV, "SWARM_AGENT_TOKEN": "leaked", "SWARM_OPERATOR_TOKEN": "op"}
        env = build_agent_env(_task(), {"llm_provider": "minimax"}, base)
        assert "SWARM_AGENT_TOKEN" not in env
        assert "SWARM_OPERATOR_TOKEN" not in env
        env = build_agent_env(_task(), {"llm_provider": "minimax"}, base, extra={"SWARM_AGENT_TOKEN": "minted"})
        assert env["SWARM_AGENT_TOKEN"] == "minted"

    def test_spawn_mints_token_and_claim_finish_revokes(self, tmp_path):
        lifecycle, db, agent_id, token = self._spawn(tmp_path)
        try:
            assert db.agent_token_lookup(token)["agent_id"] == agent_id
            # claim_finish is the single entry point for every teardown path
            handle = lifecycle.claim_finish(agent_id)
            assert handle is not None
            assert db.agent_token_lookup(token) is None
            assert db.agent_token_get(token)["revoked_at"]
        finally:
            with lifecycle._finishing_lock:
                lifecycle._finishing_agents.discard(agent_id)
            self._cleanup(lifecycle, db)

    def test_status_transition_revokes(self, tmp_path):
        lifecycle, db, agent_id, token = self._spawn(tmp_path)
        try:
            db.agent_update_status(agent_id, "failed", exit_code=-9)
            assert db.agent_token_lookup(token) is None
            # re-issuing for a new agent is independent
            db.agent_token_create("fresh", "other-agent", "t", "proj")
            assert db.agent_token_lookup("fresh")["agent_id"] == "other-agent"
        finally:
            self._cleanup(lifecycle, db)

    def test_failed_popen_revokes_token(self, tmp_path):
        import threading
        import swarm.agent_lifecycle as lifecycle
        from swarm import db
        db._db_path = None
        db._initialized = False
        db._local = threading.local()
        db.init(tmp_path / "swarm.db")
        (tmp_path / "data").mkdir(exist_ok=True)
        (tmp_path / "ws" / "proj").mkdir(parents=True, exist_ok=True)
        lifecycle.configure(workspace=tmp_path / "ws", data_dir=tmp_path / "data", use_worktrees=False,
                            config={"llm_provider": "minimax"})
        db.task_upsert({"id": "task-tok-2", "project": "proj", "type": "feature",
                        "description": "d", "priority": 50, "status": "pending", "metadata": {}})
        with patch.dict("os.environ", FULL_ENV, clear=False), \
             patch("swarm.agent_lifecycle.subprocess.Popen", side_effect=OSError("no exe")):
            assert lifecycle.spawn_agent({"id": "task-tok-2", "project": "proj", "type": "feature",
                                          "description": "d", "priority": 50, "metadata": {}},
                                         lambda t: "x") is None
        live = [r for r in db._connect().execute("SELECT * FROM agent_tokens WHERE revoked_at IS NULL")]
        assert live == []
        self._cleanup(lifecycle, db)
