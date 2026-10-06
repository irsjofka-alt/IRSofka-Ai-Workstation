#!/usr/bin/env python3
"""
Irsofka AI Workstation Core Server & Live Terminal Studio
Architecture: Python/PTY Daemon -> SQLite / PostgreSQL Game Save State -> Live xterm.js
Features:
- Main Stage: Live Streaming Terminal (Zero timeout, real-time Thinking & Tool Execution)
- Auto-Approve Permissions Toggle (--permission-mode bypass_permissions / --dangerously-skip-permissions)
- Full Controls: Model (Qwen 1M, Gemini, Claude, DeepSeek), Reasoning Effort, Context Window
- SQL Save State: ~/.ai-station/brain/workstation.db (Player profile, skills, tasks, session turns)
"""

import os
import sys
import json
import time
import fcntl
import struct
import select
import termios
import subprocess
import threading
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
import urllib.parse

# Import SQLite State Engine
try:
    sys.path.append(str(Path(__file__).parent))
    import db_state
    db_state.init_db()
except Exception:
    pass

HOST = "127.0.0.1"
PORT = 8999

AI_STATION = Path.home() / ".ai-station"
BRAIN_DIR = AI_STATION / "brain"
LOGS_DIR = AI_STATION / "logs"
SCREENSHOT_PATH = LOGS_DIR / "current_screen.png"
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# --- PTY Engine ---
class PTYSession:
    def __init__(self, name, init_command=""):
        self.name = name
        self.master_fd, self.slave_fd = os.openpty()
        self.output_buffer = bytearray()
        self.lock = threading.Lock()

        flags = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.set_winsize(32, 120)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["AI_WORKSTATION_BRAIN"] = str(BRAIN_DIR)
        env["PATH"] = f"{Path.home()}/.gemini/antigravity/bin:{Path.home()}/.local/bin:{env.get('PATH', '')}"

        self.proc = subprocess.Popen(
            ["/bin/bash", "-i"],
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
            env=env,
            cwd=str(Path.home())
        )

        if init_command:
            os.write(self.master_fd, init_command.encode("utf-8") + b"\n")

        self.running = True
        self.thread = threading.Thread(target=self._read_loop, daemon=True)
        self.thread.start()

    def set_winsize(self, rows, cols):
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except Exception:
            pass

    def _read_loop(self):
        while self.running:
            try:
                r, _, _ = select.select([self.master_fd], [], [], 0.03)
                if r:
                    chunk = os.read(self.master_fd, 4096)
                    if chunk:
                        with self.lock:
                            self.output_buffer.extend(chunk)
                            if len(self.output_buffer) > 500000:
                                self.output_buffer = self.output_buffer[-300000:]
            except Exception:
                pass
            time.sleep(0.005)

    def write(self, data: bytes):
        try:
            os.write(self.master_fd, data)
        except Exception:
            pass

    def read_pending(self) -> bytes:
        with self.lock:
            out = bytes(self.output_buffer)
            self.output_buffer.clear()
            return out


sessions = {
    "qoder": PTYSession("qoder", "echo -e '\\033[1;33m[Qoder CLI Studio Ready - Qwen 3.8 Flash 1M Context (0 Points)]\\033[0m'"),
    "antigravity": PTYSession("antigravity", "echo -e '\\033[1;36m[Antigravity CLI Studio Ready - Gemini / Claude Models Active]\\033[0m'"),
    "shell": PTYSession("shell", "echo -e '\\033[1;32m[Pop!_OS COSMIC System Shell Active]\\033[0m'")
}


def get_telemetry():
    agy_connected = False
    agy_version = "N/A"
    try:
        res = subprocess.run(["agy", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
        if res.returncode == 0:
            agy_connected = True
            agy_version = res.stdout.strip()
    except Exception:
        pass

    qoder_connected = False
    qoder_version = "N/A"
    try:
        res = subprocess.run(["qoder", "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=2)
        if res.returncode == 0:
            qoder_connected = True
            qoder_version = res.stdout.strip()
    except Exception:
        pass

    stats = {
        "user_name": os.environ.get("STATION_OWNER_NAME", "Owner"),
        "antigravity_cli": {"connected": agy_connected, "version": agy_version},
        "qoder_cli": {"connected": qoder_connected, "version": qoder_version},
        "qoder_status": "Logged In (Qwen 1M: 0 Points)" if qoder_connected else "Not Logged In",
        "antigravity_tier": "Pro / Unlimited",
        "gpu": {"name": "NVIDIA RTX 3060", "used_mb": 0, "total_mb": 12288, "load": 0},
        "ram": {"used_gb": 0, "total_gb": 32},
        "disk": {"free": "N/A", "total": "N/A"},
        "active_workspace": "None",
        "skills_count": 4,
        "rules_count": 2,
        "memories_count": 4
    }

    try:
        gpu_out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu", "--format=csv,noheader,nounits"],
            text=True, timeout=2
        ).strip().split(",")
        if len(gpu_out) >= 4:
            stats["gpu"] = {
                "name": gpu_out[0].strip(),
                "used_mb": int(gpu_out[1].strip()),
                "total_mb": int(gpu_out[2].strip()),
                "load": int(gpu_out[3].strip())
            }
    except Exception:
        pass

    try:
        mem_out = subprocess.check_output(["free", "-m"], text=True, timeout=2).splitlines()
        if len(mem_out) > 1:
            p = mem_out[1].split()
            stats["ram"] = {"used_gb": round(int(p[2]) / 1024, 1), "total_gb": round(int(p[1]) / 1024, 1)}
    except Exception:
        pass

    try:
        d = subprocess.check_output(["df", "-h", "/"], text=True, timeout=2).splitlines()
        if len(d) > 1:
            dp = d[1].split()
            stats["disk"] = {"free": dp[3], "total": dp[1]}
    except Exception:
        pass

    try:
        import db_state
        db_s = db_state.get_stats_summary()
        stats["skills_count"] = db_s.get("skills_count", 4)
        stats["memories_count"] = db_s.get("memories_count", 0)
        stats["db_engine"] = db_s.get("engine", "SQLITE")
        stats["total_quests"] = db_s.get("total_quests", 0)
    except Exception:
        stats["db_engine"] = "SQLITE"
        stats["total_quests"] = 0

    ws_link = BRAIN_DIR / "workspaces" / "current_project"
    if ws_link.exists():
        stats["active_workspace"] = str(ws_link.resolve())
    else:
        stats["active_workspace"] = "/home/irsofka/Documents/antigravity/splendid-hawking"

    return stats


HTML_DESKTOP_GUI = r"""<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Irsofka AI Workstation - Live Studio</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/css/xterm.css" />
    <script src="https://cdn.jsdelivr.net/npm/@xterm/xterm@5.5.0/lib/xterm.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/@xterm/addon-fit@0.10.0/lib/addon-fit.js"></script>

    <style>
        :root {
            --bg-deep: #07090e;
            --bg-surface: #0e121d;
            --bg-card: #141926;
            --bg-elevated: #1a2133;
            --border-subtle: rgba(255, 255, 255, 0.08);
            --border-focus: rgba(56, 189, 248, 0.4);
            --accent-cyan: #38bdf8;
            --accent-glow: rgba(56, 189, 248, 0.25);
            --accent-amber: #f59e0b;
            --accent-emerald: #10b981;
            --accent-violet: #818cf8;
            --accent-rose: #f43f5e;
            --text-main: #f1f5f9;
            --text-muted: #94a3b8;
            --text-dim: #64748b;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background-color: var(--bg-deep);
            color: var(--text-main);
            font-family: 'Inter', -apple-system, sans-serif;
            height: 100vh;
            display: flex;
            flex-direction: column;
            overflow: hidden;
            user-select: text;
            -webkit-user-select: text;
        }

        /* Topbar Header */
        header {
            height: 52px;
            background: var(--bg-surface);
            border-bottom: 1px solid var(--border-subtle);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 16px;
            z-index: 20;
        }
        .brand {
            display: flex;
            align-items: center;
            gap: 10px;
            font-weight: 700;
            font-size: 0.92rem;
        }
        .brand-badge {
            width: 28px;
            height: 28px;
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-violet));
            border-radius: 8px;
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 14px;
            box-shadow: 0 0 14px var(--accent-glow);
        }
        .header-actions {
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .pill {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            padding: 4px 10px;
            border-radius: 6px;
            font-size: 0.72rem;
            color: var(--text-muted);
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .pill b { color: var(--text-main); font-weight: 600; }
        .refresh-btn {
            background: var(--bg-elevated);
            border: 1px solid var(--border-subtle);
            color: var(--accent-cyan);
            padding: 5px 12px;
            border-radius: 6px;
            font-size: 0.75rem;
            cursor: pointer;
            transition: all 0.2s;
            font-weight: 500;
        }
        .refresh-btn:hover {
            border-color: var(--accent-cyan);
            background: rgba(56, 189, 248, 0.15);
        }

        /* Main Workspace Layout */
        .workspace {
            flex: 1;
            display: grid;
            grid-template-columns: 290px 1fr 280px;
            height: calc(100vh - 52px);
            overflow: hidden;
        }

        /* Left Rail: Controls & Parameters */
        .sidebar-left {
            background: var(--bg-surface);
            border-right: 1px solid var(--border-subtle);
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            overflow-y: auto;
        }
        .section-label {
            font-size: 0.7rem;
            text-transform: uppercase;
            letter-spacing: 0.6px;
            color: var(--text-muted);
            font-weight: 700;
            margin-bottom: 6px;
        }
        .select-input {
            width: 100%;
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            color: var(--text-main);
            padding: 8px 10px;
            border-radius: 6px;
            font-size: 0.8rem;
            outline: none;
            margin-bottom: 10px;
            cursor: pointer;
        }
        .select-input:focus { border-color: var(--accent-cyan); }

        /* Auto-Approve Toggle Card */
        .toggle-card {
            background: rgba(16, 185, 129, 0.08);
            border: 1px solid rgba(16, 185, 129, 0.3);
            border-radius: 8px;
            padding: 10px 12px;
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 10px;
            cursor: pointer;
        }
        .toggle-card.off {
            background: var(--bg-card);
            border-color: var(--border-subtle);
        }
        .toggle-title {
            font-size: 0.78rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .switch-indicator {
            font-size: 0.7rem;
            font-weight: 700;
            color: var(--accent-emerald);
            background: rgba(16, 185, 129, 0.2);
            padding: 2px 8px;
            border-radius: 10px;
        }
        .toggle-card.off .switch-indicator {
            color: var(--text-dim);
            background: rgba(255, 255, 255, 0.05);
        }

        /* Center Stage: Studio Live Terminal */
        .center-stage {
            display: flex;
            flex-direction: column;
            background: #05070c;
            height: 100%;
            overflow: hidden;
            position: relative;
        }

        /* Terminal Top Bar (Tabs & Status) */
        .term-header {
            height: 42px;
            background: var(--bg-surface);
            border-bottom: 1px solid var(--border-subtle);
            display: flex;
            align-items: center;
            justify-content: space-between;
            padding: 0 12px;
        }
        .term-tabs {
            display: flex;
            gap: 4px;
        }
        .term-tab {
            padding: 6px 14px;
            border-radius: 6px 6px 0 0;
            font-size: 0.76rem;
            font-weight: 600;
            cursor: pointer;
            color: var(--text-muted);
            background: transparent;
            border: 1px solid transparent;
            border-bottom: none;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: all 0.15s;
        }
        .term-tab:hover { color: var(--text-main); }
        .term-tab.active {
            color: var(--text-main);
            background: #05070c;
            border-color: var(--border-subtle);
        }
        .term-tab.qoder.active { border-top: 2px solid var(--accent-amber); }
        .term-tab.antigravity.active { border-top: 2px solid var(--accent-cyan); }
        .term-tab.shell.active { border-top: 2px solid var(--accent-emerald); }

        .term-actions {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .term-btn {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            color: var(--text-muted);
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.7rem;
            cursor: pointer;
            transition: all 0.15s;
        }
        .term-btn:hover { color: var(--text-main); border-color: var(--accent-cyan); }

        /* The Live Terminal Viewport */
        .terminal-container {
            flex: 1;
            width: 100%;
            padding: 8px;
            background: #05070c;
            overflow: hidden;
        }

        /* Action Chips */
        .action-chips {
            padding: 6px 14px;
            display: flex;
            gap: 8px;
            overflow-x: auto;
            background: rgba(14, 18, 29, 0.85);
            border-top: 1px solid var(--border-subtle);
        }
        .chip {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            color: var(--text-muted);
            padding: 4px 10px;
            border-radius: 14px;
            font-size: 0.72rem;
            cursor: pointer;
            white-space: nowrap;
            transition: all 0.2s;
        }
        .chip:hover { border-color: var(--accent-cyan); color: var(--text-main); background: rgba(56, 189, 248, 0.1); }

        /* Bottom Prompt Bar */
        .prompt-bar {
            height: 56px;
            background: var(--bg-surface);
            border-top: 1px solid var(--border-subtle);
            display: flex;
            align-items: center;
            padding: 0 14px;
            gap: 10px;
        }
        .prompt-input {
            flex: 1;
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            color: var(--text-main);
            padding: 10px 14px;
            font-size: 0.85rem;
            outline: none;
            font-family: inherit;
        }
        .prompt-input:focus { border-color: var(--accent-cyan); }
        .btn-send {
            background: linear-gradient(135deg, var(--accent-cyan), var(--accent-violet));
            color: #000;
            font-weight: 700;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-size: 0.85rem;
            display: flex;
            align-items: center;
            gap: 6px;
            transition: opacity 0.2s;
        }
        .btn-send:hover { opacity: 0.9; }

        /* Right Rail: Inspector */
        .sidebar-right {
            background: var(--bg-surface);
            border-left: 1px solid var(--border-subtle);
            padding: 14px;
            display: flex;
            flex-direction: column;
            gap: 14px;
            overflow-y: auto;
        }
        .card-aux {
            background: var(--bg-card);
            border: 1px solid var(--border-subtle);
            border-radius: 8px;
            padding: 12px;
        }
        .card-aux h4 {
            font-size: 0.72rem;
            color: var(--text-muted);
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
            display: flex;
            justify-content: space-between;
        }
        .screen-preview {
            width: 100%;
            height: 125px;
            background: #0b0d14;
            border-radius: 6px;
            border: 1px dashed var(--border-subtle);
            display: flex;
            align-items: center;
            justify-content: center;
            overflow: hidden;
        }
        .screen-preview img { width: 100%; height: 100%; object-fit: contain; }
        .quick-btn {
            background: var(--bg-elevated);
            color: var(--text-main);
            border: 1px solid var(--border-subtle);
            padding: 6px 10px;
            border-radius: 6px;
            font-size: 0.74rem;
            cursor: pointer;
            width: 100%;
            margin-bottom: 6px;
            transition: all 0.15s;
            text-align: left;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .quick-btn:hover { border-color: var(--accent-cyan); background: rgba(56, 189, 248, 0.1); }
    </style>
</head>
<body>
    <!-- Main Header -->
    <header>
        <div class="brand">
            <div class="brand-badge">⚛</div>
            <span>IRSOFKA AI WORKSTATION</span>
            <span class="pill" style="font-size: 0.68rem;">
                <span style="width: 7px; height: 7px; border-radius: 50%; background: var(--accent-emerald); display: inline-block;"></span>
                COSMIC Wayland
            </span>
        </div>

        <div class="header-actions">
            <div class="pill" style="border-color: rgba(245, 158, 11, 0.4);">
                <span style="width: 7px; height: 7px; border-radius: 50%; background: var(--accent-amber); display: inline-block;"></span>
                <span>QODER CLI</span> <b id="qoder-cli-badge" style="color: var(--accent-amber);">v1.1.65</b>
            </div>
            <div class="pill" style="border-color: rgba(56, 189, 248, 0.4);">
                <span style="width: 7px; height: 7px; border-radius: 50%; background: var(--accent-cyan); display: inline-block;"></span>
                <span>AGY CLI</span> <b id="agy-cli-badge" style="color: var(--accent-cyan);">v1.2.17</b>
            </div>
            <div class="pill" style="border-color: rgba(16, 185, 129, 0.4);">
                <span style="width: 7px; height: 7px; border-radius: 50%; background: var(--accent-emerald); display: inline-block;"></span>
                <span>DB</span> <b id="header-db-pill" style="color: var(--accent-emerald);">🐘 PostgreSQL</b>
            </div>
            <div class="pill">
                <span>GPU</span> <b id="gpu-info">RTX 3060</b>
            </div>
            <div class="pill">
                <span>RAM</span> <b id="ram-info">32 GB</b>
            </div>
            <button class="refresh-btn" onclick="refreshUI()" title="Muat ulang tampilan">🔄 Refresh UI</button>
            <button class="refresh-btn" style="border-color: rgba(244, 63, 94, 0.4); color: var(--accent-rose);" onclick="restartServerDaemon()" title="Restart server service">⚡ Restart Daemon</button>
        </div>
    </header>

    <div class="workspace">
        <!-- Left: Model & Execution Controls -->
        <div class="sidebar-left">
            <div>
                <div class="section-label">Active AI Model</div>
                <select id="model-select" class="select-input" onchange="onModelChange()">
                    <option value="qwen-3.8-flash" selected>🌟 Qwen 3.8 Flash (1M Context • 0 Pts • Qoder)</option>
                    <option value="qwen-3.8-max">🚀 Qwen 3.8 Max (Qoder)</option>
                    <option value="deepseek-v4-pro">🛡️ DeepSeek V4 Pro (Qoder)</option>
                    <option value="glm-5.3-flash">⚡ GLM 5.3 Flash (Qoder)</option>
                    <option value="kimi-k3">🌙 Kimi K3 (Qoder)</option>
                    <option value="gemini-3.8-flash">⚡ Gemini 3.8 Flash High (1M Context • Antigravity)</option>
                    <option value="gemini-3.1-pro">🧠 Gemini 3.1 Pro High (2M Context • Antigravity)</option>
                    <option value="claude-sonnet">🔮 Claude Sonnet 5.5 High (Antigravity)</option>
                    <option value="local-ollama">💻 Local Model (RTX 3060 Offline)</option>
                </select>

                <div class="section-label">Reasoning Effort (Pemikiran)</div>
                <select id="effort-select" class="select-input">
                    <option value="xhigh" selected>🔥 Extra High (xhigh) - Maksimal & Teliti</option>
                    <option value="high">🧠 High - Analisis Mendalam</option>
                    <option value="medium">⚖️ Medium - Standar Seimbang</option>
                    <option value="low">⚡ Low - Cepat & Ringkas</option>
                    <option value="auto">🔄 Auto - Adaptif</option>
                </select>

                <div class="section-label">Context Window (Tokens)</div>
                <select id="context-select" class="select-input">
                    <option value="1000000" selected>1,000,000 Tokens (1M Context)</option>
                    <option value="500000">500,000 Tokens (500k Context)</option>
                    <option value="200000">200,000 Tokens (200k Context)</option>
                    <option value="128000">128,000 Tokens (128k Context)</option>
                    <option value="32000">32,768 Tokens (32k Default)</option>
                </select>

                <div class="section-label">Permissions & Execution</div>
                <div id="auto-approve-card" class="toggle-card" onclick="toggleAutoApprove()">
                    <div class="toggle-title">
                        <span>⚡ Auto-Approve Tools</span>
                    </div>
                    <div id="auto-approve-indicator" class="switch-indicator">ON</div>
                </div>
                <div style="font-size: 0.68rem; color: var(--text-dim); margin-top: -6px; margin-bottom: 12px; line-height: 1.4;">
                    Saat ON: Tool & pembacaan file otomatis diizinkan tanpa jeda konfirmasi di terminal.
                </div>
            </div>

            <div>
                <div class="section-label">Database & RPG Save State</div>
                <div class="card-aux" style="padding: 10px; font-size: 0.72rem; color: var(--text-muted); line-height: 1.6;">
                    <div>💾 <b>Save State:</b> <span id="db-engine-badge" style="color: var(--accent-emerald);">🐘 POSTGRESQL Active</span></div>
                    <div>📁 <b>Skills:</b> <span id="skills-count">4</span> unlocked</div>
                    <div>📁 <b>Quests:</b> <span id="quests-count">0</span> logged</div>
                    <div>🛡️ <b>Guardrails:</b> <span id="rules-count">2</span> rules active</div>
                    <div id="db-path-badge" style="margin-top: 4px; color: var(--accent-cyan); font-family: monospace; font-size: 0.68rem;">postgresql://irsofka@localhost:5432/irsofka_ai_workstation</div>
                </div>
            </div>
        </div>

        <!-- Center: Studio Live Terminal -->
        <div class="center-stage">
            <!-- Terminal Header & Tabs -->
            <div class="term-header">
                <div class="term-tabs">
                    <div id="tab-qoder" class="term-tab qoder active" onclick="switchTab('qoder')">
                        <span>🌟 Qoder CLI (Qwen 1M • 0 Pts)</span>
                    </div>
                    <div id="tab-antigravity" class="term-tab antigravity" onclick="switchTab('antigravity')">
                        <span>⚡ Antigravity CLI (Gemini)</span>
                    </div>
                    <div id="tab-shell" class="term-tab shell" onclick="switchTab('shell')">
                        <span>🐧 COSMIC Shell</span>
                    </div>
                </div>

                <div class="term-actions">
                    <span style="font-size: 0.7rem; color: var(--accent-emerald); display: flex; align-items: center; gap: 4px;">
                        <span style="width: 6px; height: 6px; border-radius: 50%; background: var(--accent-emerald);"></span>
                        Live Stream (60 FPS)
                    </span>
                    <button class="term-btn" onclick="clearActiveTerm()">🧹 Clear</button>
                    <button class="term-btn" onclick="resetActiveSession()">🔄 Reset Session</button>
                </div>
            </div>

            <!-- The Live xterm.js Terminal Viewport -->
            <div class="terminal-container" id="terminal-box">
                <div id="terminal" style="width: 100%; height: 100%;"></div>
            </div>

            <!-- Action Chips for 1-Click Execution -->
            <div class="action-chips">
                <div class="chip" onclick="quickRun('coba kamu lihat project summary irsofka ai workstation dan jelaskan statusnya')">📄 Lihat Project Summary</div>
                <div class="chip" onclick="quickRun('analisis arsitektur sistem irsofka ai workstation')">⚡ Analisis Arsitektur</div>
                <div class="chip" onclick="quickRun('qoder audit dan review keamanan file ~/.ai-station/tools/pty_station_server.py')">🛡️ Audit Keamanan Qoder</div>
                <div class="chip" onclick="execAction('see_screen')">📸 Ambil Screenshot Desktop</div>
                <div class="chip" onclick="execAction('open_youtube')">📺 Buka YouTube (Brave)</div>
            </div>

            <!-- Bottom Prompt Dispatcher -->
            <div class="prompt-bar">
                <input id="prompt-input" class="prompt-input" placeholder="Ketik instruksi ke CLI... (Tekan Enter untuk jalankan langsung di terminal)" onkeydown="handleKey(event)">
                <button class="btn-send" onclick="sendPromptToCLI()">
                    <span>🚀 Jalankan</span>
                </button>
            </div>
        </div>

        <!-- Right: Inspector & Visual Eye -->
        <div class="sidebar-right">
            <div class="card-aux">
                <h4>Active Workspace</h4>
                <div id="active-ws-path" style="font-family: monospace; font-size: 0.7rem; background: #0c0e15; padding: 8px; border-radius: 6px; border: 1px solid var(--border-subtle); word-break: break-all; color: var(--accent-cyan);">
                    Detecting...
                </div>
            </div>

            <div class="card-aux">
                <h4>
                    <span>Visual Eye (Wayland)</span>
                    <button style="background: transparent; border: none; color: var(--accent-cyan); cursor: pointer; font-size: 0.72rem;" onclick="captureScreen()">📸 Ambil</button>
                </h4>
                <div class="screen-preview" id="screen-box">
                    <span style="font-size: 0.72rem; color: var(--text-dim);">Klik 'Ambil' untuk intip layar</span>
                </div>
            </div>

            <div class="card-aux">
                <h4>Self-Healing Tracker</h4>
                <div style="font-size: 0.75rem; color: var(--accent-emerald);">
                    ● 0 Repeat Crash (Stabil)
                </div>
                <div style="font-size: 0.7rem; color: var(--text-dim); margin-top: 4px;">
                    5x repeat trigger auto-synthesizes new skill ke workstation.db
                </div>
            </div>

            <div class="card-aux">
                <h4>OS Quick Actions</h4>
                <button class="quick-btn" onclick="execAction('open_youtube')">📺 Buka YouTube (Brave)</button>
                <button class="quick-btn" onclick="execAction('notify_test')">🔔 Kirim Test Notifikasi</button>
                <button class="quick-btn" onclick="execAction('vol_up')">🔊 Volume Up (+10%)</button>
                <button class="quick-btn" onclick="execAction('vol_down')">🔉 Volume Down (-10%)</button>
            </div>
        </div>
    </div>

    <script>
        let activeTab = 'qoder';
        let isAutoApprove = true;
        let term, fitAddon;

        function initTerminal() {
            term = new Terminal({
                cursorBlink: true,
                fontFamily: "'JetBrains Mono', 'Fira Code', monospace",
                fontSize: 13,
                lineHeight: 1.25,
                theme: {
                    background: '#05070c',
                    foreground: '#f1f5f9',
                    cursor: '#38bdf8',
                    selectionBackground: 'rgba(56, 189, 248, 0.3)',
                    black: '#000000',
                    red: '#f43f5e',
                    green: '#10b981',
                    yellow: '#f59e0b',
                    blue: '#38bdf8',
                    magenta: '#818cf8',
                    cyan: '#22d3ee',
                    white: '#ffffff'
                }
            });
            fitAddon = new FitAddon.FitAddon();
            term.loadAddon(fitAddon);
            term.open(document.getElementById('terminal'));
            fitAddon.fit();

            // Keyboard input forwarding to active PTY master FD
            term.onData(data => {
                fetch('/api/term/write', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({tab: activeTab, data: data})
                });
            });

            window.addEventListener('resize', () => {
                if (fitAddon) fitAddon.fit();
            });

            pollTerminal();
        }

        async function pollTerminal() {
            try {
                const res = await fetch('/api/term/read?tab=' + activeTab);
                if (res.ok) {
                    const text = await res.text();
                    if (text.length > 0) term.write(text);
                }
            } catch (err) {}
            setTimeout(pollTerminal, 50);
        }

        function switchTab(tab) {
            activeTab = tab;
            document.querySelectorAll('.term-tab').forEach(t => t.classList.remove('active'));
            document.getElementById('tab-' + tab).classList.add('active');
            term.clear();
            term.write(`\x1b[1;30m--- Beralih ke sesi: ${tab.toUpperCase()} ---\x1b[0m\r\n`);
            if (fitAddon) fitAddon.fit();
        }

        function toggleAutoApprove() {
            isAutoApprove = !isAutoApprove;
            const card = document.getElementById('auto-approve-card');
            const ind = document.getElementById('auto-approve-indicator');
            if (isAutoApprove) {
                card.classList.remove('off');
                ind.innerText = 'ON';
            } else {
                card.classList.add('off');
                ind.innerText = 'OFF';
            }
        }

        function onModelChange() {
            const m = document.getElementById('model-select').value;
            if (m.includes('gemini') || m.includes('claude')) {
                switchTab('antigravity');
            } else if (m.includes('qwen') || m.includes('deepseek') || m.includes('glm') || m.includes('kimi')) {
                switchTab('qoder');
            }
        }

        function handleKey(e) {
            if (e.key === 'Enter') sendPromptToCLI();
        }

        function quickRun(text) {
            document.getElementById('prompt-input').value = text;
            sendPromptToCLI();
        }

        async function sendPromptToCLI() {
            const inp = document.getElementById('prompt-input');
            const prompt = inp.value.trim();
            if (!prompt) return;
            inp.value = '';

            const model = document.getElementById('model-select').value;
            const effort = document.getElementById('effort-select').value;
            const context_size = document.getElementById('context-select').value;

            // Route tab to correct CLI
            if (model.includes('gemini') || model.includes('claude')) {
                if (activeTab !== 'antigravity') switchTab('antigravity');
            } else if (model.includes('qwen') || model.includes('deepseek') || model.includes('glm') || model.includes('kimi')) {
                if (activeTab !== 'qoder') switchTab('qoder');
            }

            try {
                await fetch('/api/cli/run', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({
                        prompt: prompt,
                        model: model,
                        effort: effort,
                        context_size: context_size,
                        auto_approve: isAutoApprove,
                        target_cli: activeTab
                    })
                });
            } catch(e) {
                term.write('\r\n\x1b[1;31m[Error Dispatching Command]\x1b[0m\r\n');
            }
        }

        function clearActiveTerm() {
            term.clear();
        }

        async function resetActiveSession() {
            if (!confirm(`Reset sesi CLI ${activeTab.toUpperCase()}?`)) return;
            await fetch('/api/term/reset', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({tab: activeTab})
            });
            term.clear();
            term.write(`\x1b[1;32m[Sesi ${activeTab.toUpperCase()} telah di-reset]\x1b[0m\r\n`);
        }

        async function fetchStats() {
            try {
                const res = await fetch('/api/stats');
                const d = await res.json();
                if (d.gpu) document.getElementById('gpu-info').innerText = `${d.gpu.used_mb}/${d.gpu.total_mb} MB (${d.gpu.load}%)`;
                if (d.ram) document.getElementById('ram-info').innerText = `${d.ram.used_gb}/${d.ram.total_gb} GB`;
                
                if (d.antigravity_cli) {
                    const el = document.getElementById('agy-cli-badge');
                    if (el) {
                        el.innerText = d.antigravity_cli.connected ? `v${d.antigravity_cli.version}` : 'OFFLINE';
                        el.style.color = d.antigravity_cli.connected ? 'var(--accent-cyan)' : 'var(--accent-rose)';
                    }
                }
                if (d.qoder_cli) {
                    const el = document.getElementById('qoder-cli-badge');
                    if (el) {
                        el.innerText = d.qoder_cli.connected ? `v${d.qoder_cli.version}` : 'OFFLINE';
                        el.style.color = d.qoder_cli.connected ? 'var(--accent-amber)' : 'var(--accent-rose)';
                    }
                }

                document.getElementById('skills-count').innerText = d.skills_count;
                if (document.getElementById('quests-count')) document.getElementById('quests-count').innerText = d.total_quests || 0;
                document.getElementById('active-ws-path').innerText = d.active_workspace;

                if (d.db_engine) {
                    const hPill = document.getElementById('header-db-pill');
                    const dbBadge = document.getElementById('db-engine-badge');
                    const dbPath = document.getElementById('db-path-badge');
                    if (d.db_engine === 'POSTGRESQL') {
                        if (hPill) { hPill.innerText = '🐘 PostgreSQL'; hPill.style.color = 'var(--accent-emerald)'; }
                        if (dbBadge) { dbBadge.innerText = '🐘 POSTGRESQL Active'; dbBadge.style.color = 'var(--accent-emerald)'; }
                        if (dbPath) dbPath.innerText = 'postgresql://irsofka@localhost:5432/irsofka_ai_workstation';
                    } else {
                        if (hPill) { hPill.innerText = '⚡ SQLite'; hPill.style.color = 'var(--accent-amber)'; }
                        if (dbBadge) { dbBadge.innerText = '⚡ SQLITE Fallback'; dbBadge.style.color = 'var(--accent-amber)'; }
                        if (dbPath) dbPath.innerText = '~/.ai-station/brain/workstation.db';
                    }
                }
            } catch(e) {}
        }

        function refreshUI() {
            window.location.reload();
        }

        async function restartServerDaemon() {
            if (!confirm('Apakah Anda yakin ingin me-restart server daemon?')) return;
            try {
                await fetch('/api/restart-server', {method: 'POST'});
                alert('Server sedang di-restart. Halaman akan dimuat ulang...');
                setTimeout(() => window.location.reload(), 2000);
            } catch(e) {
                window.location.reload();
            }
        }

        async function captureScreen() {
            const box = document.getElementById('screen-box');
            box.innerHTML = '<span style="font-size:0.72rem; color:var(--accent-cyan);">Capturing...</span>';
            const res = await fetch('/api/see', {method: 'POST'});
            const data = await res.json();
            if (data.status === 'ok') {
                box.innerHTML = `<img src="/api/screenshot/latest?t=${Date.now()}" alt="Screen" />`;
            }
        }

        function execAction(action) {
            if (action === 'see_screen') {
                captureScreen();
                return;
            }
            fetch('/api/action', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: action})
            });
        }

        window.onload = () => {
            initTerminal();
            fetchStats();
            setInterval(fetchStats, 3000);
        };
    </script>
</body>
</html>
"""


class PTYStationHandler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, *")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_cors_json(self, data_dict):
        body = json.dumps(data_dict).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, *")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            body = HTML_DESKTOP_GUI.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif path == "/api/stats":
            self.send_cors_json(get_telemetry())

        elif path == "/api/term/read":
            tab = query.get("tab", ["qoder"])[0]
            session = sessions.get(tab)
            output = session.read_pending() if session else b""
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(output)))
            self.end_headers()
            self.wfile.write(output)

        elif path == "/api/screenshot/latest":
            if SCREENSHOT_PATH.exists():
                with open(SCREENSHOT_PATH, "rb") as f:
                    img_data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(img_data)))
                self.end_headers()
                self.wfile.write(img_data)
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else b"{}"

        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {}

        if path == "/api/cli/run" or path == "/api/chat":
            prompt = payload.get("prompt", "").strip()
            model = payload.get("model", "qwen-3.8-flash")
            effort = payload.get("effort", "xhigh")
            context_size = payload.get("context_size", "1000000")
            auto_approve = payload.get("auto_approve", True)
            target_cli = payload.get("target_cli", "qoder")

            # Route target CLI based on model
            m_lower = model.lower()
            if "gemini" in m_lower or "claude" in m_lower:
                target_cli = "antigravity"
            elif "qwen" in m_lower or "deepseek" in m_lower or "glm" in m_lower or "kimi" in m_lower:
                target_cli = "qoder"

            session = sessions.get(target_cli, sessions["qoder"])

            # Escape prompt safely for bash single/double quote
            safe_prompt = prompt.replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')

            if target_cli == "qoder":
                cli_model = {
                    "qwen-3.8-flash": "Qwen3.8-Flash",
                    "qwen-3.8-max": "Qwen3.8-Max",
                    "deepseek-v4-pro": "DeepSeek-V4-Pro",
                    "glm-5.3-flash": "GLM-5.3-Flash",
                    "kimi-k3": "Kimi-K3"
                }.get(model, "Qwen3.8-Flash")

                flags = f"-m {cli_model} --context-window {context_size} --reasoning-effort {effort}"
                if auto_approve:
                    flags += " --permission-mode bypass_permissions"
                cmd = f'qoder {flags} -p "{safe_prompt}"\n'

            elif target_cli == "antigravity":
                cli_model = {
                    "gemini-3.8-flash": "gemini-3.8-flash-high",
                    "gemini-3.1-pro": "gemini-3.1-pro-high",
                    "claude-sonnet": "claude-sonnet-5-5-high"
                }.get(model, "gemini-3.8-flash-high")

                flags = f"--model {cli_model} --effort {effort}"
                if auto_approve:
                    flags += " --dangerously-skip-permissions"
                cmd = f'agy {flags} -p "{safe_prompt}"\n'

            else:
                cmd = f"{prompt}\n"

            # Execute command live into PTY session
            session.write(cmd.encode("utf-8"))

            # Log to SQLite DB
            try:
                import db_state
                ctx_int = int(context_size) if str(context_size).isdigit() else 1000000
                db_state.log_session_turn(target_cli, model, effort, ctx_int, prompt, auto_approved=auto_approve)
            except Exception:
                pass

            self.send_cors_json({
                "status": "dispatched",
                "target_cli": target_cli,
                "command": cmd
            })

        elif path == "/api/term/write":
            tab = payload.get("tab", "qoder")
            data = payload.get("data", "")
            session = sessions.get(tab)
            if session and data:
                session.write(data.encode("utf-8"))
            self.send_cors_json({"status": "ok"})

        elif path == "/api/term/reset":
            tab = payload.get("tab", "qoder")
            session = sessions.get(tab)
            if session:
                session.write(b"\x03\n") # Send Ctrl+C
                time.sleep(0.1)
                session.write(b"clear\n")
            self.send_cors_json({"status": "reset"})

        elif path == "/api/restart-server":
            def _restart():
                time.sleep(0.5)
                subprocess.run(["systemctl", "--user", "restart", "irsofka-ai-workstation.service"])
            threading.Thread(target=_restart, daemon=True).start()
            self.send_cors_json({"status": "restarting", "message": "Server sedang merestart..."})

        elif path == "/api/see":
            res = {"status": "ok", "message": "Screenshot berhasil diambil"}
            try:
                subprocess.run(["python3", str(AI_STATION / "tools" / "wayland_actor.py"), "screenshot"], check=True)
            except Exception as e:
                res = {"status": "error", "message": str(e)}
            self.send_cors_json(res)

        elif path == "/api/action":
            action = payload.get("action")
            if action == "open_youtube":
                subprocess.Popen(["flatpak", "run", "com.brave.Browser", "https://youtube.com"])
            elif action == "notify_test":
                subprocess.Popen(["notify-send", "-a", "Irsofka AI Workstation", "Halo Bro Ichsan!", "AI Workstation aktif di desktop!"])
            elif action == "vol_up":
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"])
            elif action == "vol_down":
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"])

            self.send_cors_json({"status": "ok"})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


def main():
    server = HTTPServer((HOST, PORT), PTYStationHandler)
    print(f"⚛️ Irsofka AI Workstation Running at: http://{HOST}:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
