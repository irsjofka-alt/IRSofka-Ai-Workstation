# Irsofka AI Workstation

Studio AI desktop-native di Pop!_OS 24.04 COSMIC (Wayland). Satu daemon Rust yang
meng-host tiga CLI AI interaktif (Qoder, Antigravity, shell) di dalam terminal PTY,
menampilkannya pada satu jendela native (Tao + Wry + WebKitGTK), dan mencatat setiap
aksi ke basis data agar sesi bisa dipulihkan setelah apa pun yang memutusnya.

**Repo ini sengaja berisi kode saja.** Memori, log, basis data, dan kredensial tidak
di-push — lihat [.gitignore](.gitignore).

## Peta direktori

```
bin/            daemon launcher, CLI hub `ai-station`, skrip deploy
engine-rust/    sumber Rust: daemon + GUI (axum, portable-pty, tao, wry)
tools/          ingestor log->SQL, server MCP lokal, dispatcher tri-engine, adapter DB
hooks/          penegak aturan keselamatan sesi & pencatat handoff otomatis
config/         konfigurasi; sebagian dihasilkan daemon saat startup
```

## Menjalankan

Butuh: Rust (diuji dengan 1.99), PostgreSQL 16 (atau fallback SQLite otomatis),
`tmux`, `python3` dengan `psycopg2`.

```bash
cd engine-rust && cargo build --release
install -m 0755 target/release/irsofka-station-core ~/.ai-station/bin/
~/.ai-station/bin/launch_gui.sh
```

Daemon headless berjalan sebagai systemd user service `irsofka-ai-workstation.service`;
`irsofka-tabs.service` menghidupkan server tmux di luar cgroup daemon.

### Konfigurasi kredensial (wajib, tidak ada di repo)

Daemon dan perkakas Python membaca koneksi PostgreSQL dari, berjenjang:

1. variabel lingkungan `STATION_PG_CONN`, atau `STATION_PG_HOST/PORT/USER/PASSWORD/DB`
2. berkas `~/.ai-station/config/db_local.json` (di-ignore git)
3. nilai bawaan `localhost:5432`, tanpa password

Buat berkasnya:

```bash
cat > ~/.ai-station/config/db_local.json <<'JSON'
{ "host": "localhost", "port": 5432, "user": "isi_user_anda",
  "password": "isi_password_anda", "dbname": "irsofka_ai_workstation" }
JSON
chmod 600 ~/.ai-station/config/db_local.json
```

Tanpa berkas ini hasil clone akan gagal terhubung ke PostgreSQL dan jatuh ke SQLite —
sengaja, supaya tidak ada kredensial siapa pun yang ikut tersebar.

Identitas pemilik yang di-seed ke `player_profile` juga diambil dari lingkungan
(`STATION_OWNER_NAME`, `STATION_EMAIL_QODER`, `STATION_EMAIL_ANTIGRAVITY`) dengan
placeholder netral sebagai bawaan.

## Tab tidak mati saat daemon restart

Tab berjalan di `tmux -L irsofka`, bukan sebagai anak daemon, dan `run_tab.sh`
menambah `--continue` bila `config/cli_profiles.json` menyalakan `continue_session`.
Verifikasi:

```bash
curl -s localhost:8999/api/workspace | grep -o '"backend":"[a-z]*"'   # harus "tmux"
tmux -L irsofka ls
```

## Memulihkan konteks sesi

```bash
ai-station recovery 40    # riwayat aksi dari SQL + handoff tertunda
ai-station handoff        # hanya serah-terima
ai-station snapshot       # tulis handoff mekanis sekarang
```

## Aturan keselamatan

Dua hal di bawah ini adalah penyebab sesi AI pernah hilang di sistem ini, dan kini
ditegakkan oleh hook, bukan hanya ditulis sebagai imbauan:

- **Jangan** `pkill -f "irsofka-station-core"` — pola itu ikut menghanguskan daemon
  produksi, systemd menghidupkannya kembali, dan PTY di dalamnya mati bersama sesinya.
  Matikan proses uji dengan PID spesifik; guard hanya memblokir PID yang binernya
  benar-benar biner produksi.
- **Jangan** `tmux -L irsofka kill-server` — ini menghapus seluruh tab dan sesi CLI
  yang hidup di dalamnya.

Lihat `hooks/self_preservation.py` untuk daftar blokir lengkap dan
`hooks/test_self_preservation.py` untuk kasusnya.
