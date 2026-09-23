#!/usr/bin/env bash
# AI Router — one-click Mac setup
# Run once: bash setup.sh
set -e

# ── Colors ────────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}▶ $*${RESET}"; }
success() { echo -e "${GREEN}✓ $*${RESET}"; }
warn()    { echo -e "${YELLOW}! $*${RESET}"; }
error()   { echo -e "${RED}✗ $*${RESET}"; exit 1; }
ask()     { echo -e "${BOLD}$*${RESET}"; }

echo ""
echo -e "${BOLD}╔══════════════════════════════╗${RESET}"
echo -e "${BOLD}║      AI Router — Setup       ║${RESET}"
echo -e "${BOLD}╚══════════════════════════════╝${RESET}"
echo ""

# ── 1. Python check ───────────────────────────────────────────────────────────
info "Checking Python..."

PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3; do
    if command -v "$cmd" &>/dev/null; then
        VER=$("$cmd" -c "import sys; print(sys.version_info[:2])")
        if "$cmd" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
            PYTHON="$cmd"
            success "Found $cmd ($VER)"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    warn "Python 3.10+ not found."
    if command -v brew &>/dev/null; then
        info "Installing Python via Homebrew..."
        brew install python@3.12
        PYTHON=python3.12
    else
        error "Please install Python 3.10+ first: https://www.python.org/downloads/macos/"
    fi
fi

# ── 2. Virtualenv ─────────────────────────────────────────────────────────────
if [ ! -d ".venv" ]; then
    info "Creating virtual environment..."
    "$PYTHON" -m venv .venv
    success "Virtual environment created"
else
    success "Virtual environment already exists"
fi

info "Installing dependencies (this takes ~30 seconds)..."
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
success "Dependencies installed"

# ── 3. .env setup ─────────────────────────────────────────────────────────────
if [ -f ".env" ]; then
    success ".env already exists — skipping credential setup"
else
    echo ""
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo -e "${BOLD}  Set up your AI provider credentials${RESET}"
    echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
    echo ""
    warn "You need session cookies from the AI sites you want to use."
    warn "Skip any providers you don't need (just press Enter)."
    echo ""

    # ── Claude ────────────────────────────────────────────────────────────────
    echo -e "${BOLD}── Claude (claude.ai) ──${RESET}"
    echo "  1. Open chrome://settings/cookies in Chrome"
    echo "     OR go to claude.ai → DevTools (F12) → Application → Cookies"
    echo "  2. Find the cookie named: sessionKey"
    echo "  3. Copy its value (starts with sk-ant-si...)"
    echo ""
    read -rp "  Paste CLAUDE sessionKey (or Enter to skip): " CLAUDE_SESSION_KEY
    echo ""

    # ── Gemini ────────────────────────────────────────────────────────────────
    echo -e "${BOLD}── Gemini (gemini.google.com) ──${RESET}"
    echo "  1. Go to gemini.google.com → DevTools (F12) → Application → Cookies"
    echo "  2. Find cookie: __Secure-1PSID"
    echo "  3. Also find: __Secure-1PSIDTS"
    echo ""
    read -rp "  Paste __Secure-1PSID (or Enter to skip): " GEMINI_1PSID
    if [ -n "$GEMINI_1PSID" ]; then
        read -rp "  Paste __Secure-1PSIDTS: " GEMINI_1PSIDTS
    fi
    echo ""

    # ── ChatGPT ───────────────────────────────────────────────────────────────
    echo -e "${BOLD}── ChatGPT (chatgpt.com) ──${RESET}"
    echo "  1. Go to chatgpt.com → DevTools (F12) → Application → Cookies"
    echo "  2. Find cookie: __Secure-next-auth.session-token"
    echo "  3. The value has TWO parts separated by a '|' — copy the FULL value"
    echo ""
    read -rp "  Paste __Secure-next-auth.session-token (or Enter to skip): " CHATGPT_SESSION_TOKEN
    echo ""

    # ── Kimi ──────────────────────────────────────────────────────────────────
    echo -e "${BOLD}── Kimi (kimi.ai) ──${RESET}"
    echo "  1. Go to kimi.ai → DevTools (F12) → Application → Cookies"
    echo "  2. Find cookie: refresh_token"
    echo ""
    read -rp "  Paste Kimi refresh_token (or Enter to skip): " KIMI_REFRESH_TOKEN
    echo ""

    # ── Admin key ─────────────────────────────────────────────────────────────
    echo -e "${BOLD}── Admin key ──${RESET}"
    echo "  This is the password for the /dashboard admin panel."
    read -rp "  Set an ADMIN_API_KEY (or Enter to generate a random one): " ADMIN_API_KEY
    if [ -z "$ADMIN_API_KEY" ]; then
        ADMIN_API_KEY=$(openssl rand -hex 24 2>/dev/null || python3 -c "import secrets; print(secrets.token_hex(24))")
        echo "  Generated: $ADMIN_API_KEY"
    fi
    echo ""

    # Check at least one provider
    if [ -z "$CLAUDE_SESSION_KEY" ] && [ -z "$GEMINI_1PSID" ] && \
       [ -z "$CHATGPT_SESSION_TOKEN" ] && [ -z "$KIMI_REFRESH_TOKEN" ]; then
        error "You need at least one provider credential to run the router."
    fi

    # Write .env
    cat > .env <<EOF
# AI Router configuration
# Generated by setup.sh — edit manually to update cookies

CLAUDE_SESSION_KEY=${CLAUDE_SESSION_KEY}

GEMINI_1PSID=${GEMINI_1PSID}
GEMINI_1PSIDTS=${GEMINI_1PSIDTS}

CHATGPT_SESSION_TOKEN=${CHATGPT_SESSION_TOKEN}

KIMI_REFRESH_TOKEN=${KIMI_REFRESH_TOKEN}

ADMIN_API_KEY=${ADMIN_API_KEY}

# Port (0 = auto-detect a free port)
ROUTER_PORT=0
EOF
    success ".env created"
fi

# ── 4. Launch ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
success "Setup complete! Starting the router..."
echo -e "${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${RESET}"
echo ""
echo "  Dashboard → http://localhost:<port>/dashboard"
echo "  API       → http://localhost:<port>/v1/chat/completions"
echo ""
echo "  To start it again later: ${BOLD}bash start.sh${RESET}"
echo ""

exec .venv/bin/python -m router.main
