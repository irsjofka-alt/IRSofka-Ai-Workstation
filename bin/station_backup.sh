#!/usr/bin/env bash
# Cadangkan memori jangka panjang workstation ke ~/runtime/backups.
#
#   ./station_backup.sh            -> buat satu set cadangan
#   ./station_backup.sh --prune    -> hanya pangkas retensi
#
# Yang dicadangkan:
#   1. basis data save-state (PostgreSQL primer; salinan file SQLite bila PG mati)
#   2. brain/  -> memori markdown, skills, rules, dokumen proyek
#   3. config/ -> profil tab, slots, dan db_local.json (berisi kredensial, arsip 600)
#
# Retensi: 24 titik per-jam + 30 titik harian. Cadangan lama tidak dihapus sebelum
# cadangan baru terbukti terbaca.
set -uo pipefail

RT="$HOME/runtime/backups"
STATION="$HOME/.ai-station"
NOW="$(date +%Y%m%d-%H%M%S)"
KEEP_HOURLY=24
KEEP_DAILY=30
mkdir -p "$RT/db" "$RT/brain" "$RT/config"
log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
fail() { log "❌ $*"; exit 1; }

DB_ENGINE=""
if pg_isready -h localhost -p 5432 -q 2>/dev/null; then DB_ENGINE="POSTGRESQL"; fi

# ---------- 1. basis data ----------
if [ "${1:-}" != "--prune" ]; then
  if [ "$DB_ENGINE" = "POSTGRESQL" ]; then
    DB_FILE="$RT/db/irsofka_ai_workstation-$NOW.sql.gz"
    # --no-owner --no-privileges: restore boleh ke database lain tanpa perang izin.
    if PGPASSWORD="$(python3 -c "
import sys,os; sys.path.insert(0,'$STATION/tools')
from db_state import PG_CONFIG; print(PG_CONFIG['password'])" 2>/dev/null)" \
       pg_dump -h localhost -p 5432 -U irsofka -d irsofka_ai_workstation \
             --no-owner --no-privileges 2>"$RT/db/.last-err" | gzip > "$DB_FILE"; then
      # Uji terbaca: gzip utuh, cukup besar, dan benar-benar memuat isi tabel.
      # CATATAN: JANGAN pakai `zcat | grep -q`. grep -q berhenti membaca, zcat kena
      # SIGPIPE, dan di bawah `set -o pipefail` pipeline bernilai GAGAL walau datanya ada.
      # Bug inilah yang membuat verifikasi cadangan pernah menolak dump yang sempurna.
      SAMPLE="$(zcat "$DB_FILE" 2>/dev/null | head -800)"
      if [ "$(stat -c %s "$DB_FILE")" -gt 500 ] && gzip -t "$DB_FILE" 2>/dev/null && \
         [[ "$SAMPLE" == *action_log* ]]; then
        log "✓ basis data: $(du -h "$DB_FILE" | cut -f1) (terverifikasi terbaca)"
      else
        rm -f "$DB_FILE"; fail "dump tidak terbaca — tidak dianggap cadangan"
      fi
    else
      fail "pg_dump gagal: $(tail -3 "$RT/db/.last-err")"
    fi
  else
    # PostgreSQL tumbang: amankan fallback SQLite apa adanya.
    if [ -f "$STATION/brain/workstation.db" ]; then
      cp -p "$STATION/brain/workstation.db" "$RT/db/workstation-$NOW.db" && \
        log "⚠ PostgreSQL mati — yang dicadangkan salinan SQLite"
    else
      fail "tidak ada PostgreSQL dan tidak ada workstation.db untuk dicadangkan"
    fi
  fi

  # ---------- 2 & 3. brain + config ----------
  BRAIN_FILE="$RT/brain/brain-$NOW.tar.gz"
  tar -czf "$BRAIN_FILE" -C "$HOME" \
      --exclude='.ai-station/brain/workstation.db-journal' \
      --exclude='.ai-station/brain/__pycache__' \
      .ai-station/brain .ai-station/config 2>/dev/null || true
  if [ -s "$BRAIN_FILE" ] && tar -tzf "$BRAIN_FILE" >/dev/null 2>&1; then
    chmod 600 "$BRAIN_FILE"   # berisi db_local.json
    log "✓ brain+config: $(du -h "$BRAIN_FILE" | cut -f1) (arsip 600, ada kredensial)"
  else
    rm -f "$BRAIN_FILE"; fail "arsip brain tidak valid"
  fi

  # ---------- indeks ----------
  printf '%s\t%s\tdb=%s\tbrain=%s\n' "$NOW" "$(date '+%Y-%m-%d %H:%M:%S')" \
    "${DB_ENGINE:-SQLITE}" "$(du -h "$BRAIN_FILE" 2>/dev/null | cut -f1)" >> "$RT/INDEX.txt"
fi

# ---------- retensi ----------
prune_dir() {
  local dir="$1" pat="$2" keep_h="$3" keep_d="$4"
  local files; mapfile -t files < <(find "$dir" -maxdepth 1 -type f -name "$pat" | sort -r)
  [ "${#files[@]}" -eq 0 ] && return 0
  local i=0 keep=()
  for f in "${files[@]}"; do
    i=$((i+1))
    if [ "$i" -le "$keep_h" ]; then keep+=("$f"); continue; fi
    # sisakan satu cadangan per hari sampai kuota harian habis
    local day; day="$(basename "$f" | grep -oE '[0-9]{8}' || true)"
    if [ -n "$day" ] && ! printf '%s\n' "${keep[@]}" | grep -q "$day"; then
      [ "${#keep[@]}" -lt $((keep_h + keep_d)) ] && keep+=("$f")
    fi
  done
  for f in "${files[@]}"; do
    printf '%s\n' "${keep[@]}" | grep -qx "$f" || { rm -f "$f"; log "  pangkas $(basename "$f")"; }
  done
}
prune_dir "$RT/db" 'irsofka_ai_workstation-*.sql.gz' $KEEP_HOURLY $KEEP_DAILY
prune_dir "$RT/db" 'workstation-*.db' $KEEP_HOURLY $KEEP_DAILY
prune_dir "$RT/brain" 'brain-*.tar.gz' $KEEP_HOURLY $KEEP_DAILY
rm -f "$RT/db/.last-err"

log "total cadangan: $(du -sh "$RT" 2>/dev/null | cut -f1) di $RT"
