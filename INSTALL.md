# Installation Guide

This repository contains a personal AI workstation environment published as open code.
Included: engine source code, systemd service units, runtime guardrails, and behavioral contracts.
Deliberately **excluded** from this repository:

| Excluded from Repo | Rationale |
|---|---|
| `brain/` (memory, skills, incident records, handoffs) | Personal workstation knowledge base |
| `config/db_local.json` | Database credentials |
| `logs/`, `*.db`, `*.sql` | Runtime state and audit logs |
| `engines/` | Qoder / Antigravity local session state |

A fresh clone will **explicitly fail to connect to the database** until configured with your own
credentials, preventing credential leaks by design.

License: PolyForm Noncommercial — free to use and modify for noncommercial purposes.

## Prerequisites

Requires Linux with systemd (services run as *user* units without needing root privileges), tmux,
and Python 3.

```bash
# Debian / Ubuntu / Pop!_OS
sudo apt install -y tmux python3 python3-psycopg2 postgresql postgresql-client \
  rustc cargo pkg-config libwebkit2gtk-4.1-dev libgtk-3-dev

# Arch Linux
sudo pacman -S tmux python python-psycopg2 postgresql rust cargo \
  webkit2gtk-4.1 gtk3
```

PostgreSQL is recommended; without it, the workstation falls back automatically to SQLite.
`rustc` and `cargo` are required only if building the native desktop engine GUI from source.

## 0. Configure `~/runtime` Before Installing Anything

To keep `$HOME` clean, all user-installed toolchains, caches, and application data are redirected
to `~/runtime`. This is enforced via standard XDG and environment variables:

```bash
mkdir -p ~/runtime/{cache,local,local/bin,local/share,local/state,cargo,rustup,backups}
```

Add these environment variables to `~/.bashrc` (and to `~/.config/environment.d/irsofka.conf` for
systemd user GUI sessions):

```bash
export XDG_CACHE_HOME="$HOME/runtime/cache"
export XDG_DATA_HOME="$HOME/runtime/local/share"
export XDG_STATE_HOME="$HOME/runtime/local/state"
export PYTHONUSERBASE="$HOME/runtime/local"
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
export CARGO_HOME="$HOME/runtime/cargo"
export RUSTUP_HOME="$HOME/runtime/rustup"
export HISTFILE="$HOME/runtime/local/bash_history"
export PATH="$HOME/runtime/local/bin:$CARGO_HOME/bin:$PATH"
```

The script `bin/relocate_home.sh` moves existing caches from `$HOME` to `~/runtime` and leaves
compatibility symlinks. Run it first in dry-run mode, then with `--apply`.

## 1. Clone into `~/.ai-station`

The codebase expects the exact location `~/.ai-station`:

```bash
git clone https://github.com/irsjofka-alt/IRSofka-Ai-Workstation.git ~/.ai-station
mkdir -p ~/.ai-station/{brain/memory,brain/skills,brain/rules,logs,engines,bin}
chmod +x ~/.ai-station/bin/*.sh
```

Seed the initial `brain/` indices from clean templates:

```bash
cp ~/.ai-station/documents/MEMORY.md  ~/.ai-station/brain/memory/MEMORY.md
cp ~/.ai-station/documents/SKILLS.md  ~/.ai-station/brain/skills/SKILLS.md
```

## 2. Install AI CLIs

```bash
# Qoder CLI
curl -fsSL https://qoder.com/install | bash

# Antigravity CLI (Gemini)
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Both CLIs write state to fixed directories in `$HOME` by default: Qoder writes to `~/.qoder`,
`~/.qodersec`, `~/.qmind`; Antigravity writes to `~/.gemini`. To preserve root directory cleanliness,
move these directories into the workstation and maintain compatibility symlinks:

```bash
for d in qoder qodersec qmind gemini; do
  src=~/.${d}; dst=~/.ai-station/engines/${d}
  [ -d "$src" ] && [ ! -L "$src" ] || continue
  if [ -d "$dst" ] && [ -n "$(ls -A "$dst" 2>/dev/null)" ]; then
    echo "$dst contains existing data — inspect manually"; continue
  fi
  mkdir -p "$(dirname "$dst")" && mv "$src" "$dst" && ln -sfn "$dst" "$src"
done

ls -ld ~/.qoder ~/.qodersec ~/.qmind ~/.gemini         # verify symlinks (type 'l')
```

Execute this step **after** CLI installation and **before** initial login.
The guard `hooks/self_preservation.py` acknowledges these paths as approved shims
(`config/home_shims.json`).

## 3. Shared Workspace Setup

```bash
mkdir -p ~/Documents/ai-workstation/projects
cp ~/.ai-station/documents/AGENTS.md ~/Documents/ai-workstation/AGENTS.md
cp ~/.ai-station/documents/QODER.md  ~/Documents/ai-workstation/QODER.md
cp ~/.ai-station/documents/GEMINI.md ~/Documents/ai-workstation/GEMINI.md
```

`AGENTS.md` is the primary behavioral contract read by all engines, taking precedence over
engine-specific pointer documents.

## 4. Unify Skills Across All CLIs

```bash
~/.ai-station/bin/wire_skills.sh
```

This compiles `brain/skills/` into a single tree (`engines/agents/skills/`) and symlinks it to paths
scanned by each engine: `~/.agents/skills` (Qoder) and `~/.gemini/config/plugins/station-rules/skills`
(Antigravity). Validate anytime with `wire_skills.sh --check`.

## 5. Relational Database (Long-Term Memory)

```bash
createuser --pwprompt --createdb your_username
createdb   -O your_username irsofka_ai_workstation
```

Store credentials in the gitignored configuration file:

```bash
cat > ~/.ai-station/config/db_local.json <<JSON
{ "host": "localhost", "port": 5432, "user": "your_username", "password": "your_password", "dbname": "irsofka_ai_workstation" }
JSON
chmod 600 ~/.ai-station/config/db_local.json
```

Environment variables `STATION_PG_HOST/PORT/USER/PASSWORD/DB` take precedence over this file.
Initialize tables by running:

```bash
python3 ~/.ai-station/tools/db_state.py     # calls init_db() and reports active database engine
```

If PostgreSQL is not running, the system gracefully falls back to SQLite at
`~/.ai-station/brain/workstation.db`. Core tables: `action_log`, `world_memory`, `quest_tasks`,
`session_turns`, `incident_log`, `skills_inventory`, `ai_message`.

## 6. Build the Engine and Install Systemd Units

```bash
cd ~/.ai-station/engine-rust && cargo build --release
mkdir -p ~/.ai-station/bin && cp target/release/irsofka-station-core ~/.ai-station/bin/
~/.ai-station/bin/install_units.sh --dry-run
~/.ai-station/bin/install_units.sh
```

The service `irsofka-tabs.service` hosts the tmux server **outside the daemon cgroup**, preventing
daemon restarts from terminating running AI sessions.

Launch the native desktop interface: `~/.ai-station/bin/launch_gui.sh`.
The prompt composer uses Enter for a new line and Ctrl+Enter to submit.

## 7. Register Guards, Handoff Hooks, and MCP Servers

Register runtime hooks and the workstation MCP server in `~/.qoder/settings.json`:

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.qoder/settings.json")
cfg = json.loads(open(p).read()) if os.path.exists(p) else {}
st = os.path.expanduser("~/.ai-station")
h = cfg.setdefault("hooks", {})
h["PreToolUse"] = [{"matcher": "Bash", "hooks": [{
    "type": "command", "command": f"python3 {st}/hooks/self_preservation.py",
    "name": "station-self-preservation", "timeout": 10}]}]
for ev in ("SessionEnd", "PreCompact"):
    h.setdefault(ev, []).append({"hooks": [{
        "type": "command", "command": f"python3 {st}/hooks/auto_handoff.py",
        "name": "station-auto-handoff", "timeout": 25}]})
cfg.setdefault("mcpServers", {})["local-workstation"] = {
    "command": "python3", "args": [f"{st}/tools/mcp_workstation_local.py"]}
json.dump(cfg, open(p, "w"), indent=2)
print("Registered in", p)
PY
```

For Antigravity, register the MCP server in `~/.gemini/config/mcp_config.json` and enable rules via
the plugin:

```bash
mkdir -p ~/.gemini/config/plugins/station-rules/rules
ln -sfn ~/Documents/ai-workstation/AGENTS.md ~/.gemini/config/plugins/station-rules/rules/AGENTS.md
~/.ai-station/bin/wire_skills.sh
python3 -c "import json,os;p=os.path.expanduser('~/.gemini/config/config.json');d=json.load(open(p)) if os.path.exists(p) else {};d.setdefault('plugins',{})['station-rules']={'enabled':True};json.dump(d,open(p,'w'),indent=2);print('station-rules enabled')"
```

## 8. Health Check Verification

```bash
ai-station recovery 40                                   # verify action history & handoffs
curl -s localhost:8999/api/stats | head -c 300
tmux -L irsofka ls
cd ~/.ai-station/hooks && python3 test_self_preservation.py   # verify safety guard suite
~/.ai-station/bin/wire_skills.sh --check
```

Inside Qoder CLI, running `/skills` should list the identical skill packs recognized by Antigravity.

## Core Takeaway

The pair of `hooks/self_preservation.py` and `hooks/auto_handoff.py` guarantees that AI agents
operate safely without terminating background processes or losing context during compaction —
solving the primary operational challenge of autonomous multi-agent environments.
