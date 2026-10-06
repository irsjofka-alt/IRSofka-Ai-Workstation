#!/usr/bin/env python3
"""
local_llm.py — pemanggil Ollama yang TIDAK PERNAH menahan GPU.

Aturan mesin pemilik: GPU adalah barang rebutan (Unity, ComfyUI, desktop). Local LLM
hanya tamu yang mampir sebentar untuk memverifikasi kode, lalu harus PERGI.

Karena itu berkas ini punya tiga penjaga, bukan satu:
  1. GERBANG VRAM  — sebelum memuat, hitung VRAM kosong. Kalau tidak cukup, tolak atau
                     turun ke model kecil. Mencegah tabrakan dengan Unity/Comfy di hulu.
  2. KEEP_ALIVE=0  — Ollama membuang model dari VRAM begitu respons selesai, bukan
                     menahannya 5 menit seperti bawaannya.
  3. `ollama stop` — penjaga kedua, plus verifikasi: kalau VRAM masih tinggi setelah
                     selesai, kita LAPORKAN, bukan diam-diam pura-pura bersih.

Model dipilih dari config/engines.json (tier), bukan dikeras di sini.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

AI_STATION = Path.home() / ".ai-station"
REGISTRY = AI_STATION / "config" / "engines.json"
OLLAMA_URL = os.environ.get("STATION_OLLAMA_URL", "http://127.0.0.1:11434")

# Cadangan bila engines.json belum ada.
DEFAULT_TIERS = {
    "light": {"model": "qwen3.5:4b", "min_free_mib": 3000,
              "use": "pemeriksa cepat & cadangan darurat; ~2,5 GB"},
    "verify": {"model": "qwen3.5:9b", "min_free_mib": 6500,
               "use": "pemeriksa kode sehari-hari; ~5,5 GB, masih menyisakan ruang Unity"},
    "heavy": {"model": "phi4:14b", "min_free_mib": 10500,
              "use": "penalaran berat; ~9 GB. HANYA saat tidak ada aplikasi GPU lain."},
}


def vram_free_mib():
    """VRAM kosong dalam MiB. None kalau tidak bisa dibaca (GPU AMD / driver bermasalah)."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=6).stdout.strip()
        total, used = [int(x.strip()) for x in out.split(",")[:2]]
        return max(0, total - used)
    except Exception:  # noqa: BLE001
        return None


def ram_available_mib():
    """RAM yang benar-benar tersedia (MemAvailable, bukan MemFree)."""
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except Exception:  # noqa: BLE001
        pass
    return None


def gpu_holders():
    """Proses yang sedang memegang VRAM — untuk menjelaskan PENOLAKAN, bukan sekadar menolak."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=6).stdout
        holders = []
        for line in out.strip().splitlines():
            if not line.strip():
                continue
            pid, mem = line.split(",")[:2]
            name = ""
            try:
                name = Path(f"/proc/{int(pid.strip())}/comm").read_text().strip()
            except OSError:
                pass
            holders.append((name or f"pid{pid.strip()}", int(mem.strip())))
        return holders
    except Exception:  # noqa: BLE001
        return []


def load_tiers():
    tiers = dict(DEFAULT_TIERS)
    if REGISTRY.exists():
        try:
            data = json.loads(REGISTRY.read_text())
            for name, spec in (data.get("local_tiers", {}) or {}).items():
                base = dict(tiers.get(name, {}))
                base.update(spec)
                tiers[name] = base
        except Exception as exc:  # noqa: BLE001
            print(f"[llm] engines.json tidak terbaca ({exc}); pakai bawaan", file=sys.stderr)
    return tiers


def ollama_ready():
    if not shutil.which("ollama"):
        return False, "perintah 'ollama' tidak terpasang"
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as resp:
            data = json.loads(resp.read().decode())
        return True, {m.get("name", "") for m in data.get("models", [])}
    except Exception as exc:  # noqa: BLE001
        return False, f"server ollama tidak menjawab ({type(exc).__name__})"


def unload(model):
    """Paksa lepas dari VRAM, lalu konfirmasi benar-benar lepas."""
    before = vram_free_mib()
    try:
        subprocess.run(["ollama", "stop", model], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1.2)
    after = vram_free_mib()
    return before, after


def ask(prompt, model, timeout=600, system="", cpu_only=False):
    # num_gpu=0 = inference penuh di CPU/RAM: VRAM TIDAK disentuh sama sekali, jadi
    # Unity/ComfyUI dapat GPU utuh. Bayarnya: jauh lebih lambat (bandwidth DDR4).
    options = {"temperature": 0.2, "num_ctx": 8192}
    if cpu_only:
        options["num_gpu"] = 0
    payload = {"model": model, "prompt": prompt, "stream": False,
               "keep_alive": 0, "options": options}
    if system:
        payload["system"] = system
    req = urllib.request.Request(f"{OLLAMA_URL}/api/generate",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def main():
    ap = argparse.ArgumentParser(description="Panggil Local LLM dengan pelepasan GPU wajib")
    ap.add_argument("prompt", nargs="?", default="", help="isi yang ditanyakan")
    ap.add_argument("--tier", default="verify", choices=["light", "verify", "heavy"])
    ap.add_argument("--model", default="", help="paksa model tertentu (menembus tier)")
    ap.add_argument("--allow-small", action="store_true",
                    help="turunkan ke tier lebih ringan bila VRAM tidak cukup, jangan tolak")
    ap.add_argument("--cpu", action="store_true",
                    help="paksa inference di CPU/RAM; VRAM tidak disentuh sama sekali")
    ap.add_argument("--strict-gpu", action="store_true",
                    help="jangan pernah jatuh ke mode CPU; tolak saja")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--status", action="store_true", help="lapor VRAM, model terpasang, holders")
    args = ap.parse_args()

    tiers = load_tiers()

    if args.status:
        free = vram_free_mib()
        ok, info = ollama_ready()
        print(f"  VRAM kosong   : {free if free is not None else '?'} MiB")
        print(f"  RAM tersedia  : {ram_available_mib() or '?'} MiB  (mode CPU memakai ini, bukan VRAM)")
        holders = gpu_holders()
        print(f"  pemegang GPU  : {', '.join(f'{n}({m}MiB)' for n, m in holders[:6]) or 'tidak ada'}")
        print(f"  ollama        : {'hidup' if ok else info}")
        if ok:
            for t, spec in sorted(tiers.items()):
                mark = "ada" if spec["model"] in info else "belum di-pull"
                print(f"    tier {t:<7} {spec['model']:<16} {mark:<14} butuh {spec['min_free_mib']} MiB  — {spec.get('use','')}")
        return 0

    subject = args.prompt or sys.stdin.read()
    if not subject.strip():
        print("❌ beri prompt atau --status", file=sys.stderr)
        return 2

    ok, info = ollama_ready()
    if not ok:
        print(f"UNAVAILABLE: {info}. Hasil ini BELUM diverifikasi model lokal.")
        return 3

    installed = info
    order = ["heavy", "verify", "light"]
    start = order.index(args.tier) if args.tier in order else 1
    chosen = None
    if args.model:
        chosen = {"model": args.model, "min_free_mib": 0}
    else:
        for name in order[start:]:
            spec = tiers.get(name) or {}
            model = spec.get("model", "")
            if model not in installed:
                print(f"  lewati tier {name}: model '{model}' belum di-pull")
                continue
            free = vram_free_mib()
            need = int(spec.get("min_free_mib", 0))
            if free is not None and free < need:
                holders = ", ".join(f"{n} {m}MiB" for n, m in gpu_holders()[:4]) or "tidak diketahui"
                ram = ram_available_mib()
                want = int((spec.get("ram_mib") or need * 1.35))
                if ram is not None and ram >= want and not args.strict_gpu:
                    # VRAM tidak cukup, RAM cukup -> jalan di CPU. GPU dibiarkan utuh
                    # untuk Unity/ComfyUI; kita hanya lebih lambat.
                    print(f"  tier {name}: VRAM kurang ({free}/{need} MiB; pemegang: {holders})")
                    print(f"  -> pindah ke CPU/RAM (tersedia {ram} MiB, butuh ~{want} MiB). "
                          f"GPU tidak disentuh, tapi inference jauh lebih lambat.")
                    chosen = dict(spec); chosen["_cpu"] = True
                    print(f"  tier {name} -> {model} via CPU")
                    break
                print(f"❌ DITOLAK: VRAM {free} MiB < {need} MiB dan RAM tidak memadai "
                      f"({ram} MiB) untuk mode CPU.")
                print("   pakai --allow-small untuk turun tier, atau tutup aplikasi GPU lain.")
                return 4
            chosen = spec
            print(f"  tier {name} -> {model} (VRAM kosong {free} MiB)")
            break

    if not chosen:
        print("❌ tidak ada tier yang muat di VRAM saat ini. Coba --allow-small atau --status.")
        return 4

    model = chosen["model"]
    cpu_only = bool(chosen.get("_cpu")) or args.cpu
    if cpu_only:
        print(f"  mode: CPU/RAM (num_gpu=0) — VRAM dibiarkan untuk aplikasi lain")
    started = time.time()
    try:
        data = ask(subject, model, timeout=args.timeout, cpu_only=cpu_only)
    except Exception as exc:  # noqa: BLE001
        # Gagal pun tetap wajib melepas — model bisa saja sudah termuat sebagian.
        b, a = unload(model)
        print(f"FAILED: {type(exc).__name__}: {exc}  (GPU dilepas: {b} -> {a} MiB)")
        return 1
    dur = int((time.time() - started) * 1000)
    answer = (data.get("response") or "").strip()

    b, a = unload(model)
    leaked = ""
    if b is not None and a is not None and (a - b) < 500:
        leaked = f"  ⚠ VRAM TIDAK kembali ({b} -> {a} MiB) — periksa 'ollama ps'"
    dev = "CPU/RAM" if cpu_only else "GPU"
    print(f"=== {model} | {dev} | {dur} ms | VRAM {b} -> {a} MiB ===")
    print(answer or "(jawaban kosong)")
    if leaked:
        print(leaked)
    return 0


if __name__ == "__main__":
    sys.exit(main())
