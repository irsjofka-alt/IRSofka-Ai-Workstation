# Kontrak Bersama Irsofka AI Workstation

Anda adalah bagian dari SATU ekosistem, bukan alat yang berdiri sendiri. Berkas ini dibaca
oleh semua AI di mesin ini (Qoder, Antigravity/Gemini, dan model lokal nantinya). Isinya
sedikit, dan tidak bisa ditawar.

## 1. Kamu tidak punya state sendiri — semuanya di workstation

`~/.gemini` hanyalah shim berisi symlink. State-mu yang sebenarnya tinggal di dalam
workstation:

```
~/.gemini/antigravity      -> ~/.ai-station/engines/antigravity
~/.gemini/antigravity-cli  -> ~/.ai-station/engines/antigravity-cli
~/.gemini/config           -> ~/.ai-station/engines/gemini_config
```

Jangan membuat berkas konfigurasi, cache, atau hasil kerja di luar itu. Kalau kamu butuh
menulis sesuatu, tulis lewat jalur di atas atau ke `~/runtime`.

## 2. $HOME bukan tempat sampah

Aturan keras pemilik mesin: **jangan melahirkan entri baru di `$HOME`**. Hasil install,
cache, data, artefak unduhan — semuanya ke `~/runtime`. Variabelnya sudah dipasang
(`XDG_*`, `PYTHONUSERBASE`, `CARGO_HOME`, `RUSTUP_HOME`, `HISTFILE`); jangan ditimpa.

## 3. Memori bersama: satu PostgreSQL

Basis data `irsofka_ai_workstation` adalah memori jangka panjang bersama. Tabel penting:
`action_log` (jejak tiap aksi), `world_memory` (catatan & handoff), `quest_tasks`,
`session_turns`, `incident_log`, `ai_message`.

Jangan menyimpulkan keadaan dari ingatan percakapan. Baca dari database, atau jalankan
`ai-station recovery 40`. Riwayat yang tidak tercatat dianggap tidak pernah terjadi.

## 4. Kalian saling memverifikasi, lewat kotak pesan

Server MCP `local-workstation` memberi tool yang sama ke semua CLI. Untuk bicara ke mesin
lain secara persisten:

```
ask_peer(to="qoder", from_engine="antigravity", topic="...", body="...")
check_messages(for_engine="antigravity")
resolve_message(message_id=N, status="ANSWERED" | "DISPUTED")
```

Untuk melihat/menyuruh langsung: `read_terminal(tab=...)`, `send_to_terminal(tab=..., text=...)`.

Kebijakan verifikasi:
- Kalau kamu **tidak yakin** soal kode, jangan mengarang — minta mesin lain memeriksa.
- Jawaban yang tidak cukup bukti → tandai `DISPUTED`, jangan `ANSWERED`.
- Yang diperiksa boleh menolak hasil verifikasi dengan alasan. Tidak ada hierarki kebenaran.

## 5. Jujur soal status, itu alasan utama proyek ini ada

Laporkan apa adanya. Engine/mesin yang tidak tersedia = `UNAVAILABLE`, bukan `COMPLETED`.
Pemeriksaan yang tidak kamu lakukan = **belum diverifikasi**, bukan "lolos".
Klaim palsu lebih merusak daripada kegagalan, karena mesin lain membangun di atasnya.

## 6. Jangan membunuh dirimu sendiri

- **Jangan** `pkill -f "irsofka-station-core"` — ikut membunuh daemon, systemd restart,
  dan sesi CLI di dalamnya mati bersamamu.
- **Jangan** `tmux -L irsofka kill-server` — menghapus semua tab beserta sesi AI di dalamnya.
- Muat ulang UI: `refresh_workstation_ui`. Restart daemon (boleh, tab aman di tmux):
  `restart_workstation_daemon(reason="...")` — `reason` wajib dan tercatat.

## 7. GPU adalah barang rebutan

VRAM 12 GB dipakai bersama engine grafis dan UI generatif. Model lokal wajib:
cek VRAM kosong dulu, pakai `keep_alive=0`, lalu `ollama stop`, dan pastikan `ollama ps`
kosong setelah selesai. Kalau VRAM tidak cukup, jalan di CPU/RAM — jangan merebut GPU.

## 8. Memulihkan diri

Sesi bisa mati kapan saja. Sebelum menjawab "tadi sedang apa", baca:
```
ai-station recovery 40        # riwayat aksi + handoff, dari direktori mana pun
ai-station handoff            # serah-terima + quest aktif
~/.ai-station/brain/memory/projects/handoff_*.md
```
Dokumen resmi: `~/.ai-station/README.md`, `~/.ai-station/ROADMAP.md`, dan untuk konteks
mesin/akun yang privat: `~/.ai-station/brain/DESIGN_NOTES.md`.

## 9. Skill & memori: satu pintu masuk

Jangan menaruh skill atau memori di folder tool. Semua tulis ke `brain/`, dan jangan
menyalinnya ke repo — repo ini publik dan yang diunggah hanya **aturan + templat**.

| Jenis | Lokasi tulis | Baca lewat |
|---|---|---|
| Memori jangka panjang mesin | `~/.ai-station/brain/memory/` | `brain/memory/MEMORY.md` (indeks, baca 1 file saja) |
| Skill yang ditulis manusia | `~/.ai-station/brain/skills/<kategori>/` | `brain/skills/SKILLS.md` (indeks) |
| Skill hasil belajar otomatis | `~/.ai-station/brain/skills/learned/` | dibuat sendiri oleh `incident_recorder.py` saat error terulang 5x |
| Memori/skill dari mesin lama | `~/.ai-station/brain/memory/legacy/`, `brain/skills/legacy/` | **baca `memory/legacy/LEGACY_MAP.md` dulu** — path di dalamnya sudah mati |
| State faktual (aksi, quest, turn, pesan) | PostgreSQL `irsofka_ai_workstation` | `ai-station recovery`, atau tool MCP |

State milik tool tetap milik tool, tapi badannya ada di dalam workstation:

```
~/.qoder      -> ~/.ai-station/engines/qoder       settings.json, projects/, memory/, plugins/
~/.qodersec   -> ~/.ai-station/engines/qodersec    hasil security scan per sesi
~/.qmind      -> ~/.ai-station/engines/qmind       agent memory
~/.agents     -> ~/.ai-station/engines/agents      pohon skill bersama (dibaca semua CLI)
~/.gemini     -> shim ke engines/antigravity, antigravity-cli, gemini_config
```

Satu pohon skill, beberapa pintu masuk. Yang dibangun sekali dan ditautkan ke semua engine:

```
~/.ai-station/engines/agents/skills/<pack>/SKILL.md
   Qoder         : ~/.agents/skills
   Antigravity   : ~/.gemini/config/plugins/station-rules/skills
   sumber        : symlink ke brain/skills/, tidak pernah salinan
```

Bangun/periksa ulang dengan `~/.ai-station/bin/wire_skills.sh [--check]`. Kalau engine
menemukan skill di tempat lain (folder project, cache miliknya sendiri), itu yang harus
dipindah ke `brain/` — bukan sebaliknya.

Folder `memory/` punyanya Qoder (`~/.qoder/memory/`) jangan dipakai untuk menyimpan
pengetahuan — ia kosong dan bukan tempat yang dibaca manusia. Satu-satunya tempat yang
boleh dituju adalah `brain/`.

Aturan memuat: **jangan baca seluruh indeks sekaligus.** Pilih satu baris yang relevan,
buka satu file. Konteks adalah napas, dan napas efektif dibatasi plafon per model.
