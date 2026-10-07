# 🗺️ Roadmap — Irsofka AI Workstation

Latest status: **October 7, 2026.** Every verified milestone includes reproducible evidence;
untested components are explicitly labeled. This document consolidates and supersedes
`ai_workstation_master_plan.md` and `PROJECT_SUMMARY_IRSOFKA_AI_WORKSTATION.md` along with `README.md`.

## Phases

```
F1 Brain & Rules Foundation       ✅ Completed
F2 Rust PTY Terminal Engine       ✅ Completed
F3 Telemetry & SQL State          ✅ Completed
F4 Native Rust Window GUI         ✅ Completed
F5 Memory & Session Integrity     ✅ Completed (Oct 6)
F6 Cross-Verification Ecosystem   🟡 In Progress
F7 Creative Studio (ComfyUI/UE5)  ⬜ Planned
F8 Station — System of Record     ⬜ Planned
```

## F1–F4 — Foundation (Completed & Re-verified Oct 6)

Dual-mode Rust daemon `irsofka-station-core` (`--headless` + native tao/wry desktop window), three
interactive terminal tabs, RTX 3060/RAM/disk telemetry, PostgreSQL 16 with automated SQLite fallback,
hardware MCP server, zero external browser runtime dependency.

## F5 — Memory & Session Integrity ✅ (Completed & Verified Oct 6)

Resolves historical issues where sessions terminated upon daemon restarts and multiline prompts were
prematurely sent upon pressing Enter. Root cause resolved: release binaries were built but not deployed.

Verified improvements:

| Improvement | Evidence |
|---|---|
| Prompt Composer: Enter = new line, Ctrl+Enter = send, Ctrl+Shift+Enter = line-by-line | `curl :8999/` serves `<textarea>` + `handleKey()` with `e.ctrlKey` |
| Tabs hosted in tmux outside daemon cgroup | `/api/workspace` tabs report `backend:"tmux"`; daemon adopts panes (`tmux_adopt`, stable `pane_pid`) |
| Restart-proof sessions | 5× daemon restarts on Oct 6 maintained session `63313f58`; shell cgroup isolated in `irsofka-tabs.service` |
| `--continue` fail-safe | `cli_profiles.json` `continue_session:true` → `run_tab.sh` applies flag automatically |
| Enter dispatched as `\r` instead of `\n` | Fixed legacy bug where prompts were populated into input fields but not submitted |
| Automated handoffs | Hooks `SessionEnd` + `PreCompact` → recorded to `world_memory` & `brain/memory/projects/handoff_auto_*.md` |
| Self-preservation guardrails | `hooks/self_preservation.py` passes 57/57 test cases |
| Verified self-healing | `incident_log` tracks real failures; recurring failures trigger `auto_learned=TRUE` skill creation |
| Honest status dispatcher | Offline engines report `UNAVAILABLE` rather than false `COMPLETED`; writes go through PostgreSQL adapter |
| Backups & restore test | Hourly timer; `station_restore.sh --check` verifies clean restore to temporary DB (2,973 action records) |
| Credentials removed from code | `db_local.json` (gitignored) + environment variables; 0 secrets in git history |
| Clean `$HOME` enforcement | 4.7 GB moved to `~/runtime`; safety guard blocks unapproved dotfiles in `$HOME` |

## F6 — Cross-Verification Ecosystem 🟡

Completed:
- `ai-station verify` — Bidirectional verification between Qoder and Gemini. Verified: Gemini rejects invalid claims and validates sound logic referencing exact source lines (`main.rs:1384`, `1426-1431` — line numbers predate the Oct 7 source split).
- `config/engines.json` — Configuration-based engine registry allowing new engines without code modifications.
- Message box `ai_message` + MCP tools `ask_peer` / `check_messages` / `resolve_message`.
- `local_llm.py` — Tri-guard GPU resource management with VRAM safety gates and CPU/RAM fallback.

Pending:
- ⬜ **Tier `heavy` deployment:** `phi4:14b` evaluation under concurrent GPU workloads.
- ⬜ Real-world cross-engine stress testing with simultaneous ComfyUI / graphical workloads.
- ⬜ Antigravity quota telemetry widget in the GUI (data available via `/api/usage`).

### Verified Milestones (Oct 6)
- ✅ `parallel_tri_engine.py` dynamically loads `config/engines.json`. Legacy hardcoded models removed; `never_load_if` policy enforced: if available VRAM is insufficient, local execution reports `UNAVAILABLE` to PostgreSQL and identifies current GPU processes. Ollama requests pass configured `keep_alive` values.
- ✅ **Privacy redaction in daemon:** `/api/stats` and `/api/usage` sanitize personal identification (emails, real names, user avatars) before exposing metrics.
- ✅ **Toolset catalog updated:** `tools/TOOLSET_CATALOG.md` accurately documents the Rust daemon rather than deprecated Python scripts.
- ✅ **Stale SQLite databases archived:** Stale database copies moved to backups to avoid divergence from PostgreSQL.
- ✅ **Local engines evaluated:** `qwen3.5:4b` and `qwen3.5:9b` benchmarked with `model_check.py` (43.1 tokens/sec, full VRAM deallocation verified).
- ✅ **Zero-response bug resolved:** Set `num_ctx=12288`, `num_predict=1200`, and `think=false` for local verification models to prevent reasoning token starvation.
- ✅ **Symlink guard verified:** Fixed symlink nesting bug (`ln -sfn`) with directory safety guards.
- ✅ **Handoff git tracking corrected:** Accurate repository detection relative to `~/.ai-station` root directory.

### Verified Milestones (Oct 7)

- ✅ **Daemon source split by domain:** `main.rs` reduced from 2,261 to 234 lines. Startup, window and
  the router table are all that remains; behaviour moved to `paths`, `profile`, `terminal`, `spool`,
  `probe`, `engineinfo`, `save_state`, `procinfo` and `api/{workspace,engine,stats,terminal,desktop,daemon,static_files}`.
  Release build: zero warnings. Verified live after deploy — `/api/cli/config` 200, `/api/screenshot/latest`
  772 KB, `/api/term/history` 512 KB, GPU/RAM/disk populated, three tabs `alive`, `db_engine: POSTGRESQL`.
- ✅ **Two portability defects closed during the move:** the PostgreSQL default user was a compiled-in
  constant (`"irsofka"`) that made foreign clones attempt login with an account that is not theirs; and
  `count()` interpolated table names into SQL without validation.
- ✅ **Generated architecture map:** `bin/arch_map.sh` writes `brain/memory/projects/ARCHITECTURE.md`
  (folder → file → purpose), reading each purpose from the file's own header comment — no second copy to
  drift. 68 files mapped, 0 undocumented; refreshed at the end of every deploy; `--check` exits non-zero
  on undocumented files. Contract §11 records the rule for new files.
- ✅ **Memory restore briefing corrected:** duplicate `handoff_auto_*` rows no longer crowd out real
  memory; empty handoffs are neither written nor displayed; session id recovered from the transcript
  filename when the hook payload omits it; budget raised so the briefing no longer truncates its own
  pointer lines.
- ✅ **Binary backup rotation:** `deploy_engine.sh` keeps the 8 newest backups; `bin/` dropped from
  114 MB to 50 MB.
- ✅ **Call workers are cut, not kept (contract §4).** Any process that speaks to a model outside the
  operator's tmux panes is a *call worker*: it exists for one action and must die with its whole
  process group once its answer has been read. `tools/call_workers.py` decides what is persistent by
  asking — panes from the tmux socket, engines from `config/engines.json`, at the moment of use — and
  never from a written-down PID. Python dispatchers go through `run_grouped`, Rust probes through
  `probe::run_bounded`; a deploy now fails while an engine binary is spawned outside them, or while a
  stray worker is alive. Six defects were found by measurement rather than by reading:
  `subprocess.run(timeout=…)` kills the child and leaves the grandchild holding its RSS;
  `/bin/kill -KILL -PGID` exits 0 having killed nothing, because procps reads the leading negative
  number as a signal spec (`kill -KILL -- -PGID` is the form that works) — a Rust test asserts it, and
  the test was only proven to have teeth by deleting `--` and watching it fail; `tmux window_name` is
  `bash` in all four sessions, so the pane probe found one persistent process where three exist;
  `st_atime` of `/proc/<pid>/stat` is touched by every read and can never report a process's age;
  the two Claude entries share one `binary` value, so a dict keyed by binary collapsed the engine list
  to two entries; and identity by name alone classified the Qoder desktop IDE's 15 Electron processes
  as 2.18 GB of leaked workers — one `--cut` would have closed the operator's editor instead of a leak.
  Identity is now the resolved executable directory plus the name prefix, which still recognises a CLI
  that self-updated under it (`agy` runs from a file marked `(deleted)`) and rejects its neighbours.
  End-to-end proof on a real leak: an orphaned `qoder -p` at 259.6 MB was flagged, cut by group, the
  three workspace panes survived, audit back to zero.
- ✅ **The same file audited by an engine that is not the one that wrote it.** Asked independently,
  per contract §4: `claude-opus` returned nothing usable — headless `agy -p` tried to call a tool it
  cannot approve and was auto-denied, and `cross_verify` recorded `FAILED` rather than a pass, which
  is the documented limit of that tier. The `gemini` reviewer answered (the harness reported `TIMEOUT`
  because it answered after the window closed; the pane content was read separately, so the dispatch
  record stays honest and the verdict still had to be checked by hand) and returned **FAIL** with four
  findings. Three were real, and all three were catastrophic rather than cosmetic:
  1. `pane_roots()` returned `{}` when tmux could not be read. Empty and unknown are not the same
     fact: an empty persistent set marks every pane process as a stray. Demonstrated live with the
     kill path neutralised — with a bogus socket the old code reported `ok: true` and named
     **pid 498148 (`agy`, 271.3 MB) and pid 727828 (`qoder`, 827.6 MB)**, the operator's own two
     sessions, as things to cut. `pane_roots` now returns `None` for unreadable, the audit answers
     `ok: false`, and `--cut` refuses to act on an unknown set.
  2. `cut_strays()` signalled every PGID it found, and `killpg` reaches the whole group. A worker
     that escaped its pane by re-parenting still carries the pane's PGID, so the cut could kill the
     operator's CLI while killing the leak. A group is now killed whole only when no persistent PID
     stands in it; otherwise the cut narrows to the individual stray PIDs and says so. A stray whose
     own PID is in the persistent set is skipped outright.
  3. The group was swept only on timeout. A CLI that exits 0 after daemonising a worker leaves that
     group leaderless and resident — the same leak with a friendlier exit code. Both `run_grouped`
     and `probe::run_bounded` now sweep on the success path as well.
  Finding 4 — `setsid()`/double-fork escapes any PGID signal — is a real boundary that cannot be
  fixed by this mechanism; it is closed later, by identity, at the next audit or deploy gate, and the
  contract now says a green audit is not proof that no such worker exists.
  Each of the three fixes was proven to have teeth the same way as before: remove the fix, watch the
  test fail, restore. Removing the success sweep leaves one grandchild alive in the Rust test and one
  in the Python selftest. `cargo test` is now 3 tests and is a deploy gate.

## F7 — Creative Studio ⬜

Planned integration: ComfyUI HTTP API (`:8188`) integration via `comfy_workflow` MCP tool sharing the same VRAM gate to prevent resource contention with local LLMs and graphical viewports.

## F8 — Station: System of Record ⬜

**What it is.** The Workstation (this cockpit) is where the operator *gives orders*; it must stay
open and is terminal-centric. The Station is where results are *seen* — a human-facing, modern web
view of inputs, outputs, reports and settings, modelled on an ERP: one system of record that every
other tool writes into.

Body and brain already exist (PostgreSQL, hooks, engines); the hands are still being built; the face
is not there yet. F8 is the face.

### Two invariants (the cheap-to-skip, expensive-to-revert part)

1. **One vocabulary.** `job`, `artifact`, `cost`, `role` are defined exactly once, in the schema.
   A second definition anywhere guarantees two reports that disagree, and nothing identifies which
   one is true. The cost of an ERP is paid in schema design, not in UI.
2. **Reports are derived, never editable.** No field on a report screen may be written by hand.
   One manual correction is enough for the whole ledger to lose its authority.

Consequence for build order: the ledger is finished before the dashboard. A clean interface on top
of a partial ledger displays wrong numbers confidently — and the operator trusts the neatness.

### Modules and current state

| ERP concept | Station equivalent | Today |
|---|---|---|
| Master data | teams, roles, verifier per role | `config/slots.yaml` + `config/engines.json` exist; no face |
| Transactions | prompts, commands, responses, failures | `action_log`, `session_turns`, `incident_log`; no face |
| Warehouse | images, models, commits, deploys | **no table exists** |
| Work order | project → phases → DONE | `quest_tasks` only, no phases, no team binding |
| Reporting | daily digest, weekly progress | none |
| Treasury | credits and quota left, cost per engine | none — raw data is already in transactions |

### Build order (agreed with the operator, Oct 7)

- ⬜ **F8.1 Warehouse.** `artifacts` table (path, kind, producer engine, originating action id,
  workspace, checksum, created_at) written by the ingestor, not by the AI that made the file.
  *Acceptance:* generate an image through ComfyUI and a git commit; both appear in the Station
  within one ingest cycle, with the command that produced them linked.
- ✅ **F8.2 Treasury.** Daily cost and remaining quota per engine, read from `action_log` plus
  CLI-reported usage. *Acceptance:* the numbers match what each CLI itself reports for the same
  day, or the row is marked `UNAVAILABLE` — never estimated.
  Shipped: `engine_quota` + a probe loop in the ingestor (`agy -p /usage`, `agy -p /credits`,
  and Qoder's `/usage` panel read from an isolated tmux socket), `/api/treasury`, and the
  Treasury view on the Station page. Measured, not assumed: Antigravity has no daily cost
  window at all (5h + weekly only) and Qoder exposes no per-turn credit field, so both are
  reported as `UNAVAILABLE` with the reason the data itself gives. Local tiers are
  `UNSUPPORTED`, which is a different statement and stays a different word.
- ✅ **F8.3 Master data face.** Editor for slots and engine registry: pick model per role, save.
  Writes go through schema validation, keep a timestamped backup, and refuse to save a registry
  that no engine can start from. *Acceptance:* change the verifier to another model, and the next
  `ai-station verify` run resolves the new one from config — never from memory.
  Shipped: `tools/master_data.py` owns the rules — it validates against `agy models`,
  `qoder --list-models`, the pulled ollama catalogue and the meter's own window names, and it
  refuses a role that restates the model its registry key already defines — `/api/master` owns
  the files (whitelist of writable paths, timestamped backup, tmp+rename, re-read and re-validate
  from disk after writing, rollback if the read is bad, one `action_log` row per save), and the
  Station's "Roles & verifier" view is the editor. 30 validator selftest cases pass.
  `slots.yaml` is validated but never written: rewriting YAML without `ruamel` would delete the
  contract comment inside it, so the editor refuses a changed `slots` instead of silently eating it.
  Measured, not assumed: the acceptance run set `claude-sonnet` to `claude-sonnet-5-5-medium`,
  `ai-station verify --list` resolved the new id out of config, and the value was set back.
  Two defects were only visible in the diff. `serde_json::Value` sorts object keys, so the first
  version of the endpoint rewrote all 104 lines of `engines.json` without changing a single value
  — fixed by passing raw bytes in both directions, and `deploy_engine.sh` now fails a deploy if
  the daemon ever reorders the registry again. And a candidate that placed an engine key *beside*
  `engines` instead of inside it saved as `200 OK` while changing nothing at all, so the registry
  document now has a declared shape and anything outside it is refused.
- ⬜ **F8.4 Work order.** Project entity with phases (intake → plan → build → debug → done),
  bound team and leader. *Open decision:* whether each phase requires the operator's approval
  before the next one runs. Recommendation on record: yes — with a thin wallet, an unattended
  pipeline is a meter running.
- ⬜ **F8.5 Daily digest.** `/api/day` grouping the day's transactions per session, summarised by
  the local model, raw rows always one click below the summary.

**Open question (F8.1):** record metadata only (path + provenance), or store a small copy of each
result (thumbnail, diff, screenshot) so the Station renders it without touching the producing app?
Metadata-only is cheaper and cannot go stale silently; copies look better and survive deletion of
the original. Not yet decided.

## Known Boundaries & Constraints

- **Token usage counters:** Some upstream CLI tools record `0` tokens in raw logs; the workstation preserves telemetry integrity without fabricating numbers.
- **CLI session working directory binding:** `--continue` flags bind to specific working directories; cross-directory persistence is bridged via PostgreSQL `world_memory` and handoff documents.
- **Unmovable system dotfiles:** Specific system paths (`.bashrc`, `.config`, `.var`, `.nv`, `.qoder`) remain as required by host tools; runtime data resides in `~/runtime`.
- **Guardrail scope:** Hooks intercept commands executed via AI shell tooling; manual operator commands bypass hooks by design.

## Historical Context

Early revisions of `ai_workstation_master_plan.md` and `PROJECT_SUMMARY_...md` noted 100% completion prematurely before daemon and logging layers were fully productionized. On October 6, 2026, those documents were archived in `~/.ai-station/archive/`, and all verified architectural truth was consolidated into `README.md` and this roadmap.
