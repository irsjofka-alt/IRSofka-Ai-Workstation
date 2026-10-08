# Shared Contract: Irsofka AI Workstation

You are part of a SINGLE unified ecosystem, not a standalone tool. This contract is read
by all AI engines operating on this workstation (Qoder, Antigravity/Gemini, and local models).
Its terms are definitive and non-negotiable.

## 1. You Have No Standalone State — Everything Lives in the Workstation

Directories like `~/.gemini` are compatibility shims made of symlinks. Your persistent state
lives inside the workstation:

```
~/.gemini/antigravity      -> ~/.ai-station/engines/antigravity
~/.gemini/antigravity-cli  -> ~/.ai-station/engines/antigravity-cli
~/.gemini/config           -> ~/.ai-station/engines/gemini_config
```

Never create configuration files, caches, or build artifacts outside these designated locations.
If you need to write files, use the paths above or write to `~/runtime`.

## 2. $HOME is Not a Dumping Ground

Strict workstation policy: **do not create new top-level entries in `$HOME`**. Build outputs,
caches, data, and downloaded artifacts must go to `~/runtime`. Environment variables are
pre-configured (`XDG_*`, `PYTHONUSERBASE`, `CARGO_HOME`, `RUSTUP_HOME`, `HISTFILE`); do not override them.

## 3. Shared Long-Term Memory: Single PostgreSQL Database

The database `irsofka_ai_workstation` serves as shared long-term memory across all engines.
Primary tables: `action_log` (audit trail), `world_memory` (notes & handoffs), `quest_tasks`,
`session_turns`, `incident_log`, and `ai_message`.

Never infer system state purely from conversation memory. Query the database directly or execute
`ai-station recovery 40`. Actions not recorded in the database are treated as never having occurred.

## 4. Peer Cross-Verification via Messaging

The `local-workstation` MCP server provides identical tooling across all CLIs. To communicate
persistently between engines:

```
ask_peer(to="qoder", from_engine="antigravity", topic="...", body="...")
check_messages(for_engine="antigravity")
resolve_message(message_id=N, status="ANSWERED" | "DISPUTED")
```

For real-time terminal interaction: `read_terminal(tab=...)`, `send_to_terminal(tab=..., text=...)`.

**Verification tier.** When a verdict must be independent rather than cheap, resolve the
`decider` / `verifier` role from `config/slots.yaml` and `config/engines.json` at the moment of
use — `tools/cross_verify.py --list` prints what is registered and what is currently allowed.
On this workstation those roles point at `claude-opus-5-5-high` and `claude-sonnet-5-5-high`,
reached through the Antigravity CLI (`agy --model <id> --print`). Quota pools are separate and
this is the reason the tier exists: the `Claude and GPT models` pool (shared by Opus, Sonnet and
GPT-OSS) does not draw on the `Gemini Models` pool, so a real independent audit does not shorten
the working agent's breath. The operator authorised spending that pool on verification whenever
its `5h` **and** `weekly` windows still have allowance; that authorisation is enforced by the
`quota_gate` block in the registry, not by an engine remembering it. A gate that cannot read a
meter refuses the dispatch as `UNAVAILABLE` — absence of evidence never means the quota exists.

Two measured limits of this tier, so no one rediscovers them as a false pass:
- `agy -p` is headless and cannot approve tool permissions. A verifier that must inspect code
  has to receive that code in the prompt (`cross_verify.py --file`); it will not go looking.
- Exit 0 with text is not evidence of an audit. A headless run that only prints a permission
  denial exits 0; `cross_verify.py` now records such an answer as `FAILED`, never `COMPLETED`.

**Call workers are cut, not kept.** The Active Workspace is the only persistent set on this
workstation: the tmux panes the operator works in. Every other process that speaks to a model —
a verification dispatch, a quota meter probe, a `--version` poll — is a **call worker**. It exists
for one action, and it must be terminated together with its whole process group as soon as its
answer has been read. `tools/call_workers.py` is the single place that decides what is persistent,
and it decides by asking: the pane list comes from the tmux socket and the engine list from
`config/engines.json`, at the moment of use. No PID is ever written into a rule or a script —
PIDs change at every boot, and a remembered list is a wrong list (§12).

- Inspect it with `ai-station workers`; it prints what is persistent and what is still alive
  beyond it, with each process's RSS.
- Engine calls are dispatched through `run_grouped` (Python) or `probe::run_bounded` (Rust), which
  start the command in its own process group and kill the group on timeout. Plain
  `subprocess.run(timeout=…)` kills only the direct child: measured on this workstation, one hung
  call left one grandchild alive holding its memory. `deploy_engine.sh` fails a deploy while any
  engine binary is spawned outside those two helpers, or while a stray call worker is alive.
- The sweep runs on the **success** path too, not only on timeout. A CLI that exits 0 after
  daemonising a worker leaves that group leaderless and resident, which is the same leak with a
  friendlier exit code.
- **Unknown is never treated as empty.** When the pane list cannot be read, the audit reports
  `ok: false` and refuses to cut anything. An empty persistent set reads as "nothing here is
  protected", and that misclassification was demonstrated live on 2026-10-07 to name the
  operator's own Qoder and Antigravity PIDs as strays. Absence of evidence is not permission.
- A process group is killed whole **only when no persistent PID stands in it**. A worker that
  escaped its pane by re-parenting still carries the pane's PGID, and `killpg` on that PGID reaches
  every process in it — including the operator's CLI. The cut then narrows to the individual stray
  PIDs and says so.
- Never cut by name pattern (§6 says the same about `pkill`). Cuts are PID-specific and
  group-wide. Identity is the resolved executable path, not `argv[0]`: the Qoder desktop IDE is
  also named `qoder`, and a name-only audit classified its 15 Electron processes as 2.18 GB of
  leaked workers. Killing that would be the operator's editor, not a leak.
- Known boundary: a worker that calls `setsid()` or double-forks leaves its group and cannot be
  reached by any PGID signal. It is caught later, by identity, at the next audit or deploy gate —
  so the sweep must keep running, and a green audit is not proof that no such worker exists.
- "Call worker" is not the same thing as a stray GUI window of the daemon (`--clean-orphans` in
  `deploy_engine.sh`). Two different failures, two different words.

Verification Policy:
- If you are **uncertain** about code, do not speculate — request verification from peer engines.
- Responses lacking sufficient evidence must be marked `DISPUTED`, not `ANSWERED`.
- Reviewed engines may dispute verification conclusions with reasoned justification. Truth is evidence-based.

**Each engine's memory is shared memory.** Standing order set by the operator on 2026-10-08 10:44, during
an unattended run: while working AUTOPILLOT or SEMI, an engine may consult the *other* engine's brain —
asking Antigravity/Gemini to recall a decision it was party to, or to challenge a reading of the roadmap,
the plan, or what is in SQL. This is authorised without asking again, because an agent that only trusts its
own surviving context re-litigates decisions its partner already heard. It is a **memory and audit aid, not
a second decision-maker**: a peer may remind, dispute, and supply evidence, but a peer answer never replaces
an operator answer, and every exchange is written to `ai_message` (`ask_peer` → `check_messages` →
`resolve_message`) so the record — not whoever is awake — decides what was asked. Where a verdict must be
independent rather than cheap, that is the `decider`/`verifier` tier above, not this one.

**Decision routing — the operator is not a menu.** When an engine faces two or more acceptable paths and
no measurement decides between them, it does **not** put the choice to the operator. It resolves the
`resolver` role from `config/slots.yaml` and `config/engines.json` at the moment of use, dispatches the
question with the evidence for *every* option inside the prompt (a headless worker cannot go looking —
same rule as `--file` above), and records which model id answered. Standing order set by the operator on
2026-10-08: preference-level questions are answered by the long-breath engine, named there as
`gemini` / Flash 3.8 High, because the `Claude and GPT models` pool can empty first and the operator's own
reading is that the answer would be the same one they would have given. The implementation is a **three-rung
ladder of roles in `config/slots.yaml`** — `resolver` → `resolver-alt` → `resolver-local` — each pointing at
a `config/engines.json` key, never restating a model id. The order is config and the operator can invert it
from the Station, because a meter that reads 98 % free tonight is not the meter that is free next week, and
because §12 forbids selecting a model from memory. When no rung's meter can be read, the question lands
`ESCALATED`; it is never answered by a substitute engine picked in the moment.

A choice becomes the operator's only when the item crosses a **named threshold, measured when the item
runs**: free disk, free VRAM, quota window fraction, an irreversible git or database operation, or real
mouse and keyboard input with nobody at the desk. Thresholds are re-read, never remembered — the operator's
own worked example (10 GB disk left against a 14B model download) did **not** apply on 2026-10-08, where
`/` had 389 G free and 928 MiB of 12 288 MiB VRAM in use. The Station carries one tab for those:
**Human Decide**. It is an exception queue, and an empty one is the normal state; a tab that fills up is
reporting that the thresholds are wrong, not that the engines are diligent.

An exception queue nobody can answer is a display, not a queue, so the tab carries the answer column and
the write path behind it. `POST /api/decide` is the only surface that records a human answer; it runs
`tools/work_order.py answer`, which sets `state='ANSWERED'`, `rung_used='human'` and `answered_by`, and
refuses to overwrite a row already answered — a second save returns the first answer's timestamp rather
than replacing it. Every question keeps an `item_id`, the quest it unblocks; without that link an answer is
an archive entry that wakes nobody. Recording happens **before** delivery, and delivery is a separate call
(`tools/drainer.py deliver`), so a failed keystroke cannot destroy a decision: the answer stays in
PostgreSQL and the reply reports the deferral as written. That hand is the human hand, and it is reachable
only from a click — `deliver_answer` is the one function in the drainer allowed to send, and the selftest
scans the module function by function so no timer path can name it. Delivery refuses, without discarding
the answer, while the target claim still reads `WORKING`, when the pane cannot be read, and when the row
carries no `cli_engine`: the engine woken is the one written on the item, never the one a tick likes.

**Routing is by weight, not only by exhaustion.** The operator named three tiers: the primary worker (the
engine doing the task), a **heavy reviewer** for anything that would otherwise be a human decision, and a
**light reviewer** for quick secondary checks. Those map onto registry tiers that already exist — `gemini`
is registered `reviewer` and `local` (`qwen3.5:9b`) is registered `validator` — so the rule selects by the
*shape of the question*, and never invents a second word for the same job:

- A quick, low-compute check (does this diff contradict the file it touches, is this claim consistent with
  the row it cites) goes to **`resolver-local`** first: no quota at all, and the §7 VRAM gate still has to
  clear it before it runs.
- A decision that would have woken the operator goes to **`resolver`** — `gemini`, Flash 3.8 High, the
  operator's named proxy — because it has its own tools and can read the code it is judging.
- If that meter reads exhausted or unreadable, **`resolver-alt`** takes it, from a pool the working
  architect does not draw on.
- Weight is not a licence to skip evidence: a light check gets a light *question*, not a hidden one. Both
  tiers record the answering model id, and both are refused — not downgraded — when their meter cannot be
  read.

**Unattended ticks inherit every rule above.** Autopilot changes *who triggers* a tick, never *what a tick
may do*: the deploy gates, the call-worker cut, the `DISPUTED`-over-silence rule and §6 all apply with no
human watching. The daemon may nudge an engine it believes has stopped only when two independent signals
agree — an open task claim, an `action_log` row for that engine that is older than T minutes, and a pane
whose text is unchanged across two reads taken with a real gap between them. Absence of evidence is not
permission on either side, so each of these is `UNKNOWN` and never `idle`: a pane that cannot be read, a
ledger that cannot be read, a ledger that is readable but holds **no row under the name holding the
claim** (`claimed_by` and the ledger's engine name are two naming spaces with nothing guaranteeing they
agree), and two pane reads taken with no interval, where "it did not change" is a tautology rather than
an observation. `tools/work_order.py heartbeat` is the only reader of that rule and it sends nothing.
The hand that acts on it is `tools/drainer.py`, and it acts only as far as a ladder lets it:
**`observe`** reads and logs and touches nothing, **`dispatch`** additionally runs deterministic gates
and hands a new item to the engine named in that row's own `cli_engine`, and **`resume`** is the single
rung allowed to type `CONTINUE` — and only into a claim `heartbeat()` has just proved `AT_REST`, at most
`MAX_RESUME` times per item, counted where it is written so the budget cannot be spent by a bug. The
drainer never raises its own level: a loop that grants itself hands has no off switch.

## 5. Honest Status Reporting

Report actual status without embellishment. An unavailable engine or model is `UNAVAILABLE`,
never `COMPLETED`. An unperformed audit is **unverified**, not "passed".
False status reports harm system reliability because subsequent engines build upon them.

## 6. Self-Preservation Guardrails

- **Do NOT** execute `pkill -f "irsofka-station-core"` — this terminates the production daemon,
  triggering systemd respawn and destroying your active CLI sessions.
- **Do NOT** execute `tmux -L irsofka kill-server` — this destroys all terminal tabs and running AI sessions.
- Reload interface: `refresh_workstation_ui`.
- Restart daemon (safe, tmux preserves tabs): `restart_workstation_daemon(reason="...")` — `reason` is required and logged.

**Autopilot (`ROADMAP` F10.8).** The cockpit carries one toggle. ON entrusts the machine to the engines
for a bounded run; OFF means a human is at the desk and watching. The rules below are load-bearing:

- **Turning it off must not need the AI's cooperation.** Every route below was proven from a headless
  context before it was named here: the GUI toggle, `ai-station autopilot off` from any shell,
  `systemctl --user stop irsofka-autopilot.timer` (ends the tick even with the daemon untouched), and
  `python3 tools/work_order.py disarm` (drops the hand to `observe`, needs no TTY). If stopping requires
  asking the loop politely, it is not a switch. An escape route named before it exists is worse than one
  fewer route: it teaches an operator to type a command that fails at the exact moment they have lost
  control of the machine. Raising the hand is the deliberate opposite — `arm` refuses wherever `/dev/tty`
  cannot be opened, which was measured to fail from systemd units, headless spawns and this assistant's
  own shell tool, so no unattended process can widen its own reach.
- **Three answers to "who owns the machine": OFF, SEMI, ON.** They differ by *basket*, not by
  strength of hand. OFF = a human is at the desk and nothing moves. **SEMI** = still automatic, but
  the only work it may take is a command the operator typed himself into SQL (`origin='operator'`);
  roadmap items are refused by name, in the queue filter *and* inside `claim()`. ON = the computer is
  given to the engines and anything available may be taken. The `level` ladder is shared by SEMI and
  ON and has no SEMI variant — raising a hand is still a TTY-only act. SEMI's trigger is the engine's
  own turn boundary (`hooks/semi_wake.py`, a `Stop` hook): it may block a stop **only as the
  consequence of having claimed a real PENDING row**, so an empty queue injects nothing and the queue
  itself is the loop guard. Silence is the failure state this exists to remove.
- **ON grants panes and headless APIs; it does not grant `ydotool`.** Moving the real mouse and typing
  real keys steals the desktop of a sleeping person, and a misclick into whatever window is frontmost at
  03:00 is the one failure the ledger cannot undo — no `action_log` row restores a file deleted inside an
  application that has no CLI. An item that genuinely needs the desktop joins the human queue instead of
  clicking.
- **Writing to git while ON is allowed, but only behind a verifier.** Answered by the operator on
  2026-10-08 07:00 in the Human Decide tab (decision 6, `answered_by=operator-gui`): an unattended run may
  commit and push, provided a verification pass clears the work first and finds nothing — "kalau tidak di
  temukan masalah boleh langsung di push". The condition is the whole point of the answer: a push is the
  one action that leaves the machine, and an unreviewed one becomes everyone's history before anyone
  wakes. A run that cannot reach a verifier — meter unreadable, engine down, no evidence in the prompt —
  commits locally and leaves the push in the morning digest.
- **ON expires by itself**, at a wall-clock boundary or an item count, and emits the morning digest. A
  toggle left ON because everyone forgot is a background process running with the operator's identity.
- **A fast failure stops the run; it does not retry it.** An unattended loop that fails in under a second
  has learned nothing and will spend the whole weekly window proving it. The breaker opens after
  `NIGHT_MAX_FAILURES` rows in `FAILED`/`PARKED` inside the current ON window, or `NIGHT_MAX_ITEMS` items
  attempted — both are read as numbers when the claim happens, never recited, and `claim` refuses with the
  figure that tripped. A queue also never re-offers a row it already answered: `FAILED` and `UNAVAILABLE`
  are conclusions, not invitations.
- **A dirty working tree is not a clean one.** While the repo has uncommitted changes, no new item is
  claimed. The correct response is to name the files and escalate; **not** to clean them. A resolver
  proposed `git checkout -- .` on 2026-10-08 and it is refused here: it discards whatever another engine
  was mid-way through writing, which invariant 3 puts beyond any tick's reach. Equally, a breaker that
  cannot read git reports `UNKNOWN` and refuses — the same asymmetry that makes the call-worker cut safe.

## 7. GPU Resource Discipline

The 12 GB VRAM is shared between desktop rendering, generative workflows, and local models.
Local models must:
Verify free VRAM first, specify `keep_alive=0`, invoke `ollama stop`, and confirm `ollama ps`
is empty upon completion. If VRAM is constrained, run on CPU/RAM (`num_gpu=0`) — never seize GPU resources.

## 8. Self-Recovery Protocol

Sessions may restart at any time. Before attempting to recall previous work, inspect:
```
ai-station recovery 40        # action history and handoffs, runnable from any directory
ai-station handoff            # handoffs and active quests
~/.ai-station/brain/memory/projects/handoff_*.md
```
Official documentation: `~/.ai-station/README.md`, `~/.ai-station/ROADMAP.md`, and for private
machine contexts: `~/.ai-station/brain/DESIGN_NOTES.md`.

## 9. Skills & Memory: Single Entry Point

Never place skills or memories inside tool-specific folders. All knowledge resides in `brain/`,
and is not committed to public git — the public repository tracks **rules and templates only**.

| Type | Write Target | Read Via |
|---|---|---|
| Machine long-term memory | `~/.ai-station/brain/memory/` | `brain/memory/MEMORY.md` (index only) |
| Human-authored skills | `~/.ai-station/brain/skills/<category>/` | `brain/skills/SKILLS.md` (index only) |
| Autonomous learned skills | `~/.ai-station/brain/skills/learned/` | Generated by `incident_recorder.py` on 5x repeat errors |
| Legacy memories & skills | `~/.ai-station/brain/memory/legacy/` | Read `memory/legacy/LEGACY_MAP.md` first — legacy paths are inactive |
| Factual state (actions, quests, messages) | PostgreSQL `irsofka_ai_workstation` | `ai-station recovery` or MCP tools |

CLI state directories reside inside the workstation:

```
~/.qoder      -> ~/.ai-station/engines/qoder       settings.json, projects/, memory/, plugins/
~/.qodersec   -> ~/.ai-station/engines/qodersec    security scan outputs per session
~/.qmind      -> ~/.ai-station/engines/qmind       agent memory
~/.agents     -> ~/.ai-station/engines/agents      shared skill tree read by all CLIs
~/.gemini     -> shim to engines/antigravity, antigravity-cli, gemini_config
```

Unified skill tree, shared entry points:

```
~/.ai-station/engines/agents/skills/<pack>/SKILL.md
   Qoder         : ~/.agents/skills
   Antigravity   : ~/.gemini/config/plugins/station-rules/skills
   Source        : symlinks to brain/skills/, never raw copies
```

Rebuild/verify with `~/.ai-station/bin/wire_skills.sh [--check]`. If an engine encounters skills
elsewhere (project directories, engine caches), relocate them to `brain/`.

Qoder's internal `memory/` folder (`~/.qoder/memory/`) is not used for knowledge storage — the
canonical knowledge base is `brain/`.

Selective Loading Policy: **Do not read entire indices into context.** Select the single
relevant entry and open only that file. Context is precious.

## 10. Language Convention

**Documentation and UI are written in English.** This covers: `README.md`, `INSTALL.md`,
`ROADMAP.md`, everything under `documents/`, `brain/**/*.md` notes and skill files, all GUI
labels and messages in `engine-rust/src/gui.html`, and anything shown globally to the
operator. Internal strings — hook denial reasons, context-recovery briefings, database
notes consumed by engines — may stay in Indonesian; they are conversation with the machine,
not with a person.

**Commit messages are written in English.** Subject and body, no exceptions: history is the
one artifact every engine and every future reader greps, and a bilingual log forces a
translation step onto whoever is doing `git bisect`. This rule was set by the operator on
2026-10-07; commits before that date are Indonesian and stay as they are.

**Code comments may stay in Indonesian.** `//`, `/* */`, `#`, `--`, `<!-- -->` and docstrings
that only a maintainer reads are exempt: the reasoning that made a line exist is easier to
keep accurate in the language it was formed in.

Why English: this workstation is published, and every engine on it reads the same files.
Mixed-language documentation forces each reader to hold two vocabularies for one contract,
and translation drift is silent — a rule understood differently is a rule not enforced.

When you edit a file that is still in Indonesian, convert the parts you touch and say so in
the commit message. Do not rewrite untouched sections just for language: that buries the
actual change under noise.

## 11. Architecture Map — Generated, Never Hand-Written

`~/.ai-station/brain/memory/projects/ARCHITECTURE.md` is the file map of the workstation:
folder → file → what it is for. Read it before grepping; the section that owns the behaviour
tells you which directory to search, and folder boundaries are the design.

| Need | Command |
|---|---|
| Rebuild the map | `bash ~/.ai-station/bin/arch_map.sh` (runs automatically at the end of every `deploy_engine.sh`) |
| Audit documentation debt | `bash ~/.ai-station/bin/arch_map.sh --check` — exits non-zero while a mapped file has no purpose |

A new file requires **no edit to the map**. It requires one line about itself, in whatever the
file type supports:

| File type | Where the purpose is read from |
|---|---|
| Rust | `//!` module doc as the first line of the file |
| Python | module docstring (first line = one-sentence summary) |
| Shell / systemd / YAML / TOML | first `#` comment after the shebang |
| HTML | first `<!-- -->` block |
| Markdown | first `# ` heading |
| JSON | a top-level `"_comment"` key |

Files that structurally cannot carry a comment are the only exception, and they are named with
their reason in the `FALLBACK` table inside `bin/arch_map.sh`. Do not grow that table to dodge
writing a real header — `"_comment"` keys parse fine in every JSON file here except ones bound
to a typed schema.

## 12. Two Surfaces: Workstation and Station

The **Workstation** is the cockpit in `engine-rust/src/gui.html`: terminal-centric, always open,
where the operator gives orders. The **Station** (roadmap F8) is the human-facing record: what went
in, what came out, what it cost, and who is on the team. Do not merge them — a console that also
tries to be a report ends up being neither.

Two rules apply to everything in F8, and they are not negotiable at implementation time:

1. **One vocabulary.** `job`, `artifact`, `cost`, `role` are defined once, in the database schema.
   A second definition in a second module produces two reports that disagree, with no way to tell
   which one is true.
2. **Reports are derived.** Nothing shown as a report may be written by hand. One manual
   correction is enough to destroy the ledger's authority.
3. **A control may sit on the Station only where the record and the decision are the same object.** The
   exception queue is that case: a page showing a question with no answer column is a museum placard, and
   an operator who must open a console to unblock one item will not unblock it. Such a control writes
   through the module that already owns the word — `POST /api/decide` runs `work_order.py answer`,
   `POST /api/autopilot` runs `work_order.py autopilot` — so the page holds no policy, no threshold and no
   second definition. What stays forbidden is a control that *computes*: a button whose JavaScript decides
   what is allowed has become a second copy of the rule, and two copies will disagree.

Related rule for engines: never select a model, verifier or teammate from memory. Resolve the role
from `config/slots.yaml` and `config/engines.json` at the moment of use — those files are the
contract, and the operator edits them from the Station.
