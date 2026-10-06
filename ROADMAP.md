# 🗺️ Roadmap — Irsofka AI Workstation

Status terakhir: **6 Oktober 2026, 15:4x WIB.** Setiap tanda di bawah punya bukti yang bisa
diuji ulang; yang belum teruji ditandai jelas. Dokumen ini menggantikan `ai_workstation_master_plan.md`
dan `PROJECT_SUMMARY_IRSOFKA_AI_WORKSTATION.md` yang sudah digabung ke sini + `README.md`.

## Fase

```
F1 Fondasi Brain & Rules      ✅ selesai
F2 Mesin Terminal PTY Rust    ✅ selesai
F3 Telemetri & State SQL      ✅ selesai
F4 Jendela Native Rust        ✅ selesai
F5 Integritas Memori & Sesi   ✅ selesai 6 Okt (baru)
F6 Ekosistem Verifikasi Silang 🟡 berjalan
F7 Studio Kreatif (Unity/ComfyUI) ⬜ belum mulai
```

## F1–F4 — fondasi (selesai, diverifikasi ulang 6 Okt)

Daemon Rust `irsofka-station-core` dual-mode (`--headless` + jendela tao/wry), tiga tab
interaktif, telemetri RTX 3060/RAM/disk, PostgreSQL 16 dengan fallback SQLite otomatis,
server MCP hardware, tanpa ketergantungan browser eksternal.

## F5 — Integritas memori & sesi ✅ (dikerjakan & diuji 6 Okt)

Masalah awal: dua keluhan pengguna — sesi tereset tiap restart daemon, dan composer Enter
mengirim alih-alih turun baris. Akar tunggal: build baru tidak pernah dipasang; restart
service bukan install build baru.

Yang selesai dan **terbukti**:

| Perbaikan | Bukti |
|---|---|
| Composer: Enter = baris baru, Ctrl+Enter = kirim, Ctrl+Shift+Enter = per baris | `curl :8999/` memuat `<textarea>` + `handleKey()` dengan `e.ctrlKey` |
| Tab pindah ke tmux di luar cgroup daemon | `/api/workspace` semua tab `backend:"tmux"`; daemon baru **mengadopsi** pane (`tmux_adopt`, `pane_pid` tetap) |
| Sesi tahan restart | 5× restart daemon pada 6 Okt, `session_id` tetap `63313f58`; cgroup shell pindah dari unit daemon ke `irsofka-tabs.service` |
| `--continue` sebagai jaring kedua | `cli_profiles.json` `continue_session:true` → `run_tab.sh` menambah flag |
| Enter dikirim sebagai `\r`, bukan `\n` | bug lama: prompt masuk kotak input tapi **tidak pernah disubmit** — akar kegagalan dispatch Antigravity |
| Handoff otomatis | hook `SessionEnd` + `PreCompact` → `world_memory` + `brain/memory/projects/handoff_auto_*.md` |
| Guard anti-bunuh-diri | `hooks/self_preservation.py`, **57/57** uji lulus |
| Self-healing benar-benar hidup | `incident_log` dulu 0 baris; kini 16 kegagalan nyata → 14 insiden, satu capai 7× → skill `auto_learned=TRUE` terbentuk |
| Dispatcher jujur | `COMPLETED` palsu untuk engine absen → kini `UNAVAILABLE`; tulis lewat adapter PostgreSQL, bukan SQLite mentah |
| Cadangan + uji restore | timer tiap jam; `station_restore.sh --check` memulihkan ke DB sementara: 2.973 `action_log`, 20 `world_memory`, 16 quest |
| Kredensial dicabut dari sumber | `db_local.json` (di-ignore) + env; 0 password di seluruh riwayat git publik |
| `$HOME` dirapikan | 4,7 GB ke `~/runtime`; guard menolak entri baru di `$HOME` |

## F6 — Ekosistem verifikasi silang 🟡

Selesai:
- `ai-station verify` — Qoder ↔ Gemini, dua arah. Gemini terbukti **menolak klaim salah**
  dan mengiyakan klaim benar, dengan merujuk baris kode (`main.rs:1384`, `1426-1431`).
- `config/engines.json` — registry berbasis konfigurasi; menambah mesin tanpa ubah kode.
- Kotak pesan `ai_message` + MCP `ask_peer` / `check_messages` / `resolve_message`.
- `local_llm.py` — tiga penjaga pelepasan GPU + **gerbang VRAM** dengan fallback CPU/RAM.

Belum:
- ⬜ **Tier `verify` belum terpasang.** Yang sudah di-pull dan diuji hanya `qwen3.5:4b`
  (3,3 GB, tier `light`/`local-fallback`). `qwen3.5:9b` terpotong di 39% karena batas waktu
  perintahnya salah hitung; `phi4:14b` belum dimulai. Nama tag ketiganya sudah dipastikan ADA
  di registry Ollama (manifest HTTP 200), jadi tinggal unduh.
- ⬜ Uji beban lintas engine yang sesungguhnya: verifikasi berjalan sambil ComfyUI/Unity
  memakai GPU, dan buktikan gerbang VRAM menolak pada saat itu. Yang teruji baru
  "sesi AI lain aktif", bukan "aplikasi GPU aktif".
- ⬜ Monitor kuota Antigravity di GUI (`/api/usage` sudah menyediakan datanya).

### Selesai ronde ini (6 Okt sore)
- ✅ `parallel_tri_engine.py` membaca `config/engines.json` (2026-10-06). Model default
  `qwen2.5-coder:7b` — yang tidak pernah ada di registry — dibuang; `gemini`, `qoder`, dan
  `local` kini diambil dari registry, dan tier lokal bisa dipilih lewat `STATION_LOCAL_ENGINE`.
  Kebijakan `never_load_if` ditegakkan: saat VRAM kosong tidak cukup, engine menolak dan
  menyebut pemakai GPU-nya ("brave 137 MiB, antigravity 104 MiB"), dengan status `UNAVAILABLE`
  yang tercatat ke PostgreSQL. Payload Ollama sekarang mengirim `keep_alive` dari registry —
  sebelumnya tidak, jadi model tertahan di VRAM lima menit setelah tugas selesai.
- ✅ **Daemon berhenti melayani identitas.** `/api/stats` dan `/api/usage` meneruskan
  `qoder status -o json` apa adanya — email, nama lengkap, avatar URL. Daemon hanya listen di
  127.0.0.1, tapi tiap engine di tab punya shell, jadi `curl localhost:8999` cukup untuk
  memasukkan identitas itu ke konteks model cloud. Redaksi di satu sumber (`qoder_account`)
  plus penyaring berbentuk; kolom `username` `player_profile` dikeluarkan (nol rujukan di
  `gui.html`). Verifikasi: 0 temuan email/nama pada tiga endpoint.
- ✅ **Katalog perkakas ditulis ulang.** `tools/TOOLSET_CATALOG.md` menyebut server Python lama
  sebagai `[CORE]` dan mengklaim systemd menghidupkannya di port 8999 — yang melayani port itu
  daemon Rust. Server Python dinyatakan arsip lokal (sudah di-gitignore sejak lama).
- ✅ **SQLite basi diarsipkan.** `brain/workstation.db` dipindah ke
  `~/runtime/backups/db/…sqlite-stale-20261006` setelah dipastikan 0 barisnya tidak ada di
  PostgreSQL; tersimpannya file itu memberi peluang sesi fallback membaca salinan basi sebagai
  keadaan sekarang.
- ✅ **Engine lokal teruji terhadap model nyata untuk pertama kalinya.** `qwen3.5:4b` di-pull
  (3,3 GB) lalu dijalankan lewat dispatcher: `COMPLETED` dalam 71,4 s, `ollama ps` kosong
  sesudahnya, dan VRAM benar-benar kembali (10.882 → 10.979 MiB bebas). Cabang penolakan ikut
  diuji dengan memaksa kebutuhan 999999 MiB — ia menolak dan menyebut pemakai GPU-nya.
- ✅ **Handoff berhenti melaporkan state git yang salah.** `snapshot()` mem-probe
  `engine-rust/` dan melabelinya "git engine-rust", padahal direktori itu tidak punya repo
  sendiri — git naik ke repo induk, sehingga sesi berikutnya membaca keadaan yang keliru.
  Sekarang probe ke `~/.ai-station` dengan branch, cap waktu, dan hitungan berkas yang belum
  di-commit. Bagian "Keadaan mesin" juga tidak lagi terpotong: pembaca `handoff` membatasi isi
  pada 700 karakter — tepat di depan blok terpenting.

## F7 — Studio kreatif ⬜

Belum ada sama sekali: `~/Creative-Studio` tidak ditemukan, ComfyUI belum terpasang.
Rencana sinkronisasi: ComfyUI membuka API HTTP di `:8188`, jadi titik tempelnya jelas —
satu tool MCP `comfy_workflow` yang **memakai gerbang VRAM yang sama**, supaya generatif
dan LLM lokal tidak berebut GPU dengan engine grafis.

## Batas yang diketahui (jangan di-"perbaiki" tanpa perlu)

- **Akuntansi token tidak tersedia.** Kolom `input_tokens`/`output_tokens` terisi tapi
  bernilai 0, karena **CLI-nya sendiri yang menulis 0** ke lognya (dipindai 527 event mentah).
  Ingestor tidak membuang data. `duration_ms` dan `exit_code` tetap akurat.
- **Sesi CLI terikat direktori kerja.** `--continue` tidak menyeberang antar folder;
  jembatannya PostgreSQL + handoff, yang terbukti lintas-direktori.
- **Sebagian dotfile di `$HOME` tidak bisa dipindah** (`.bashrc`, `.config`, `.var`, `.nv`,
  `.qoder`) — aplikasinya membaca path persis. Yang bisa sudah pindah ke `~/runtime`.
- **Guard hanya mengikat tindakan AI lewat shell**, bukan niat mengakali (`base64 | bash`
  tetap mungkin) dan bukan aplikasi yang membuat `~/.nama` saat dijalankan manusia.

## Riwayat singkat

Dokumen `ai_workstation_master_plan.md` (v2.3.0) dan `PROJECT_SUMMARY_...md` pernah menandai
"Fase 5 TERCAPAI 100%" sementara Engine-3 tidak terpasang, `logs/server.log` 0 byte,
`incident_log` kosong, dan `world_memory` tidak terisi. Keduanya dipensiunkan 6 Oktober dan
isinya digabung ke `README.md` + roadmap ini, dengan klaim yang tidak terbukti dicabut.
Salinan aslinya disimpan di `~/.ai-station/archive/`.
