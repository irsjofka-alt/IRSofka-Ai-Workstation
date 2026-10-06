# 🛠️ Katalog Perkakas Workstation

**Direktori:** `~/.ai-station/tools/` — ini yang ikut repo.
**Host referensi:** Pop!_OS 24.04 LTS (COSMIC Desktop, Wayland), PostgreSQL 16.
**Yang melayani port 8999 adalah daemon Rust** (`engine-rust/`, biner
`irsofka-station-core`), bukan skrip Python di folder ini.

Setiap perkakas punya `--help` sendiri; dokumen ini hanya bilang **kapan** suatu perkakas
dipakai dan **apa yang dijaminnya**, supaya mesin tidak menebak dari nama berkas.

```
tools/
├── db_state.py                adapter penyimpanan dua engine
├── session_ingestor.py        perekam jejak nyata ke SQL
├── mcp_workstation_local.py   server MCP (13 tool) untuk semua CLI
├── cross_verify.py            satu AI mengaudit pekerjaan AI lain
├── local_llm.py               pemanggil Ollama yang tidak menahan GPU
├── parallel_tri_engine.py     dispatcher tiga engine paralel
├── incident_recorder.py       penghitung kegagalan → skill otomatis
├── wayland_actor.py           screenshot & input di COSMIC/Wayland
└── brain_bridge.py            penaut memori/skill ke workspace proyek
```

## Isi

### `db_state.py` — satu pintu ke memori faktual
PostgreSQL primer, SQLite cadangan. `get_db_connection()` mengembalikan pasangan
`(koneksi, 'POSTGRESQL'|'SQLITE')` dan pemakai **wajib** memakai placeholder yang cocok —
di sinilah beberapa kegagalan tulis terjadi ketika dua basis data dianggap sama.
Dijalankan langsung (`python3 tools/db_state.py`) akan memanggil `init_db()` dan mencetak
engine aktif. Tabel inti: `action_log`, `world_memory`, `quest_tasks`, `session_turns`,
`incident_log`, `skills_inventory`, `ai_message`, `player_profile`.

### `session_ingestor.py` — jejak, bukan kesan
`watch` memantau log sesi Qoder dan menulis tiap prompt/perintah/jawaban ke SQL; `snapshot`
menulis handoff mekanis; `sweep_failures` menghitung kegagalan per `error_signature`
(path direduksi ke basename supaya kegagalan identik menumpuk di satu hitungan, bukan
menyebar jadi banyak insiden palsu).

### `mcp_workstation_local.py` — tangan AI ke mesin
13 tool: `take_screenshot_wayland`, `get_hardware_telemetry`, `query_workstation_db`,
`update_quest_task`, `send_desktop_notification`, `control_system_volume`, `read_terminal`,
`send_to_terminal`, `refresh_workstation_ui`, `restart_workstation_daemon`, `ask_peer`,
`check_messages`, `resolve_message`. Server ini wajib menjawab **setiap** request MCP —
`resources/list`, `prompts/list`, `ping` termasuk. Versi dulu diam pada metode yang tidak
diketahui, dan karena itu Antigravity berhenti memuat tool sama sekali.

### `cross_verify.py` — verifikasi lintas mesin
Membaca `config/engines.json` (tidak ada engine yang di-hardcode di sini). Tiga transport:
`pane` (tab tmux yang hidup — cepat, dan memakai sesi yang sudah login), `cli` (spawn proses
baru), `ollama` (model lokal). `VERDICT:` diambil dari kemunculan **terakhir**, karena
scrollback pane menyimpan verdict lama; mengambil yang pertama memberi hasil salah yang
terlihat benar. Engine yang tidak ada dilaporkan `UNAVAILABLE`.

### `local_llm.py` — disiplin GPU yang bisa dijalankan
Tiga penjaga: gerbang VRAM sebelum muat (`min_free_mib` dari registry), `keep_alive=0` supaya
model tidak tertahan lima menit, dan `ollama stop` + periksa VRAM benar-benar kembali (cetak
⚠ kalau tidak). Tanpa GPU cukup, jalankan di CPU/RAM (`--cpu`, `options.num_gpu=0`).
`--status` menjelaskan apa yang menghalangi sekarang.

### `parallel_tri_engine.py` — tiga engine sekaligus
Dispatcher paralel yang **membaca registry yang sama** dengan `cross_verify.py`. Batas waktu
lama 120 detik terbukti menghasilkan laporan `FAILED` palsu (satu giliran kerja Qoder yang sah
bisa belasan menit), sekarang `STATION_TASK_TIMEOUT` default 1800. `TIMEOUT`, `UNAVAILABLE`,
dan `FAILED` adalah status berbeda dan tidak pernah dilebur.

### `incident_recorder.py` — kegagalan yang berulang jadi pelajaran
Hitungan per `error_signature` masuk ke `incident_log`; pada kelipatan 5x ia menulis
`brain/skills/learned/<signature>.md`, mendaftar skill di `skills_inventory`
(`auto_learned = TRUE`), dan menandai insiden `resolved`. Tanpa pendaftaran ke SQL, skill
"terbelajar" hanya ada sebagai teks yang tidak pernah dihitung oleh siapa pun.

### `wayland_actor.py` — mata dan tangan
Tangkap layar COSMIC/Wayland dan kirim input mouse/keyboard. Batasnya nyata: Wayland menolak
screenshot dari proses tanpa sesi desktop, dan `ydotool` butuh akses socket uinput. Kalau keduanya gagal, jawabannya "belum terverifikasi visual", bukan "aplikasinya rusak".

### `brain_bridge.py` — menautkan, tidak menyalin
Menghubungkan workspace proyek ke `~/.ai-station/brain/` lewat berkas pointer, supaya satu
perubahan memori cukup ditulis sekali. Menyalin isi brain ke folder proyek adalah cara tercepat mendapatkan dua AI dengan cerita berbeda.

## Yang tidak ada di daftar ini, dan alasannya

`tools/pty_station_server.py` (server Python lama) masih ada di mesin ini sebagai arsip
lokal dan di-gitignore. Ia **digantikan** `engine-rust/src/main.rs`; dokumen lama sempat
menuliskannya sebagai `[CORE]`, yang membuat orang mengira systemd menghidupkan skrip
Python itu di port 8999. Kalau kamu menemukan berkas itu: jangan dijalankan, dan jangan
dijadikan rujukan.
