# Hermes team for the aihf lab

The lab's agents run on an **isolated Hermes**: a clean clone of
`NousResearch/hermes-agent` at a pinned release, with its own venv, its own
`HERMES_HOME` (`~/.hermes-lab`), its own login, profiles and kanban boards.
Nothing touches your everyday `~/.hermes`. The Hermes source is never edited.

```bash
lab/hermes/install-isolated-hermes.sh            # clone v0.21.7 → ~/Desktop/hermes-lab, build env (+mcp extra)
~/Desktop/hermes-lab/bin/hermes-lab auth add nous --type oauth   # the lab's own login
HERMES_BIN=~/Desktop/hermes-lab/bin/hermes-lab lab/hermes/setup.sh                  # profiles + MCP + board
HERMES_BIN=~/Desktop/hermes-lab/bin/hermes-lab lab/hermes/setup.sh --install-roles  # after reviewing roles/*.md
```

`setup.sh` refuses to run against `~/.hermes` unless `ALLOW_MAIN_HERMES=1`.

| File | What |
|---|---|
| `PROTOCOL.md` | Shared rules: card stages `[hyp]→[impl]→[red]→[hold]→[ops]`, budgets, DSR bar, what needs a human |
| `roles/<profile>.md` | Role prompts for `director`, `quant`, `redteam`, `ops`; `--install-roles` renders them into each profile's `SOUL.md` |
| `setup.sh` | Idempotent: profiles (`--clone --no-alias`), `aihf_lab` MCP on each (timeout 3600), kanban toolset for `director`, board `hedge-fund-lab` |
| `install-isolated-hermes.sh` | Builds the isolated Hermes and the `hermes-lab` launcher |

Use the team:

```bash
L=~/Desktop/hermes-lab/bin/hermes-lab
$L -p director                                   # chat with the research director
$L kanban --board hedge-fund-lab ls              # the pipeline
$L -p quant mcp test aihf_lab                    # check the lab tools
$L gateway start                                 # runs the kanban dispatcher (spawns workers for ready cards)
```

**Upgrading Hermes**: `git -C ~/Desktop/hermes-lab/hermes-agent fetch --tags && git checkout <new tag>`.
Then remove `hermes-agent/.venv` and re-run `install-isolated-hermes.sh`. Profiles and boards live in
`~/.hermes-lab` and survive the rebuild.
