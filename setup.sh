#!/usr/bin/env bash
# setup.sh — Single-command setup for review-like-him
# Usage: curl -sSL ... | bash  OR  bash setup.sh
# Works on macOS and Linux.
#
# Windows users: Use WSL or run these steps manually:
#   1. Install Python 3.11+ from https://python.org
#   2. pip install uv
#   3. uv venv .venv && .venv\Scripts\activate
#   4. uv pip install -e .
#   5. Install Claude CLI: npm install -g @anthropic-ai/claude-code
#   6. review-bot init
set -euo pipefail

# ─── Colors & helpers ────────────────────────────────────────────────────────

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
RESET='\033[0m'

step=0

info()  { echo -e "${BLUE}ℹ ${RESET} $*"; }
ok()    { echo -e "${GREEN}✓ ${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠ ${RESET} $*"; }
fail()  { echo -e "${RED}✗ ${RESET} $*"; exit 1; }

step() {
    step=$((step + 1))
    echo ""
    echo -e "${BOLD}${CYAN}[$step]${RESET} ${BOLD}$*${RESET}"
}

# ─── Detect OS ───────────────────────────────────────────────────────────────

detect_os() {
    case "$(uname -s)" in
        Linux*)  OS="linux";;
        Darwin*) OS="macos";;
        MINGW*|MSYS*|CYGWIN*)
            fail "Windows detected. Please use WSL or follow the manual steps at the top of this script."
            ;;
        *)
            fail "Unsupported OS: $(uname -s). This script supports macOS and Linux."
            ;;
    esac
    ok "Detected OS: $OS"
}

# ─── Step 1: Check Python 3.11+ ─────────────────────────────────────────────

check_python() {
    step "Checking Python 3.11+"

    local py_cmd=""
    for cmd in python3 python; do
        if command -v "$cmd" &>/dev/null; then
            local ver
            ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "0.0")
            local major minor
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                py_cmd="$cmd"
                ok "Found $cmd ($ver)"
                break
            fi
        fi
    done

    if [ -z "$py_cmd" ]; then
        echo ""
        warn "Python 3.11+ not found."
        echo ""
        echo "  Install options:"
        if [ "$OS" = "macos" ]; then
            echo "    brew install python@3.12"
        else
            echo "    sudo apt update && sudo apt install python3.12 python3.12-venv  (Debian/Ubuntu)"
            echo "    sudo dnf install python3.12  (Fedora)"
        fi
        echo "    Or visit https://python.org/downloads"
        echo ""
        fail "Please install Python 3.11+ and re-run this script."
    fi

    PYTHON="$py_cmd"
}

# ─── Step 2: Check / install uv ─────────────────────────────────────────────

check_uv() {
    step "Checking for uv package manager"

    if command -v uv &>/dev/null; then
        ok "Found uv ($(uv --version 2>/dev/null || echo 'unknown version'))"
        INSTALLER="uv"
        return
    fi

    info "uv not found — installing..."
    if curl -LsSf https://astral.sh/uv/install.sh | sh 2>/dev/null; then
        # Add uv to PATH for this session
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if command -v uv &>/dev/null; then
            ok "Installed uv ($(uv --version 2>/dev/null || echo 'unknown version'))"
            INSTALLER="uv"
            return
        fi
    fi

    warn "Could not install uv automatically. Falling back to pip."
    if command -v pip3 &>/dev/null; then
        INSTALLER="pip3"
    elif command -v pip &>/dev/null; then
        INSTALLER="pip"
    else
        fail "Neither uv nor pip found. Install uv: https://docs.astral.sh/uv/getting-started/installation/"
    fi
    ok "Using $INSTALLER as fallback"
}

# ─── Step 3: Create venv & install ───────────────────────────────────────────

setup_venv() {
    step "Creating virtual environment & installing package"

    local venv_dir=".venv"

    if [ "$INSTALLER" = "uv" ]; then
        if [ ! -d "$venv_dir" ]; then
            uv venv "$venv_dir" --python "$PYTHON"
            ok "Created virtual environment at $venv_dir"
        else
            ok "Virtual environment already exists at $venv_dir"
        fi

        # shellcheck disable=SC1091
        source "$venv_dir/bin/activate"
        uv pip install -e ".[dev]" 2>&1 | tail -1
        ok "Installed review-like-him in editable mode (with dev deps)"
    else
        if [ ! -d "$venv_dir" ]; then
            "$PYTHON" -m venv "$venv_dir"
            ok "Created virtual environment at $venv_dir"
        else
            ok "Virtual environment already exists at $venv_dir"
        fi

        # shellcheck disable=SC1091
        source "$venv_dir/bin/activate"
        "$INSTALLER" install -e ".[dev]" 2>&1 | tail -1
        ok "Installed review-like-him in editable mode (with dev deps)"
    fi

    # Verify the CLI is available
    if command -v review-bot &>/dev/null; then
        ok "review-bot CLI is available"
    else
        warn "review-bot CLI not found on PATH — you may need to activate the venv:"
        echo "    source $venv_dir/bin/activate"
    fi
}

# ─── Step 4: Check Claude CLI ───────────────────────────────────────────────

check_claude_cli() {
    step "Checking for Claude CLI"

    if command -v claude &>/dev/null; then
        ok "Found Claude CLI"
    else
        warn "Claude CLI not found."
        echo ""
        echo "  Install it with:"
        echo "    npm install -g @anthropic-ai/claude-code"
        echo ""
        echo "  Requires Node.js 18+. If you don't have Node.js:"
        if [ "$OS" = "macos" ]; then
            echo "    brew install node"
        else
            echo "    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
            echo "    sudo apt install -y nodejs"
        fi
        echo ""
        info "You can continue setup, but Claude CLI is required for reviews."
    fi
}

# ─── Step 5: Run review-bot init ─────────────────────────────────────────────

run_init() {
    step "Running review-bot init"

    if command -v review-bot &>/dev/null; then
        info "Starting interactive setup wizard..."
        echo ""
        review-bot init || {
            warn "review-bot init exited with an error (this is OK if config already exists)."
        }
    else
        warn "Skipping — review-bot CLI not available. Activate the venv and run:"
        echo "    review-bot init"
    fi
}

# ─── Step 6: Summary ────────────────────────────────────────────────────────

print_summary() {
    step "Setup complete!"

    echo ""
    echo -e "${BOLD}${GREEN}┌────────────────────────────────────────────────┐${RESET}"
    echo -e "${BOLD}${GREEN}│        review-like-him is ready! 🎉            │${RESET}"
    echo -e "${BOLD}${GREEN}└────────────────────────────────────────────────┘${RESET}"
    echo ""
    echo -e "${BOLD}Next steps:${RESET}"
    echo ""
    echo "  1. Activate the virtual environment (if not already):"
    echo -e "     ${CYAN}source .venv/bin/activate${RESET}"
    echo ""
    echo "  2. Create a reviewer persona from a GitHub user:"
    echo -e "     ${CYAN}review-bot persona create <github-username>${RESET}"
    echo ""
    echo "  3. Review a PR with that persona:"
    echo -e "     ${CYAN}review-bot review <owner/repo> <pr-number> --persona <name>${RESET}"
    echo ""
    echo "  4. Or start the webhook server for automatic reviews:"
    echo -e "     ${CYAN}review-bot server start${RESET}"
    echo ""
    echo -e "  Run ${CYAN}review-bot --help${RESET} for all available commands."
    echo ""
}

# ─── Main ────────────────────────────────────────────────────────────────────

main() {
    echo ""
    echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  review-like-him — Automated Setup${RESET}"
    echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"

    detect_os
    check_python
    check_uv
    setup_venv
    check_claude_cli
    run_init
    print_summary
}

main "$@"
