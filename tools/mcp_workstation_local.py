#!/usr/bin/env python3
"""
Irsofka AI Workstation - Native Local System MCP Server
Exposes Pop!_OS COSMIC Desktop, Hardware Telemetry (RTX 3060), Audio, Wayland Vision,
dan Game-State SQL Database ke Google Antigravity & Qoder melalui Model Context Protocol (MCP).
"""

import sys
import json
import os
import subprocess
import urllib.request
from pathlib import Path

AI_STATION = Path.home() / ".ai-station"
BRAIN_DIR = AI_STATION / "brain"
LOGS_DIR = AI_STATION / "logs"
SCREENSHOT_PATH = LOGS_DIR / "current_screen.png"
DB_PATH = BRAIN_DIR / "workstation.db"

# Sumber kebenaran save-state: adapter dual-engine (PostgreSQL primer, SQLite fallback).
# Menghindari "split-brain" di mana MCP membaca SQLite sementara daemon Rust membaca PostgreSQL.
sys.path.insert(0, str(AI_STATION / "tools"))
from db_state import get_db_connection  # noqa: E402

READONLY_TABLES = {
    "player_profile", "skills_inventory", "quest_tasks", "world_memory",
    "incident_log", "session_turns", "action_log",
}


def db_ph(engine: str) -> str:
    return "%s" if engine == "POSTGRESQL" else "?"


STATION_PORT = int(os.environ.get("STATION_PORT", "8999"))


def station_api(path: str, method: str = "GET", timeout: float = 8.0):
    """Panggil HTTP API daemon workstation itu sendiri.

    Sengguh lewat API, bukan subprocess systemctl: daemon sudah punya endpoint resmi untuk
    ini, jadi jalur izin dan pencatatan ke spool event tetap satu pintu. Kalau daemon mati,
    kita kembalikan error yang jujur, bukan pura-pura sukses.
    """
    url = f"http://127.0.0.1:{STATION_PORT}{path}"
    try:
        req = urllib.request.Request(url, method=method, data=b"{}" if method == "POST" else None,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace").strip()
        return True, body or "{}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"

TOOLS = [
    {
        "name": "get_hardware_telemetry",
        "description": "Mengambil status hardware real-time: NVIDIA RTX 3060 VRAM, GPU Load, Suhu, RAM, CPU, dan sisa kapasitas NVMe SSD.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "take_screenshot_wayland",
        "description": "Mengambil screenshot desktop Pop!_OS COSMIC (Wayland) untuk inspeksi visual mata AI.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "send_desktop_notification",
        "description": "Mengirimkan notifikasi banner langsung ke layar desktop Pop!_OS COSMIC pengguna.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Judul notifikasi"},
                "message": {"type": "string", "description": "Isi pesan notifikasi"}
            },
            "required": ["title", "message"]
        }
    },
    {
        "name": "control_system_volume",
        "description": "Mengatur volume speaker/headphone Pop!_OS melalui PipeWire/PulseAudio pactl.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["up", "down", "mute", "unmute"], "description": "Aksi volume: up (+10%), down (-10%), mute, unmute"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "query_workstation_db",
        "description": "Membaca save-state dari PostgreSQL (fallback SQLite): profil, skill, quest, memori, insiden, turn, dan action_log (riwayat perintah AI untuk pemulihan sesi).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "enum": sorted(READONLY_TABLES)},
                "limit": {"type": "integer", "description": "Batas jumlah baris (default 10)"}
            },
            "required": ["table_name"]
        }
    },
    {
        "name": "update_quest_task",
        "description": "Mencatat atau memperbarui task/quest coding di database save-state workstation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Nama quest / task"},
                "status": {"type": "string", "enum": ["IN_PROGRESS", "COMPLETED", "FAILED"]},
                "model_assigned": {"type": "string", "description": "Model yang mengerjakan"},
                "summary": {"type": "string", "description": "Rangkuman hasil pekerjaan"}
            },
            "required": ["title", "status"]
        }
    },
    {
        "name": "refresh_workstation_ui",
        "description": "Memuat ulang jendela GUI Irsofka AI Workstation agar HTML/state terbaru tampil. Aman: hanya menyuruh halaman WebView reload, tidak menyentuh tab PTY maupun sesi CLI di dalamnya.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": []
        }
    },
    {
        "name": "restart_workstation_daemon",
        "description": "Restart daemon irsofka-ai-workstation.service lewat endpoint resminya. Sejak tab berjalan di tmux (-L irsofka), sesi Qoder/Antigravity TIDAK ikut mati. Pakai bila perubahan biner atau konfigurasi butuh dimuat ulang.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Alasan singkat restart, dicatat ke spool event workstation"
                }
            },
            "required": ["reason"]
        }
    },
    {
        "name": "read_terminal",
        "description": "Baca ISI LAYAR sebuah tab workstation (qoder | antigravity | shell) secara seketika lewat tmux capture-pane. Pakai ini untuk melihat jawaban mesin lain tanpa menunggu apa pun (~3 ms). Pasangkan dengan send_to_terminal untuk bertanya.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab": {"type": "string", "enum": ["qoder", "antigravity", "shell"], "description": "tab yang dibaca"},
                "tail_lines": {"type": "integer", "description": "berapa baris terakhir diambil (default 60)"}
            },
            "required": ["tab"]
        }
    },
    {
        "name": "send_to_terminal",
        "description": "Ketik ke tab CLI workstation lalu tekan Enter di sana (CR, bukan LF — TUI tidak mengenali LF). Dipakai untuk meminta mesin lain meninjau pekerjaan, mis. Gemini. Balasnya dibaca dengan read_terminal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab": {"type": "string", "enum": ["qoder", "antigravity", "shell"], "description": "tab tujuan"},
                "text": {"type": "string", "description": "isi yang diketik"},
                "submit": {"type": "boolean", "description": "tekan Enter setelah mengetik (default true)"}
            },
            "required": ["tab", "text"]
        }
    }
]

def handle_call_tool(name, args):
    if name == "get_hardware_telemetry":
        data = {}
        try:
            gpu_out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
                text=True, timeout=2
            ).strip().split(",")
            if len(gpu_out) >= 5:
                data["gpu"] = {
                    "name": gpu_out[0].strip(),
                    "vram_used_mb": int(gpu_out[1].strip()),
                    "vram_total_mb": int(gpu_out[2].strip()),
                    "load_percent": int(gpu_out[3].strip()),
                    "temp_c": int(gpu_out[4].strip())
                }
        except Exception as e:
            data["gpu_error"] = str(e)

        try:
            mem_out = subprocess.check_output(["free", "-m"], text=True, timeout=2).splitlines()
            if len(mem_out) > 1:
                p = mem_out[1].split()
                data["ram"] = {"used_gb": round(int(p[2]) / 1024, 1), "total_gb": round(int(p[1]) / 1024, 1)}
        except Exception:
            pass

        try:
            d = subprocess.check_output(["df", "-h", "/"], text=True, timeout=2).splitlines()
            if len(d) > 1:
                dp = d[1].split()
                data["disk"] = {"free": dp[3], "total": dp[1]}
        except Exception:
            pass

        return json.dumps(data, indent=2)

    elif name == "take_screenshot_wayland":
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["python3", str(AI_STATION / "tools" / "wayland_actor.py"), "screenshot"], stdout=subprocess.PIPE, text=True)
        return f"Screenshot berhasil diambil dan disimpan di {SCREENSHOT_PATH}. Log: {res.stdout.strip()}"

    elif name == "refresh_workstation_ui":
        ok, body = station_api("/api/ui/reload", "POST")
        if not ok:
            return f"GAGAL meminta muat ulang UI (daemon di port {STATION_PORT} tidak menjawab): {body}"
        return f"Permintaan muat ulang UI terkirim. {body}"

    elif name == "restart_workstation_daemon":
        reason = (args.get("reason") or "").strip()
        if not reason:
            return "DITOLAK: isi 'reason' dulu, supaya restart tercatat dan bisa ditelusuri."
        ok, body = station_api("/api/restart-server", "POST")
        if not ok:
            return f"GAGAL meminta restart daemon: {body}"
        return (f"Daemon di-restart (alasan: {reason}). {body}\n"
                "Tab qoder/antigravity/shell hidup di tmux dan tidak ikut mati. "
                "Verifikasi: systemctl --user is-active irsofka-ai-workstation.service")

    elif name == "read_terminal":
        tab = args.get("tab") or "qoder"
        tail = int(args.get("tail_lines") or 60)
        sock = os.environ.get("STATION_TMUX_SOCKET", "irsofka")
        try:
            res = subprocess.run(["tmux", "-L", sock, "capture-pane", "-p", "-t", f"station-{tab}"],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, timeout=10)
        except FileNotFoundError:
            return "tmux tidak tersedia — tab workstation tidak bisa dibaca."
        if res.returncode != 0:
            return f"tab '{tab}' tidak dapat dibaca: {res.stderr.strip()[:200]}"
        screen = res.stdout
        body = [l.rstrip() for l in screen.splitlines() if l.strip()]
        busy = (any(ch in screen for ch in "⠁⠂⠄⡀⢀⠐⠈⠋⠙⠹⠸⠼⠴⠦⠇⠏⣾⣽⢿⣷")
                or "esc to cancel" in screen.lower())
        return (f"tab {tab} | {len(body)} baris konten | status={'BEKERJA' if busy else 'IDLE'}\n"
                + "\n".join(body[-tail:]))

    elif name == "send_to_terminal":
        tab = args.get("tab") or "antigravity"
        text = str(args.get("text") or "")
        submit = args.get("submit", True)
        if not text.strip():
            return "DITOLAK: teks kosong."
        sock = os.environ.get("STATION_TMUX_SOCKET", "irsofka")
        target = f"station-{tab}"
        try:
            # -l = literal, supaya karakter khusus tidak ditafsir sebagai nama tombol
            subprocess.run(["tmux", "-L", sock, "send-keys", "-t", target, "-l", text],
                           check=True, timeout=10)
            if submit:
                # Enter = CR. tmux menormalkannya sendiri; mengirim "\n" manual tidak akan
                # dianggap Enter oleh TUI (penyebab bug dispatch yang diperbaiki 2026-10-06).
                subprocess.run(["tmux", "-L", sock, "send-keys", "-t", target, "Enter"],
                               check=True, timeout=10)
        except FileNotFoundError:
            return "tmux tidak tersedia — tidak bisa mengirim ke tab."
        except subprocess.CalledProcessError as exc:
            return f"gagal mengirim ke tab '{tab}': {exc.stderr.strip()[:200] if exc.stderr else exc}"
        return (f"Terkirim ke tab {tab}{' + Enter' if submit else ''}. "
                f"Baca jawabannya dengan read_terminal(tab='{tab}').")

    elif name == "send_desktop_notification":
        title = args.get("title", "Irsofka AI Workstation")
        msg = args.get("message", "")
        subprocess.Popen(["notify-send", "-a", "Irsofka AI Workstation", title, msg])
        return f"Notifikasi terkirim ke desktop: '{title} - {msg}'"

    elif name == "control_system_volume":
        action = args.get("action", "up")
        if action == "up":
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"])
            return "Volume dinaikkan +10%"
        elif action == "down":
            subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"])
            return "Volume diturunkan -10%"
        elif action == "mute":
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"])
            return "Speaker di-mute"
        elif action == "unmute":
            subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
            return "Speaker di-unmute"
        return "Aksi tidak dikenali"

    elif name == "query_workstation_db":
        table = args.get("table_name", "skills_inventory")
        limit = int(args.get("limit", 10) or 10)
        if table not in READONLY_TABLES:
            return json.dumps({"error": f"tabel tidak diizinkan: {table}"})
        conn, engine = get_db_connection()
        try:
            cur = conn.cursor()
            # action_log & session_turns bertumpuk tiap detik: ambil yang TERBARU, bukan paling tua.
            order = "ORDER BY id DESC" if table in ("action_log", "session_turns", "quest_tasks") else ""
            cur.execute(f"SELECT * FROM {table} {order} LIMIT {db_ph(engine)}", (limit,))
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
            cur.close()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return json.dumps({"db_engine": engine, "rows": rows}, indent=2, default=str)

    elif name == "update_quest_task":
        title = args.get("title")
        status = args.get("status", "IN_PROGRESS")
        model = args.get("model_assigned", "Qwen/Gemini")
        summary = args.get("summary", "")
        if not title:
            return "Gagal: argumen 'title' wajib diisi."
        conn, engine = get_db_connection()
        ph = db_ph(engine)
        try:
            cur = conn.cursor()
            done = "CURRENT_TIMESTAMP" if status == "COMPLETED" else "NULL"
            if engine == "POSTGRESQL":
                cur.execute(f"""
                INSERT INTO quest_tasks (title, status, model_assigned, cli_engine, summary, completed_at)
                VALUES ({ph}, {ph}, {ph}, 'qoder', {ph}, {done});
                """, (title, status, model, summary))
            else:
                cur.execute("""
                INSERT INTO quest_tasks (title, status, model_assigned, cli_engine, summary, completed_at)
                VALUES (?, ?, ?, 'qoder', ?, CASE WHEN ? = 'COMPLETED' THEN CURRENT_TIMESTAMP ELSE NULL END);
                """, (title, status, model, summary, status))
            conn.commit()
            cur.close()
        except Exception as exc:
            try:
                conn.rollback()
            except Exception:
                pass
            return f"Gagal mencatat quest: {exc}"
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return f"Quest Task '{title}' status '{status}' berhasil dicatat di save-state {engine}."

    return f"Tool {name} tidak ditemukan."

def main():
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue

        msg_id = req.get("id")
        method = req.get("method")
        params = req.get("params", {})

        if method == "initialize":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {
                        "name": "irsofka-local-workstation-mcp",
                        "version": "1.0.0"
                    }
                }
            }
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        elif method == "notifications/initialized":
            pass

        elif method == "ping":
            res = {"jsonrpc": "2.0", "id": msg_id, "result": {}}
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        elif method == "tools/list":
            res = {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {
                    "tools": TOOLS
                }
            }
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

        elif method == "tools/call":
            tool_name = params.get("name")
            tool_args = params.get("arguments", {})
            try:
                content_text = handle_call_tool(tool_name, tool_args)
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": str(content_text)}]
                    }
                }
            except Exception as e:
                res = {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "content": [{"type": "text", "text": f"Error: {e}"}],
                        "isError": True
                    }
                }
            sys.stdout.write(json.dumps(res) + "\n")
            sys.stdout.flush()

if __name__ == "__main__":
    main()
