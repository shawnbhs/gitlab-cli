#!/bin/bash
# install.sh — GitLab CLI setup
#
# Creates a venv, installs dependencies, and puts a `gitlab-cli` command
# on your PATH. It does not ask for a token — the first run of the
# program opens its own setup wizard.

set -e

CYAN='\033[0;36m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
DIM='\033[2m'
BOLD='\033[1m'
RESET='\033[0m'

echo ""
echo -e "${BOLD}${CYAN}╔══════════════════════════════════════╗"
echo -e "║      GitLab CLI  —  Install          ║"
echo -e "╚══════════════════════════════════════╝${RESET}"
echo ""

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# ─── 0. python check ──────────────────────────────────────
if ! command -v python3 >/dev/null 2>&1; then
    echo -e "${YELLOW}✖ python3 is not installed.${RESET}"
    echo -e "  Ubuntu/Debian:  sudo apt install python3 python3-venv"
    exit 1
fi

if ! command -v git >/dev/null 2>&1; then
    echo -e "${YELLOW}✖ git is not installed.${RESET}"
    echo -e "  Ubuntu/Debian:  sudo apt install git"
    exit 1
fi

# ─── 1. venv ──────────────────────────────────────────────
echo -e "${CYAN}[1/3]${RESET} venv..."
if [ -d venv ]; then
    echo -e "      ${GREEN}✔ venv already exists${RESET}"
else
    python3 -m venv venv
    echo -e "      ${GREEN}✔ venv created${RESET}"
fi

# ─── 2. dependencies ──────────────────────────────────────
echo -e "${CYAN}[2/3]${RESET} Dependencies..."
./venv/bin/pip install -q --upgrade pip >/dev/null 2>&1 || true
./venv/bin/pip install -q -r requirements.txt
echo -e "      ${GREEN}✔ installed${RESET}"

# ─── 3. global command ────────────────────────────────────
echo -e "${CYAN}[3/3]${RESET} The 'gitlab-cli' command..."

ALIAS_CMD="alias gitlab-cli='${SCRIPT_DIR}/venv/bin/python ${SCRIPT_DIR}/gitlab_cli.py'"

if [ -n "$ZSH_VERSION" ] || [ -f "$HOME/.zshrc" ]; then
    SHELL_RC="$HOME/.zshrc"
elif [ -f "$HOME/.bashrc" ]; then
    SHELL_RC="$HOME/.bashrc"
else
    SHELL_RC="$HOME/.profile"
fi

if grep -q "alias gitlab-cli=" "$SHELL_RC" 2>/dev/null; then
    # Path may have changed since last install, so refresh it.
    sed -i.bak "s|alias gitlab-cli=.*|${ALIAS_CMD}|" "$SHELL_RC"
    echo -e "      ${GREEN}✔ alias updated in ${SHELL_RC}${RESET}"
else
    {
        echo ""
        echo "# GitLab CLI"
        echo "$ALIAS_CMD"
    } >> "$SHELL_RC"
    echo -e "      ${GREEN}✔ alias added to ${SHELL_RC}${RESET}"
fi

# ─── Done ─────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}✔ Install complete.${RESET}"
echo ""
echo -e "${BOLD}Next steps:${RESET}"
echo ""
echo -e "  1. Reload your shell:"
echo -e "     ${YELLOW}source ${SHELL_RC}${RESET}"
echo ""
echo -e "  2. Run it — the setup wizard will ask for what it needs:"
echo -e "     ${YELLOW}gitlab-cli${RESET}"
echo ""
echo -e "${DIM}  You will need a token: GitLab → Settings → Access Tokens"
echo -e "  Scopes: read_api (minimum) or api + read_repository${RESET}"
echo ""
