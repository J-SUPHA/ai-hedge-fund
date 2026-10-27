#!/usr/bin/env bash
# Set up the Hermes side of the aihf research lab.
#
#   lab/hermes/setup.sh                  profiles, MCP server, kanban board (idempotent)
#   lab/hermes/setup.sh --install-roles  ALSO write each profile's SOUL.md from lab/hermes/roles/
#
# Run it against an ISOLATED Hermes so the lab never touches your everyday
# install: HERMES_BIN=~/Desktop/hermes-lab/bin/hermes-lab lab/hermes/setup.sh
# (see lab/hermes/README.md). Defaults to `hermes` on PATH.
#
# SOUL.md is an agent-instruction file, so installing roles is a separate,
# explicit step. Read lab/hermes/roles/*.md before running it.
set -euo pipefail
# A PYTHONPATH from another environment (e.g. a Hermes install) breaks the lab venv.
unset PYTHONPATH PYTHONHOME

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
HERMES="${HERMES_BIN:-hermes}"
LAB_BIN="$REPO/.venv/bin/aihf-lab"
BOARD="hedge-fund-lab"
PROFILES=(director quant redteam ops)
INSTALL_ROLES=0
[[ "${1:-}" == "--install-roles" ]] && INSTALL_ROLES=1

describe() {  # bash 3.2 (macOS) has no associative arrays
  case "$1" in
    director) echo "Research director: turns ideas into falsifiable hypothesis cards, routes impl/red-team work, requests holdout looks. Never codes, approves, or deploys." ;;
    quant)    echo "Quant researcher/engineer: implements alpha models and mandates in a worktree, runs <=5 registered backtests per card via the aihf_lab MCP." ;;
    redteam)  echo "Red team: adversarially re-runs and audits candidates (lookahead, costs, sub-periods, universes, DSR). Returns pass/fail with trial ids." ;;
    ops)      echo "Ops: registry health, paper-fund deployment after human approval, cron ticks, kill switch (halt only), daily reports." ;;
  esac
}

[[ -x "$LAB_BIN" ]] || { echo "missing $LAB_BIN — run: uv venv .venv && uv pip install -e '.[lab]'" >&2; exit 1; }

# 1. The lab itself (registry + vault config). Never re-initialises.
if ! "$LAB_BIN" status >/dev/null 2>&1; then
  "$LAB_BIN" init
fi

profile_home() {
  local h
  h="$("$HERMES" profile show "$1" 2>/dev/null | awk '/^Path:/ {print $2}' || true)"
  echo "${h:-${HERMES_HOME:-$HOME/.hermes}/profiles/$1}"
}

# Refuse to install the lab into the everyday Hermes unless asked explicitly.
target_home="$("$HERMES" profile show default 2>/dev/null | awk '/^Path:/ {print $2}' || true)"
if [[ "$target_home" == "$HOME/.hermes" && "${ALLOW_MAIN_HERMES:-0}" != 1 ]]; then
  echo "refusing: $HERMES points at your main Hermes home ($target_home)." >&2
  echo "Use the isolated install (HERMES_BIN=~/Desktop/hermes-lab/bin/hermes-lab) or set ALLOW_MAIN_HERMES=1." >&2
  exit 1
fi

# 2. Profiles: clone model/provider/keys from the active profile, then wire the lab MCP.
existing="$("$HERMES" profile list 2>/dev/null || true)"
for p in "${PROFILES[@]}"; do
  if ! grep -qE "^[[:space:]]*◆?$p[[:space:]]" <<<"$existing"; then
    # --no-alias: wrapper scripts in ~/.local/bin would shadow other installs' profiles
    "$HERMES" profile create "$p" --clone --no-alias --description "$(describe "$p")"
  else
    "$HERMES" profile describe "$p" --text "$(describe "$p")" >/dev/null 2>&1 || true
  fi
  cfg="$(profile_home "$p")/config.yaml"
  # `mcp list` shows stdio transports as "?", so read config.yaml. A failed probe saves the
  # server with `enabled: false` (e.g. a Hermes built without the `mcp` extra): re-add it.
  if [[ ! -f "$cfg" ]] || ! grep -q "command: $LAB_BIN" "$cfg" \
     || grep -A6 "^  aihf_lab:" "$cfg" | grep -q "enabled: false"; then
    # `mcp add` probes the server and asks "overwrite?" / "enable all tools?"; EOF means cancel.
    printf 'y\ny\n' | "$HERMES" -p "$p" mcp add aihf_lab --command "$LAB_BIN" --args serve-mcp >/dev/null
    if ! grep -q "command: $LAB_BIN" "$cfg" || grep -A6 "^  aihf_lab:" "$cfg" | grep -q "enabled: false"; then
      echo "failed to enable aihf_lab MCP on $p (is the Hermes build missing the 'mcp' extra?)" >&2; exit 1
    fi
  fi
  # Backtests with LLM analysts can take a long time.
  "$HERMES" -p "$p" config set mcp_servers.aihf_lab.timeout 3600 >/dev/null
done

# Lab skills (idea-farming, alt-data-ingest) into every profile, repo path rendered.
for p in "${PROFILES[@]}"; do
  for skill in "$REPO"/lab/hermes/skills/*/; do
    name="$(basename "$skill")"
    dest="$(profile_home "$p")/skills/aihf-lab/$name"
    mkdir -p "$dest"
    sed "s#{{REPO}}#$REPO#g" "$skill/SKILL.md" > "$dest/SKILL.md"
  done
done

# The director routes work: give it the orchestrator kanban toolset.
"$HERMES" -p director tools enable kanban >/dev/null

# 3. The board. Tasks default to the repo; [impl] cards should use worktrees.
if ! "$HERMES" kanban boards list 2>/dev/null | grep -q "$BOARD"; then
  "$HERMES" kanban boards create "$BOARD" --name "Hedge Fund Lab" --icon "📈" \
    --description "aihf research pipeline: [hyp] -> [impl] -> [red] -> [hold] -> [ops]" \
    --default-workdir "$REPO"
fi

# 4. Roles (explicit opt-in).
if [[ $INSTALL_ROLES == 1 ]]; then
  for p in "${PROFILES[@]}"; do
    home="$(profile_home "$p")"
    sed "s#{{REPO}}#$REPO#g" "$REPO/lab/hermes/roles/$p.md" > "$home/SOUL.md"
    echo "installed role -> $home/SOUL.md"
  done
fi

echo
echo "Lab:      $("$LAB_BIN" status | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["lab_dir"], "· research_end", d["research_end"], "· trials", d["trials_counted"])')"
echo "Profiles: ${PROFILES[*]}"
echo "Board:    $BOARD   ($HERMES kanban --board $BOARD ls)"
missing=""
for p in "${PROFILES[@]}"; do grep -q "^# Role:" "$(profile_home "$p")/SOUL.md" 2>/dev/null || missing="$missing $p"; done
if [[ -n "$missing" ]]; then
  echo "Roles NOT installed for:$missing. Review lab/hermes/roles/*.md, then: lab/hermes/setup.sh --install-roles"
else
  echo "Roles:    installed (${PROFILES[*]})"
fi
