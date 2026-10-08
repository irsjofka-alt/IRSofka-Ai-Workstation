# 🗺️ Roadmap — Irsofka AI Workstation

Latest status: **October 8, 2026.** Every verified milestone includes reproducible evidence;
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
F8 Station — System of Record     🟡 In Progress (F8.2, F8.3 shipped)
F9 Refleks — Closed Sense-Act Loop ⬜ Planned
F10 Autopilot — Improvement Unattended ⬜ Planned
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

## F8 — Station: System of Record 🟡

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
- 🟡 **F8.4 Work order.** Project entity with phases (intake → plan → build → debug → done),
  bound team and leader. *Open decision:* whether each phase requires the operator's approval
  before the next one runs. Recommendation on record: yes — with a thin wallet, an unattended
  pipeline is a meter running.
  Measured precondition (Oct 8): `quest_tasks` has exactly eight columns —
  `id, title, status, model_assigned, cli_engine, summary, created_at, completed_at` — and holds
  18 `COMPLETED`, 4 `FAILED`, 4 `UNAVAILABLE` rows. It cannot express a check command, a phase, a
  dependency or who is holding the task, so no engine can pick work from it. F8.4 is therefore the
  enabler for F10, not a sibling: an autopilot that cannot read the queue would have to invent one,
  and §12 forbids a second definition of `job`. This migration must add `phase`, `check_command`,
  `depends_on`, `claimed_by`, `lease_expires_at`, `evidence` — and `evidence` is what makes
  "COMPLETED" mean something other than "someone felt like it".
  Shipped the same night, as one unit with F10.2: `tools/work_order.py` is the only module that
  owns the words `job`, `claim`, `lease` and `evidence`, and it creates `quest_tasks` — which no
  code did before, so the eight-column table had been produced by hand all along. `claim()` is a
  conditional `UPDATE` with a read-back, not a `SELECT` then an `UPDATE`, and the lease is stored as
  an integer epoch because SQLite writes UTC where PostgreSQL writes local time — a `TIMESTAMP`
  there is a seven-hour silent drift. `COMPLETED` is refused without evidence, and legacy rows keep
  the status they were written with: `IN_PROGRESS` reads as `CLAIMED` through one `ALIAS` map and is
  never rewritten, because a report edited by hand stops being a report (§12).
  Two of its own bugs came from the live backend, not the test: `psycopg2` returns tuples, so every
  `dict(r)` in the module raised in production while the SQLite selftest stayed green — fixed in
  `run()`, and permanently watched by `selftest --live`, which asserts the same things against the
  backend actually in use and cleans up rows it made under its own marker.
  Remaining, and named so it is not mistaken for done: the Station work-order *view* (this table is
  written and read from the CLI only), and the open decision above about per-phase approval.
  *Acceptance:* `ai-station work-order next` offers an item whose dependencies are complete; the same
  `claim` from two CLIs admits exactly one; `complete` without evidence fails.
  Measured (Oct 8, 01:00): `selftest` 30/30, `selftest --live` on `POSTGRESQL` 11/11, and `next`
  returns item 27 — the seeded roadmap queue, not a legacy row.
- ⬜ **F8.5 Daily digest.** `/api/day` grouping the day's transactions per session, summarised by
  the local model, raw rows always one click below the summary.

**Open question (F8.1):** record metadata only (path + provenance), or store a small copy of each
result (thumbnail, diff, screenshot) so the Station renders it without touching the producing app?
Metadata-only is cheaper and cannot go stale silently; copies look better and survive deletion of
the original. Not yet decided.

## F9 — Refleks: Closing the Sense–Act Loop ⬜

**What it is.** The operator's own decomposition of this machine: head (`brain/`), eyes (Wayland
screenshot), hands (ydotool + tmux), body (one PostgreSQL). Head and body are built. Eyes and hands
are **open-loop**: an engine moves the mouse and then *assumes* the screen followed, presses Enter
and *assumes* the pane answered. F9 replaces each assumption with a measurement, so the cheapest
local model behaves like a careful one — it is not smarter, it is fed facts it cannot hallucinate.

Every item below is a re-graded survivor of `~/Documents/Ide Nyeleneh` 1–6 (reviewed Oct 8 against
measurements, not against enthusiasm). The refused ideas are recorded with a **numeric re-open
threshold**: a refusal without a threshold is a preference, and preferences rot.

- ⬜ **F9.1 Deterministic environment state.** On every command exit the Rust side writes one
  structured row — exit code, cwd, the process tree still alive, and the pane delta — and the
  SessionStart/PreToolUse briefing injects that row instead of letting the model recall what it ran.
  This is the single highest-value item in all six notebooks because it attacks the known failure:
  a model guessing terminal state. *Acceptance:* ask an engine "what is running in this pane" with
  no tools available; it answers from the row, and its answer is byte-checkable against
  `ps`/`tmux list-panes`. A wrong answer is a `DISPUTED`, not a retry.
- ⬜ **F9.2 Action ↔ outcome graph.** Link `action_log` to its verdict (success / FAILED / DISPUTED)
  keyed by command *shape*, then answer one question before an engine types: has this shape failed on
  this path before? Partially exists already — `incident_recorder` promotes an error signature after
  5 repeats (52 incident rows → 2 learned skills). F9.2 lowers the latency from "5 disasters" to
  "1, remembered per path" and turns the answer into injected prompt text rather than a note nobody
  opens. *Acceptance:* reproduce a recorded failure command; the briefing must warn about it before
  the command runs, naming the incident id.
- ⬜ **F9.3 Reflex proof — did the screen actually change.** After any `ydotool` act, diff a tiny
  region (64×64 around the target is enough) before and after, in Rust, and report changed/not-changed
  with the delta. Today the `station-visual` skill can only say "belum terverifikasi visual" when
  capture fails; this converts *unknown* into *known*, which is the whole point of §5.
  *Acceptance:* move the mouse over a window that exists and one that does not; the tool returns
  CHANGED for the first and NO_CHANGE for the second, without an LLM in the loop.
- ⬜ **F9.4 Wake on event instead of asking.** Two cheap event paths replace two poll loops: `epoll`
  on the PTY master fd (a character arriving wakes the reader — the value is *no polling*, not the
  fantasy of beating tmux's measured ~3 ms capture) and PostgreSQL `LISTEN`/`NOTIFY` so a new
  `quest_tasks` row pushes to the daemon instead of being re-selected. Both keep the current API;
  neither changes a number the operator sees. *Acceptance:* a row inserted from `psql` reaches the
  daemon with no timer in between, proven by a log line carrying the row id, and `ai-station workers`
  shows one fewer busy loop.

### F9 — refused, with the threshold that would un-refuse it

| idea (source) | why refused on this machine | re-open when |
|---|---|---|
| `/dev/shm` zero-copy IPC (4.1) | the path it would speed up is measured at **0.88 ms** per recall query and ~3 ms per pane capture; SHM shaves microseconds and adds a concurrency-bug class PostgreSQL cannot see | a measured tick of the ingest loop exceeds **20 ms** |
| time-partitioned memory tables (4.3) | whole DB is **17 MB**, `action_log` 12 599 rows / 7.7 MB; a seq scan already finishes sub-millisecond | `action_log` passes **2 M rows** or DB passes **2 GB** |
| Grimoire in `pgvector` (6.3, 4) | `vector` is **not installed** (`pg_available_extensions` empty for it), and the brain is 42 skill + 24 memory files, which the selective-loading rule already ranks by hand | the index stops ranking right: a needed file exists but grep misses it **twice in a week** |
| `synchronous_commit = off` (4.2) | the contract's premise is "unrecorded history never happened"; this trades the body's memory for throughput the body does not need | never — this one is refused on principle, not on budget |
| Java core: Loom, ZGC, Panama SIMD, R2DBC (2) | the SIMD claim rests on AVX-512 and **`/proc/cpuinfo` shows no `avx512*` flag** on the i7-12700F; a JVM is a second runtime that can call a model, which §1 forbids; and our ceiling is tokens/sec, not thread count | never as a core; a JVM may exist only as a *tool* behind a gate |
| CXL unified memory, kernel module pinning weights into GPU registers, neuromorphic co-processor, NVMe-oF (3.1) | no CXL device in `lspci`; registers are per-thread and hold kilobytes, not 6.6 GB of weights; the NVIDIA driver is binary; a custom kernel module is the one idea here that can stop the box booting | new hardware that actually enumerates, and a reason that survives a benchmark |
| PL/Rust in triggers, `pgai`/`pgvectorscale` in-DB RAG (5.1, 5.2) | neither extension is installed; more importantly a model call born *inside* PostgreSQL bypasses `run_grouped`/`probe::run_bounded`, so `ai-station workers` cannot see it and cannot cut it, and the DB — the body — becomes an engine | never; the database does not speak to models |
| pg_cron as the scheduler (5.4) | `pg_cron` is not installed and would need `shared_preload_libraries` plus a restart of the DB every engine shares; a user timer already exists and is proven (`irsofka-memory-backup.timer`) | never — right tool exists |
| genetic prompt evolver rewriting `brain/**` (1.1) | §12 rule 2: reports are derived. A machine rewriting the contract ends the contract | only as **proposals** into the F10.4 queue, approved by the operator |

## F10 — Autopilot: Improving Without Being Told ⬜

**What it is.** The operator asked (Oct 8): how do you keep improving with no order, forever, without
stopping — and should every choice you offer me be thrown at Antigravity/Gemini instead, since that
pool has longer breath while Qoder's is cheap to spend here.

Measured before designing, so this section is not aspiration:

- Qoder hooks **already fire unattended** — `PreToolUse`, `SessionStart`, `SessionEnd`, `PreCompact`,
  `PostCompact` are configured in `engines/qoder/settings.json`.
- The "AI failed → AI learns" wire **already runs**: `session_ingestor.py` calls `incident_recorder`
  on sweep, and 2 learned skills exist out of 52 incident rows.
- A user-level systemd timer **already works on this box** (`irsofka-memory-backup.timer`, hourly).
- Nothing was scheduled by the assistant: `CronList` → no jobs. So the missing piece is not a
  trigger. It is a **queue with a check command**, a **lease**, and a **stop condition** — and those
  are F8.4, which is why F10 is written after it and not before it.

### Four invariants (the cheap-to-skip, expensive-to-revert part)

1. **Autonomy in selection, never in scope.** The loop may decide *which* queued item to do next; it
   may not decide what is allowed to exist. Scope stays in the contract and in the deploy gate.
   Without this, "always improving" is a process with write access to the repo and no owner.
2. **A tick ends with a check command passing, or a row saying why it did not.** Silence is the one
   output an unattended loop must never produce; an empty tick that invents work is indistinguishable
   from a healthy one. Idle with an empty queue means *stop*, not *find something*.
3. **Unattended never touches:** git history (force push, reset, branch deletion), PostgreSQL
   durability parameters, contract/`§` text, files outside the claimed item's paths, `pkill -f` or
   `tmux kill-server` (§6). Those become `ESCALATED` rows that wait for the operator.
4. **`UNAVAILABLE` stops the tick; it never switches engine silently.** A quota gate that cannot read
   a meter refuses the dispatch (§4). An autopilot that responds to a closed pool by quietly using a
   different one produces work whose cost is attributed to nobody — and F8.2 exists precisely so cost
   has an owner.
5. **Unknown is never idle.** The mirror of the invariant that made the call-worker cut safe
   ("unknown is never empty"): a watchdog that cannot read the pane must not conclude the engine
   stopped. Measured reason — a `gemini` verification dispatch was recorded `TIMEOUT` by the harness
   while the tmux pane had already answered. Absence of a signal is not evidence of rest, and acting
   on that absence resumes a session that was mid-sentence.
6. **The off switch does not require the AI's cooperation.** If the only way to stop an unattended
   loop is to ask it politely, it is not a switch. See F10.8.

### Build order — set by review, not by numbering (Oct 8)

The modules keep their numbers, because the queue rows and `tools/work_order.py` already cite them.
The order they are *built* in does not follow those numbers. The ordering question went to the
`resolver` role (per §4: `gemini`, with every measurement above inside the prompt — a headless worker
cannot go looking), and the answer was `PERLU_KOREKSI` on three points. All three are accepted:

1. **F10.8 before F10.3.** A drainer with no switch is a loop with no off. Building F10.3 first would
   have meant one night of testing the drainer by hand, which is exactly the night nothing goes wrong,
   and then bolting the toggle on afterwards — invariant 6 says the switch is not an accessory.
2. **F8.4 and F10.2 are one unit, not a dependency chain.** Split, they produce a schema someone
   migrates and a lease someone else forgets. Merged, one module owns the row and the claim on it, so
   `claimed_by` cannot exist in a table that `claim()` does not know about.
3. **The circuit breaker was missing entirely.** The design had a resume budget (3 nudges) and nothing
   for the faster failure: an item whose `check_command` exits 1 in 0.2 s is claimed, released, retried,
   and retried — the loop looks extremely busy while it drains the weekly window, and a dirty working
   tree from the crashed run then poisons the *next* item too. Shipped tonight in `work_order.py`:
   `NIGHT_MAX_FAILURES = 3` and `NIGHT_MAX_ITEMS = 25` are counted over the current ON window,
   `claim()` refuses with the figure that tripped, and a working tree that is dirty — or unreadable,
   which is `UNKNOWN`, not clean — holds the queue until a person names the files.

Two of its suggestions were **refused**, and the refusals are written down because a review that is
only agreed with is not a review:

- It proposed `git checkout -- .` to clear a dirty tree before the next item. Refused: that discards
  whatever another engine was mid-way through writing, and invariant 3 puts uncommitted work of someone
  else beyond any tick's reach. The correct behaviour is to refuse the claim and escalate the filenames.
  (The same reasoning rejected keystroke injection on the first night the operator was asleep: F10.7's
  detector shipped at 02:10 and its `CONTINUE` is designed and deferred, not shipped, because a resume
  path that has never been watched over a shoulder is the same class of mistake as an off switch that
  has not been built yet.)
- It deferred F9.1 until F10 lands. Refused: F9.1 reads exit codes, process trees and pane deltas —
  deterministic, read-only, no dependency on the queue — and F10.7 needs exactly those measurements to
  exist. Parking it costs the one thing F10 is built on. F9.1 stays unblocked and parallel.

Order as it will be built: **F8.4 + F10.2** (✅ 01:00) → **F10.8** (🟡 01:30, switch shipped; expiry
and drainer still owed) → **F10.1** (🟡 01:44, broker shipped; nothing forces an engine to call it yet)
→ **F10.7** (🟡 02:10, the sense shipped; the hand — `CONTINUE` — deliberately not) →
**F10.3** → F10.4 / F10.5 / F10.6 / F10.9. The seeded queue encodes this through `depends_on`, so an engine
that skips the order cannot claim.

### Modules

- 🟡 **F10.1 Decision broker.** When an engine would ask the operator "option 1 or 2", it writes a
  `decision` row — question, options, evidence *per option*, and cost per option — then dispatches the
  resolver **role** resolved from `config/slots.yaml` at the moment of use. The answer is recorded in
  the same row, with the model id that actually answered.
  *Why the operator's instinct is right:* `slots.yaml` already names `architect` as a long-breath
  engine (`gemini-3.1-pro`) and `auditor` as Qoder, so routing a choice to the other pool is not a new
  direction — it is the team as configured, finally used for decisions instead of only for code.
  Quota pools are separate (measured in §4), so a decision spent there does not shorten the working
  agent's breath. Decisions are also the cheapest thing to delegate: a few hundred tokens.
  *Where it must not go:* independence. `decider`/`verifier` stay on the Claude tier for exactly the
  reason a peer review is worth something — the reviewer is not the reviewed. A resolver that both
  picks the direction and grades the result is not a reviewer. And invariant 3 outranks the broker:
  irreversible calls are never delegated to any engine, however much quota it has.
  *Honest constraint from measurement:* headless `agy -p` cannot approve tool permissions, so the
  decision must arrive fully evidenced in the prompt — the broker sends the file, not a pointer. One
  `gemini` dispatch was also measured as `TIMEOUT` by the harness while the pane had already answered:
  a broker that reads only the pipe loses real answers, so it reads the message table first.
  *Acceptance:* pose a two-option question with an empty queue; `ai-station decisions` shows the row
  resolved by a model id taken from config, the working engine's own quota untouched, and — if the
  resolver's pool reads `UNAVAILABLE` — the row lands `ESCALATED` rather than answered by someone else.

  **Standing order from the operator (Oct 8, 00:30), now contract §4:** a preference-level choice —
  "option 1 or 2", which of two designs, which queued item next — is **never** put to the operator.
  It is resolved by the `resolver` role and the answer is recorded. Only an item that crosses a named
  threshold waits for a human (F10.8 queue). The operator's stated reason is that the engine choices
  would come out the same anyway, and the point of the notebook is not to be consulted twice.

  The pool the operator named is `gemini` (`gemini-3.8-flash-high`, registered as `reviewer` in
  `cross_verify --list`, and it has its own tools so it can read code before answering). And on the
  fresh meter it is the right call — measured 00:33 the same night, read rather than believed:

  | pool | 5h left | weekly left | whose breath it competes with |
  |---|---|---|---|
  | Gemini Models | 0.9802 | **0.9792** | the `architect` role — the engine writing the code |
  | Claude and GPT models | 1.0000 | **0.6224** | nobody's; 38 % of it went to yesterday's audit dispatches |
  | Qoder add-on credits | — | 451 / 1500 used | the working agent (this engine) |

  So `gemini` has more weekly air than the verify tier does tonight, which is the operator's whole
  point. The one caveat worth keeping in writing: that air is the *architect's* air, so a long Gemini
  implementation session and a long Gemini decision queue draw from the same meter — which is why the
  order is a **ladder of three roles in `config/slots.yaml`**, not a hard-coded name: `resolver` →
  `gemini`, `resolver-alt` → `claude-sonnet` (a pool the architect does not touch), `resolver-local` →
  `local` (`qwen3.5:9b`: no quota at all, only VRAM, and the §7 gate still has to clear it). A meter
  that reads 98 % free tonight is not the meter that is free next week, and §12 forbids picking a model
  from memory — so the rung order is config, editable from the F8.3 face, and the contract text names
  roles only. Each rung skipped must be skipped as `UNAVAILABLE` with its own number, never silently.
  Verified as shipped (Oct 8): the three rungs parse under `active_team`, `master_data.py selftest`
  still passes 30/30, and `cross_verify --list` still resolves the registry — the rungs are role ids
  pointing at existing keys, so nothing was restated.
  The row half of this module is shipped with F8.4: a `decisions` table (question, options, weight,
  rung used, engine key, **model id that answered**, state) plus `work_order.py decide|resolve`, and
  `resolve` refuses `RESOLVED` without a model id — a decision attributed to nobody is the F8.2 failure
  shape again.
  Shipped 2026-10-08 01:44 (`33fee41`): the dispatch itself — `work_order.py broker`, reached by
  `decide --route` / `route <id>`. It stays thin: registry, quota gate and the pane reader already
  belong to `cross_verify.py`, so the broker adds only the ladder (read from `config/slots.yaml` per
  call), the answer contract, and the rule that an exhausted ladder lands `ESCALATED` instead of being
  answered by a substitute. `route --dry-run` prints the ladder and the exact prompt without sending
  anything, which is how the ladder gets checked before quota is spent.
  Three real defects, each found by running it:
  - **The pane echoes the prompt.** The parser read its own instructions: `ALASAN` captured the
    template line, and — far worse — the rules text contains ``PILIHAN: TIDAK``, so a pane that had
    only echoed the prompt would have been recorded as a resolver that *refused*. A conclusion
    invented from our own sentence. `answer_tail()` cuts at the last occurrence of the prompt's final
    line, and a regression check asserts the old reading was wrong.
  - **`resolve_decision` sliced `answer` unconditionally**, and the escalation path passes `None`.
    The first question no machine could settle would have crashed instead of reaching a person —
    precisely the failure this module exists to handle.
  - **`decisions.weight` is `VARCHAR(10)` in PostgreSQL**; `irreversible` is 12 characters. SQLite
    does not check lengths, so the fixture stayed green for the third time on a production-only bug.
    The DDL says 20 and `ensure_schema` widens a narrow existing column, reading the current width
    first so it never rewrites one that is already right.
  Measured: selftest 48/48, live 14/14 on `POSTGRESQL`; decision 3 (two options, evidence and cost per
  option) resolved in 30 s by `gemini-3.8-flash-high` through the `resolver` rung, model id read from
  config; decision 6 born `ESCALATED` under invariant 3 and visible in the Human Decide tab; deploy
  gates green.
  Remaining, and named so it is not mistaken for done:
  - Only rung 1 has ever been exercised for real. The skip-and-fall path is proven by test, not by
    quota.
  - **Nothing forces an engine to use this.** The standing order is honoured by convention plus
    contract text; the enforcement point would be a hook that turns an "option 1 or 2?" into a
    `decide --route`, and it is not built. Until then F10.1 is a door, not a corridor.
  - "Working engine's quota untouched" is evidenced by the dispatch *path* (pane → antigravity, zero
    Qoder calls), not by a credit delta: the Qoder credit meter returned no number tonight, and
    writing 0 there would have been fabrication.
- ✅ **F10.2 Task lease — the actual sync primitive** *(built inside F8.4, one unit — see the build
  order above)*. Two engines coordinating by chatting is two
  engines racing to read the same prose. Add `claimed_by` + `lease_expires_at` (F8.4 columns): one
  `UPDATE ... WHERE claimed_by IS NULL` claims the item, an expired lease is reclaimable, and the
  Station's team view shows who holds what. This is what "bekerja saling sinkron" has to mean, or it
  means duplicate work with nicer vocabulary. *Acceptance:* start the same item from both CLIs;
  exactly one proceeds, the other prints the holder's id and leaves no half-written file.
  Shipped (Oct 8): the race is won by the write, not by reading first — `claim()` is a conditional
  `UPDATE` followed by a read-back, so two CLIs claiming in the same instant cannot both pass a check
  they performed before the other landed. Lease is 900 s without a renewed lease and an expired lease is
  reclaimable by design. Measured against the live backend, not a fixture: `selftest --live` claims
  `selftest-live-<epoch>-A` as `qoder`, then `antigravity`, and the second is refused with the holder
  named in its own message. Remaining here: the Station's *team view* of who holds what — the lease is
  readable from the CLI today and invisible on the page until F8's reporting rows are built.
- 🟡 **F10.3 The drainer.** A systemd user timer runs a deterministic script — no LLM — which picks
  the next unclaimed `PENDING` item, runs its `check_command` before and after, and only then calls an
  engine to do the work. The gate stays the same one that blocks a human: `cargo test -q` and
  `call_workers.py --guard`. *Acceptance:* the timer's log for a whole night contains item ids, exit
  codes, and at least one item it refused to touch because its scope hit invariant 3.
  Two things about its place in the queue, both from the resolver's review: it is built **after F10.8**,
  because a timer that can start this loop needs a switch that can stop it first, and after **F10.7**,
  because a drainer that cannot tell a stalled engine from a finished one re-claims held work. That
  judgement is now owned: `heartbeat()` returns `WORKING` / `AT_REST` / `UNKNOWN` per open claim, and the
  drainer is the only caller allowed to turn `AT_REST` into a keystroke — it must not re-derive the two
  signals in the timer, because a second definition of "resting" is how a machine ends up typing into a
  session that was mid-sentence (§12). `resume_count` is incremented there, never in the sense.
  Its circuit breaker is already written and green, though — counted per ON window, enforced inside
  `claim()` rather than in the timer, so a human running the same commands tonight trips the same
  number. The queue also refuses to re-offer a concluded row: `FAILED` and `UNAVAILABLE` are skipped by
  `next_item()`, which was caught by `next` handing back legacy row 15 (`UNAVAILABLE`, an engine saying
  it could not run the item) as if it were fresh work.
  *Shipped (Oct 8, 02:58), hand deliberately closed.* `tools/drainer.py` is one deterministic tick:
  it reads the ladder (`drain_level`), the switch (`autopilot`) and the breaker (`night_state`) — all
  three derived, none re-implemented — then behaves at the level a human raised it to. `observe` reads
  and logs only; `dispatch` additionally runs an item's `check_command` through `run_grouped`
  (`CHECK_TIMEOUT` 240 s, its own process group, swept on success too) and writes the ledger from the
  measured exit code; `resume` additionally types `CONTINUE` to a claim that `heartbeat()` proved
  `AT_REST`. **Raising the ladder requires a controlling terminal**, and that was measured from inside
  the timer's own environment, not argued: `systemd-run --user` running `work_order.py arm resume`
  exits 1 with `/dev/tty tidak terbuka dari proses ini` and changes nothing. Lowering it never needs
  permission (invariant 6), so `disarm` works from a headless process and is now one of the proven
  `OFF_PATHS`. `MAX_RESUME` is enforced at the write, inside `nudge()`, following the breaker's
  precedent in `claim()`. Invariant 3 is a refusal *before* a process exists: a gate containing an
  undoable verb escalates the item to `HUMAN` and the selftest proves non-execution with a canary file
  (`touch … && rm -rf …`) that is asserted never to appear.
  Measured: `work_order selftest 98/98`, `selftest --live 31/31` on PostgreSQL, `drainer selftest
  43/43` (injected `beats`/`runner`/`sender`, zero processes, zero keystrokes, zero daemon calls, on a
  temp SQLite file — it never touches the live ledger). One real tick from `systemctl --user start
  irsofka-autopilot.service` wrote `{"action":"CLOSED_OFF","level":"observe","mode":"OFF"}` to
  `logs/drainer-2026-10-08.jsonl`; the units are installed but `is-enabled` reads `disabled`, because a
  timer that can start this loop is switched on by a person who is awake to read its log.
  Two defects found by measuring, both fixed: `items_done` was read from a column nothing ever
  incremented, so both surfaces displayed a fabricated 0 forever — it is now derived from the ledger
  through the ON window, and the window filter was wrong in *both* backends (`strftime('%s',…)` returns
  TEXT, and bare `EXTRACT(EPOCH FROM completed_at)` reads naive values as UTC, off by 25 200 s = the
  Asia/Jakarta offset; both now CAST/`::timestamptz` and asserted on the live backend). And F10.3's own
  seeded gate was `systemctl --user list-timers <unit>`, which exits **0 with an empty table for a unit
  that does not exist** — a gate that is always true is an invitation to mark work COMPLETED without
  doing it, so it now demands the installed file *and* an enabled timer, and `selftest` refuses to
  seed any bare `list-timers` gate again.
  Still owed, and it is not code: the acceptance is *a whole night's log*. That needs the operator to
  run `systemctl --user enable --now irsofka-autopilot.timer` and `arm dispatch` from a terminal, then
  read the log in the morning. Until then this bullet stays 🟡 and the hand stays at `observe`.
- ⬜ **F10.4 Proposal queue — the safe form of "improve forever".** The loop may *author* new roadmap
  items, never *adopt* them: proposals land as `PROPOSED` rows carrying the observation, the proposed
  check command, and the files it would touch. Bro approves by editing one column in the Station.
  This is also where the notebook's prompt-evolver idea becomes legitimate: it mutates candidates in a
  queue, and the contract stays written by hand. *Acceptance:* a proposal produced from a repeated
  failure shows up in `/api/decisions` with its evidence, and nothing it suggested has run.
- ⬜ **F10.5 Dream phase.** On idle ≥30 min (the notebook's file 1 idea, on a user timer instead of
  `pg_cron`): re-rank `world_memory`, compact stale session rows into handoffs, run
  `arch_map.sh --check` for documentation debt, and re-scan `incident_log` for a signature at 4
  repeats so the 5th one is already answered. *Acceptance:* one night of dreaming emits a digest row
  and changes no tracked file without naming it in the digest.
- ⬜ **F10.6 Whose breath is spent.** The operator's premise — "Gemini has long breath, Qoder's points
  are plenty and you barely cost anything" — must be enforced, not believed. F8.2 measured that
  Qoder exposes **no per-turn credit field**, so my own cost reads `UNAVAILABLE` to me; the panel Bro
  reads is the source, and I may not reason as if I had measured it. So the budget is expressed as
  *pool + window*, resolved from `config/engines.json` per role, and the autopilot states on every
  tick which pool it is spending. *Acceptance:* a tick that would exceed a configured window fraction
  goes `ESCALATED` with the meter's own numbers.
- 🟡 **F10.7 Heartbeat — is that engine working or resting?** This is the piece the operator described
  directly (Oct 8): when a CLI stops because a session got long, Rust and PostgreSQL must tell
  *"mid-task"* from *"at rest"*, and only the second one gets a `continue`. The daemon already owns the
  state it needs — `action_log` rows arrive per action, the pane is readable in ~3 ms, and
  `quest_tasks.claimed_by` (F10.2) says what is held — so the detector is a join, not a new sense.
  It requires **two independent signals agreeing** before it may send anything: an open claim, no
  `action_log` row for T minutes, and a pane whose text is not changing. Invariant 5 is the whole
  design: if the pane cannot be read, the state is `UNKNOWN`, the tick is logged as `UNKNOWN`, and no
  keystroke is sent. A resume budget per item (default 3) then parks it, because a task that needs
  four nudges is not asleep — it is stuck, and nudging it again is noise that looks like progress.
  *Acceptance:* stop a session mid-item on purpose and the daemon resumes it once, visibly, with the
  item id in the log; then make the pane unreadable and prove it sends nothing at all.
  Shipped as data only (Oct 8, earlier slice): `resume_count` and `due_epoch` columns, and `stalled()`,
  which lists candidates and says in its own docstring that a list is not a decision to type.
  Shipped (Oct 8, 02:10): `heartbeat()` — the sense. It takes two independent readings per open claim
  (pane text sampled twice, and the age of the engine's last `action_log` row) and returns
  `WORKING` / `AT_REST` / `UNKNOWN` **per claim**, with the reason string, both readability flags, the
  lease state and the remaining resume budget. `AT_REST` requires both signals to agree; `UNKNOWN` is
  what any single dead signal produces, and there is no path through the function that sends anything.
  Read live, not from a fixture: with item 32 held by this session it answered `WORKING`, "pane
  menampilkan spinner/esc-to-cancel", `pane_readable: true`, `quiet_seconds: 2`; run with the tmux
  socket pointed at a name that does not exist — the acceptance's "make the pane unreadable" — it
  answered `UNKNOWN`, "pane tidak terbaca — bukan berarti diam", and sent nothing, because there is
  nothing in it to send.
  Three defects found while building it, all of them the kind that only exists in production:
  **the word `heartbeat` had two owners** — lease renewal and this detector shared one name, and adding
  the second definition silently shadowed the first: for a few minutes of working tree the CLI's
  `work_order.py heartbeat <id>` was calling the *detector* with an item id as its `quiet_seconds`
  argument (§12's warning made concrete: two definitions for one word, and the file would have shipped
  that way had the collision not been noticed before the commit). Renewal is now `renew_lease()` /
  `work_order.py lease <id>`; no caller outside the file used the old name — checked across
  `tools/`, `engine-rust/src/`, `systemd/`, `bin/` and the docs before renaming.
  **`action_log`'s time column is `ts` on both backends**, but the SQLite branch of the stalled query
  read `created_at`, which is a `quest_tasks` column. Measured, not assumed: this machine has no
  fallback DB file (`brain/workstation.db` does not exist, PostgreSQL is primary), so a throwaway SQLite
  file was built from `session_ingestor.DDL_SQLITE` itself — the old query raises
  `OperationalError: no such column: created_at` there, the new one returns `(epoch, True)`, and because
  SQLite's `CURRENT_TIMESTAMP` is UTC the parsed epoch matched `time.time()` exactly; read as local time
  it would have been 25 200 s off. **A missing row and an unreadable table are not the same fact**:
  `_last_action()` now returns `(epoch, readable)`, because the old single value made "cannot read the
  ledger" look like "this engine has done nothing", which is the exact wrong conclusion — the wrong
  direction of the one error this module is not allowed to make.
  The independent audit (`cross_verify` → `claude-sonnet-5-5-high`, 02:05, verdict `PERLU_KOREKSI`)
  found two more of that same direction, both of which my own green selftest had *blessed* rather
  than guarded: a readable ledger with **no row under the claim holder's name** was treated as "quiet"
  (`claimed_by` and `action_log.engine` are two naming spaces with nothing guaranteeing they agree, so
  that None can equally mean "recorded as something else"), and two pane reads taken with `sample_gap=0`
  make "the text did not change" a tautology rather than an observation. Both now return `UNKNOWN`;
  `MIN_SAMPLE_GAP = 1` floors the production path while the injected test seam keeps its zero-delay
  reads; a `capture` that *raises* is now a dead reader (`UNKNOWN`) instead of a crash; a timestamp
  that parses to no number is reported as unreadable, not as "no action". The audit's claim that the
  no-keyskeystroke check was too narrow was also right — it read one function's source — so the check
  now reads the source of every function on the read path (`heartbeat`, `_pane_text`, `_last_action`,
  `stalled`) and separately asserts that the only names borrowed from `cross_verify` are the two
  readers, `capture_pane` and `pane_is_busy`. Its `InFailedSqlTransaction` worry was checked and does
  not apply: `db_state.py:71` sets `conn.autocommit = True`, and that reason is now a comment at the
  `except`, not a memory. `selftest` 65/65 offline — 18 checks
  that drive the pane through injected fakes, replacing the one placeholder that only asserted the
  function existed, so no test can touch a live terminal — and 19/19
  against live PostgreSQL, including an invariant check that reads the function's own source and fails
  if a keystroke path (`send-keys`, `send_to_terminal`, `ydotool`, `subprocess`) is ever added to it.
  What is **not** shipped: the hand. `CONTINUE` injection is deliberately absent — **No keystroke is
  injected yet, deliberately, on the first night this design existed** — the resume path has never been
  watched over an operator's shoulder, and F10.7 is the one module whose failure mode is a machine
  typing into a person's terminal. Built after F10.8 for that reason: a thing that sends `CONTINUE` must
  have a switch that stops it before it has users. Remaining: (a) the drainer (F10.3) is the only place
  allowed to act on `AT_REST`, and it does not exist yet; (b) `resume_count` is never incremented by
  anything, so the budget is reported but unenforced until the hand exists; (c) the acceptance's first
  half — "stop a session mid-item on purpose and the daemon resumes it once, visibly" — cannot be
  demonstrated until (a) lands, and it must be watched with the operator awake, not proved at 03:00.
- 🟡 **F10.8 Autopilot switch — ON means the machine is entrusted, not that it is unowned.** A toggle on
  the Workstation cockpit (`gui.html` is the control surface; the *state* of the toggle is recorded as
  a row so the Station can show who had the machine and when). The operator's framing is the spec:
  ON = this computer belongs to the AI and its ecosystem for the night; OFF = a human is at the desk.
  Four things make that survivable, and they are the non-negotiable part of this module:
  1. **Three off paths, none of which asks permission.** The GUI toggle, `ai-station autopilot off`
     from any shell, and `systemctl --user stop` of the drainer timer — the last one only once F10.3
     exists, which it did not when this was first written down. Invariant 6: if the loop is
     misbehaving at 03:00, waking it to negotiate is not an off switch.
  2. **Self-expiry.** ON carries a wall-clock boundary and an item count; either one reached ends the
     run and emits the morning digest. A switch that stays ON because everyone forgot is a background
     process running with the operator's identity.
  3. **The hands floor.** Autopilot ON grants tmux panes and headless APIs. It does **not** grant
     `ydotool`: moving the real mouse and typing real keys steals the desktop of a sleeping person,
     and a misclick into whatever window is frontmost at 03:00 is the one failure this ledger cannot
     undo — no `action_log` row restores a deleted file from an app that has no CLI. GUI control is
     allowed only for an item that explicitly claims the `visual_operator` team, and — with autopilot
     ON and nobody at the desk — such an item enters the human queue instead of clicking.
  4. **The human queue is for facts, not preferences.** The operator's own example, checked instead of
     assumed: "OS disk left 10 GB but must download `phi4:14b`". Measured tonight: **389 G free** on
     `/` (449 G total, 9 % used), VRAM **928 MiB used of 12 288**, and `phi4:14b` genuinely not pulled
     (`cross_verify --list`: server alive, catalogue holds `qwen3.5:4b`, `qwen3.5:9b`, `tev1:0.8b`).
     So that item does not wait for a human tonight — and that is precisely why the threshold is a
     measured number evaluated when the item runs, never a remembered number.
     *Acceptance:* with the toggle ON, walk away; the morning digest lists every item attempted, every
     item skipped and why, which pool each cost came from, and the queue contains only items whose
     measured resource line actually crossed its threshold.
  Shipped 2026-10-08 01:30 (`a7726cf`, `35f0c80`), split the way F8.3 split: `work_order.py` owns the
  vocabulary and every breaker number, `api/autopilot.rs` owns HTTP and the process boundary and copies
  no policy, and neither HTML page restates a rule — each figure on screen is read from
  `work_order.py status`. The Station's Human Decide tab shipped read-only at first, on the argument that
  F8's two-surfaces rule forbids controls there. That argument was retired the same night, because it was
  wrong in a specific way: a queue that shows a question with no answer column is not a record of a
  decision, it is a transcript of one nobody can make. The tab now carries the option list, the answer
  column and the switch as control, and each of them writes through `work_order.py`, which stays the only
  owner of every number and every rule — the boundary is now stated as §12.3.
  Remaining, and named so nobody mistakes the switch for the loop:
  - **Self-expiry is checked at claim time, not by a timer.** A window that lapses at 02:00 stops
    nothing until the next `claim`. Tolerable while no drainer exists to make claims unattended;
    F10.3 must re-read the window per item, not once per run.
  - **ON currently governs nothing.** The switch is honest — it says who held the machine, and for how
    long — but the drainer that acts on it is F10.3. The order was set the other way round on purpose:
    a loop with no off is the failure this module exists to prevent.
  - **`CONTINUE` injection is deliberately not shipped.** An engine that looks idle may be thinking.
  - The morning digest (F8.5) reads the same table but does not emit yet.
  Measured: `selftest` 32/32, `selftest --live` on `POSTGRESQL` 12/12; deploy gates green (cargo test 3
  passed, call-worker guard "0 left behind / 3 pane processes protected", arch map 75 files / 0
  undocumented); endpoint round-trip ON→OFF with both 400 paths refused; the 503 body forced by hiding
  `work_order.py` names only routes proven to exist; `ai-station autopilot` verified without the daemon;
  4 `autopilot_switch` rows reached `action_log`; both surfaces confirmed by screenshot.
  Later the same night, the answer path — where F10.1's queue and F10.3's hand meet. `decisions.item_id`
  links a question to the quest waiting on it and `decisions.answered_by` names the person who answered;
  `work_order.py answer` writes `ANSWERED` with `rung_used='human'` and refuses to overwrite an answer
  already given; and `finish()` on a `HUMAN` escalation now poses a linked question of its own, so an item
  that crosses a threshold arrives with something to answer rather than only a title. `POST /api/decide`
  runs that command and then `drainer.py deliver --decision N`, recording **before** sending: the answer
  survives any failure of the keystroke, and the reply carries the drainer's reason verbatim.
  `deliver_answer` is the only function on this machine allowed to type a person's answer into an engine's
  pane, it is reachable only from a click, and the drainer selftest enforces that by scanning the module
  function by function instead of banning tokens file-wide.
  Measured: `work_order.py selftest` 109/109 offline and 31/31 on `POSTGRESQL`, `drainer.py selftest`
  59/59; a live `POST /api/decide` returned 200 with the row read back `ANSWERED` / `answered_by`, and its
  delivery stage reported honestly that it was deferred because the target claim still read `WORKING`
  ("jawaban sudah tercatat, pengiriman ditunda — bukan digagalkan"); a second save on the same row
  returned 409 quoting the first answer's timestamp; an empty answer returned 400 and left the question
  open; `POST /api/autopilot` with `mode:"maybe"` and with `minutes:0` each returned 400 and left
  `mode OFF` untouched, while `mode:"off"` returned 200 and wrote `changed_by=operator-gui` plus the
  reason into the ledger. The click layer was then proven by the only hand allowed to use it: the operator
  answered three rows from the page at 07:00, each landing in PostgreSQL as `ANSWERED` /
  `answered_by=operator-gui`, and each producing a `drainer-2026-10-08.jsonl` line that refused to send —
  once because the question carried no `item_id` ("tidak ada pekerjaan yang bisa dibangunkan") and twice
  because the target claim still read `WORKING`. No answer was lost to a refused keystroke.
  Two of those answers are decisions, not tests, and they are binding: **ON may commit and push, but only
  after a verifier clears the work** (decision 6), and **the drainer's timer stays off tonight because the
  operator is back at the desk** (decision 19) — so F10.3's acceptance half, one night of unattended log,
  remains owed by explicit choice rather than by oversight.

- ✅ **F10.10 Dashboard — the Station becomes a per-project command center, and SEMI stops being
  a word without a mechanism.** Shipped 2026-10-08 10:50, commits `4df4a08`…`0d9df2e` + the verb fix.
  The operator's complaint was usable as written: *berantakan, tidak eye catching, harus scroll ke
  bawah sangat banyak* — and the cause was mechanical, not aesthetic: `.card` was constructed at eight
  places in `station.html` and had **no CSS rule at all**, so every card was a transparent div with
  browser-default headings on a dark background, inside a container that was `flex-direction:column`
  by construction. The file also had **zero `@media` blocks** and no spacing scale. It now has three
  columns, a real card, and a fold for the eight stacked diagnostic sections.
  Renamed from *Human Decide*, moved to the top of the nav, with `#decide` kept as a live alias so an
  old bookmark does not die because a tab was renamed; hash is now written back and listened to.

  **SEMI is a mode, not a level** — a third answer to *who owns the machine*, stored in
  `autopilot_state.mode` beside the switch, never in a second table. Its whole content is the basket:
  `origin='operator'` rows only, enforced twice — in `next_item(origin=…)` and again inside `claim()`,
  because a limit that only filters the queue is a limit the caller can skip. The night breaker and
  the dirty-tree rule apply unchanged; a selftest check proves a roadmap row is refused *by name*
  while an operator row is accepted in the same queue.

  **The trigger is the engine's own turn boundary.** Measured in `qodercli-1.1.65` before designing
  on it: `Stop` is a real hook event, `decision` accepts `"block"` (normalised to `"deny"`), and a
  blocking Stop hook returns `{blocked, inject, retryAsStopHook}` — so `hooks/semi_wake.py` can hand a
  queued command to the session that is already running without touching a pane, `ydotool`, or a
  timer. Blocking is only ever the consequence of a successful claim on a PENDING row, so an empty
  queue injects nothing and **SEMI cannot run away**: the queue is the loop guard, which is why
  `stop_hook_active` is deliberately *not* used to cap it (that would limit the operator to one
  command per turn). Proven live at 10:50: item 85 → `{"decision":"block"}`, a 4138-character prompt,
  `claimed_by=qoder`, and `logs/semi_wake.jsonl` naming the item.

  **Project identity got exactly one new word.** Three notions of "where work happens" already lived
  here — `REPO` (the only execution use being `drainer.py:133 cd {REPO} && gate`), `TabProfile.workspace`,
  and the `current_project` symlink Rust never reads — which is precisely the *confusing identitas* the
  operator warned about. `quest_tasks.ws_path` is the new one, NULL meaning the old behaviour exactly,
  and the gate now runs in the row's own directory: an engine working on one tree while its verdict is
  read from another is a report about someone else. `workspaces` is a registry that **never claims the
  focus** — `is_focus` is derived from the symlink at read time, and a selftest check asserts the table
  has no `active` column to drift out of agreement.

  Three defects were caught by looking at real data rather than by testing: `log(why, **rest)`
  collided with call sites passing `why=` so the hook's *refusal* path raised `TypeError`, and the
  crash handler had the same defect — a trigger that crashes while reporting a refusal is worse than
  one that stays quiet; guideline dedupe keyed on the *relative* path, so this project's `AGENTS.md`
  silently deleted the workstation's from the list; and the Dashboard's word `guidelines` did not
  match the CLI verb `guides`, so the first real write failed with argparse exit 2. All three fixed at
  the source, not papered over — the last by renaming the verb rather than adding a mapping table,
  because a mapping is where two words for one thing hide. Selftests: work_order 110 → 135, drainer 59 → 83.
- ⬜ **F10.9 Push gate — the answer to decision 6 has to stop being a sentence.** The operator allowed an
  unattended run to commit and push *on condition* that a verifier clears the work first. That condition
  currently lives only in §6 prose, which means it binds exactly as far as an engine remembers it — and
  §12 forbids a rule with two definitions while the ledger has none. What is missing is a row: a push
  claims nothing until a `decisions`-shaped verdict exists for the commit it is about to publish, written
  by the resolver ladder with its own `model_id`, `state='COMPLETED'` and the diff it read. A run with no
  verdict commits locally and lists the pending push in the morning digest; it does not ask the operator,
  and it does not push. *Acceptance:* with the toggle ON, one commit reaches `origin` carrying a verifier
  row naming the model that cleared it, and one commit is deliberately made unverifiable (meter hidden) and
  is still local at dawn — both visible in `action_log`, neither produced by a human hand.

## Known Boundaries & Constraints

- **Token usage counters:** Some upstream CLI tools record `0` tokens in raw logs; the workstation preserves telemetry integrity without fabricating numbers.
- **CLI session working directory binding:** `--continue` flags bind to specific working directories; cross-directory persistence is bridged via PostgreSQL `world_memory` and handoff documents.
- **Unmovable system dotfiles:** Specific system paths (`.bashrc`, `.config`, `.var`, `.nv`, `.qoder`) remain as required by host tools; runtime data resides in `~/runtime`.
- **Guardrail scope:** Hooks intercept commands executed via AI shell tooling; manual operator commands bypass hooks by design.

## Historical Context

Early revisions of `ai_workstation_master_plan.md` and `PROJECT_SUMMARY_...md` noted 100% completion prematurely before daemon and logging layers were fully productionized. On October 6, 2026, those documents were archived in `~/.ai-station/archive/`, and all verified architectural truth was consolidated into `README.md` and this roadmap.
