# PRD: Unified Dashboard Chat

## Overview

Replace the two separate chat surfaces (Ask Manager and per-project Debug Chat) with a single unified chat panel that serves as a first-class control plane for the swarm. The chat is context-aware based on what the user is looking at, persistent across sessions, and smart enough to investigate and act without requiring the user to know about tools or endpoints. It includes session compaction, two-tier memory (swarm-level and per-project), and catastrophic action prevention so bad LLM responses cannot silently wreck projects. This should feel like talking to a knowledgeable co-pilot who knows the whole system.

## Goals

- One chat surface instead of two (eliminate confusion about which chat to use)
- Context-aware: switches scope when user navigates to a project in the sidebar
- Persistent: conversation history survives page refresh and server restart
- Memory: swarm-level and per-project knowledge files injected into every session
- Safe: destructive actions require explicit confirmation; catastrophic commands blocked
- Compaction: long conversations summarized so quality does not degrade over time
- Accessible: any team member (not just developers) can use it productively

## Quality Gates

These commands must pass for every user story:
- `.venv/bin/pytest tests/test_project_debug_chat.py -x -q` — existing debug chat tests must keep passing
- `.venv/bin/pytest tests/test_api.py -x -q` — API regression suite

For backend stories that add new routes, also:
- Manual curl smoke test of the new endpoint

## User Stories

### US-001: Unified chat backend endpoint

**Description:** As a developer, I want a single `/api/unified-chat` endpoint that handles both global (swarm-wide) and project-scoped conversations so the frontend has one integration point.

**Acceptance Criteria:**
- [ ] `POST /api/unified-chat` accepts `{message, session_id, project}` where `project` is optional
- [ ] When `project` is omitted or `null`, operates in global mode (access to all projects' tasks/agents)
- [ ] When `project` is set, operates in project-scoped mode (file tools restricted to that project)
- [ ] Returns `{reply, session_id, tool_calls, scope}` — scope is `"global"` or project name
- [ ] Session JSONL stored at `data/chat_sessions/_global/<id>.jsonl` for global, `data/chat_sessions/<project>/<id>.jsonl` for project
- [ ] 7-day TTL cleanup on session files (same as existing debug chat)
- [ ] Existing `/api/chat` and `/api/project-debug` endpoints remain functional (backward compat)

### US-002: Two-tier memory injection

**Description:** As a user, I want the chat agent to remember things about the swarm overall and about individual projects so I don't have to re-explain context every session.

**Acceptance Criteria:**
- [ ] Swarm-level memory file at `data/SWARM_KNOWLEDGE.md` — created empty if missing
- [ ] Project-level memory file at `data/project_knowledge/<project>.md` — created empty if missing
- [ ] Both files are injected into the system prompt of every chat session
- [ ] Agent can write to swarm memory via tool `write_swarm_memory(content)` — overwrites the file
- [ ] Agent can write to project memory via tool `write_project_memory(project, content)` — overwrites that project's file
- [ ] Files are human-editable markdown (users can maintain them manually too)
- [ ] Memory injection happens at session start; changes during a session take effect next session

### US-003: Catastrophic action prevention

**Description:** As a user, I want destructive actions to require confirmation so a bad LLM response cannot silently delete tasks or kill agents.

**Acceptance Criteria:**
- [ ] Destructive tools (`delete_task`, `kill_agent`, `reset_all_tasks`) return a confirmation challenge before executing: `{"requires_confirmation": true, "action": "...", "confirm_token": "<uuid>"}`
- [ ] Client must re-send the request with `confirm_token` to execute
- [ ] Tokens expire after 60 seconds
- [ ] Hard-blocked shell commands (`rm -rf`, `git push --force`, `drop table`, etc.) are never executed regardless of confirmation
- [ ] The system prompt explicitly lists which actions require confirmation
- [ ] Unit tests cover: confirm challenge issued, confirm token works, expired token rejected, hard-blocked command rejected

### US-004: Session compaction

**Description:** As a user, I want long conversations to stay sharp so the chat doesn't degrade after 50+ messages.

**Acceptance Criteria:**
- [ ] When estimated tokens in a session exceed 80,000, the middle of the conversation is summarized via a separate LLM call
- [ ] Summary replaces middle messages; system prompt + last 4 messages are preserved verbatim
- [ ] Compaction is logged to the session with a `[COMPACTED]` marker message
- [ ] Compaction does not discard tool call results from the last 4 messages
- [ ] Unit test: session with 100 mock messages triggers compaction and resulting session is under threshold

### US-005: Emergency stop

**Description:** As a user, I want to immediately halt a runaway chat agent tool loop without reloading the page.

**Acceptance Criteria:**
- [ ] `POST /api/unified-chat/<session_id>/stop` injects an interrupt into the active tool loop
- [ ] The running loop exits cleanly after the current tool call completes (no mid-tool abort)
- [ ] Response to the original POST returns whatever partial reply was built before the stop
- [ ] Frontend Stop button (or Escape key) calls this endpoint
- [ ] Stop clears automatically when the response finishes (not sticky)

### US-006: Frontend — unified chat panel

**Description:** As a user, I want a single chat panel in the dashboard that replaces the two separate chat buttons so I always know where to go.

**Acceptance Criteria:**
- [ ] One chat panel accessible from the toolbar (replaces separate Manager and Debug Chat buttons)
- [ ] Panel header shows current scope: "Swarm Chat" in global mode, "Project: <name>" in project mode
- [ ] Scope switches automatically when user clicks a project in the sidebar (injects a context-switch message into the session)
- [ ] Tool calls rendered as collapsible blocks (same as existing debug chat)
- [ ] Stop button in the panel header; Escape key triggers stop when panel is focused
- [ ] New Session button clears the current session and starts fresh
- [ ] Session persists across page refresh (session_id stored in localStorage keyed by scope)

### US-007: Context injection on scope switch

**Description:** As a user, I want the chat to automatically load relevant context when I switch to a project so I can immediately ask questions without having to say "look at project X".

**Acceptance Criteria:**
- [ ] When scope switches to a project, a synthetic context message is prepended: last 5 git commits, current task queue summary (pending/in_progress counts by type), active agent count, project memory file contents
- [ ] Context message is visually distinct in the chat (e.g., grey/muted style, "📍 Context loaded" header)
- [ ] Context injection does not create a new session — it appends to the existing project session
- [ ] Switching back to global mode injects a global context summary (total tasks, agents, auto-mode state)
- [ ] Context injection is skipped if the last message in the session was a context injection less than 60 seconds ago (debounce)

### US-008: Migrate and deprecate old chat surfaces

**Description:** As a developer, I want the old Manager Chat and per-project Debug Chat removed from the UI so users have one clear place to go.

**Acceptance Criteria:**
- [ ] Remove "Ask Manager" (❓) button from the dashboard toolbar
- [ ] Remove "Debug" (🔍) button from project sidebar rows
- [ ] Remove `#managerChatPanel` and `#debugChatPanel` HTML, CSS, and JS
- [ ] `/api/chat` and `/api/project-debug` backend routes remain (backward compat for any external callers)
- [ ] All existing `tests/test_project_debug_chat.py` tests still pass (they test the backend, not the removed UI)
- [ ] Update CLAUDE.md to document the new unified endpoint and remove references to the two old UI surfaces

## Functional Requirements

- FR-1: The unified endpoint must support both global and project scope in a single route
- FR-2: Memory files must be human-editable and injected at session start (not cached between calls)
- FR-3: Confirmation tokens must be single-use and expire after 60 seconds
- FR-4: Hard-blocked commands must be checked server-side regardless of any client behavior
- FR-5: Session compaction must preserve the system prompt and last 4 messages verbatim
- FR-6: The stop mechanism must not corrupt session JSONL (partial writes are not acceptable)
- FR-7: Scope switching must not lose the previous conversation history
- FR-8: The frontend must work without JavaScript module bundling (existing dashboard uses vanilla JS)

## Non-Goals

- Per-user chat history (single shared session per scope)
- Real-time streaming (SSE) for chat responses — polling or single-response is fine for now
- Voice input
- Attachment/file upload from the browser
- Multi-turn tool approval UI (the confirmation challenge is sufficient for now)

## Technical Considerations

- Reuse `_run_debug_tool_loop`, `_load_session`, `_save_session` from `swarm/api_chat.py`
- Confirmation token store: in-memory dict `{token: (action_args, expires_at)}` — no DB needed
- Memory files: plain markdown, no schema, agent writes them in full (no append/patch API)
- Compaction: reuse the pattern from `agent_runtime.py` context compaction
- Stop signal: threading.Event per session_id, stored in module-level dict, cleared after response

## Success Metrics

- Zero support questions of the form "should I use Manager or Debug chat?"
- Chat remains coherent after 100+ message sessions (compaction working)
- No task or agent accidentally destroyed without explicit confirmation (zero incidents in 30 days)

## Open Questions

- None — all resolved during PRD review
