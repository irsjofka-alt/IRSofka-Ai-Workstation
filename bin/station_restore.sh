#!/usr/bin/env bash
# Pulihkan memori workstation dari cadangan di ~/runtime/backups.
#
#   ./station_restore.sh --list
#   ./station_restore.sh --check              uji cadangan terbaru ke DB sementara (AMAN, default)
#   ./station_restore.sh --db <file> --into <dbname>
#   ./station_restore.sh --brain <file> --into <dir>
#   ./station_restore.sh --latest --live      pulihkan ke produksi (butuh konfirmasi ketik)
#
# `--check` ada karena cadangan yang belum pernah di-restore bukan cadangan — ia cuma harapan.
set -uo pipefail

RT="$HOME/runtime/backups"
STATION="$HOME/.ai-station"
DB_NAME="irsofka_ai_workstation"
PGUSER_="irsofka"
fail() { echo "❌ $*" >&2; exit 1; }
latest_db() { ls -1t "$RT"/db/irsofka_ai_workstation-*.sql.gz 2>/dev/null | head -1; }
latest_brain() { ls -1t "$RT"/brain/brain-*.tar.gz 2>/dev/null | head -1; }
pgpass() { python3 -c "
import sys; sys.path.insert(0,'$STATION/tools')
from db_state import PG_CONFIG; print(PG_CONFIG['password'])" 2>/dev/null; }

MODE=""; SRC_DB=""; SRC_BRAIN=""; INTO=""; LIVE=0
while [ $# -gt 0 ]; do
  case "$1" in
    --list)   echo "=== cadangan basis data ==="; ls -lht "$RT"/db/ 2>/dev/null | tail -n +2 | sed 's/^/  /'
              echo "=== cadangan brain+config ==="; ls -lht "$RT"/brain/ 2>/dev/null | tail -n +2 | sed 's/^/  /'
              echo "=== indeks 10 terakhir ==="; tail -10 "$RT/INDEX.txt" 2>/dev/null | sed 's/^/  /'; exit 0 ;;
    --check)  MODE=check ;;
    --db)     SRC_DB="${2:-}"; shift ;;
    --brain)  SRC_BRAIN="${2:-}"; shift ;;
    --into)   INTO="${2:-}"; shift ;;
    --live)   LIVE=1 ;;
    --latest) MODE="${MODE:-restore}"; ;;
    *) fail "argumen tak dikenal: $1" ;;
  esac
  shift
done

if [ "$MODE" = "check" ] || { [ "$MODE" = "restore" ] && [ "$LIVE" = "0" ]; }; then
  SRC_DB="${SRC_DB:-$(latest_db)}"; [ -n "$SRC_DB" ] || fail "tidak ada cadangan di $RT/db"
  TESTDB="station_restore_test"
  echo "=== UJI RESTORE (tidak menyentuh produksi) ==="
  echo "  sumber : $(basename "$SRC_DB") ($(du -h "$SRC_DB" | cut -f1))"
  export PGPASSWORD="$(pgpass)"
  dropdb --if-exists -h localhost -U "$PGUSER_" "$TESTDB" 2>/dev/null
  createdb -h localhost -U "$PGUSER_" "$TESTDB" || fail "gagal membuat DB uji $TESTDB"
  if zcat "$SRC_DB" | psql -q -h localhost -U "$PGUSER_" -d "$TESTDB" >/dev/null 2>&1; then
    echo "  ✓ dump dimuat ke DB '$TESTDB'"
    for t in action_log world_memory quest_tasks session_turns incident_log skills_inventory; do
      n=$(psql -At -h localhost -U "$PGUSER_" -d "$TESTDB" -c "select count(*) from $t" 2>/dev/null || echo "-")
      printf '    %-18s %s baris\n' "$t" "$n"
    done
    echo "  ✓ restore terverifikasi. DB uji dibuang lagi."
    dropdb --if-exists -h localhost -U "$PGUSER_" "$TESTDB" 2>/dev/null
  else
    dropdb --if-exists -h localhost -U "$PGUSER_" "$TESTDB" 2>/dev/null
    fail "dump TIDAK bisa dimuat — anggap cadangan ini rusak"
  fi
  exit 0
fi

# ---- pemulihan ke produksi: gerbang konfirmasi, bukan sekali tekan ----
[ "$LIVE" = "1" ] || fail "tidak ada yang dilakukan (pakai --live untuk menimpa produksi)"
echo "⚠️  Ini akan MENIMPA memori produksi:"
[ -n "$SRC_DB" ] && echo "   db    : $(basename "$SRC_DB")"
[ -n "$SRC_BRAIN" ] && echo "   brain : $(basename "$SRC_BRAIN")"
read -r -p "   ketik PULIHKAN untuk lanjut: " ok
[ "$ok" = "PULIHKAN" ] || { echo " dibatalkan."; exit 0; }

if [ -n "$SRC_DB" ]; then
  export PGPASSWORD="$(pgpass)"
  systemctl --user stop irsofka-ai-workstation.service 2>/dev/null
  dropdb --if-exists -h localhost -U "$PGUSER_" "$DB_NAME"
  createdb -h localhost -U "$PGUSER_" "$DB_NAME"
  zcat "$SRC_DB" | psql -q -h localhost -U "$PGUSER_" -d "$DB_NAME" || fail "restore db gagal"
  systemctl --user start irsofka-ai-workstation.service 2>/dev/null
  echo "  ✓ basis data dipulihkan"
fi
if [ -n "$SRC_BRAIN" ]; then
  tar -xzf "$SRC_BRAIN" -C "$HOME" && echo "  ✓ brain+config dipulihkan"
fi
echo "Selesai."
