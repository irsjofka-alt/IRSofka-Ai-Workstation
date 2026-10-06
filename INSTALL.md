# Instal

Repo ini adalah workstation pribadi yang dipublikasikan, bukan produk jadi. Yang dijamin
ada: kode, unit systemd, guard, dan aturan. Yang **tidak** ikut ter-clone — dan memang
sengaja:

| Tidak di repo | Kenapa |
|---|---|
| `brain/` (memori, skill, insiden, handoff) | isi kepala mesin ini, bukan milik orang lain |
| `config/db_local.json` | kredensial database |
| `logs/`, `*.db`, `*.sql` | data dan jejak |
| `engines/` | state milik Qoder/Antigravity |

Jadi hasil clone yang bersih akan **gagal terhubung ke database secara terang-terangan**,
bukan ikut membawa password milik saya. Itu perilaku yang benar.

Lisensi: PolyForm Noncommercial — dipakai dan dimodifikasi boleh, dikomersialkan tidak.

## Prasyarat

Butuh Linux dengan systemd (unit-nya *user* service, jadi tidak perlu root untuk
menjalankan workstation), tmux, dan Python 3.

```bash
# Debian / Ubuntu / Pop!_OS
sudo apt install -y tmux python3 python3-psycopg2 postgresql postgresql-client \
  rustc cargo pkg-config libwebkit2gtk-4.1-dev libgtk-3-dev

# Arch
sudo pacman -S tmux python python-psycopg2 postgresql rust cargo \
  webkit2gtk-4.1 gtk3
```

PostgreSQL optional — tanpa itu ia jatuh ke SQLite. `rustc`/`cargo` hanya perlu kalau kamu
build engine-nya sendiri (GUI native); tanpa itu kamu tetap bisa pakai daemon headless +
tool CLI.

## 0. Siapkan `~/runtime` dulu, sebelum install apa pun

Aturan yang membuat home tetap bersih: **hasil install berbasis user masuk `~/runtime`**,
tidak menumpuk setingkat di `$HOME`. Ini bukan hiasan — ia dipasang lewat variabel
standard, jadi installer mana pun yang menghormati XDG ikut ke sana.

```bash
mkdir -p ~/runtime/{cache,local,local/bin,local/share,local/state,cargo,rustup,backups}
```

Tambahkan ke `~/.bashrc` (dan ke `~/.config/environment.d/irsofka.conf` untuk sesi GUI
systemd-user):

```bash
export XDG_CACHE_HOME="$HOME/runtime/cache"
export XDG_DATA_HOME="$HOME/runtime/local/share"
export XDG_STATE_HOME="$HOME/runtime/local/state"
export PYTHONUSERBASE="$HOME/runtime/local"
export PIP_CACHE_DIR="$XDG_CACHE_HOME/pip"
export CARGO_HOME="$HOME/runtime/cargo"
export RUSTUP_HOME="$HOME/runtime/rustup"
export HISTFILE="$HOME/runtime/local/bash_history"
export PATH="$HOME/runtime/local/bin:$CARGO_HOME/bin:$PATH"
```

`bin/relocate_home.sh` memindahkan cache yang **sudah terlanjur** ada ke `~/runtime` dan
meninggalkan symlink kompatibilitas. Jalankan tanpa argumen dulu (dry-run), baru `--apply`.

## 1. Clone ke `~/.ai-station`

Struktur ini yang dibaca kode (`station_dir()` = `$HOME/.ai-station`); bukan nama yang bisa
diganti sembarangan.

```bash
git clone https://github.com/irsjofka-alt/IRSofka-Ai-Workstation.git ~/.ai-station
mkdir -p ~/.ai-station/{brain/memory,brain/skills,brain/rules,logs,engines,bin}
chmod +x ~/.ai-station/bin/*.sh
```

Beri isi awal `brain/` dari templat, jangan dari memori saya:

```bash
cp ~/.ai-station/documents/MEMORY.md  ~/.ai-station/brain/memory/MEMORY.md
cp ~/.ai-station/documents/SKILLS.md  ~/.ai-station/brain/skills/SKILLS.md
```

## 2. Install CLI AI-nya

```bash
# Qoder CLI
curl -fsSL https://qoder.com/install | bash

# Antigravity CLI (Gemini)
curl -fsSL https://antigravity.google/cli/install.sh | bash
```

Kalau kamu baru mau mencoba Qoder, pemilik repo menyimpan tautan referral di
[README](README.md#instal) — mendaftar lewat itu memberi kamu 500 kredit dan dia 1.000
saat pembayaran pertama. Tidak wajib, dan lisensi proyek ini tidak berubah karenanya.

Sebelum mengeksekusi apa pun dari internet, bacalah dulu:

```bash
curl -fsSL https://qoder.com/install -o /tmp/qoder-install.sh
less /tmp/qoder-install.sh && bash /tmp/qoder-install.sh
```

Kedua CLI keras kepala menulis state ke path tetap di `$HOME`: Qoder ke `~/.qoder`,
`~/.qodersec`, `~/.qmind`; Antigravity ke `~/.gemini`. Itu tidak bisa dicegah lewat
variabel lingkungan. Yang bisa dilakukan adalah **memindahkan badannya ke dalam
workstation dan meninggalkan symlink di path lama**, supaya cuma ada satu tempat yang
bisa dibaca dan `$HOME` tetap berisi satu baris per tool:

```bash
for d in qoder qodersec qmind gemini; do
  src=~/.${d}; dst=~/.ai-station/engines/${d}
  [ -d "$src" ] && [ ! -L "$src" ] || continue          # sudah symlink? lewati
  if [ -d "$dst" ] && [ -n "$(ls -A "$dst" 2>/dev/null)" ]; then
    echo "$dst sudah berisi — pindahkan isinya manual, jangan ditimpa"; continue
  fi
  mkdir -p "$(dirname "$dst")" && mv "$src" "$dst" && ln -sfn "$dst" "$src"
done

ls -ld ~/.qoder ~/.qodersec ~/.qmind ~/.gemini         # harus "l", bukan "d"
```

Lakukan ini **setelah** menginstal CLI-nya dan **sebelum** login pertama ke mereka:
memindah state yang sudah terlanjur berisi tetap bisa, tapi sesi yang sedang jalan akan
terputus — jadi matikan CLI-nya lebih dulu kalau kamu berada di sini.

`hooks/self_preservation.py` mengenali path-path itu sebagai *shim* yang sah
(`config/home_shims.json`) dan menolak bentuk lain: `mkdir ~/.qoder anything`,
`echo > ~/.qoder`, atau symlink yang sumbernya dari luar workstation.

## 3. Satu workspace untuk semua AI

```bash
mkdir -p ~/Documents/ai-workstation/projects
cp ~/.ai-station/documents/AGENTS.md ~/Documents/ai-workstation/AGENTS.md
cp ~/.ai-station/documents/QODER.md  ~/Documents/ai-workstation/QODER.md
cp ~/.ai-station/documents/GEMINI.md ~/Documents/ai-workstation/GEMINI.md
```

`AGENTS.md` adalah kontrak yang dibaca semua engine, dan ia menang kalau bertabrakan
dengan `QODER.md`/`GEMINI.md`. Isinya sedikit tapi tidak bisa ditawar: state bersama di
database, `$HOME` bukan tempat sampah, jujur soal status, dan jangan membunuh diri sendiri.

## 4. Skill yang sama untuk semua CLI

```bash
~/.ai-station/bin/wire_skills.sh
```

Menyatukan `brain/skills/` ke satu pohon (`engines/agents/skills/`) lalu menautkannya ke
path yang dipindai tiap engine: `~/.agents/skills` (Qoder) dan
`~/.gemini/config/plugins/station-rules/skills` (Antigravity). Periksa kapan saja dengan
`wire_skills.sh --check`. Kalau menambah pack skill, letakkan di `brain/skills/<nama>/`
dengan `SKILL.md` ber-frontmatter, lalu tautkan — jangan taruh di folder engine.

## 5. Database (memori jangka panjang)

```bash
createuser --pwr --createdb namamu
createdb   -O namamu workstation_ai
```

Kredensial ditulis ke berkas yang di-gitignore, bukan ke sumber:

```bash
cat > ~/.ai-station/config/db_local.json <<JSON
{ "host": "localhost", "port": 5432, "user": "namamu", "password": "isi_password_anda", "dbname": "workstation_ai" }
JSON
chmod 600 ~/.ai-station/config/db_local.json
```

Variabel lingkungan `STATION_PG_HOST/PORT/USER/PASSWORD/DB` menang atas berkas itu, jadi
alternatifnya tanpa file juga bisa. Skema dibuat sendiri — tidak ada `.sql` yang perlu
diimpor:

```bash
python3 ~/.ai-station/tools/db_state.py     # memanggil init_db(), lalu mencetak engine aktif
```

Tanpa database apa pun sistem jatuh ke SQLite di `~/.ai-station/brain/workstation.db`.
Berfungsi, tapi tidak multi-engine: dua CLI yang menulis ke satu PostgreSQL adalah cara
workstation ini menjaga mereka tidak punya cerita berbeda. Tabel intinya `action_log`,
`world_memory`, `quest_tasks`, `session_turns`, `incident_log`, `skills_inventory`,
`ai_message`.

## 6. Build engine dan pasang unit

```bash
cd ~/.ai-station/engine-rust && cargo build --release
mkdir -p ~/.ai-station/bin && cp target/release/irsofka-station-core ~/.ai-station/bin/
~/.ai-station/bin/install_units.sh --dry-run    # lihat dulu
~/.ai-station/bin/install_units.sh              # pasang + enable
```

`install_units.sh` **menolak menimpa** unit yang sudah ada dan berbeda, kecuali `--force`.
Unit paling penting di antaranya `irsofka-tabs.service`: ia menaungi server tmux di **luar
cgroup daemon**. Itulah yang membuat restart daemon tidak lagi membunuh sesi AI — tanpanya,
`KillMode=control-group` systemd akan ikut mematikan CLI yang kamu sedang pakai.

Jalankan GUI-nya: `~/.ai-station/bin/launch_gui.sh`. Composer-nya Enter untuk baris baru,
Ctrl+Enter untuk mengirim.

## 7. Daftarkan guard, handoff, dan MCP ke Qoder

Tanpa langkah ini dua fitur utama proyek ini tidak berjalan: `self_preservation.py`
(AI tidak bisa membunuh daemon/sesinya sendiri, dan `$HOME` tidak bisa dikotori) dan
`auto_handoff.py` (serah-terima mekanis tiap sesi berakhir atau konteks diringkas).
Keduanya adalah *hook*, jadi harus didaftarkan di `~/.qoder/settings.json`.

```bash
python3 - <<'PY'
import json, os
p = os.path.expanduser("~/.qoder/settings.json")
cfg = json.loads(open(p).read()) if os.path.exists(p) else {}
st = os.path.expanduser("~/.ai-station")
h = cfg.setdefault("hooks", {})
h["PreToolUse"] = [{"matcher": "Bash", "hooks": [{
    "type": "command", "command": f"python3 {st}/hooks/self_preservation.py",
    "name": "station-self-preservation", "timeout": 10}]}]
for ev in ("SessionEnd", "PreCompact"):
    h.setdefault(ev, []).append({"hooks": [{
        "type": "command", "command": f"python3 {st}/hooks/auto_handoff.py",
        "name": "station-auto-handoff", "timeout": 25}]})
cfg.setdefault("mcpServers", {})["local-workstation"] = {
    "command": "python3", "args": [f"{st}/tools/mcp_workstation_local.py"]}
json.dump(cfg, open(p, "w"), indent=2)
print("terdaftar di", p)
PY
```

Skrip di atas **menimpa** kunci `hooks` yang sudah ada. Kalau `~/.qoder/settings.json`
kamu sudah punya hook sendiri, gabungkan manual — jangan jalankan apa adanya.

Untuk Antigravity, MCP yang sama didaftarkan di `~/.gemini/config/mcp_config.json` dan
kontraknya diinjeksi lewat plugin:

```bash
mkdir -p ~/.gemini/config/plugins/station-rules/rules
ln -sfn ~/Documents/ai-workstation/AGENTS.md ~/.gemini/config/plugins/station-rules/rules/AGENTS.md
~/.ai-station/bin/wire_skills.sh
python3 -c "import json,os;p=os.path.expanduser('~/.gemini/config/config.json');d=json.load(open(p)) if os.path.exists(p) else {};d.setdefault('plugins',{})['station-rules']={'enabled':True};json.dump(d,open(p,'w'),indent=2);print('station-rules diaktifkan')"
```

`restart_workstation_daemon` dan `refresh_workstation_ui` adalah tool MCP yang sama yang
dipakai AI untuk memuat ulang UI dan daemon tanpa Anda suruh — restart daemon aman sejak
tab pindah ke tmux.

## 8. Periksa bahwa ia benar-benar hidup

```bash
ai-station recovery 40                                   # riwayat aksi + handoff
curl -s localhost:8999/api/stats | head -c 300
tmux -L irsofka ls
cd ~/.ai-station/hooks && python3 test_self_preservation.py   # guard: 66 kasus
~/.ai-station/bin/wire_skills.sh --check
```

Di dalam Qoder CLI, `/skills` (atau daftar skill yang muncul di awal sesi) harus
menyebutkan pack yang sama dengan yang dilihat Antigravity. Kalau tidak, `wire_skills.sh
--check` yang bilang "sehat" belum cukup — tanyakan langsung ke engine-nya, karena skill
yang tidak muncul di daftar engine adalah skill yang tidak akan pernah terpakai.

## Kalau kamu hanya ingin satu hal

Guard `hooks/self_preservation.py` + `PreCompact`/`SessionEnd` → `hooks/auto_handoff.py`.
Itu sepasang yang membuat AI di mesin ini tidak membunuh dirinya sendiri dan tidak
kehilangan jejak saat konteksnya diringkas — masalah yang seluruh proyek ini ada untuk itu.
