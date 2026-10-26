#!/usr/bin/env bash
# Build an ISOLATED Hermes for the aihf lab, separate from your everyday Hermes.
#
#   lab/hermes/install-isolated-hermes.sh [--ref v0.21.7] [--dir ~/Desktop/hermes-lab] [--home ~/.hermes-lab]
#
# What you get:
#   <dir>/hermes-agent/          clean clone of NousResearch/hermes-agent at --ref (never edited)
#   <dir>/hermes-agent/.venv     its own environment, built by Hermes' PM with the `mcp` extra
#   <dir>/bin/hermes-lab         launcher: HERMES_HOME=<home>, so state never touches ~/.hermes
#   <home>/                      its own config, auth, profiles, kanban boards, Python tool store
#
# Then:  <dir>/bin/hermes-lab auth add nous --type oauth     (its own login)
#        HERMES_BIN=<dir>/bin/hermes-lab lab/hermes/setup.sh [--install-roles]
set -euo pipefail
unset PYTHONPATH PYTHONHOME

REF="v0.21.7"
DIR="$HOME/Desktop/hermes-lab"
LAB_HOME="$HOME/.hermes-lab"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref) REF="$2"; shift 2 ;;
    --dir) DIR="$2"; shift 2 ;;
    --home) LAB_HOME="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
[[ "$LAB_HOME" != "$HOME/.hermes" ]] || { echo "--home must not be your main ~/.hermes" >&2; exit 1; }

SRC="$DIR/hermes-agent"
mkdir -p "$DIR/bin" "$LAB_HOME"
if [[ ! -d "$SRC/.git" ]]; then
  git clone --branch "$REF" https://github.com/NousResearch/hermes-agent.git "$SRC"
fi

# PM needs a Python 3.14 to start; borrow one read-only, then the build provisions
# the lab's own interpreter into $LAB_HOME/tools.
BOOT_PY="$(ls -d "$LAB_HOME"/tools/python-3.14*/bin/python3 "$HOME"/.hermes/tools/python-3.14*/bin/python3 2>/dev/null | head -1 || true)"
BOOT_PY="${BOOT_PY:-$(command -v python3.14 || true)}"
[[ -n "$BOOT_PY" ]] || { echo "need a Python 3.14 to bootstrap Hermes' PM" >&2; exit 1; }

if [[ ! -x "$SRC/.venv/bin/hermes" ]]; then
  (cd "$SRC" && HERMES_HOME="$LAB_HOME" "$BOOT_PY" -m pm.build_env --source . --out .venv --extra mcp)
fi

cat > "$DIR/bin/hermes-lab" <<EOF
#!/bin/sh
# Isolated Hermes for the aihf research lab: its own checkout, venv and HERMES_HOME.
# Never touches ~/.hermes (the everyday app).
unset PYTHONPATH PYTHONHOME
export HERMES_HOME="$LAB_HOME"
exec "$SRC/.venv/bin/hermes" "\$@"
EOF
chmod +x "$DIR/bin/hermes-lab"

"$DIR/bin/hermes-lab" --version | head -1
"$DIR/bin/hermes-lab" profile show default | awk '/^Path:/ {print "home:    " $2}' || true
echo "next:    $DIR/bin/hermes-lab auth add nous --type oauth"
echo "         HERMES_BIN=$DIR/bin/hermes-lab $(cd "$(dirname "$0")/../.." && pwd)/lab/hermes/setup.sh"
