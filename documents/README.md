# `documents/` — Architectural Map, Not Storage

Files in this folder provide **templates and operational rules** that route AI CLI workspaces
into the workstation. This directory is tracked in the public git repository; `brain/` is not.

The boundary is deliberate and strictly maintained:

| | Public Repository (Here) | Local Host (`~/.ai-station/brain/`) |
|---|---|---|
| Content | Rules, blank templates, shared contracts | Memory, skills, incidents, handoffs, account notes |
| Read Access | Public | Machine local filesystem |
| Private Data / Secrets | **Never** | Gitignored local files |
| System State | Stateless | Stateful |

When cloning this repository to a new host, this directory is **not** your memory store. It merely
instructs CLIs where to read and write. Copy templates to the target machine, then point the CLIs
accordingly.

## Directory Manifest

To install from scratch on a new machine, refer to [`../INSTALL.md`](../INSTALL.md).

| File | Audience | Purpose |
|---|---|---|
| `AGENTS.md` | All engines | Shared contract; takes precedence over all other docs |
| `QODER.md` | Qoder CLI | Pointer referencing `AGENTS.md` |
| `GEMINI.md` | Antigravity / Gemini CLI | Pointer referencing `AGENTS.md` |
| `MEMORY.md` | Template author | Memory index template + selective loading rules |
| `SKILLS.md` | Template author | Skill catalog template + file numbering schema |

## Canonical Host Paths

```
Shared Workspace   : ~/Documents/ai-workstation/     (Active AGENTS.md lives here)
Memory Store       : ~/.ai-station/brain/memory/     (Indexed by MEMORY.md)
Skill Store        : ~/.ai-station/brain/skills/     (Indexed by SKILLS.md)
Factual State      : PostgreSQL irsofka_ai_workstation
```

CLI configuration folders do not store knowledge. Paths `~/.qoder`, `~/.qodersec`, `~/.qmind`,
and `~/.gemini` on this host are symlinks/shims targeting `~/.ai-station/engines/` to prevent
conflicting copies.
