# 🛠️ AI Workstation Toolset Catalog
**Direktori Utama:** `~/.ai-station/toolset/` (disymlink juga sebagai `~/.ai-station/tools/`)  
**Status:** Terpusat, Terstruktur, dan Terintegrasi  
**Host Environment:** Pop!_OS 24.04 LTS (COSMIC Desktop, Wayland)  

---

## 📂 Struktur & Daftar Skrip Aktif

Seluruh perkakas kerja AI Workstation telah dikonsolidasi secara rapi di dalam direktori ini agar tidak berceceran di direktori `home` atau tempat lain:

```
~/.ai-station/toolset/
├── pty_station_server.py      # [CORE] Server Daemon HTTP + Live PTY Streaming (Port 8999)
├── db_state.py                # [STORAGE] Dual-Engine Adapter (PostgreSQL 16 Primary + SQLite Fallback)
├── mcp_workstation_local.py   # [MCP] Model Context Protocol Native Hardware & OS Server
├── parallel_tri_engine.py     # [SWARM] Tri-Engine Parallel Task Dispatcher (Antigravity + Qoder + Ollama)
├── wayland_actor.py           # [DESKTOP] COSMIC Wayland Screen Capture & Desktop Interactor
├── brain_bridge.py            # [CONTEXT] Injektor Konteks Global & Brain Workspace Linker
├── incident_recorder.py       # [REPAIR] Error Tracker & Auto-Synthesizer Skill Baru (5x Repeat Crash)
├── TOOLSET_CATALOG.md         # Dokumen katalog ini
└── archive/                   # Folder arsip skrip purwarupa/lama (cth: station_dashboard.py)
```

---

## 🔍 Detail Setiap Modul

### 1. `pty_station_server.py` (Core Server Daemon)
- **Peran:** Backend server utama yang menjalankan antarmuka grafis desktop dan live streaming terminal.
- **Port:** `8999` (`http://127.0.0.1:8999`).
- **Teknologi:** Python 3.12 Asynchronous PTY Multiplexer + HTTP REST API.
- **Fitur Kunci:**
  - Mengelola pseudo-terminal Linux (`pty.openpty()`) untuk 3 sesi: Qoder CLI, Antigravity CLI, dan Pop!_OS Shell.
  - Menyediakan endpoint streaming output terminal (`/api/term/read`) dan input keyboard (`/api/term/write`).
  - Fitur saklar *Auto-Approve Tools* untuk menyuntikkan bypass flag secara otomatis (`--permission-mode bypass_permissions` dan `--dangerously-skip-permissions`).
  - Dikelola oleh systemd: `systemctl --user status irsofka-ai-workstation.service`.

### 2. `db_state.py` (Dual-Engine Database Adapter)
- **Peran:** Mengelola state penyimpanan profil pengembang, inventaris skill, telemetry GPU/RAM, dan task quest.
- **Koneksi:**
  - **Primer:** PostgreSQL 16 Enterprise (`postgresql://irsofka@localhost:5432/irsofka_ai_workstation`).
  - **Sekunder:** SQLite (`~/.ai-station/brain/workstation.db`) dengan failover zero-downtime otomatis jika PostgreSQL terhenti.
- **Fungsi Utama:** `init_db()`, `get_db_connection()`, `log_session_turn()`, `record_quest_task()`, `get_stats_summary()`.

### 3. `mcp_workstation_local.py` (Native Local MCP Server)
- **Peran:** Menghubungkan AI (Antigravity & Qoder) secara langsung ke hardware dan OS fisik menggunakan protokol JSON-RPC 2.0 stdio.
- **Status:** Terdaftar di `agy mcp list` dan `qoder mcp list -s user`.
- **Tools Tersedia:**
  - `get_hardware_telemetry`: Monitoring suhu GPU RTX 3060, VRAM, RAM, CPU load.
  - `take_screenshot_wayland`: Tangkapan layar desktop COSMIC via `cosmic-screenshot`.
  - `send_desktop_notification`: Menembakkan pop-up notifikasi desktop via `notify-send`.
  - `control_system_volume`: Mengontrol volume audio via `pactl`.
  - `query_workstation_db`: Eksekusi query baca dari database relasional.
  - `update_quest_task`: Mencatat progres tugas langsung ke PostgreSQL.

### 4. `parallel_tri_engine.py` (Tri-Engine Parallel Swarm)
- **Peran:** Eksekutor tugas paralel yang membagi instruksi pengguna ke dalam 3 mesin AI sekaligus:
  - **Engine 1:** Google Antigravity (Gemini 3.8 Flash High / 3.1 Pro High) ➔ High Architecture & Logic.
  - **Engine 2:** Qoder CLI (Qwen 3.8 Flash 1M Context / 0 Points) ➔ Codebase Synthesis & Deep File Audit.
  - **Engine 3:** Local Ollama / RTX 3060 ➔ Offline Micro-Tasks.
- **Output:** Menghimpun respons dari ketiga engine dan mencatat hasilnya ke tabel `quest_tasks` di PostgreSQL.

### 5. `wayland_actor.py` (Wayland Desktop Actor)
- **Peran:** Mengintegrasikan kapabilitas "Eye & Hand" pada Wayland COSMIC Desktop.
- **Fungsi:** Mengambil screenshot desktop ke `~/.ai-station/brain/memory/screenshot_latest.png` dan menyiapkan integrasi simulator input Wayland (`ydotool`).

### 6. `brain_bridge.py` (Global Brain Linker)
- **Peran:** Menghubungkan proyek coding di luar folder AI Station (misalnya `splendid-hawking` atau proyek game dev) ke direktori Brain global (`~/.ai-station/brain/`).
- **Fungsi:** Menghasilkan ringkasan status workspace dan menyuntikkan rules/skills otomatis.

### 7. `incident_recorder.py` (Self-Healing Auto Synthesizer)
- **Peran:** Memantau error CLI dan crash sistem. Jika jenis error yang sama terjadi sebanyak $\ge 5$ kali, modul ini secara otomatis menyintesis file *Learned Skill* baru di `~/.ai-station/brain/skills/learned/`.
