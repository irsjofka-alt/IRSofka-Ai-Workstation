# QODER.md — Pointer, Not Primary Content

Workspace rules for Qoder CLI are defined in `AGENTS.md` in the same directory.
That document takes precedence in all cases of conflicting instructions.

Quick Summary for Qoder CLI:

- **Your state does not live literally in `~/.qoder`.** On this workstation, `~/.qoder` is a
  symlink to `~/.ai-station/engines/qoder`. Write all state through that path; never create new
  root entries in `$HOME`.
- **Memory** → `~/.ai-station/brain/memory/`, read via `MEMORY.md`, one file per topic.
- **Skills** → `~/.ai-station/brain/skills/`, read via `SKILLS.md`. The directory
  `<workspace>/.agents/skills/<name>/SKILL.md` is scanned automatically.
- **Factual State** → PostgreSQL database `irsofka_ai_workstation` or `ai-station recovery 40`.
  Never infer state from conversational memory.
- **Cross-Engine Commands** → `local-workstation` MCP server: `ask_peer`, `check_messages`,
  `read_terminal`, `send_to_terminal`, `refresh_workstation_ui`,
  `restart_workstation_daemon(reason=...)`.
- **Never** execute `pkill` on the daemon or `tmux kill-server`; this destroys other engine sessions,
  including your own.
- In the GUI composer: Enter creates a new line, Ctrl+Enter sends to Qoder.

The authoritative contract is: `AGENTS.md`.
