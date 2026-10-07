# Irsofka AI Workstation

An AI orchestration console for Pop!_OS 24.04 COSMIC (Wayland). A single Rust daemon hosts
multiple interactive AI CLIs inside persistent tmux terminals, presents them in a single
native desktop window, and records **every** action to a relational database — ensuring sessions
can be recovered after any interruption.

This is not an IDE. There is no built-in text editor, file tree, or debugger. The focus is:
multiple AI engines working concurrently, cross-verifying each other, and leaving an immutable
audit trail that can be recovered at any time.

## Installation

A complete, step-by-step setup guide is available in **[INSTALL.md](INSTALL.md)** — prerequisites,
installing AI CLIs without cluttering `$HOME`, shared workspace setup, database configuration,
systemd user units, and verification steps. Summary:

```bash
# 1. Hosted CLIs (official installers; inspect before executing)
curl -fsSL https://qoder.com/install | bash
curl -fsSL https://antigravity.google/cli/install.sh | bash

# 2. Clone the workstation repository
git clone https://github.com/irsjofka-alt/IRSofka-Ai-Workstation.git ~/.ai-station
```

If you are trying Qoder for the first time and wish to support this project, you may register
using this referral link — granting **1,000 credits** upon first payment within 30 days:
<https://qoder.com/activities?referral_code=bBfZBkx5dhkUBQDRBYz29LOXv59SLZiB>

> When your friend registers via your link and makes their first payment within 30 days,
> you get 1,000 Credits and your friend gets 500 Credits.

Using the referral link is entirely optional. The software and license terms are identical
regardless of how you register.

What is **not** included in the git clone: `brain/` (machine memory & skills),
`config/db_local.json` (credentials), `logs/`, and `engines/` (CLI local states). The repository
deliberately tracks only code, rules, and templates — a clean clone will fail cleanly when
connecting to the database rather than shipping personal credentials.

## Architecture

```
                    ┌──────────────────────────────────────┐
   User ──────────► │  native window (tao + wry + webkit)  │
                    └───────────────┬──────────────────────┘
                                    │ HTTP :8999
                    ┌───────────────▼──────────────────────┐
                    │  irsofka-station-core (--headless)   │
                    │  axum · portable-pty · JSONL spool   │
                    └───┬───────────────┬──────────────┬───┘
              tmux -L irsofka           │              │
        ┌───────────────┬───────────────┐   MCP    ┌───▼────────────┐
        │ qoder pane    │ antigravity   │   server │ shell pane     │
        │ (builder)     │ pane (reviewer)│         │ (COSMIC bash)  │
        └───────┬───────┴───────┬───────┘  └───┬───┴────────────────┘
                └───────────────┴──────────────┘
                                │
                    ┌───────────▼───────────────────┐
                    │ PostgreSQL 16 (SQLite fallback)│
                    │ + brain/ markdown + handoff    │
                    └────────────────────────────────┘
```

Three layers of memory form the core of this system:

| Layer | Medium | Lifespan |
|---|---|---|
| Conversation context | In-memory inside CLI process | Ephemeral, lost when process exits |
| **Long-term memory** | PostgreSQL: `action_log`, `session_turns`, `world_memory`, `quest_tasks`, `incident_log`, `ai_message` | Permanent, cross-directory & cross-engine |
| Grimoire | `brain/**/*.md` (rules, skills, identity) | Permanent, readable by human & AI |

## Directory Map

```
bin/            daemon launcher, `ai-station` CLI hub, deployment, backup & restore scripts
engine-rust/    Rust source: daemon + GUI (axum, portable-pty, tao, wry)
tools/          log→SQL ingestor, MCP server, cross-engine verifier, local LLM, DB adapter
hooks/          session safety guards + automated handoff recorder
config/         engines.json (registry), slots.yaml; runtime files generated at startup
~/runtime/      OUTSIDE $HOME: cache, data, toolchains, package installs, backups/
```

## Running the Workstation

Requirements: Rust (tested on 1.99), PostgreSQL 16 (or automatic SQLite fallback), `tmux`,
`python3` + `psycopg2`.

```bash
cd engine-rust && cargo build --release
install -m 0755 target/release/irsofka-station-core ~/.ai-station/bin/
~/.ai-station/bin/launch_gui.sh
```

Systemd user units: `irsofka-ai-workstation` (daemon), `irsofka-tabs` (tmux server, isolated
outside daemon cgroup), `irsofka-action-log` (log→SQL ingestor), `irsofka-memory-backup` (timer).

### Database Credentials (Not Tracked in Git)

PostgreSQL credentials resolve in order: environment variables → `config/db_local.json` →
default local trust socket.

```bash
cat > ~/.ai-station/config/db_local.json <<'JSON'
{ "host": "localhost", "port": 5432, "user": "your_username",
  "password": "your_password", "dbname": "irsofka_ai_workstation" }
JSON
chmod 600 ~/.ai-station/config/db_local.json
```

A clean clone without this file will fail connection openly rather than leaking credentials.
Station owner identities also resolve via environment: `STATION_OWNER_NAME`,
`STATION_EMAIL_QODER`, `STATION_EMAIL_ANTIGRAVITY`.

## Tabs Survive Daemon Restarts

Tabs run under `tmux -L irsofka`, completely decoupled from the daemon cgroup. `run_tab.sh`
automatically appends `--continue` when `cli_profiles.json` enables `continue_session`.

```bash
curl -s localhost:8999/api/workspace | grep -o '"backend":"[a-z]*"'   # must return "tmux"
tmux -L irsofka ls
```

Changes to `gui.html` are dynamically reloaded on each request.
Changes to `main.rs` require a rebuild, deployment, and daemon restart.

## Context Recovery

```bash
ai-station recovery 40    # action history + handoff, runnable from any directory
ai-station handoff        # active quests and handoff summaries only
ai-station snapshot       # write mechanical handoff snapshot immediately
```

The `SessionEnd` and `PreCompact` hooks write mechanical handoff records automatically —
capturing real executed commands rather than hallucinated post-compaction summaries.

## Cross-Engine Verification

When one AI engine is uncertain about code, the peer engine verifies it. Registered engines
live in `config/engines.json`.

```bash
ai-station verify gemini --file engine-rust/src/main.rs
ai-station verify gemini "Claim: Function X is safe because Y"
python3 tools/local_llm.py --status          # inspect free VRAM, RAM, and GPU holders
python3 tools/local_llm.py --tier verify "..."
```

| Engine | Role | Notes |
|---|---|---|
| Qoder | Builder | Massive context window (1M); primary code implementer |
| Antigravity / Gemini | Reviewer | Equipped with tools to inspect code before rendering verdicts |
| Local LLM (Ollama) | Validator | Offline; tiers: `light` / `verify` / `heavy`. Returns `UNAVAILABLE` when models are not loaded |

Both CLIs share the same local MCP server, allowing **bidirectional** peer verification via the
`ai_message` database table: `ask_peer`, `check_messages`, `resolve_message`. Disagreements are
flagged as `DISPUTED`, never silently resolved.

## GPU Resource Discipline

GPU VRAM is shared between desktop compositor, graphical engines, and local AI models.
Local LLMs operate under strict constraints:

1. **VRAM Gate**: Verifies available VRAM before loading. If insufficient, falls back to CPU/RAM (`num_gpu=0`).
2. **`keep_alive=0`**: Unloads models immediately after response generation rather than lingering.
3. **`ollama stop`**: Enforces memory deallocation and asserts VRAM recovery.

```bash
ollama ps        # must be empty following verification
```

## MCP Server `local-workstation`

Registered in both AI CLIs. Exposes 13 tools: hardware telemetry, Wayland desktop screenshots,
system notifications, volume control, SQL state queries, quest task logging, terminal PTY
read/write (`read_terminal`, `send_to_terminal`), UI refresh, daemon restart, and inter-engine
peer messaging.

## Safety Guardrails

Two critical rules are enforced by hooks rather than mere guidelines:

- **Never** run `pkill -f "irsofka-station-core"` — this terminates the production daemon,
  triggering systemd respawn and killing active PTY sessions.
- **Never** run `tmux -L irsofka kill-server` — this destroys all running AI tabs and shells.

`hooks/self_preservation.py` also blocks destructive file system operations (`rm -rf brain/`,
`DROP/TRUNCATE`, `DELETE` without `WHERE`, empty file redirects to memory files) and prevents
unauthorized root `$HOME` creation by directing runtime artifacts to `~/runtime`.
Verification suite: `hooks/test_self_preservation.py`.

## Backups & State Preservation

```bash
~/.ai-station/bin/station_backup.sh          # run manual backup
systemctl --user list-timers irsofka-memory-backup.timer
~/.ai-station/bin/station_restore.sh --check # test restore to temporary DB (production safe)
```

Automated hourly backups with `Persistent=true` catch up after power cycles, retaining 24 hourly
and 30 daily snapshots in `~/runtime/backups/`. Restore operations are verified against a temporary
database before acceptance.

## License

Code in this repository is licensed under the **PolyForm Noncommercial License 1.0.0** — free to
use, modify, and distribute for noncommercial purposes. Full terms in [`LICENSE`](LICENSE).

Third-party dependencies maintain their respective licenses: xterm.js (MIT) and Inter / JetBrains Mono
fonts (SIL OFL 1.1) — see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

> Required Notice: Copyright © 2026 irsjofka-alt
