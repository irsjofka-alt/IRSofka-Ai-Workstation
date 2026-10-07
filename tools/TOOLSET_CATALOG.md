# 🛠️ Workstation Toolset Catalog

**Directory:** `~/.ai-station/tools/` — tracked in the repository.
**Reference Host:** Pop!_OS 24.04 LTS (COSMIC Desktop, Wayland), PostgreSQL 16.
**Port 8999 is served by the Rust daemon** (`engine-rust/`, binary `irsofka-station-core`),
not Python scripts in this folder.

Every tool provides its own `--help` flag; this document specifies **when** to use a tool and
**what guarantees it provides**, preventing engines from guessing behavior from filenames.

```
tools/
├── db_state.py                Dual-engine database storage adapter (PostgreSQL / SQLite)
├── session_ingestor.py        Records verified CLI actions and turns to SQL
├── mcp_workstation_local.py   MCP server (13 tools) shared across all CLIs
├── cross_verify.py            Auditing tool for cross-engine peer verification
├── local_llm.py               Ollama invoker enforcing strict GPU discipline
├── parallel_tri_engine.py     Parallel tri-engine dispatcher
├── incident_recorder.py       Failure aggregator triggering automated skill creation
├── wayland_actor.py           Wayland / COSMIC desktop screenshots & input automation
├── brain_bridge.py            Links brain memory & skills to project workspaces
└── model_check.py             Local LLM benchmark and validation utility
```

## Tool Documentation

### `db_state.py` — Single Gateway to Factual Memory
PostgreSQL primary, SQLite fallback. `get_db_connection()` returns a tuple `(connection, 'POSTGRESQL'|'SQLITE')`
and callers **must** utilize the corresponding parameter syntax. Running directly (`python3 tools/db_state.py`)
invokes `init_db()` and reports the active database engine. Core tables: `action_log`, `world_memory`,
`quest_tasks`, `session_turns`, `incident_log`, `skills_inventory`, `ai_message`, `player_profile`.

### `session_ingestor.py` — Real Actions, Not Assumptions
`watch` monitors Qoder CLI session logs and writes prompts, commands, and responses to SQL;
`snapshot` writes mechanical handoff state; `sweep_failures` calculates error recurrence grouped by
`error_signature` (paths sanitized to basenames to aggregate identical failures into single signatures).

### `mcp_workstation_local.py` — Workstation Hands & Eyes
13 tools: `take_screenshot_wayland`, `get_hardware_telemetry`, `query_workstation_db`,
`update_quest_task`, `send_desktop_notification`, `control_system_volume`, `read_terminal`,
`send_to_terminal`, `refresh_workstation_ui`, `restart_workstation_daemon`, `ask_peer`,
`check_messages`, `resolve_message`. Responds to all standard MCP RPC calls (`resources/list`,
`prompts/list`, `ping`).

### `cross_verify.py` — Cross-Engine Peer Verification
Reads `config/engines.json`. Three transports: `pane` (live tmux tab — fast, reuses logged-in session),
`cli` (spawns new sub-process), `ollama` (local model). `VERDICT:` is extracted from the **latest**
occurrence to prevent stale scrollback contamination. Offline engines report `UNAVAILABLE`.

### `local_llm.py` — Enforced GPU Discipline
Tri-guard protection: VRAM safety gate before loading (`min_free_mib` from registry), `keep_alive=0`
to prevent lingering memory usage, and `ollama stop` with verified VRAM recovery. If VRAM is
insufficient, executes on CPU/RAM (`--cpu`, `options.num_gpu=0`). `--status` details current GPU holders.

### `parallel_tri_engine.py` — Concurrent Tri-Engine Dispatcher
Parallel dispatcher sharing the engine registry with `cross_verify.py`. Default task timeout is 1800s.
Statuses `TIMEOUT`, `UNAVAILABLE`, and `FAILED` are strictly distinguished.

### `incident_recorder.py` — Recurrent Errors to Learned Skills
Error counts per `error_signature` are logged to `incident_log`. On 5x multiples, it writes
`brain/skills/learned/<signature>.md`, registers the skill in `skills_inventory` (`auto_learned = TRUE`),
and marks the incident `resolved`.

### `wayland_actor.py` — Desktop Vision & Actuation
Captures COSMIC / Wayland screenshots and dispatches mouse/keyboard events. Operates within Wayland
security boundaries: processes without desktop session tokens cannot capture screens, and `ydotool`
requires uinput socket access.

### `brain_bridge.py` — Link, Do Not Duplicate
Connects project workspaces to `~/.ai-station/brain/` via pointer files, keeping memory updates
centralized in a single place.

### `model_check.py` — Empirical Model Validation
Evaluates whether an Ollama model is **worth retaining** on disk. Measures non-empty output, peak VRAM,
verified GPU release (`ollama ps` empty), returned VRAM, and tokens/sec generation speed. Outputs clear
verdicts: retain or remove via `ollama rm`.

## Excluded Tools & Historical Context

`tools/pty_station_server.py` (legacy Python server) is archived locally and gitignored. It is
superseded by `engine-rust/src/main.rs`. Never run or reference the legacy Python server script.
