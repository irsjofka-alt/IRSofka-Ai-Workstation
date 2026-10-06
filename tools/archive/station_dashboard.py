#!/usr/bin/env python3
"""
AI Workstation Lightweight Core Dashboard & OS Companion Server
Berjalan secara native dengan Python 3 Standard Library (Zero external dependencies).
"""

import os
import sys
import json
import subprocess
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

HOST = "127.0.0.1"
PORT = 8999

AI_STATION = Path.home() / ".ai-station"
BRAIN_DIR = AI_STATION / "brain"
CONFIG_FILE = AI_STATION / "config" / "slots.yaml"


def get_system_stats():
    stats = {
        "cpu_ram": "N/A",
        "gpu": "N/A",
        "disk": "N/A",
        "active_workspace": "Belum terhubung",
        "skills_count": 0,
        "rules_count": 0,
        "facts_count": 0
    }

    # GPU
    try:
        gpu_out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, text=True
        ).strip().split(",")
        if len(gpu_out) >= 4:
            stats["gpu"] = {
                "name": gpu_out[0].strip(),
                "used_mb": int(gpu_out[1].strip()),
                "total_mb": int(gpu_out[2].strip()),
                "load_percent": int(gpu_out[3].strip())
            }
    except Exception:
        pass

    # Disk
    try:
        disk_out = subprocess.check_output(["df", "-h", "/"], text=True).splitlines()
        if len(disk_out) > 1:
            parts = disk_out[1].split()
            stats["disk"] = {"total": parts[1], "used": parts[2], "avail": parts[3], "percent": parts[4]}
    except Exception:
        pass

    # RAM
    try:
        mem_out = subprocess.check_output(["free", "-m"], text=True).splitlines()
        if len(mem_out) > 1:
            mem_parts = mem_out[1].split()
            stats["ram"] = {"total_mb": int(mem_parts[1]), "used_mb": int(mem_parts[2]), "avail_mb": int(mem_parts[6])}
    except Exception:
        pass

    # Brain stats
    stats["skills_count"] = len([s for s in (BRAIN_DIR / "skills").rglob("*.md") if s.name != "SKILLS.md"])
    stats["rules_count"] = len(list((BRAIN_DIR / "rules").glob("*.md")))
    stats["memories_count"] = len([m for m in (BRAIN_DIR / "memory").rglob("*.md") if m.name != "MEMORY.md"])

    facts_path = BRAIN_DIR / "memory" / "global_facts.jsonl"
    if facts_path.exists():
        with open(facts_path) as f:
            stats["facts_count"] = sum(1 for line in f if line.strip())

    ws_link = BRAIN_DIR / "workspaces" / "current_project"
    if ws_link.exists():
        stats["active_workspace"] = str(ws_link.resolve())

    return stats


def execute_action(action_name):
    if action_name == "open_youtube":
        subprocess.Popen(["xdg-open", "https://youtube.com"])
        return {"status": "ok", "message": "Membuka YouTube di browser default"}
    elif action_name == "notify_test":
        subprocess.Popen(["notify-send", "-a", "AI Workstation", "Halo Bro!", "Sistem Workstation AI kamu aktif dan berjalan lancar!"])
        return {"status": "ok", "message": "Notifikasi test terkirim"}
    elif action_name == "vol_up":
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"])
        return {"status": "ok", "message": "Volume naik +10%"}
    elif action_name == "vol_down":
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"])
        return {"status": "ok", "message": "Volume turun -10%"}
    return {"status": "error", "message": f"Aksi '{action_name}' tidak dikenal"}


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AI Workstation Hub - Pop!_OS</title>
    <style>
        :root {
            --bg: #0f111a;
            --card-bg: #1a1c29;
            --accent: #48b0d5;
            --accent-glow: rgba(72, 176, 213, 0.3);
            --pop-orange: #f28b25;
            --text-main: #f0f3f6;
            --text-dim: #9aa5b1;
            --border: #2a2e45;
        }
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: 'Inter', -apple-system, sans-serif; }
        body { background: var(--bg); color: var(--text-main); padding: 24px; }
        header { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 24px; }
        .logo { font-size: 1.4rem; font-weight: 700; color: var(--pop-orange); display: flex; align-items: center; gap: 8px; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; margin-bottom: 24px; }
        .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 18px; box-shadow: 0 4px 12px rgba(0,0,0,0.2); }
        .card h3 { font-size: 0.95rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 12px; }
        .stat-value { font-size: 1.8rem; font-weight: 700; color: var(--accent); }
        .stat-sub { font-size: 0.85rem; color: var(--text-dim); margin-top: 4px; }
        .badge { display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600; background: var(--accent-glow); color: var(--accent); }
        .btn { background: #24283b; color: var(--text-main); border: 1px solid var(--border); padding: 10px 14px; border-radius: 8px; cursor: pointer; transition: all 0.2s; font-size: 0.85rem; }
        .btn:hover { background: var(--accent); color: #000; }
        .btn-action { margin-right: 8px; margin-bottom: 8px; }
        .active-ws { font-family: monospace; font-size: 0.85rem; word-break: break-all; background: #12141f; padding: 10px; border-radius: 8px; border: 1px dashed var(--border); }
        .team-pill { background: #1e2238; border: 1px solid var(--border); padding: 12px; border-radius: 8px; margin-bottom: 10px; }
        .team-title { font-weight: 600; color: #fff; font-size: 0.95rem; }
        .team-desc { font-size: 0.8rem; color: var(--text-dim); margin-top: 4px; }
    </style>
</head>
<body>
    <header>
        <div class="logo">
            <span>🚀</span> AI Workstation Core (Pop!_OS Wayland)
        </div>
        <div>
            <span class="badge">SESSION: COSMIC WAYLAND</span>
            <span class="badge" style="background: rgba(242, 139, 37, 0.2); color: var(--pop-orange);">RTX 3060 12GB</span>
        </div>
    </header>

    <div class="grid">
        <div class="card">
            <h3>GPU Telemetry</h3>
            <div id="gpu-val" class="stat-value">Detecting...</div>
            <div id="gpu-sub" class="stat-sub">VRAM Load</div>
        </div>
        <div class="card">
            <h3>RAM Telemetry</h3>
            <div id="ram-val" class="stat-value">Detecting...</div>
            <div id="ram-sub" class="stat-sub">System Memory</div>
        </div>
        <div class="card">
            <h3>Brain Knowledge Base</h3>
            <div id="brain-val" class="stat-value">3 Skills / 2 Rules</div>
            <div id="brain-sub" class="stat-sub">Centralized in ~/.ai-station/brain</div>
        </div>
        <div class="card">
            <h3>Quota & Accounts</h3>
            <div class="stat-value" style="font-size: 1.2rem; color: #10b981;">● Active & Ready</div>
            <div class="stat-sub">Antigravity CLI / Qoder CLI / Gemini</div>
        </div>
    </div>

    <div class="grid">
        <div class="card" style="grid-column: span 2;">
            <h3>Active Workspace</h3>
            <div id="active-ws" class="active-ws">Memuat lokasi project...</div>
            <p style="margin-top: 10px; font-size: 0.8rem; color: var(--text-dim);">
                Semua CLI (Antigravity & Qoder) secara otomatis menggunakan file <code>.ai_brain_link.md</code> di folder di atas untuk membaca otak dan aturan yang sama.
            </p>
        </div>
        <div class="card">
            <h3>Linux Virtual Assistant Actions</h3>
            <button class="btn btn-action" onclick="runAction('open_youtube')">📺 Buka YouTube</button>
            <button class="btn btn-action" onclick="runAction('notify_test')">🔔 Kirim Test Notif</button>
            <button class="btn btn-action" onclick="runAction('vol_up')">🔊 Vol +10%</button>
            <button class="btn btn-action" onclick="runAction('vol_down')">🔉 Vol -10%</button>
            <div id="action-status" style="font-size: 0.8rem; color: var(--pop-orange); margin-top: 8px;"></div>
        </div>
    </div>

    <div class="card">
        <h3>Registered Capability & Team Slots</h3>
        <div class="team-pill">
            <div class="team-title">⚡ Team Programmers (Active)</div>
            <div class="team-desc">Lead Architect (Gemini 3.1 Pro) ➔ Code Implementer (Antigravity CLI) ➔ QA Auditor (Qoder CLI)</div>
        </div>
        <div class="team-pill">
            <div class="team-title">👁️ Visual & Desktop Automation Squad</div>
            <div class="team-desc">Observer (Gemini 3.8 Flash) ➔ Actuator (ydotool Wayland) untuk simulasi klik & ketik</div>
        </div>
        <div class="team-pill">
            <div class="team-title">🐧 Pop!_OS Desktop Companion</div>
            <div class="team-desc">Perintah sistem cepat, kontrol media player, alarm, dan shortcut via Linux API</div>
        </div>
        <div class="team-pill">
            <div class="team-title">📊 Deep Analysis & Long Context Squad</div>
            <div class="team-desc">Membaca dokumen raksasa hingga 1M tokens via Qwen 3.8 Flash / Gemini Flash</div>
        </div>
    </div>

    <script>
        async function fetchStats() {
            try {
                const res = await fetch('/api/stats');
                const data = await res.json();
                if (data.gpu && typeof data.gpu === 'object') {
                    document.getElementById('gpu-val').innerText = `${data.gpu.used_mb} / ${data.gpu.total_mb} MB`;
                    document.getElementById('gpu-sub').innerText = `Load: ${data.gpu.load_percent}% (${data.gpu.name})`;
                }
                if (data.ram) {
                    document.getElementById('ram-val').innerText = `${(data.ram.used_mb / 1024).toFixed(1)} / ${(data.ram.total_mb / 1024).toFixed(1)} GB`;
                }
                document.getElementById('brain-val').innerText = `${data.skills_count} Skills / ${data.rules_count} Rules`;
                document.getElementById('brain-sub').innerText = `${data.facts_count} Long-Term Memory Facts`;
                document.getElementById('active-ws').innerText = data.active_workspace;
            } catch (err) {
                console.error(err);
            }
        }

        async function runAction(name) {
            const st = document.getElementById('action-status');
            st.innerText = 'Menjalankan aksi...';
            try {
                const res = await fetch('/api/action', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({action: name})
                });
                const d = await res.json();
                st.innerText = d.message;
            } catch(e) {
                st.innerText = 'Gagal: ' + e;
            }
        }

        fetchStats();
        setInterval(fetchStats, 3000);
    </script>
</body>
</html>
"""


class StationHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_TEMPLATE.encode("utf-8"))
        elif self.path == "/api/stats":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            stats = get_system_stats()
            self.wfile.write(json.dumps(stats).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path == "/api/action":
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            try:
                payload = json.loads(body.decode("utf-8"))
                result = execute_action(payload.get("action"))
            except Exception as e:
                result = {"status": "error", "message": str(e)}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Mute logging to keep terminal clean
        return


def run_server():
    server = HTTPServer((HOST, PORT), StationHandler)
    print(f"🚀 AI Workstation Core Dashboard running at: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server...")
        server.server_close()


if __name__ == "__main__":
    run_server()
