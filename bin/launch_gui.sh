#!/usr/bin/env bash
# Irsofka AI Workstation Dedicated App Launcher (100% Native Rust Window)

AI_DIR="$HOME/.ai-station"

# 1. Pastikan background server aktif via systemd user service jika belum
if ! curl -s --max-time 1 http://127.0.0.1:8999/api/stats >/dev/null 2>&1; then
    if systemctl --user is-active --quiet irsofka-ai-workstation.service; then
        systemctl --user restart irsofka-ai-workstation.service
    else
        systemctl --user start irsofka-ai-workstation.service 2>/dev/null
    fi
    # Tunggu sampai port 8999 siap
    for i in {1..20}; do
        if curl -s --max-time 1 http://127.0.0.1:8999/api/stats >/dev/null 2>&1; then
            break
        fi
        sleep 0.1
    done
fi

# 2. Buka Native Rust Desktop Window (Tao + Wry + WebKitGTK) - Bebas dari Web Browser!
exec "$AI_DIR/bin/irsofka-station-core" "$@"
