# Irsofka AI Workstation

Konsol orkestrasi AI untuk Pop!_OS 24.04 COSMIC (Wayland). Satu daemon Rust meng-host
beberapa CLI AI interaktif di dalam terminal yang hidup di tmux, menampilkannya pada satu
jendela desktop native, dan mencatat **setiap** aksi ke basis data — supaya sesi bisa
dipulihkan setelah apa pun yang memutusnya.

Bukan IDE. Tidak ada editor, pohon berkas, atau debugger di sini. Fokusnya: beberapa mesin
AI bekerja bersamaan, saling memverifikasi, dan semuanya meninggalkan jejak yang bisa dibaca
kembali.

## Arsitektur

```
                    ┌──────────────────────────────────────┐
  Pengguna ───────► │  jendela native (tao + wry + webkit) │
                    └───────────────┬──────────────────────┘
                                    │ HTTP :8999
                    ┌───────────────▼──────────────────────┐
                    │  irsofka-station-core (--headless)   │
                    │  axum · portable-pty · spool JSONL   │
                    └───┬───────────────┬──────────────┬───┘
              tmux -L irsofka           │              │
        ┌───────────────┬───────────────┐   MCP    ┌───▼────────────┐
        │ qoder pane    │ antigravity   │   server │ shell pane     │
        │ (builder)     │ pane (reviewer)│         │ (COSMIC bash)  │
        └───────┬───────┴───────┬───────┘  └───┬───┴────────────────┘
                └───────────────┴──────────────┘
                                │
                    ┌───────────▼───────────────────┐
                    │ PostgreSQL 16 (SQLite fallback)│
                    │ + brain/ markdown + handoff    │
                    └────────────────────────────────┘
```

Tiga lapis memori, dan itu inti proyek ini:

| Lapisan | Wujud | Umur |
|---|---|---|
| konteks percakapan | di dalam proses CLI | pendek, hilang saat proses mati |
| **memori panjang** | PostgreSQL: `action_log`, `session_turns`, `world_memory`, `quest_tasks`, `incident_log`, `ai_message` | permanen, lintas-direktori & lintas-mesin |
| grimoire | `brain/**/*.md` (rules, skills, identitas) | permanen, dibaca manusia & AI |

## Peta direktori

```
bin/            daemon launcher, CLI hub `ai-station`, deploy, cadangan & pemulihan
engine-rust/    sumber Rust: daemon + GUI (axum, portable-pty, tao, wry)
tools/          ingestor log→SQL, server MCP, verifier lintas-mesin, local LLM, adapter DB
hooks/          penegak keselamatan sesi + pencatat handoff otomatis
config/         engines.json (registry mesin), slots.yaml; sisanya dihasilkan saat startup
~/runtime/      DI LUAR $HOME: cache, data, toolchain, hasil install, backups/
```

## Menjalankan

Butuh: Rust (diuji 1.99), PostgreSQL 16 (atau fallback SQLite otomatis), `tmux`,
`python3` + `psycopg2`.

```bash
cd engine-rust && cargo build --release
install -m 0755 target/release/irsofka-station-core ~/.ai-station/bin/
~/.ai-station/bin/launch_gui.sh
```

Unit systemd user: `irsofka-ai-workstation` (daemon), `irsofka-tabs` (server tmux, di luar
cgroup daemon), `irsofka-action-log` (ingestor log→SQL), `irsofka-memory-backup` (timer).

### Kredensial (tidak ada di repo)

Koneksi PostgreSQL dibaca berjenjang: variabel lingkungan → `config/db_local.json` → bawaan
tanpa password.

```bash
cat > ~/.ai-station/config/db_local.json <<'JSON'
{ "host": "localhost", "port": 5432, "user": "isi_user_anda",
  "password": "isi_password_anda", "dbname": "irsofka_ai_workstation" }
JSON
chmod 600 ~/.ai-station/config/db_local.json
```

Hasil clone tanpa berkas ini gagal terhubung secara terang-terangan, bukan ikut membawa
password orang lain. Identitas pemilik juga dari lingkungan: `STATION_OWNER_NAME`,
`STATION_EMAIL_QODER`, `STATION_EMAIL_ANTIGRAVITY`.

## Tab tidak mati saat daemon restart

Tab berjalan di `tmux -L irsofka`, bukan sebagai anak daemon, dan `run_tab.sh` menambah
`--continue` bila `cli_profiles.json` menyalakan `continue_session`.

```bash
curl -s localhost:8999/api/workspace | grep -o '"backend":"[a-z]*"'   # harus "tmux"
tmux -L irsofka ls
```

Perubahan `gui.html` cukup dimuat ulang (daemon membacanya dari disk tiap permintaan).
Perubahan `main.rs` wajib build + deploy + restart daemon.

## Memulihkan konteks

```bash
ai-station recovery 40    # riwayat aksi + serah-terima, bekerja dari direktori mana pun
ai-station handoff        # hanya handoff & quest aktif
ai-station snapshot       # tulis handoff mekanis sekarang
```

Hook `SessionEnd` dan `PreCompact` menulis handoff mekanis secara otomatis — jejak perintah
yang benar-benar tereksekusi, bukan karangan AI yang sedang kehilangan konteks.

## Verifikasi silang antar mesin

Kalau satu mesin tidak yakin soal kode, mesin lain yang memeriksa. Registry ada di
`config/engines.json` — menambah perangkat cukup di situ.

```bash
ai-station verify gemini --file engine-rust/src/main.rs
ai-station verify gemini "Klaim: fungsi X aman karena Y"
python3 tools/local_llm.py --status          # VRAM kosong, RAM tersedia, pemegang GPU
python3 tools/local_llm.py --tier verify "..."
```

| Mesin | Peran | Catatan |
|---|---|---|
| Qoder | builder | konteks besar; jalur utama penulisan kode |
| Antigravity / Gemini | reviewer | punya tool sendiri: membaca kode Anda sebelum memberi verdict |
| Local LLM (Ollama) | validator | offline; tier `light`/`verify`/`heavy` |

Kedua CLI memakai server MCP yang sama, jadi verifikasi berjalan **dua arah** lewat tabel
`ai_message`: `ask_peer`, `check_messages`, `resolve_message`. Ketidaksepakatan ditandai
`DISPUTED`, tidak ditutup diam-diam.

## Disiplin GPU

VRAM adalah barang rebutan (mesin grafis, compositor, UI generatif). Local LLM hanya tamu:

1. **gerbang VRAM** sebelum memuat — kalau kurang, jatuh ke CPU/RAM (`num_gpu=0`), GPU dibiarkan utuh
2. **`keep_alive=0`** — model dibuang begitu respons selesai, bukan ditahan 5 menit
3. **`ollama stop`** + verifikasi VRAM benar-benar kembali; kalau tidak, ia mencetak peringatan

```bash
ollama ps        # harus kosong setelah verifikasi
```

## Server MCP `local-workstation`

Terdaftar pada kedua CLI. 13 tool: telemetri hardware, tangkapan layar Wayland, notifikasi
desktop, kontrol volume, baca save-state SQL, catat quest, **baca/tulis isi terminal**
(`read_terminal` ~2 ms, `send_to_terminal`), muat ulang UI, restart daemon, dan kotak pesan
antar-mesin.

## Aturan keselamatan

Dua hal di bawah ini adalah penyebab sesi AI pernah hilang di sistem ini, dan kini ditegakkan
oleh hook, bukan hanya ditulis sebagai imbauan:

- **Jangan** `pkill -f "irsofka-station-core"` — pola itu ikut menghanguskan daemon produksi;
  systemd menghidupkannya lagi, dan PTY di dalamnya mati bersama sesinya.
- **Jangan** `tmux -L irsofka kill-server` — menghapus seluruh tab beserta sesi CLI di dalamnya.

`hooks/self_preservation.py` juga memblokir penghapusan memori (`rm -rf brain/`,
`DROP/TRUNCATE`, `DELETE` tanpa `WHERE`, `>` yang mengosongkan berkas memori) dan menolak
lahirnya entri baru di `$HOME` — data dan hasil install menuju `~/runtime`.
Ujinya: `hooks/test_self_preservation.py`.

## Cadangan

```bash
~/.ai-station/bin/station_backup.sh          # sekali jalan
systemctl --user list-timers irsofka-memory-backup.timer
~/.ai-station/bin/station_restore.sh --check # uji restore ke DB sementara (produksi aman)
```

Timer tiap jam, `Persistent=true` (mengejar setelah mesin mati), retensi 24 titik per-jam +
30 harian ke `~/runtime/backups/`. Restore selalu diuji ke database sementara sebelum
dianggap sah.

## Lisensi

Kode di repo ini: **PolyForm Noncommercial License 1.0.0** — bebas dipakai, dimodifikasi, dan
disebarkan selama bukan untuk komersial. Teks lengkap di [`LICENSE`](LICENSE).

Aset pihak ketiga **tidak** ikut dibatasi: xterm.js (MIT) dan font Inter/JetBrains Mono
(SIL OFL 1.1) — rinciannya di [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

> Required Notice: Copyright © 2026 irsjofka-alt
