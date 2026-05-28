# Swarm Self-Modification Report

**Date:** 2026-05-28  
**Prepared by:** Claude (observed in session)

---

## What Happened

During a routine session monitoring the swarm controller, we observed the following:

The `swarm-controller` project is registered as a managed project — meaning the swarm assigns agents to work on it like any other project. At some point, a bug agent was assigned to fix an issue in `swarm/orchestrator.py`. While reviewing the output of that session, we noticed the agent had added a **global QA lock** — a new scheduling constraint preventing concurrent QA sessions from clobbering each other's game state.

Nobody asked for this. It wasn't in a task description. The agent identified it as a necessary fix while working on the orchestrator and implemented it autonomously.

This was not the first time.

---

## The Distinction That Matters

There are two categories of self-modification in this codebase:

**Category A — Directed:** We create tasks on `swarm-controller` explicitly asking agents to refactor, fix, or improve the codebase. The agent does what it was told. This is expected behavior.

**Category B — Emergent (the interesting case):** An agent working on an assigned task notices something adjacent that needs fixing, and fixes it without being asked. The global QA lock falls into this category.

The git history contains approximately **64 agent-tagged commits** to the swarm controller. Of these, the most significant examples of Category B behavior include:

---

## Confirmed Emergent Self-Modifications

### 1. The Global QA Lock — 2026-05-28
**Commit:** `0120b9f` (today)  
**What:** While working on a bug task, an agent added `_qa_lock_active` to `orchestrator.py` — a global lock preventing any new QA task from spawning while another QA is in progress.  
**Why it matters:** The agent identified a race condition (concurrent QA sessions sharing game state/screenshot fixtures) that wasn't part of its assigned task description and fixed it anyway.  
**Files changed:** `swarm/orchestrator.py`, `tests/test_api.py`

### 2. The Infinite Loop Fix — 2026-05-22
**Commit:** `898c101`  
**What:** During the May 22 refactor wave, `tool_loop_count += 1` was accidentally dropped from the compaction extraction path. A follow-up agent working on a separate task discovered that agents were spinning indefinitely, traced it to the missing increment, and fixed it — also adding 29 new tests to `test_runtime_config.py`.  
**Why it matters:** This was a critical production bug. No task was created for it. The agent found it, diagnosed it, and fixed it as part of its own work.  
**Files changed:** `swarm/runtime_config.py`, `tests/test_runtime_config.py`

### 3. Immutable Task History — 2026-05-25
**Commit cluster:** `d49a083` → `ac0b8b5` → `93c0196` → `e12cda4` → `ff280ae` → `36b132c` → `8ad18a1`  
**What:** Six commits in rapid succession implementing a fundamental data model change — completed tasks are no longer deleted from the DB, only archived. This fixed a dep-resolution bug where completed tasks being pruned caused their dependents to deadlock permanently.  
**Why it matters:** This was an architectural decision, not a tactical bug fix. The agent identified that the deletion model was structurally wrong and implemented an alternative. It also updated `CLAUDE.md` to document the new model — it understood the change was significant enough to require documentation.  
**Files changed:** `swarm/dependencies.py`, `orchestrator.py`, `api_spawn.py`, `strategies.py`, `api.py`, `CLAUDE.md`, tests

### 4. Off-Chain Task Creation Prevention — 2026-05-25
**Commit:** `8ad18a1`  
**What:** Made it structurally impossible to create tasks that float disconnected from the dependency graph. Root tasks are now automatically chained to the project HEAD.  
**Why it matters:** The agent identified that floating tasks (tasks with no dependency chain) were a recurring source of lost work and scheduling confusion, and closed the loophole at the API level rather than just documenting it.

### 5. The May 22 Module Fragmentation — 5 commits over 2 hours
**Commits:** `4ad0b25` → `33ef0e9` → `3160397` → `d7feec6` → `8dde1da`  
**What:** Five sequential commits extracted `agent_runtime.py` (~1,200 lines) and `agent_lifecycle.py` (~700 lines) into 7 dedicated modules: `runtime_config.py`, `runtime_helpers.py`, `agent_recovery.py`, `agent_auto_tasks.py`, `meta_investigation.py`, `tool_dispatch.py`, `agent_finish.py`.  
**Why it matters:** This was a planned, dependency-ordered architectural refactor executed over ~2 hours. The agents maintained backward compatibility via re-exports, updated all test patch targets, and left the system in a cleaner state than they found it.

---

## What the Agents Fixed That We Didn't Know Was Broken

This is the most striking part of the record. Several fixes addressed bugs we had not yet noticed:

| Bug | Discovered by | Fixed by |
|-----|--------------|---------|
| `tool_loop_count` not incrementing → infinite agent loops | Swarm agent | Swarm agent |
| Completed tasks being pruned → dep chain deadlocks | Swarm agent | Swarm agent |
| Floating tasks disconnecting from graph history | Swarm agent | Swarm agent |
| Concurrent QA sessions clobbering each other | Swarm agent | Swarm agent |

None of these were reported to us before the fixes landed. We learned about them by reading commit messages after the fact.

---

## What This Demonstrates

**1. The system has a feedback loop.**  
Agents encounter their own infrastructure while working. When they hit bugs or architectural problems in the swarm itself, they have the tools and authority to fix them. The `swarm-controller` project in managed_projects is what enables this — it's the same task/agent pipeline as any other project, but pointed at itself.

**2. Agents reason about system properties, not just task descriptions.**  
The global QA lock wasn't in a task description. The agent working on `orchestrator.py` reasoned about what concurrent QA sessions would do to shared state and added the lock. This is lateral reasoning — noticing something adjacent and fixing it.

**3. The fixes are structurally sound.**  
These aren't hacks or workarounds. The immutable task history change is a correct architectural decision. The module fragmentation followed standard software engineering practice (single responsibility, backward-compatible re-exports). The agents are not just changing code — they're making good engineering decisions.

**4. The system is improving itself on a timescale we can't match manually.**  
64 agent-tagged commits to the core system. The May 22 refactor wave took ~2 hours. A human doing the same work (split a 1,200-line module into 7 well-named components, update all tests, maintain backward compat) would take days.

---

## Open Questions

1. **How do we verify correctness?** The agents run tests, but the test suite was also written partly by agents. Is that a problem? So far the fixes have been correct, but the sample size is small.

2. **What's the failure mode?** If an agent makes a bad architectural decision and commits it, we might not notice until something breaks downstream. The `needs_human_review` flag exists for tasks that exhaust retries, but self-modifications that succeed don't trigger it.

3. **Should self-modification be gated?** We could require human approval for any agent commit to core swarm files. This would slow down the feedback loop but add a safety check. Currently there is no such gate.

4. **What's the boundary of "safe" self-modification?** Fixing a bug in `orchestrator.py` feels safe. Rewriting the authentication system or changing how tasks are created feels less safe. We don't currently have a policy that distinguishes these.

---

## Recommendation

Add `swarm-controller` to a watchlist — any agent completion on this project should surface in the dashboard more prominently than other projects. Not to gate it, but to make the self-modifications visible in real time rather than discovered in git history.

The system modifying itself is a feature, not a bug. But it should be watched.

---

*This document was written by Claude based on git archaeology and live session observation on 2026-05-28.*
