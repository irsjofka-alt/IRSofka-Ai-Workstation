#!/usr/bin/env bash
# Deploy biner engine-rust hasil build ke ~/.ai-station/bin lalu restart daemon.
#
#   ./deploy_engine.sh [--build] [--yes] [--clean-orphans]
#
# PERINGATAN: restart service menghancurkan PTY yang DIBUKNYA SENDIRI. Sejak tab
# dipindah ke tmux (irsofka-tabs.service + SessionKind::Tmux), sesi Qoder/Antigravity
# berada di LUAR cgroup daemon sehingga restart tidak lagi memutusnya.
# Jaring pengaman bertingkat:
#   1. tab berjalan di `tmux -L irsofka` → proses CLI & scrollback bertahan,
#   2. cli_profiles.json continue_session=true → run_tab.sh menambah --continue,
#      jadi kalau pane mati pun CLI menyambung sesi terakhir,
#   3. irsofka-action-log.service merekam tiap prompt/perintah/jawaban ke SQL,
#   4. hook SessionEnd/PreCompact menulis handoff mekanis ke world_memory + .md,
#   5. `ai-station recovery 40` menyusun ulang konteks dari semuanya.
# KECUALI: tab yang masih berstatus backend="pty" (daemon lama) MATI saat restart.
# Cek dulu dengan: curl -s localhost:8999/api/workspace | grep -o '"backend":"[a-z]*"'
set -euo pipefail

ROOT="$HOME/.ai-station/engine-rust"
BIN_SRC="$ROOT/target/release/irsofka-station-core"
BIN_DST="$HOME/.ai-station/bin/irsofka-station-core"
SERVICE="irsofka-ai-workstation.service"
DO_BUILD=0; ASSUME_YES=0; CLEAN_ORPHANS=0

for arg in "$@"; do
  case "$arg" in
    --build) DO_BUILD=1 ;;
    --yes) ASSUME_YES=1 ;;
    --clean-orphans) CLEAN_ORPHANS=1 ;;
    *) echo "argumen tak dikenal: $arg"; exit 2 ;;
  esac
done

if [ "$DO_BUILD" = "1" ]; then
  echo "🔨 cargo build --release ..."
  ( cd "$ROOT" && cargo build --release )
  echo "🧪 cargo test ..."
  ( cd "$ROOT" && cargo test -q ) || { echo "  ✗ uji Rust gagal — deploy dihentikan"; exit 1; }
fi
[ -x "$BIN_SRC" ] || { echo "❌ biner hasil build tidak ada di $BIN_SRC"; exit 1; }

if [ "$ASSUME_YES" != "1" ]; then
  echo "⚠️  Deploy akan me-restart $SERVICE dan MEMUTUS sesi AI yang sedang jalan di tab."
  read -r -p "   Lanjutkan? [y/N] " reply
  [ "${reply,,}" = "y" ] || { echo " dibatalkan."; exit 0; }
fi

STAMP=$(date +%Y%m%d-%H%M%S)
if [ -f "$BIN_DST" ]; then
  cp -p "$BIN_DST" "$BIN_DST.bak-$STAMP"
  echo "🗄️  cadangan lama: $BIN_DST.bak-$STAMP"
fi
# Cadangan lama menumpuk ~5 MB tiap deploy dan tidak ada yang me-rollback 20 langkah.
# Sisa 8 terbaru (±40 MB) cukup untuk membandingkan regresi; sisanya dibuang.
ls -1t "$BIN_DST.bak-"* 2>/dev/null | tail -n +9 | while IFS= read -r old; do
  rm -f "$old"
  echo "🧹 cadangan usang dihapus: $(basename "$old")"
done
install -m 0755 "$BIN_SRC" "$BIN_DST"
echo "📦 biner baru terpasang ($(stat -c %s "$BIN_DST") byte, build $(stat -c %y "$BIN_SRC" | cut -d. -f1))"

# Install bisa terpotong (disk penuh, file masih dieksekusi). Pastikan identik sebelum restart.
if [ "$(md5sum "$BIN_SRC" | cut -d' ' -f1)" != "$(md5sum "$BIN_DST" | cut -d' ' -f1)" ]; then
  echo "❌ hasil install tidak identik dengan hasil build — batalkan, jangan restart."
  echo "   Pulihkan: install -m 0755 $BIN_DST.bak-$STAMP $BIN_DST"
  exit 1
fi

# Profil CLI hanya ditulis oleh biner baru; pastikan isinya wajar sebelum restart.
systemctl --user restart "$SERVICE"
echo "🔄 $SERVICE di-restart, menunggu daemon siap..."
for _ in $(seq 1 30); do
  if curl -sf --max-time 2 http://127.0.0.1:8999/api/stats > /dev/null 2>&1; then break; fi
  sleep 1
done

echo
echo "=== VERIFIKASI ==="
fail=0

# 0) Buktikan biner yang BERJALAN sama dengan biner yang baru dipasang.
#    Memeriksa file di disk saja tidak cukup: daemon bisa saja masih proses lama
#    (restart gagal, atau unit-nya tidak dihidupkan ulang).
DPID=$(systemctl --user show "$SERVICE" -p MainPID --value 2>/dev/null || echo "")
if [ -n "$DPID" ] && [ -r "/proc/$DPID/exe" ]; then
  RUN_MD5=$(md5sum "/proc/$DPID/exe" 2>/dev/null | cut -d' ' -f1)
  DST_MD5=$(md5sum "$BIN_DST" | cut -d' ' -f1)
  if [ -n "$RUN_MD5" ] && [ "$RUN_MD5" = "$DST_MD5" ]; then
    echo "  ✓ daemon pid $DPID menjalankan biner baru ($RUN_MD5)"
  else
    echo "  ✗ daemon pid $DPID menjalankan biner LAIN: $RUN_MD5 ≠ $DST_MD5"
    fail=1
  fi
else
  echo "  ✗ tidak bisa membaca /proc/${DPID:-?}/exe — daemon tidak aktif?"
  fail=1
fi

probe() {
  local path="$1" need="${2:-200}"
  local body rc
  body=$(curl -s --max-time 25 "http://127.0.0.1:8999$path"); rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$body" ]; then
    echo "  ✗ $path tidak menjawab (curl rc=$rc)"; fail=1; return
  fi
  if [ "$path" = "/" ]; then
    # Tanpa pipe. `printf '%s' "$body" | grep -q POLA` membuat grep keluar lebih awal,
    # lalu printf kena SIGPIPE; di bawah `set -o pipefail` pipeline bernilai GAGAL
    # walaupun polanya cocok. Bug ini pernah melaporkan deploy yang sebenarnya SUKSES
    # sebagai gagal, lalu mencetak instruksi rollback ke biner lama.
    if [[ "$body" == *'Ctrl</kbd>+<kbd>Enter'* && "$body" == *'<textarea id="prompt-input"'* ]]; then
      echo "  ✓ / composer baru aktif (textarea + Ctrl+Enter), ${#body} byte"
    else
      echo "  ✗ / masih HTML lama — biner tidak memuat gui.html yang benar"; fail=1
    fi
    return
  fi
  echo "  ✓ $path ${body:0:$need}"
}
probe /api/workspace 300
probe /api/recovery 260
probe /api/log 160
probe /api/models 160
probe /api/usage 200
probe /api/stats 200
probe / 1

# Urutan kunci master data tidak boleh berubah lewat daemon. serde_json menyimpan objek JSON
# sebagai map TERURUT NAMA KUNCI, jadi satu kali parse lalu Json(v) sudah cukup untuk mengacak
# ulang seluruh engines.json: 104 baris "berbeda" padahal tidak ada satu nilai pun yang berubah.
# Perbaikan = kirim byte mentah bolak-balik; probe ini menjaga supaya tidak diam-diam kembali,
# karena kegagalannya baru terlihat setelah puluhan save bertumpuk jadi diff yang tak terbaca.
if command -v jq >/dev/null 2>&1; then
  ORDER_FILE=$(jq -c '.engines | keys_unsorted' "$HOME/.ai-station/config/engines.json" 2>/dev/null)
  ORDER_API=$(curl -s --max-time 40 http://127.0.0.1:8999/api/master | jq -c '.document.engines.engines | keys_unsorted')
  if [ -z "$ORDER_API" ] || [ "$ORDER_API" = "null" ]; then
    echo "  ✗ /api/master tidak mengembalikan dokumen master data"; fail=1
  elif [ "$ORDER_FILE" = "$ORDER_API" ]; then
    echo "  ✓ urutan kunci master data utuh lewat daemon (${ORDER_FILE:0:56}…)"
  else
    echo "  ✗ daemon menata ulang kunci master data:"
    echo "      berkas: $ORDER_FILE"
    echo "      api   : $ORDER_API"
    fail=1
  fi
fi

# Kontrak §4: satu panggilan engine harus mati bersama pemanggilnya. Dua hal diperiksa, dan
# keduanya pernah terbukti gagal tanpa suara: subprocess.run(timeout=) meninggalkan anak proses
# yang tetap memegang RSS (kontrol terukur: 1 cucu lolos), dan `/bin/kill -KILL -PGID` keluar
# dengan rc=0 tanpa membunuh apa pun karena angka negatif dibaca sebagai nomor sinyal.
# Yang dijaga: bentuk kode (spawner liar membuat bocor lagi besok) dan keadaan mesin (yatim yang
# ada sekarang adalah RAM yang sedang hilang).
if ! python3 "$HOME/.ai-station/tools/call_workers.py" --guard; then
  fail=1
fi

# 1) Buktikan tab benar-benar pindah ke tmux, bukan PTY milik daemon.
#    Ini satu-satunya perbedaan antara "restart menghapus sesi" dan tidak.
if command -v tmux >/dev/null 2>&1; then
  TABS=$(tmux -L irsofka list-sessions 2>/dev/null | grep -c '^station-' || true)
  if [ "${TABS:-0}" -ge 1 ]; then
    echo "  ✓ $TABS tab berjalan di tmux -L irsofka (bertahan dari restart daemon)"
  else
    echo "  ✗ tidak ada tab station-* di tmux -L irsofka — sesi masih PTY daemon"
    fail=1
  fi
fi

echo
echo "=== tab aktif pasca-deploy ==="
curl -s --max-time 20 http://127.0.0.1:8999/api/workspace | python3 -c "
import json,sys
d=json.load(sys.stdin)
for k,t in (d.get('tabs') or {}).items():
    s=t.get('session') or {}
    print(f\"  {k:<12} alive={t['alive']} cwd={t['live_cwd']} model_cmd={t['model_in_command'] or '-'} sesi={(s.get('session_id') or '-')[:8]}\")
" || echo "  (gagal membaca workspace)"

# Peta struktur ikut segar setiap deploy: berkas baru muncul sendiri di
# brain/memory/projects/ARCHITECTURE.md, tanpa ada yang perlu mengingatnya.
if [ -f "$HOME/.ai-station/bin/arch_map.sh" ]; then
  echo
  echo "=== peta struktur ==="
  bash "$HOME/.ai-station/bin/arch_map.sh" | sed 's/^/  /' || echo "  (peta gagal dibangkitkan — bukan penyebab deploy gagal)"
fi

if [ "$CLEAN_ORPHANS" = "1" ]; then
  echo
  echo "🧹 membersihkan jendela GUI yatim (proses tanpa --headless, induk PID 1) ..."
  for pid in $(pgrep -x irsofka-station-core || true); do
    args=$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)
    ppid=$(awk '{print $4}' "/proc/$pid/stat" 2>/dev/null || echo "")
    case "$args" in
      *--headless*) ;;
      *) if [ "$ppid" = "1" ]; then echo "   kill $pid ($args)"; kill -TERM "$pid"; fi ;;
    esac
  done
fi

if [ "$fail" = "1" ]; then
  echo
  echo "✗ Ada pemeriksaan yang gagal. JANGAN langsung rollback — rollback ke biner lama"
  echo "  justru menghapus persistensi tmux, sehingga sesi mati saat restart berikutnya."
  echo "  Baca dulu baris ✗ di atas dan perbaiki penyebabnya."
  echo "  Cadangan biner sebelumnya : $BIN_DST.bak-$STAMP"
  echo "  Rollback (opsi terakhir)  : install -m 0755 $BIN_DST.bak-$STAMP $BIN_DST && systemctl --user restart $SERVICE"
  exit 1
fi
echo
echo "✅ Deploy selesai. Sesi tab hidup di tmux, jadi restart daemon tidak lagi memutusnya."
echo "   Pulihkan konteks kapan saja dengan: ai-station recovery 40"
