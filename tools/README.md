# tools/

CLI utilities that ship with swarm-controller. All are standalone scripts
with minimal dependencies — designed to work from a terminal or over SSH
(e.g. via shrimpterm).

---

## shrimp-agent

An interactive local coding agent. Think Claude Code, but powered by your
own LLMs (MiniMax M3, Athena qwen35, OpenRouter, etc.).

```bash
# Interactive REPL in current directory
python3 tools/shrimp-agent.py

# One-shot task, exits when done
python3 tools/shrimp-agent.py "add dark mode to the settings screen"

# Override provider
python3 tools/shrimp-agent.py --provider athena-qwen35
python3 tools/shrimp-agent.py --provider claude

# Run in a specific directory
python3 tools/shrimp-agent.py --dir ~/workspace/shrimpterm

# Combine
python3 tools/shrimp-agent.py "fix the SSH timeout bug" --provider minimax --dir ~/workspace/shrimpterm
```

**Provider** defaults to `SWARM_PROVIDER` env var, then `llm_provider` in `config.json`,
then `minimax`. All providers registered in `config.json` are available.

**REPL commands:**
- `/clear` — wipe conversation history and start fresh
- `/provider` — show active provider and model
- `/exit` or Ctrl+C — quit

Reads `config.json` and `.env` from the swarm-controller root automatically,
so your API keys and custom providers (Athena, etc.) are available without
any extra setup.

**Design:** wraps `swarm/agent_runtime.py` directly — same tool loop, same
providers, same compaction and stall detection as background agents, but
interactive. No Flask, no SQLite, no orchestrator overhead.

**Pair with shrimpterm:** SSH into your mac from your phone via shrimpterm,
run `shrimp-agent` in any project directory, and you have a full coding
agent in your terminal.

---

## swarm-code

CLI harness for the swarm controller API. Fire tasks, tail agent logs,
chat with the swarm co-pilot.

```bash
# Create a task and block until it finishes (streams agent log)
python3 tools/swarm-code.py <project> "<description>" --wait
python3 tools/swarm-code.py raccoon-city "fix the save bug" --type=bug --wait

# Fire and forget
python3 tools/swarm-code.py raccoon-city "add a leaderboard"

# Interactive chat REPL — global or project-scoped
python3 tools/swarm-code.py --chat
python3 tools/swarm-code.py --chat raccoon-city

# Pipe input
echo "how many agents running?" | python3 tools/swarm-code.py --chat

# Tail a running agent's log
python3 tools/swarm-code.py --watch <agent_id>

# Health + active agents + task counts
python3 tools/swarm-code.py --status
```

**Environment:** `SWARM_URL` (default `http://localhost:5001`), `SWARM_TOKEN`
(if `login_required: true` in config).

---

## Environment variables

| Var | Used by | Description |
|-----|---------|-------------|
| `SWARM_URL` | swarm-code | Swarm controller base URL |
| `SWARM_TOKEN` | swarm-code | Bearer token (if auth enabled) |
| `SWARM_PROVIDER` | shrimp-agent | Default LLM provider |
| `NO_COLOR` | both | Disable ANSI colours |
| `MINIMAX_API_KEY` | shrimp-agent | MiniMax API key |
| `ANTHROPIC_API_KEY` | shrimp-agent | Claude API key |
| `OPENROUTER_API_KEY` | shrimp-agent | OpenRouter API key |
