#!/usr/bin/env bash
# setup.sh — Single-command setup for review-like-him
# Usage: curl -sSL ... | bash  OR  bash setup.sh [--no-init]
# Works on macOS and Linux.
#
# Options:
#   --no-init    Skip the interactive review-bot init wizard
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

step_num=0
SKIP_INIT=false

info()  { echo -e "${BLUE}ℹ ${RESET} $*"; }
ok()    { echo -e "${GREEN}✓ ${RESET} $*"; }
warn()  { echo -e "${YELLOW}⚠ ${RESET} $*"; }
fail()  { echo -e "${RED}✗ ${RESET} $*"; exit 1; }

step() {
    step_num=$((step_num + 1))
    echo ""
    echo -e "${BOLD}${CYAN}[$step_num]${RESET} ${BOLD}$*${RESET}"
}

# ─── Parse arguments ─────────────────────────────────────────────────────────

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --no-init)
                SKIP_INIT=true
                shift
                ;;
            -h|--help)
                echo "Usage: ./setup.sh [--no-init]"
                echo ""
                echo "Options:"
                echo "  --no-init    Skip the interactive review-bot init wizard"
                echo "  -h, --help   Show this help message"
                exit 0
                ;;
            *)
                warn "Unknown option: $1 (use --help for usage)"
                shift
                ;;
        esac
    done
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

# ─── Check internet connectivity ─────────────────────────────────────────────

check_internet() {
    step "Checking internet connectivity"

    if command -v curl &>/dev/null; then
        if curl -sf --connect-timeout 5 --max-time 10 https://pypi.org/simple/ >/dev/null 2>&1; then
            ok "Internet connectivity verified (pypi.org reachable)"
            return
        fi
    elif command -v wget &>/dev/null; then
        if wget -q --timeout=5 --spider https://pypi.org/simple/ 2>/dev/null; then
            ok "Internet connectivity verified (pypi.org reachable)"
            return
        fi
    fi

    warn "Could not verify internet connectivity."
    info "Package installation may fail if you're offline."
    info "If you're behind a proxy, ensure HTTP_PROXY/HTTPS_PROXY are set."
}

# ─── Check Python 3.11+ ─────────────────────────────────────────────────────

check_python() {
    step "Checking Python 3.11+"

    local py_cmd=""
    for cmd in python3.13 python3.12 python3.11 python3 python; do
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
            echo "    sudo pacman -S python  (Arch)"
        fi
        echo "    Or visit https://python.org/downloads"
        echo ""
        fail "Please install Python 3.11+ and re-run this script."
    fi

    # Verify venv module is available (common issue on Debian/Ubuntu)
    if ! "$py_cmd" -c 'import venv' 2>/dev/null; then
        echo ""
        warn "Python venv module not found."
        echo ""
        echo "  On Debian/Ubuntu, install it with:"
        echo "    sudo apt install python3-venv"
        echo ""
        fail "Please install the Python venv module and re-run this script."
    fi

    PYTHON="$py_cmd"
}

# ─── Check / install uv ─────────────────────────────────────────────────────

check_uv() {
    step "Checking for uv package manager"

    if command -v uv &>/dev/null; then
        ok "Found uv ($(uv --version 2>/dev/null || echo 'unknown version'))"
        INSTALLER="uv"
        return
    fi

    info "uv not found — installing..."

    # Check that curl is available for uv install
    if ! command -v curl &>/dev/null; then
        warn "curl not found — cannot install uv automatically. Falling back to pip."
        _fallback_to_pip
        return
    fi

    if curl -LsSf --connect-timeout 10 --max-time 60 https://astral.sh/uv/install.sh | sh 2>/dev/null; then
        # Add uv to PATH for this session
        export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
        if command -v uv &>/dev/null; then
            ok "Installed uv ($(uv --version 2>/dev/null || echo 'unknown version'))"
            INSTALLER="uv"
            return
        fi
    fi

    warn "Could not install uv automatically. Falling back to pip."
    _fallback_to_pip
}

_fallback_to_pip() {
    if "$PYTHON" -m pip --version &>/dev/null; then
        INSTALLER="pip-module"
        ok "Using 'python -m pip' as fallback"
    elif command -v pip3 &>/dev/null; then
        INSTALLER="pip3"
        ok "Using pip3 as fallback"
    elif command -v pip &>/dev/null; then
        INSTALLER="pip"
        ok "Using pip as fallback"
    else
        fail "No package installer found. Install uv (https://docs.astral.sh/uv/) or pip and re-run."
    fi
}

# ─── Create venv & install ───────────────────────────────────────────────────

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
        info "Installing package with uv (this may take a moment)..."
        if ! uv pip install -e ".[dev]"; then
            fail "uv pip install failed. Check the output above for details."
        fi
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

        info "Upgrading pip..."
        if [ "$INSTALLER" = "pip-module" ]; then
            "$PYTHON" -m pip install --upgrade pip 2>/dev/null || warn "pip upgrade failed (continuing anyway)"
        else
            "$INSTALLER" install --upgrade pip 2>/dev/null || warn "pip upgrade failed (continuing anyway)"
        fi

        info "Installing package with pip (this may take a moment)..."
        if [ "$INSTALLER" = "pip-module" ]; then
            if ! "$PYTHON" -m pip install -e ".[dev]"; then
                fail "pip install failed. Check the output above for details."
            fi
        else
            if ! "$INSTALLER" install -e ".[dev]"; then
                fail "$INSTALLER install failed. Check the output above for details."
            fi
        fi
        ok "Installed review-like-him in editable mode (with dev deps)"
    fi

    # Verify the package can be imported
    info "Verifying package installation..."
    if ! "$PYTHON" -c 'import review_bot' 2>/dev/null; then
        fail "Installation verification failed: 'import review_bot' did not succeed."
    fi
    ok "Package verification passed (import review_bot)"

    # Verify the CLI is available
    if command -v review-bot &>/dev/null; then
        ok "review-bot CLI is available"
    else
        warn "review-bot CLI not found on PATH — you may need to activate the venv:"
        echo "    source $venv_dir/bin/activate"
    fi
}

# ─── Check Claude CLI ───────────────────────────────────────────────────────

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
        if ! command -v node &>/dev/null; then
            echo "  Node.js is required. Install it first:"
            if [ "$OS" = "macos" ]; then
                echo "    brew install node"
            else
                echo "    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -"
                echo "    sudo apt install -y nodejs  (Debian/Ubuntu)"
                echo "    sudo dnf install nodejs  (Fedora)"
            fi
            echo ""
        fi
        info "You can continue setup, but Claude CLI is required for reviews."
    fi
}

# ─── Run review-bot init ─────────────────────────────────────────────────────

run_init() {
    step "Running review-bot init"

    # Skip if --no-init flag was passed
    if [ "$SKIP_INIT" = true ]; then
        info "Skipping init (--no-init flag set)."
        echo "    Run manually later: review-bot init"
        return
    fi

    # Skip if stdin is not a TTY (e.g., piped install: curl ... | bash)
    if [ ! -t 0 ]; then
        warn "Skipping init — stdin is not a terminal (non-interactive mode detected)."
        echo "    Run manually later: review-bot init"
        return
    fi

    if command -v review-bot &>/dev/null; then
        info "Starting interactive setup wizard..."
        echo ""
        review-bot init || {
            warn "review-bot init exited with an error (this is OK if config already exists)."
        }
    else
        warn "Skipping — review-bot CLI not available. Activate the venv and run:"
        echo "    source .venv/bin/activate && review-bot init"
    fi
}

# ─── Summary ─────────────────────────────────────────────────────────────────

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
    echo -e "     ${CYAN}review-bot persona create deepam --github-user deepam-kapur${RESET}"
    echo ""
    echo "  3. Start the webhook server for automatic reviews:"
    echo -e "     ${CYAN}review-bot server start${RESET}"
    echo ""
    echo "  Or run a one-off review:"
    echo -e "     ${CYAN}review-bot review https://github.com/org/repo/pull/42 --as deepam${RESET}"
    echo ""
    echo -e "  Run ${CYAN}review-bot --help${RESET} for all available commands."
    echo ""
}

# ─── Main ────────────────────────────────────────────────────────────────────

main() {
    parse_args "$@"

    echo ""
    echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"
    echo -e "${BOLD}${CYAN}  review-like-him — Automated Setup${RESET}"
    echo -e "${BOLD}${CYAN}═══════════════════════════════════════════════════${RESET}"

    detect_os
    check_internet
    check_python
    check_uv
    setup_venv
    check_claude_cli
    run_init
    print_summary
}

main "$@"
