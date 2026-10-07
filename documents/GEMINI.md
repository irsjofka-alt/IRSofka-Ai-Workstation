# GEMINI.md — Pointer, Not Primary Content

Targeted for Antigravity CLI / Gemini CLI. The authoritative rules reside in `AGENTS.md` in
the same directory; if any conflict arises, `AGENTS.md` takes precedence.

- **Do not create standalone configurations outside the workstation.** On this host, `~/.gemini`
  is a compatibility shim: `antigravity`, `antigravity-cli`, and `config` map to
  `~/.ai-station/engines/`. Caches and runtime artifacts belong in `~/runtime`, not `$HOME`.
- **Memory** → `~/.ai-station/brain/memory/` (indexed via `MEMORY.md`).
- **Skills** → `~/.ai-station/brain/skills/` (indexed via `SKILLS.md`). Rules plugins reside in
  `~/.gemini/config/plugins/station-rules/` and symlink to the shared contract, ensuring Qoder
  and Gemini share identical instructions.
- **Factual State** → PostgreSQL database `irsofka_ai_workstation` or the `local-workstation`
  MCP server. Claims lacking verification in the database are treated as unverified.
- **Cross-Verification** → `ask_peer(to="qoder", ...)`, then `check_messages` and
  `resolve_message(status="ANSWERED"|"DISPUTED")`. No hierarchy of truth: reviewed engines may
  dispute findings with technical justification.
- **Honest Status Reporting.** Missing engines or models report `UNAVAILABLE`, not `COMPLETED`.
- **GPU Discipline.** Check free VRAM before loading local models, pass `keep_alive=0`, invoke
  `ollama stop`, and verify VRAM deallocation.
