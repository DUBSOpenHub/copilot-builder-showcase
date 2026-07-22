#!/usr/bin/env bash
# Hackathon Judge installer.
# Authenticated one-command install:
# gh api -H "Accept: application/vnd.github.raw" \
#   repos/DUBSOpenHub/hackathon-judge/contents/install.sh | bash

set -euo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

[[ -n "${HOME:-}" ]] || die "HOME must be set."

REPOSITORY="${HJ_REPOSITORY:-DUBSOpenHub/hackathon-judge}"
REF="${HJ_REF:-main}"
REPOSITORY_URL="${HJ_REPOSITORY_URL:-}"
INSTALL_DIR="${HJ_INSTALL_DIR:-$HOME/.local/share/hackathon-judge}"
BIN_DIR="${HJ_BIN_DIR:-$HOME/.local/bin}"
COMMAND_PATH="$BIN_DIR/hackathon-judge"
VENV_DIR="${HJ_VENV_DIR:-$INSTALL_DIR/.venv}"
TEXTUAL_REQUIREMENT="${HJ_TEXTUAL_REQUIREMENT:-textual>=8,<9}"

require_command git
require_command python3

PYTHON_BIN="$(command -v python3)"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  die "Hackathon Judge requires Python 3.11 or newer."
fi

if [[ -L "$INSTALL_DIR" ]]; then
  die "Install directory must not be a symbolic link: $INSTALL_DIR"
fi

install_checkout() {
  mkdir -p "$(dirname "$INSTALL_DIR")"

  if [[ -n "$REPOSITORY_URL" ]]; then
    git clone --quiet --depth 1 --branch "$REF" -- "$REPOSITORY_URL" "$INSTALL_DIR"
    return
  fi

  if command -v gh >/dev/null 2>&1 && gh auth status --hostname github.com >/dev/null 2>&1; then
    gh repo clone "$REPOSITORY" "$INSTALL_DIR" -- --depth 1 --branch "$REF"
    return
  fi

  git clone --quiet --depth 1 --branch "$REF" -- "https://github.com/$REPOSITORY.git" "$INSTALL_DIR"
}

if [[ -d "$INSTALL_DIR/.git" ]]; then
  printf 'Updating Hackathon Judge...\n'
  git -C "$INSTALL_DIR" fetch --quiet --depth 1 origin "$REF"
  git -C "$INSTALL_DIR" checkout --quiet "$REF"
  git -C "$INSTALL_DIR" pull --ff-only --quiet origin "$REF"
elif [[ -e "$INSTALL_DIR" ]]; then
  die "Install directory exists but is not a Hackathon Judge checkout: $INSTALL_DIR"
else
  printf 'Installing Hackathon Judge...\n'
  install_checkout
fi

[[ -f "$INSTALL_DIR/hackathon_judge.py" ]] || die "Installed checkout is missing hackathon_judge.py."

if [[ -L "$VENV_DIR" ]]; then
  die "Virtual environment directory must not be a symbolic link: $VENV_DIR"
fi

printf 'Preparing the audience projector dashboard...\n'
if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
  die "Could not create the Python virtual environment at $VENV_DIR."
fi
VENV_PYTHON="$VENV_DIR/bin/python"
if ! "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check "$TEXTUAL_REQUIREMENT"; then
  die "Could not install the Textual audience dashboard dependency."
fi
if ! "$VENV_PYTHON" -c 'import textual'; then
  die "Textual installed but could not be imported by $VENV_PYTHON."
fi

mkdir -p "$BIN_DIR"
if [[ -L "$COMMAND_PATH" || (-e "$COMMAND_PATH" && ! -f "$COMMAND_PATH") ]]; then
  die "Command path must be a regular file: $COMMAND_PATH"
fi

printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/hackathon_judge.py" > "$COMMAND_PATH"
chmod 0755 "$COMMAND_PATH"

printf '\n🏆 Hackathon Judge is ready.\n'
printf '   Command: %s\n' "$COMMAND_PATH"
printf '   Try:     hackathon-judge --help\n'

case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '\nAdd this directory to your PATH before using the short command:\n'
    printf '   export PATH="%s:$PATH"\n' "$BIN_DIR"
    ;;
esac
