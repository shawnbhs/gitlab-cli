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
    #
    # Not `sed -i.bak`: that silently overwrites any ~/.zshrc.bak the
    # user already had, replaces a symlinked rc file with a plain copy,
    # and treats `&` or `|` in the install path as sed syntax.
    #   - the backup name is timestamped, so nothing existing is replaced
    #   - the new text is written back THROUGH the original file
    #     (cat >), keeping its inode, mode and any symlink intact
    #   - awk reads the alias from the environment, so it is literal
    RC_BACKUP="${SHELL_RC}.gitlab-cli-$(date +%Y%m%d-%H%M%S).bak"
    cp -p "$SHELL_RC" "$RC_BACKUP"
    RC_TMP="$(mktemp "${SHELL_RC}.gitlab-cli.XXXXXX")"
    trap 'rm -f "$RC_TMP"' EXIT
    NEW_ALIAS="$ALIAS_CMD" awk '{
        i = index($0, "alias gitlab-cli=")
        if (i) { print substr($0, 1, i - 1) ENVIRON["NEW_ALIAS"] } else { print }
    }' "$SHELL_RC" > "$RC_TMP"
    cat "$RC_TMP" > "$SHELL_RC"
    rm -f "$RC_TMP"
    trap - EXIT
    echo -e "      ${GREEN}✔ alias updated in ${SHELL_RC}${RESET}"
    echo -e "      ${DIM}previous version saved as ${RC_BACKUP}${RESET}"
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
echo -e "  Scopes: read_api + read_repository (minimum);"
echo -e "  add api for member/management actions, write_repository to push.${RESET}"
echo ""
