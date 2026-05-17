# Open-Source Checklist

Concrete OSS-prep checklist for `swarm-controller`.

This document is the working checklist for release readiness. Use it alongside:

- `docs/open_source_readiness_findings.md`
- `docs/release_checklist.md`
- `docs/windows_compatibility_findings.md`

## Must Fix Before Public

- [ ] Cut the release from a clean git worktree.
- [ ] Classify all active local changes as release-bound, experimental, or abandoned.
- [ ] Remove or clearly gate internal deployment assumptions.
- [ ] Keep remote repo provisioning documented as optional, not required.
- [ ] Scrub release-facing internal hostnames, local paths, org names, and project codenames.
- [ ] Keep the README local-first and runnable without private infrastructure.
- [ ] Make the platform support contract explicit:
  - macOS: full core controller support
  - Linux: core controller/runtime support
  - Windows: core controller/runtime support, limited GUI QA parity
- [ ] Keep security defaults obvious:
  - API is unauthenticated by default
  - auth must be enabled before exposing the server outside localhost or a trusted LAN
- [ ] Make canonical architecture paths easy for a new contributor to find:
  - `swarm/api.py`
  - `swarm/db.py`
  - `swarm/orchestrator.py`
  - `swarm/agent_lifecycle.py`
  - `swarm/agent_runtime.py`
  - `swarm/tools/core.py`
  - `swarm/validation.py`
  - `swarm/closure/*`
- [ ] Reconcile human-facing and agent-facing docs with the current runtime behavior.

## Nice To Fix Before Public

- [ ] Reduce ambiguity between legacy file-backed abstractions and the active SQLite-backed path.
- [ ] Explain the current module-global config injection flow between:
  - `swarm/api.py`
  - `swarm_runner.py`
  - `swarm/orchestrator.py`
  - `swarm/agent_runtime.py`
- [ ] Separate optional integrations from core product docs:
  - MCP
  - RAG
  - repo provisioning
  - wizard/chat convenience flows
- [ ] Add a short contributor architecture guide that traces one task from API creation to validation.
- [ ] Add or keep smoke-test instructions for fresh startup on macOS, Linux, and Windows.

## Can Remain As Known Debt

- [ ] Compatibility shims that are stable and clearly documented.
- [ ] macOS-only GUI QA helper asymmetry, as long as unsupported paths fail clearly.
- [ ] Representative example projects in test fixtures or explicitly labeled docs.
- [ ] Internal complexity in the closure/verification subsystem, as long as product docs describe it honestly.

## Documentation Audit

Before release, verify these surfaces together:

- [ ] `README.md` matches the current runtime and support matrix.
- [ ] `CLAUDE.md` matches the current architecture, defaults, and contributor workflow.
- [ ] `AGENT_KNOWLEDGE.md` reflects current canonical modules and defaults.
- [ ] `docs/release_checklist.md` reflects current release hygiene requirements.
- [ ] `docs/new_project_setup.md` matches the current Godot bootstrap path.
- [ ] `docs/agent-ops/` helper docs use current endpoints, defaults, and platform-safe commands.

## Recommended Validation Pass

- [ ] `python -m pytest tests/test_release_hygiene.py -q`
- [ ] `python -m pytest tests/test_api.py -k "bootstraps_godot_support_files" -q`
- [ ] `python -m pytest tests/test_login.py tests/test_login_api.py -q`
- [ ] Start the server from a fresh venv with `python swarm_runner.py api`
- [ ] Verify `/`, `/api/projects`, `/api/tasks`, and `/api/agents`
- [ ] Verify missing optional tooling fails clearly:
  - Godot not configured
  - GUI QA helpers unavailable
  - no Dolt remote configured
