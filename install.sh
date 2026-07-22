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
PRIMARY_COMMAND_PATH="$BIN_DIR/hackathon"
ADVANCED_COMMAND_PATH="$BIN_DIR/hackathon-judge"
VENV_DIR="${HJ_VENV_DIR:-$INSTALL_DIR/.venv}"
TEXTUAL_REQUIREMENT="${HJ_TEXTUAL_REQUIREMENT:-textual>=8,<9}"
SKIP_OPTIONAL_MONITOR="${HJ_SKIP_OPTIONAL_MONITOR:-0}"

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
[[ -f "$INSTALL_DIR/hackathon_launcher.py" ]] || die "Installed checkout is missing hackathon_launcher.py."

if [[ -L "$VENV_DIR" ]]; then
  die "Virtual environment directory must not be a symbolic link: $VENV_DIR"
fi

printf 'Preparing Hackathon Judge...\n'
if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
  die "Could not create the Python virtual environment at $VENV_DIR."
fi
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ "$SKIP_OPTIONAL_MONITOR" != "1" ]]; then
  printf 'Adding the optional run monitor...\n'
  if ! "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check "$TEXTUAL_REQUIREMENT"; then
    printf 'Warning: the optional run monitor could not be installed. The Live Show is still ready.\n' >&2
  elif ! "$VENV_PYTHON" -c 'import textual'; then
    printf 'Warning: the optional run monitor could not be loaded. The Live Show is still ready.\n' >&2
  fi
fi

mkdir -p "$BIN_DIR"

for command_path in "$PRIMARY_COMMAND_PATH" "$ADVANCED_COMMAND_PATH"; do
  if [[ -L "$command_path" || (-e "$command_path" && ! -f "$command_path") ]]; then
    die "Command path must be a regular file: $command_path"
  fi
done

printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/hackathon_launcher.py" > "$PRIMARY_COMMAND_PATH"
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/hackathon_judge.py" > "$ADVANCED_COMMAND_PATH"
chmod 0755 "$PRIMARY_COMMAND_PATH" "$ADVANCED_COMMAND_PATH"

printf '\n🏆 Hackathon Judge is ready.\n'
printf '   Type: hackathon\n'
printf '   Then paste project links, one per line.\n'
printf '   Practice first: hackathon --demo\n'

case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '\nOne final setup step: add this directory to your PATH:\n'
    printf '   export PATH="%s:$PATH"\n' "$BIN_DIR"
    printf '\nOr start immediately with:\n'
    printf '   PATH="%s:$PATH" hackathon --demo\n' "$BIN_DIR"
    ;;
esac
