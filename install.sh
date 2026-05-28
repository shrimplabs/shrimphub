#!/usr/bin/env bash
# Swarm Controller installer
# Usage: curl -fsSL https://shrimphub.ai/install.sh | bash
set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

info()    { echo -e "${BOLD}$*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}⚠ $*${RESET}"; }
error()   { echo -e "${RED}✗ $*${RESET}" >&2; }
die()     { error "$*"; exit 1; }

ask() {
    # ask <prompt> <default>  — prints prompt, reads answer, falls back to default
    local prompt="$1" default="$2" answer
    echo -en "${BOLD}${prompt}${RESET} [${default}]: "
    read -r answer
    echo "${answer:-$default}"
}

ask_secret() {
    # ask_secret <prompt>  — reads without echo
    local prompt="$1" answer
    echo -en "${BOLD}${prompt}${RESET}: "
    read -rs answer
    echo
    echo "$answer"
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

echo
echo -e "${BOLD}╔═══════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║       Swarm Controller Installer      ║${RESET}"
echo -e "${BOLD}╚═══════════════════════════════════════╝${RESET}"
echo

# ---------------------------------------------------------------------------
# 1. Python version check
# ---------------------------------------------------------------------------

info "Checking Python version..."

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" -c 'import sys; print(sys.version_info[:2])' 2>/dev/null || echo "")
        major=$("$candidate" -c 'import sys; print(sys.version_info[0])' 2>/dev/null || echo 0)
        minor=$("$candidate" -c 'import sys; print(sys.version_info[1])' 2>/dev/null || echo 0)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    die "Python 3.11 or later is required but was not found.\nInstall it from https://python.org/downloads or via your package manager."
fi

PY_VERSION=$("$PYTHON" --version 2>&1)
success "Found $PY_VERSION"

# ---------------------------------------------------------------------------
# 2. Git check
# ---------------------------------------------------------------------------

command -v git &>/dev/null || die "git is required but was not found. Install git and try again."

# ---------------------------------------------------------------------------
# 3. Choose install directory
# ---------------------------------------------------------------------------

DEFAULT_DIR="$HOME/swarm-controller"
echo
INSTALL_DIR=$(ask "Install directory" "$DEFAULT_DIR")
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"   # expand leading ~

if [ -d "$INSTALL_DIR/.git" ]; then
    warn "Directory already exists. Pulling latest changes instead of cloning."
    git -C "$INSTALL_DIR" pull --ff-only || warn "git pull failed — continuing with existing code."
else
    info "Cloning Swarm Controller..."
    git clone https://github.com/shrimphub-ai/swarm-controller.git "$INSTALL_DIR"
    success "Cloned to $INSTALL_DIR"
fi

cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. Create virtual environment
# ---------------------------------------------------------------------------

echo
info "Creating Python virtual environment..."
"$PYTHON" -m venv .venv
success "venv created"

VENV_PY="$INSTALL_DIR/.venv/bin/python"
VENV_PIP="$INSTALL_DIR/.venv/bin/pip"

info "Installing dependencies (this may take a minute)..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet -r requirements.txt
success "Dependencies installed"

# ---------------------------------------------------------------------------
# 5. Config setup
# ---------------------------------------------------------------------------

echo
info "Setting up configuration..."

if [ -f config.json ]; then
    warn "config.json already exists — skipping (edit it manually if needed)."
else
    cp config.example.json config.json

    # Workspace path
    DEFAULT_WORKSPACE="$HOME/workspace"
    WORKSPACE=$(ask "Workspace directory (where your game projects live)" "$DEFAULT_WORKSPACE")
    WORKSPACE="${WORKSPACE/#\~/$HOME}"
    mkdir -p "$WORKSPACE"

    # Write workspace into config.json using Python (avoids jq dependency)
    "$VENV_PY" - <<PYEOF
import json, pathlib
cfg = json.loads(pathlib.Path("config.json").read_text())
cfg["workspace"] = "$WORKSPACE"
pathlib.Path("config.json").write_text(json.dumps(cfg, indent=2))
PYEOF
    success "Workspace set to $WORKSPACE"
fi

# ---------------------------------------------------------------------------
# 6. API key setup
# ---------------------------------------------------------------------------

echo
info "API key setup"
echo "Swarm Controller supports MiniMax (default), Claude, OpenRouter, and Kimi."
echo "You need at least one key. You can add more later in .env"
echo

ENV_FILE="$INSTALL_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    warn ".env already exists — skipping API key setup."
else
    touch "$ENV_FILE"

    MINIMAX_KEY=$(ask_secret "MiniMax API key (press Enter to skip)")
    if [ -n "$MINIMAX_KEY" ]; then
        echo "MINIMAX_API_KEY=$MINIMAX_KEY" >> "$ENV_FILE"
        success "MiniMax key saved"
    fi

    ANTHROPIC_KEY=$(ask_secret "Anthropic (Claude) API key (press Enter to skip)")
    if [ -n "$ANTHROPIC_KEY" ]; then
        echo "ANTHROPIC_API_KEY=$ANTHROPIC_KEY" >> "$ENV_FILE"
        success "Anthropic key saved"

        # If Claude key provided and no MiniMax key, set claude as default provider
        if [ -z "$MINIMAX_KEY" ]; then
            "$VENV_PY" - <<PYEOF
import json, pathlib
cfg = json.loads(pathlib.Path("config.json").read_text())
cfg["llm_provider"] = "claude"
pathlib.Path("config.json").write_text(json.dumps(cfg, indent=2))
PYEOF
            info "Set Claude as the default LLM provider."
        fi
    fi

    if [ -z "$MINIMAX_KEY" ] && [ -z "$ANTHROPIC_KEY" ]; then
        warn "No API keys entered. Add them to $ENV_FILE before starting."
    fi
fi

# ---------------------------------------------------------------------------
# 7. Godot detection
# ---------------------------------------------------------------------------

echo
info "Looking for Godot..."

GODOT_PATH=""
GODOT_CANDIDATES=(
    "$(command -v godot 2>/dev/null || true)"
    "/Applications/Godot.app/Contents/MacOS/Godot"
    "/opt/homebrew/bin/godot"
    "/usr/local/bin/godot"
    "/usr/bin/godot"
    "$HOME/.local/bin/godot"
)

for candidate in "${GODOT_CANDIDATES[@]}"; do
    if [ -n "$candidate" ] && [ -x "$candidate" ]; then
        GODOT_PATH="$candidate"
        break
    fi
done

if [ -n "$GODOT_PATH" ]; then
    GODOT_VERSION=$("$GODOT_PATH" --version 2>/dev/null || echo "unknown version")
    success "Found Godot at $GODOT_PATH ($GODOT_VERSION)"

    "$VENV_PY" - <<PYEOF
import json, pathlib
cfg = json.loads(pathlib.Path("config.json").read_text())
cfg["godot_path"] = "$GODOT_PATH"
pathlib.Path("config.json").write_text(json.dumps(cfg, indent=2))
PYEOF
else
    warn "Godot not found. Set 'godot_path' in config.json when you install it."
    warn "Download from: https://godotengine.org/download"
fi

# ---------------------------------------------------------------------------
# 8. Create launcher script
# ---------------------------------------------------------------------------

echo
info "Creating launcher script..."

# Prefer ~/.local/bin (no sudo needed), fall back to /usr/local/bin
if [ -d "$HOME/.local/bin" ] && [[ ":$PATH:" == *":$HOME/.local/bin:"* ]]; then
    BIN_DIR="$HOME/.local/bin"
elif command -v sudo &>/dev/null && [ -d "/usr/local/bin" ]; then
    BIN_DIR="/usr/local/bin"
else
    BIN_DIR="$HOME/.local/bin"
    mkdir -p "$BIN_DIR"
    warn "~/.local/bin created. Add it to your PATH: export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

LAUNCHER="$BIN_DIR/swarm-controller"

cat > "$LAUNCHER" <<LAUNCHER
#!/usr/bin/env bash
# Swarm Controller launcher — generated by install.sh
SWARM_DIR="$INSTALL_DIR"
VENV_PY="\$SWARM_DIR/.venv/bin/python"

case "\${1:-start}" in
    start)
        echo "Starting Swarm Controller at http://localhost:5001"
        cd "\$SWARM_DIR"
        exec "\$VENV_PY" swarm_runner.py api
        ;;
    update)
        echo "Updating Swarm Controller..."
        git -C "\$SWARM_DIR" pull --ff-only
        "\$SWARM_DIR/.venv/bin/pip" install --quiet -r "\$SWARM_DIR/requirements.txt"
        echo "Done. Restart the server to apply changes."
        ;;
    config)
        "\${EDITOR:-nano}" "\$SWARM_DIR/config.json"
        ;;
    *)
        echo "Usage: swarm-controller [start|update|config]"
        echo "  start   — start the server (default)"
        echo "  update  — pull latest code and update dependencies"
        echo "  config  — open config.json in your editor"
        ;;
esac
LAUNCHER

chmod +x "$LAUNCHER"
success "Launcher created at $LAUNCHER"

# ---------------------------------------------------------------------------
# 9. Done
# ---------------------------------------------------------------------------

echo
echo -e "${BOLD}╔═══════════════════════════════════════╗${RESET}"
echo -e "${BOLD}║           Installation complete!      ║${RESET}"
echo -e "${BOLD}╚═══════════════════════════════════════╝${RESET}"
echo
echo -e "  Start the server:   ${BOLD}swarm-controller start${RESET}"
echo -e "  Open the dashboard: ${BOLD}http://localhost:5001${RESET}"
echo -e "  Edit config:        ${BOLD}swarm-controller config${RESET}"
echo -e "  Update:             ${BOLD}swarm-controller update${RESET}"
echo
echo -e "  Install directory:  $INSTALL_DIR"
echo -e "  Config file:        $INSTALL_DIR/config.json"
echo -e "  API keys:           $INSTALL_DIR/.env"
echo
if [ -z "$GODOT_PATH" ]; then
    warn "Remember to set 'godot_path' in config.json after installing Godot."
fi
echo
