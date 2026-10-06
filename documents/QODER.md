# QODER.md — pointer, bukan isi

Yang dibaca Qoder CLI sebagai aturan workspace: `AGENTS.md` di direktori yang sama.
Berkas itu menang kalau isinya bertabrakan dengan berkas ini.

Ringkas, untuk Qoder CLI:

- **State kamu bukan di `~/.qoder` secara harfiah.** Di mesin ini `~/.qoder` adalah symlink
  ke `~/.ai-station/engines/qoder`. Tulis apa pun lewat jalur itu, jangan membuat entri baru
  di `$HOME`.
- **Memori** → `~/.ai-station/brain/memory/`, baca lewat `MEMORY.md`, satu file per topik.
- **Skill** → `~/.ai-station/brain/skills/`, baca lewat `SKILLS.md`. Direktori
  `<workspace>/.agents/skills/<nama>/SKILL.md` ikut dipindai (bawaan aktif, butuh restart tab).
- **State faktual** → PostgreSQL `irsofka_ai_workstation`, atau `ai-station recovery 40`.
  Jangan simpulkan dari ingatan percakapan.
- **Perintah lintas engine** → tool MCP `local-workstation`: `ask_peer`, `check_messages`,
  `read_terminal`, `send_to_terminal`, `refresh_workstation_ui`,
  `restart_workstation_daemon(reason=...)`.
- **Jangan** `pkill` daemon atau `tmux kill-server`; itu membunuh sesi mesin lain, termasuk
  dirimu sendiri.
- Enter untuk baris baru, Ctrl+Enter untuk mengirim ke Qoder (di composer GUI).

Versi lengkap dan satu-satunya yang authoritative: `AGENTS.md`.
