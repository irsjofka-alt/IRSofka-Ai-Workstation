#!/usr/bin/env python3
"""
Irsofka AI Workstation - Tri-Engine Parallel Task Dispatcher (Swarm Orchestrator)
Menjalankan 3 Task Sekaligus Secara Konkuren / Paralel:
1. Engine Antigravity (Architect & System Planner - Low to Hard)
2. Engine Qoder (1M Context Code Synthesizer & Auditor - Low to Hard)
3. Engine Ollama / Local (Offline Micro-Refactoring & Validation - Very Low to Medium)

Hasil dan kemajuan tugas otomatis disinkronkan ke Game Save State (~/.ai-station/brain/workstation.db).
"""

import sys
import json
import time
import os
import subprocess
import threading
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".ai-station" / "tools"))
from db_state import record_quest_task  # noqa: E402  adapter dual-engine resmi

AI_STATION = Path.home() / ".ai-station"
BRAIN_DIR = AI_STATION / "brain"
DB_PATH = BRAIN_DIR / "workstation.db"

# Batas waktu tugas. Angka lama 120 detik terbukti menghasilkan laporan FAILED palsu:
# satu giliran kerja Qoder yang sah bisa berjalan belasan menit.
TASK_TIMEOUT = int(os.environ.get("STATION_TASK_TIMEOUT", "1800"))
LOCAL_TIMEOUT = int(os.environ.get("STATION_LOCAL_TIMEOUT", "120"))
OLLAMA_URL = os.environ.get("STATION_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")
LOCAL_MODEL = os.environ.get("STATION_LOCAL_MODEL", "qwen2.5-coder:7b")

def run_antigravity_task(task_title: str, prompt: str, model="gemini-3.8-flash-high", effort=""):
    cmd = ["agy", "--model", model, "--dangerously-skip-permissions", "-p", prompt]
    if effort and ("high" not in model and "low" not in model and "medium" not in model):
        cmd.extend(["--effort", effort])
    start = time.time()
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=TASK_TIMEOUT)
        output = res.stdout.strip() or res.stderr.strip() or "(CLI tidak menghasilkan output)"
        status = "COMPLETED" if res.returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        output = f"Batas waktu {TASK_TIMEOUT}s terlampaui; tugas dihentikan sebelum selesai."
        status = "TIMEOUT"
    except FileNotFoundError:
        output = "Perintah 'agy' tidak ditemukan di PATH."
        status = "UNAVAILABLE"
    except Exception as e:  # noqa: BLE001
        output = f"Error: {e}"
        status = "FAILED"
    duration = int((time.time() - start) * 1000)
    recorded = record_task_to_db("antigravity", model, task_title, status, output[:500])
    return {"engine": "antigravity", "status": status, "output": output,
            "duration_ms": duration, "recorded": recorded}

def run_qoder_task(task_title: str, prompt: str, model="Qwen3.8-Flash", effort="xhigh", ctx="1000000"):
    cmd = ["qoder", "-m", model, "--context-window", ctx, "--reasoning-effort", effort,
           "--permission-mode", "bypass_permissions", "-p", prompt]
    start = time.time()
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, timeout=TASK_TIMEOUT)
        output = res.stdout.strip() or res.stderr.strip() or "(CLI tidak menghasilkan output)"
        status = "COMPLETED" if res.returncode == 0 else "FAILED"
    except subprocess.TimeoutExpired:
        output = f"Batas waktu {TASK_TIMEOUT}s terlampaui; tugas dihentikan sebelum selesai."
        status = "TIMEOUT"
    except FileNotFoundError:
        output = "Perintah 'qoder' tidak ditemukan di PATH."
        status = "UNAVAILABLE"
    except Exception as e:  # noqa: BLE001
        output = f"Error: {e}"
        status = "FAILED"
    duration = int((time.time() - start) * 1000)
    recorded = record_task_to_db("qoder", model, task_title, status, output[:500])
    return {"engine": "qoder", "status": status, "output": output,
            "duration_ms": duration, "recorded": recorded}

def run_local_task(task_title: str, prompt: str, model: str = ""):
    """Engine 3: validasi mikro offline lewat Ollama.

    Kalau Ollama tidak ada, statusnya UNAVAILABLE — bukan COMPLETED. Status palsu di sini
    membuat AI lain mengira kode sudah divalidasi padahal tidak ada satu pun pemeriksaan
    yang berjalan.
    """
    model = model or LOCAL_MODEL
    start = time.time()
    try:
        import urllib.request
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({"model": model, "prompt": prompt, "stream": False}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=LOCAL_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        output = (data.get("response") or "").strip()
        status = "COMPLETED" if output else "FAILED"
        if not output:
            output = "Ollama menjawab kosong — tidak ada hasil validasi."
    except Exception as exc:  # noqa: BLE001
        output = (f"Engine lokal tidak aktif ({type(exc).__name__}: {str(exc)[:160]}). "
                  f"Ollama belum berjalan atau model '{model}' belum di-pull. "
                  f"Tugas ini BELUM divalidasi.")
        status = "UNAVAILABLE"
    duration = int((time.time() - start) * 1000)
    recorded = record_task_to_db("local", model, task_title, status, output[:500])
    return {"engine": "local", "status": status, "output": output,
            "duration_ms": duration, "recorded": recorded}

def record_task_to_db(cli_engine, model, title, status, summary):
    """Catat hasil ke save-state lewat adapter dual-engine resmi (PostgreSQL primer).

    Versi lama membuka sqlite3 langsung ke workstation.db, jadi hasil dispatch masuk ke
    basis data yang tidak dibaca daemon — memori terbelah dua — dan tiap kegagalan tulis
    ditelan `except: pass` sehingga hilang tanpa jejak.
    """
    try:
        record_quest_task(cli_engine, model, title, status, summary)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"[tri-engine] GAGAL mencatat '{title}' [{status}]: {exc}\n")
        return False
    return True

def dispatch_parallel_swarm(task_antigravity: dict, task_qoder: dict, task_local: dict = None):
    results = {}
    threads = []

    def _worker_anti():
        results["antigravity"] = run_antigravity_task(
            task_antigravity.get("title", "Antigravity Architectural Task"),
            task_antigravity.get("prompt", "")
        )

    def _worker_qoder():
        results["qoder"] = run_qoder_task(
            task_qoder.get("title", "Qoder Code Synthesis Task"),
            task_qoder.get("prompt", "")
        )

    def _worker_local():
        if task_local:
            results["local"] = run_local_task(
                task_local.get("title", "Local RTX Micro Task"),
                task_local.get("prompt", "")
            )

    t1 = threading.Thread(target=_worker_anti)
    t2 = threading.Thread(target=_worker_qoder)
    threads.extend([t1, t2])
    if task_local:
        t3 = threading.Thread(target=_worker_local)
        threads.append(t3)

    for t in threads:
        t.start()
    for t in threads:
        t.join()

    return results

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "test":
        print("Testing Tri-Engine Parallel Dispatcher...")
        res = dispatch_parallel_swarm(
            task_antigravity={"title": "Test GDD Architecture", "prompt": "Jawab 1 kalimat: Arsitektur Game RPG Player state."},
            task_qoder={"title": "Test Code Generation", "prompt": "Jawab 1 kalimat: Script C# PlayerController."},
            task_local={"title": "Test Micro Validation", "prompt": "Cek sintaks validasi."}
        )
        print(json.dumps(res, indent=2))
