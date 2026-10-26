#!/usr/bin/env bash
# Set up the Hermes side of the aihf research lab.
#
#   lab/hermes/setup.sh                  profiles, MCP server, kanban board (idempotent)
#   lab/hermes/setup.sh --install-roles  ALSO write each profile's SOUL.md from lab/hermes/roles/
#
# SOUL.md is an agent-instruction file, so installing roles is a separate,
# explicit step. Read lab/hermes/roles/*.md before running it.
set -euo pipefail
# A PYTHONPATH from another environment (e.g. a Hermes install) breaks the lab venv.
unset PYTHONPATH PYTHONHOME

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
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
  h="$(hermes profile show "$1" 2>/dev/null | awk '/^Path:/ {print $2; exit}')"
  echo "${h:-$HOME/.hermes/profiles/$1}"
}

# 2. Profiles: clone model/provider/keys from the active profile, then wire the lab MCP.
existing="$(hermes profile list 2>/dev/null || true)"
for p in "${PROFILES[@]}"; do
  if ! grep -qE "^[[:space:]]*◆?$p[[:space:]]" <<<"$existing"; then
    hermes profile create "$p" --clone --description "$(describe "$p")"
  else
    hermes profile describe "$p" --text "$(describe "$p")" >/dev/null 2>&1 || true
  fi
  cfg="$(profile_home "$p")/config.yaml"
  if ! grep -q "command: $LAB_BIN" "$cfg"; then  # `mcp list` shows stdio transports as "?"
    # `mcp add` probes the server and asks "overwrite?" / "enable all tools?"; EOF means cancel.
    printf 'y\ny\n' | hermes -p "$p" mcp add aihf_lab --command "$LAB_BIN" --args serve-mcp >/dev/null
    grep -q "command: $LAB_BIN" "$cfg" || { echo "failed to add aihf_lab MCP to $p" >&2; exit 1; }
  fi
  # Backtests with LLM analysts can take a long time.
  hermes -p "$p" config set mcp_servers.aihf_lab.timeout 3600 >/dev/null
done

# The director routes work: give it the orchestrator kanban toolset.
hermes -p director tools enable kanban >/dev/null

# 3. The board. Tasks default to the repo; [impl] cards should use worktrees.
if ! hermes kanban boards list 2>/dev/null | grep -q "$BOARD"; then
  hermes kanban boards create "$BOARD" --name "Hedge Fund Lab" --icon "📈" \
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
echo "Board:    $BOARD   (hermes kanban --board $BOARD ls)"
[[ $INSTALL_ROLES == 1 ]] || echo "Roles NOT installed. Review lab/hermes/roles/*.md, then: lab/hermes/setup.sh --install-roles"
