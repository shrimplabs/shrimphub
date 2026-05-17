# Release Checklist

Run this checklist before publishing or merging a release branch.

Use this together with:

- `docs/open_source_checklist.md`
- `docs/windows_compatibility_findings.md`

## Source Hygiene

- `git status --short` shows only intentional changes.
- The release branch/worktree is clean before tagging or publishing.
- `git ls-files` does not include local runtime data such as `.env`, `config.json`,
  `data/` logs or databases, `.claude/`, `.venv/`, `.pytest_cache/`,
  `.ruff_cache/`, or `workspace/`.
- `config.example.json` contains only safe placeholders and keeps
  `disable_remote_repo` enabled by default.
- Public docs do not require private machines, private hostnames, or a configured
  Dolt remote.
- Representative internal project names appear only in explicitly labeled
  fixture/example docs or seed data, not in generic product branding.

## Project Creation

- Local-only project creation works without configuring any remote repo host.
- If remote repo provisioning is enabled, the docs label it as optional and
  provider-specific.
- Godot templates include:
  - `templates/godot/autoload/state_server.gd`
  - `templates/godot/autoload/test_harness.gd`
  - `templates/godot/check_scripts.gd`
  - `templates/godot/icon.svg`
  - `templates/godot/test/unit/test_placeholder.gd`
- Godot bootstrap uses the cached external GUT installer in
  `swarm/godot_bootstrap.py` instead of a vendored addon tree.
- New Godot projects receive `icon.svg` and `project.godot` references
  `config/icon="res://icon.svg"`.

## Documentation Shape

- `README.md`, `CLAUDE.md`, and `AGENT_KNOWLEDGE.md` all reflect the current runtime defaults and support matrix.
- Operator-only AI-agent helper docs live under `docs/agent-ops/`.
- Stale planning/session memory files are not tracked at repo root.
- Current optional RAG setup is documented in `docs/rag.md`.
- Quick-start docs describe a local-first path that works without private
  infrastructure.
- Manual Godot bootstrap docs clearly distinguish imported/manual projects from
  new projects created through the dashboard/chat flow.
- Agent helper docs use current endpoints, current defaults, and platform-safe command examples.

## Validation

Run:

```bash
python -m pytest tests/test_release_hygiene.py -q
python -m pytest tests/test_api.py -k "bootstraps_godot_support_files" -q
```

And verify the public onboarding artifacts manually:

```bash
python3 -m json.tool config.example.json >/dev/null
rg -n "localhost:3000|example-user|example-token" README.md config.example.json docs/new_project_setup.md
```

For broader confidence before merging:

```bash
python -m pytest tests/test_release_hygiene.py tests/test_db.py tests/test_api.py tests/test_project_graph_policy.py tests/test_concurrency_dependencies.py tests/test_improvements.py tests/test_lifecycle.py tests/test_orchestrator.py tests/test_chat_actions.py tests/test_code_nav_tools.py tests/test_web_search_fetch.py tests/test_login.py tests/test_login_api.py -q
```
