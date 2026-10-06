#!/usr/bin/env python3
"""model_check.py — nilai satu model Ollama terhadap kebutuhan workstation ini.

Dipakai untuk menjawab satu pertanyaan dengan angka: LAYAK dipertahankan di disk, atau
hapus. Yang dinilai bukan "pintar atau tidak", tapi empat hal yang benar-benar dipakai
mesin ini: muat di VRAM, lepas kembali setelah selesai, menjawab (tidak kosong), dan
cukup cepat untuk jadi pemeriksa.

  model_check.py qwen3.5:9b
  model_check.py qwen3.5:4b --prompt "Balas hanya: SIAP"
"""
import argparse, json, os, subprocess, sys, threading, time, urllib.request

URL = os.environ.get("STATION_OLLAMA_URL", "http://127.0.0.1:11434/api/generate")


def nvidia(field):
    try:
        out = subprocess.run(["nvidia-smi", f"--query-gpu={field}", "--format=csv,noheader,nounits"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=8).stdout
        return int(out.strip().splitlines()[0])
    except Exception:  # noqa: BLE001
        return None


def ollama_ps():
    try:
        out = subprocess.run(["ollama", "ps"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             text=True, timeout=15).stdout
    except Exception:  # noqa: BLE001
        return "?"
    rows = [l for l in out.splitlines()[1:] if l.strip()]
    return len(rows)


def sample_peak(stop, box):
    while not stop.is_set():
        v = nvidia("memory.used")
        if v is not None:
            box["peak_used"] = max(box.get("peak_used", 0), v)
        time.sleep(1.0)


def run(model, prompt, timeout):
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "keep_alive": 0}).encode()
    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode())
    return data, time.time() - t0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("model")
    ap.add_argument("--prompt", default="Sebutkan tiga alasan sebuah daemon harus memisahkan sesi "
                                        "kerjanya dari cgroup-nya sendiri. Jawab ringkas.")
    ap.add_argument("--timeout", type=int, default=600)
    args = ap.parse_args()

    free_before, used_before = nvidia("memory.free"), nvidia("memory.used")
    if free_before is None:
        print("nvidia-smi tidak tersedia — penilaian VRAM tidak bisa dilakukan.", file=sys.stderr)
        return 2
    print(f"sebelum : {used_before} MiB terpakai · {free_before} MiB bebas · model terpasang di GPU: {ollama_ps()}")

    stop, box = threading.Event(), {}
    th = threading.Thread(target=sample_peak, args=(stop, box), daemon=True)
    th.start()
    err = None
    try:
        data, dur = run(args.model, args.prompt, args.timeout)
    except Exception as exc:  # noqa: BLE001
        err, data, dur = f"{type(exc).__name__}: {exc}", {}, 0.0
    finally:
        stop.set(); th.join(timeout=3)

    peak = box.get("peak_used", used_before)
    answer = (data.get("response") or "").strip()
    ev = data.get("eval_count") or 0
    tps = ev / dur if dur and ev else 0
    loaded_after = ollama_ps()
    used_after = nvidia("memory.used") or 0

    checks = [
        ("menjawab tidak kosong", bool(answer)),
        (f"muat di VRAM (puncak {peak} MiB dari {free_before + used_before} MiB kartu)",
         peak > used_before and peak <= free_before + used_before),
        (f"GPU LEPAS setelah selesai (model tertinggal: {loaded_after})", loaded_after == 0),
        (f"VRAM kembali ({used_after} MiB vs {used_before} MiB awal)",
         used_after <= used_before + 400),
        (f"kecepatan cukup untuk pemeriksa ({tps:.1f} tok/s, {dur:.0f} s)", tps >= 4 and dur <= args.timeout * 0.9),
    ]
    if err:
        checks.insert(0, (f"GAGAL dimuat — {err}", False))

    print(f"jawaban : {answer[:150] or '(kosong)'}")
    print("penilaian:")
    for label, ok in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
    good = sum(1 for _, ok in checks if ok)
    print(f"\n{good}/{len(checks)} kriteria lolos")
    if good == len(checks):
        print("→ layak dipertahankan di disk.")
    elif any("GAGAL" in l or "tidak kosong" in l for l, ok in checks if not ok):
        print("→ tidak berguna dalam bentuk ini: hapus (ollama rm) atau ganti tag.")
    else:
        print("→ menumpang ruang: pertahankan hanya kalau ada peran yang tidak bisa diisi model lain.")
    return 0 if good == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
