#!/usr/bin/env bash
# Satu tick Autopilot (F10.3): membaca tangga tangan, menjalankan gerbang, dan berhenti sendiri
# kalau sakelarnya OFF atau jendelanya sudah lewat.
#
# Dibungkus skrip, bukan dipanggil sebagai `python3 ...` langsung dari unit, karena systemd user
# tidak mewarisi PATH shell dan lingkungan `~/.profile`: `cd` ke akar repositori adalah satu-satunya
# cara perintah gerbang di ledger (`python3 tools/work_order.py selftest`, `bash bin/deploy_engine.sh`)
# menunjuk berkas yang benar. Tick itu sendiri tidak menulis apa pun ke $HOME di luar logs/.
set -uo pipefail

STATION="${STATION_DIR:-$HOME/.ai-station}"
cd "$STATION" || { echo "autopilot: tidak bisa masuk $STATION" >&2; exit 1; }

PYTHON="$(command -v python3 || true)"
[ -n "$PYTHON" ] || { echo "autopilot: python3 tidak ditemukan di PATH systemd" >&2; exit 1; }

exec "$PYTHON" tools/drainer.py tick
