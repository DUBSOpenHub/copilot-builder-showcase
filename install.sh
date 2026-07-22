#!/usr/bin/env bash
# Copilot Builder Showcase installer.
# One-command install:
# bash -o pipefail -c 'gh api repos/DUBSOpenHub/copilot-builder-showcase/contents/install.sh \
#   -H "Accept: application/vnd.github.raw+json" | bash'

set -euo pipefail

die() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

[[ -n "${HOME:-}" ]] || die "HOME must be set."

REPOSITORY="${CBS_REPOSITORY:-${HJ_REPOSITORY:-DUBSOpenHub/copilot-builder-showcase}}"
REF="${CBS_REF:-${HJ_REF:-main}}"
REPOSITORY_URL="${CBS_REPOSITORY_URL:-${HJ_REPOSITORY_URL:-}}"
INSTALL_DIR="${CBS_INSTALL_DIR:-${HJ_INSTALL_DIR:-$HOME/.local/share/copilot-builder-showcase}}"
BIN_DIR="${CBS_BIN_DIR:-${HJ_BIN_DIR:-$HOME/.local/bin}}"
PRIMARY_COMMAND_PATH="$BIN_DIR/showcase"
ADVANCED_COMMAND_PATH="$BIN_DIR/copilot-builder-showcase"
LEGACY_PRIMARY_COMMAND_PATH="$BIN_DIR/hackathon"
LEGACY_ADVANCED_COMMAND_PATH="$BIN_DIR/hackathon-judge"
VENV_DIR="${CBS_VENV_DIR:-${HJ_VENV_DIR:-$INSTALL_DIR/.venv}}"
TEXTUAL_REQUIREMENT="${CBS_TEXTUAL_REQUIREMENT:-${HJ_TEXTUAL_REQUIREMENT:-textual>=8,<9}}"
SKIP_OPTIONAL_MONITOR="${CBS_SKIP_OPTIONAL_MONITOR:-${HJ_SKIP_OPTIONAL_MONITOR:-0}}"

require_command git
require_command python3

PYTHON_BIN="$(command -v python3)"
if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  die "Copilot Builder Showcase requires Python 3.11 or newer."
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
  printf 'Updating Copilot Builder Showcase...\n'
  git -C "$INSTALL_DIR" fetch --quiet --depth 1 origin "$REF"
  git -C "$INSTALL_DIR" checkout --quiet "$REF"
  git -C "$INSTALL_DIR" pull --ff-only --quiet origin "$REF"
elif [[ -e "$INSTALL_DIR" ]]; then
  die "Install directory exists but is not a Copilot Builder Showcase checkout: $INSTALL_DIR"
else
  printf 'Installing Copilot Builder Showcase...\n'
  install_checkout
fi

[[ -f "$INSTALL_DIR/builder_showcase.py" ]] || die "Installed checkout is missing builder_showcase.py."
[[ -f "$INSTALL_DIR/showcase_launcher.py" ]] || die "Installed checkout is missing showcase_launcher.py."

if [[ -L "$VENV_DIR" ]]; then
  die "Virtual environment directory must not be a symbolic link: $VENV_DIR"
fi

printf 'Preparing Copilot Builder Showcase...\n'
if ! "$PYTHON_BIN" -m venv "$VENV_DIR"; then
  die "Could not create the Python virtual environment at $VENV_DIR."
fi
VENV_PYTHON="$VENV_DIR/bin/python"
if [[ "$SKIP_OPTIONAL_MONITOR" != "1" ]]; then
  printf 'Adding the optional run monitor...\n'
  if ! "$VENV_PYTHON" -m pip install --quiet --disable-pip-version-check "$TEXTUAL_REQUIREMENT"; then
    printf 'Warning: the optional run monitor could not be installed. The showcase is still ready.\n' >&2
  elif ! "$VENV_PYTHON" -c 'import textual'; then
    printf 'Warning: the optional run monitor could not be loaded. The showcase is still ready.\n' >&2
  fi
fi

mkdir -p "$BIN_DIR"

for command_path in \
  "$PRIMARY_COMMAND_PATH" \
  "$ADVANCED_COMMAND_PATH" \
  "$LEGACY_PRIMARY_COMMAND_PATH" \
  "$LEGACY_ADVANCED_COMMAND_PATH"; do
  if [[ -L "$command_path" || (-e "$command_path" && ! -f "$command_path") ]]; then
    die "Command path must be a regular file: $command_path"
  fi
done

printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/showcase_launcher.py" > "$PRIMARY_COMMAND_PATH"
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/builder_showcase.py" > "$ADVANCED_COMMAND_PATH"
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/hackathon_launcher.py" > "$LEGACY_PRIMARY_COMMAND_PATH"
printf '#!/usr/bin/env bash\nexec %q %q "$@"\n' \
  "$VENV_PYTHON" "$INSTALL_DIR/hackathon_judge.py" > "$LEGACY_ADVANCED_COMMAND_PATH"
chmod 0755 \
  "$PRIMARY_COMMAND_PATH" \
  "$ADVANCED_COMMAND_PATH" \
  "$LEGACY_PRIMARY_COMMAND_PATH" \
  "$LEGACY_ADVANCED_COMMAND_PATH"

printf '\n🏆 Copilot Builder Showcase is ready.\n'
printf '   Type: showcase\n'
printf '   Then paste project links, one per line.\n'
printf '   Practice first: showcase --demo\n'
printf '   Compatibility aliases: hackathon, hackathon-judge\n'

case ":${PATH:-}:" in
  *":$BIN_DIR:"*) ;;
  *)
    printf '\nOne final setup step: add this directory to your PATH:\n'
    printf '   export PATH="%s:$PATH"\n' "$BIN_DIR"
    printf '\nOr start immediately with:\n'
    printf '   PATH="%s:$PATH" showcase --demo\n' "$BIN_DIR"
    ;;
esac
