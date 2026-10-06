# GEMINI.md — pointer, bukan isi

Ditujukan untuk Antigravity CLI / Gemini CLI. Aturan sebenarnya ada di `AGENTS.md` di
direktori yang sama; kalau kedua berkas bertabrakan, `AGENTS.md` yang menang.

- **Jangan membuat konfigurasi sendiri di luar workstation.** Di mesin ini `~/.gemini`
  hanyalah shim: `antigravity`, `antigravity-cli`, dan `config` diarahkan ke
  `~/.ai-station/engines/`. Cache dan hasil kerja ke `~/runtime`, tidak ke `$HOME`.
- **Memori** → `~/.ai-station/brain/memory/` (indeks `MEMORY.md`).
- **Skill** → `~/.ai-station/brain/skills/` (indeks `SKILLS.md`). Plugin aturan berada di
  `~/.gemini/config/plugins/station-rules/` dan isinya symlink ke kontrak yang sama,
  supaya Qoder dan Gemini membaca satu teks, bukan dua salinan.
- **State faktual** → PostgreSQL `irsofka_ai_workstation`, atau tool MCP
  `local-workstation`. Jawaban tanpa bukti di database dianggap belum terjadi.
- **Verifikasi silang** → `ask_peer(to="qoder", ...)`, lalu `check_messages` dan
  `resolve_message(status="ANSWERED"|"DISPUTED")`. Tidak ada hierarki kebenaran: yang
  diperiksa boleh menolak dengan alasan.
- **Jujur soal status.** Engine yang tidak ada = `UNAVAILABLE`, bukan `COMPLETED`.
- **GPU rebutan.** Cek VRAM sebelum memuat model lokal, `keep_alive=0`, lalu `ollama stop`
  dan pastikan VRAM benar-benar kembali.
