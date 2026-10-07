#!/usr/bin/env bash
# Pasang unit systemd milik workstation ke ~/.config/systemd/user lalu aktifkan.
#
#   install_units.sh              pasang yang belum ada, jangan menimpa
#   install_units.sh --dry-run    lihat saja apa yang akan dilakukan
#   install_units.sh --force      timpa unit yang sudah ada (mis. habis ubah di repo)
#
# Kenapa perlu: unit inilah yang membuat tab AI bertahan dari restart daemon
# (irsofka-tabs.service menaungi server tmux di LUAR cgroup daemon). Tanpa unit ini,
# orang yang mengkloning repo akan mengira daemonnya "suka membunuh sesi".
set -euo pipefail

STATION="${STATION_DIR:-$HOME/.ai-station}"
SRC="$STATION/systemd"
DEST="${SYSTEMD_DIR_OVERRIDE:-$HOME/.config/systemd/user}"
UNITS=(irsofka-ai-workstation.service irsofka-tabs.service irsofka-action-log.service
       irsofka-memory-backup.service irsofka-memory-backup.timer
       irsofka-autopilot.service irsofka-autopilot.timer)
ENABLE=(irsofka-ai-workstation.service irsofka-tabs.service irsofka-action-log.service
        irsofka-memory-backup.timer)
# irsofka-autopilot.timer sengaja tidak ikut ENABLE. Sakelar yang menyalakan loop ini harus
# dinyalakan oleh orang yang sadar dan bisa melihat lognya, bukan oleh installer yang berjalan
# di tengah kerja mesin. Menyalakannya: systemctl --user enable --now irsofka-autopilot.timer

DRY=0; FORCE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    *) echo "argumen tidak dikenal: $arg" >&2; exit 2 ;;
  esac
done

command -v systemctl >/dev/null || { echo "systemctl tidak ada — unit ini butuh systemd." >&2; exit 1; }
[ -d "$SRC" ] || { echo "folder unit tidak ada: $SRC" >&2; exit 1; }
[ "$DRY" = 1 ] || mkdir -p "$DEST"

copied=0; skipped=0
for u in "${UNITS[@]}"; do
  [ -f "$SRC/$u" ] || { echo "  HILANG  $SRC/$u tidak ada di repo"; skipped=$((skipped+1)); continue; }
  if [ -e "$DEST/$u" ] && [ "$FORCE" = 0 ]; then
    if cmp -s "$SRC/$u" "$DEST/$u"; then
      echo "  sama    $u  (tidak perlu disalin)"
    else
      echo "  BIARKAN  $u sudah ada dan BERBEDA. Periksa isinya dulu, lalu ulangi --force."
      diff -u "$DEST/$u" "$SRC/$u" | head -20 || true
      skipped=$((skipped+1))
    fi
    continue
  fi
  echo "  salin   $u"
  copied=$((copied+1))
  [ "$DRY" = 1 ] || install -m 644 "$SRC/$u" "$DEST/$u"
done

if [ "$DRY" = 1 ]; then
  echo ""; echo "--dry-run: tidak ada yang ditulis ($copied akan disalin, $skipped dilewati)"; exit 0
fi

systemctl --user daemon-reload
for u in "${ENABLE[@]}"; do
  [ -e "$DEST/$u" ] || { echo "  lewati enable $u (tidak terpasang)"; continue; }
  systemctl --user enable --now "$u" && echo "  aktif   $u"
done

echo ""
echo "=== status ==="
for u in "${UNITS[@]}"; do
  printf "  %-34s %s\n" "$u" "$(systemctl --user is-active "$u" 2>/dev/null || echo inactive)"
done
tmux -L irsofka ls 2>/dev/null | sed 's/^/  tmux: /' || echo "  (tmux server irsofka belum jalan — start irsofka-tabs.service)"
echo ""
echo "Catatan: timer cadangan baru berguna setelah PostgreSQL siap; lihat INSTALL.md."
