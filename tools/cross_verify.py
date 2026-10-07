#!/usr/bin/env python3
"""
cross_verify.py — verifikasi lintas mesin: satu AI mengaudit pekerjaan AI lain.

Alasan keberadaan: pemilik workstation ini tidak butuh IDE. Kalau satu mesin kekurangan
keahlian untuk memverifikasi hasilnya, yang memverifikasi adalah mesin LAIN, bukan manusia
dan bukan tebakan. Ini juga jalan keluar untuk "AI HALU": klaim yang tidak lolos audit
mesin kedua tercatat sebagai ketidaksepakatan, bukan sebagai kebenaran.

Registry mesin dibaca dari ~/.ai-station/config/engines.json bila ada, sehingga menambah
perangkat (Local LLM 7-9B, API lain) cukup dengan menyunting konfigurasi — tanpa mengubah
berkas ini.

  ./cross_verify.py --list
  ./cross_verify.py gemini --file ../engine-rust/src/main.rs
  ./cross_verify.py gemini "Fungsi pg_conn_str() sudah aman karena membaca db_local.json"
  ./cross_verify.py qoder --file tools/session_ingestor.py --remember
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

AI_STATION = Path.home() / ".ai-station"
REGISTRY_PATH = AI_STATION / "config" / "engines.json"
TIMEOUT = int(os.environ.get("STATION_VERIFY_TIMEOUT", "900"))

# Bawaan. Nilai ini HANYA dipakai bila engines.json tidak ada.
DEFAULT_ENGINES = {
    "gemini": {
        "kind": "pane", "tab": "antigravity", "role": "reviewer",
        "model": "gemini-3.8-flash-high",
        "note": "Lewat pane Antigravity yang sudah hidup (hangat). "
                "Proses 'agy -p' baru butuh >100 detik hanya untuk login+bangun konteks.",
    },
    "qoder": {
        "kind": "cli", "binary": "qoder", "role": "builder",
        "model": "Qwen3.8-Flash",
        "template": ["qoder", "-m", "{model}", "--context-window", "1000000",
                     "--reasoning-effort", "high", "--permission-mode", "bypass_permissions",
                     "-p", "{prompt}"],
        "note": "Konteks 1M; cocok menelaah berkas besar.",
    },
    "local": {
        "kind": "ollama", "role": "validator", "model": "qwen2.5-coder:7b",
        "url": "http://127.0.0.1:11434/api/generate",
        "note": "Belum terpasang. Akan dipakai sebagai pemeriksa sintaks murah & offline.",
    },
}

REVIEW_BRIEF = """Kamu adalah PEMERIKSA BEBAS untuk Irsofka AI Workstation. Tugas kamu bukan
menyetujui, melainkan mencari kesalahan. Periksa klaim/berikut, lalu jawab WAJIB dengan format:

VERDICT: SETUJU | PERLU_KOREKSI | TIDAK_YAKIN
ALASAN: <2-5 kalimat>
TEMUAN: <daftar masalah konkret, atau 'tidak ada'>
BUKTI: <baris/fungsi/perintah yang kamu rujuk>

Jangan menyebut diri kamu membantu atau sopan. Kalau bukti tidak cukup untuk memastikan,
pakai TIDAK_YAKIN — itu jawaban yang berharga, bukan kegagalan.

=== YANG PERLU DIPERIKSA ===
"""


def load_registry():
    """Gabung bawaan dengan engines.json. Config pengguna selalu menang."""
    reg = dict(DEFAULT_ENGINES)
    if REGISTRY_PATH.exists():
        try:
            extra = json.loads(REGISTRY_PATH.read_text())
            for name, spec in (extra.get("engines", extra) or {}).items():
                base = dict(reg.get(name, {}))
                base.update(spec)
                reg[name] = base
        except Exception as exc:  # noqa: BLE001
            print(f"[verify] engines.json tidak terbaca ({exc}); pakai bawaan", file=sys.stderr)
    return reg


def treasury_quota():
    """(baris kuota, batas umur pembacaan) dari Treasury. (None, None) kalau tak ada sumber.

    Sumbernya API daemon; kalau daemon tidak hidup, tanyakan langsung ke ingestor. Keduanya
    query, bukan angka yang kita karang: gate kuota yang menebak lebih buruk daripada gate
    yang tidak ada, karena ia mengizinkan pemanggilan model sambil berpura-pura sudah
    memeriksa sisanya.
    """
    payload = None
    try:
        payload = json.loads(daemon_get("/api/treasury", timeout=20))
    except Exception:  # noqa: BLE001
        payload = None
    if payload is None:
        try:
            sys.path.insert(0, str(AI_STATION / "tools"))
            from session_ingestor import Store, latest_quota
            payload = {"quota": latest_quota(Store()), "interval_s": 900}
        except Exception:  # noqa: BLE001
            return None, None
    interval = payload.get("interval_s") or 900
    return payload.get("quota") or [], max(2 * int(interval), 1800)


def _finite(value):
    """float hanya untuk angka yang benar-benar angka: None, teks, NaN dan inf semua gagal."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _age_seconds(ts):
    """Umur sebuah pembacaan meter, atau None kalau cap waktunya tidak bisa dibaca."""
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.astimezone()
    return (datetime.now().astimezone() - when).total_seconds()


def quota_gate_reason(spec):
    """Alasan mesin ini TIDAK boleh diberangkatkan, atau None kalau jendelanya masih ada.

    Pemilik mesin memutuskan kolam Anthropic/GPT boleh dihabiskan untuk verifikasi — tapi
    keputusannya bersyarat: 'selama kuota 5 hours ada dan weekly nya ada'. Syarat yang tidak
    dibaca adalah syarat yang dilanggar diam-diam, jadi setiap jalan yang membuat kita tidak
    punya bukti (state bukan laporan, angka tak terbaca, cap waktu hilang atau dari masa
    depan, baris lama menggeser yang baru) berujung penolakan, bukan keberangkatan.
    """
    gate = spec.get("quota_gate")
    if not gate:
        return None
    engine, scope = str(gate.get("engine") or ""), str(gate.get("scope") or "")
    windows = list(gate.get("windows") or [])
    floor = _finite(gate.get("min_fraction", 0.0)) or 0.0
    rows, fresh_s = treasury_quota()
    if rows is None:
        return (f"kuota {engine}/{scope} tidak terbaca: daemon mati dan ingestor tidak "
                "menghasilkan angka — tidak ada bukti jendela ini masih ada")
    newest = {}
    for r in rows:
        if str(r.get("engine")) != engine or str(r.get("scope")) != scope:
            continue
        w = str(r.get("limit_window"))
        if w not in newest or str(r.get("ts") or "") > str(newest[w].get("ts") or ""):
            newest[w] = r
    for w in windows:
        r = newest.get(w)
        if r is None:
            return (f"meter tidak pernah melaporkan jendela {w} untuk {engine}/{scope} — "
                    "tidak ada dasar untuk memberangkatkan mesin ini")
        state = str(r.get("state") or "")
        if state != "REPORTED":
            return (f"pembacaan terakhir jendela {w} berstate {state or 'kosong'}, bukan "
                    "REPORTED: tidak ada bukti kuota")
        frac = _finite(r.get("remaining_fraction"))
        if frac is None:
            return f"jendela {w} dilaporkan tanpa angka sisa yang bisa dibaca"
        if frac <= floor:
            return f"jendela {w} tinggal {frac:.3f} (habis)"
        age = _age_seconds(r.get("ts"))
        if age is None:
            return f"pembacaan jendela {w} tidak punya cap waktu yang bisa dibaca"
        if age < 0:
            return (f"pembacaan jendela {w} berumur {age / 60:.0f} menit — jam mesin dan "
                    "cap meter tidak sepakat, jadi kesegarannya tidak bisa dibuktikan")
        if age > fresh_s:
            return (f"pembacaan jendela {w} berumur {age / 60:.0f} menit, lebih tua dari "
                    f"2x interval meter ({fresh_s / 60:.0f} menit)")
    return None


def engine_present(spec):
    """Apakah mesin ini BENAR-BENAR bisa dipakai sekarang.

    Menebak dari 'kind' akan melaporkan 'ada' untuk Ollama yang tidak terpasang —
    persis jenis kebohongan yang membuat AI mengira sesuatu sudah diverifikasi.
    """
    kind = spec.get("kind", "cli")
    if kind == "pane":
        tab = spec.get("tab", "antigravity")
        try:
            data = json.loads(daemon_get("/api/workspace"))
            info = (data.get("tabs") or {}).get(tab) or {}
            if not info.get("alive"):
                return False, f"pane '{tab}' tidak hidup di workstation"
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"daemon tidak menjawab ({type(exc).__name__})"
    if kind == "cli":
        if not shutil.which(spec.get("binary", "")):
            return False, ""
        blocked = quota_gate_reason(spec)
        return (False, blocked) if blocked else (True, "")
    if kind == "ollama":
        # Probe dan pemanggilan HARUS memakai sumber URL yang sama. Dulu probe membaca
        # spec.get("url","") tanpa bawaan, sehingga mesin yang hanya ada di engines.json
        # (tidak punya entri bawaan) selalu dianggap mati padahal sebenarnya hidup.
        gen_url = spec.get("url") or "http://127.0.0.1:11434/api/generate"
        url = gen_url.replace("/api/generate", "/api/tags")
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            have = {m.get("name") for m in data.get("models", [])}
            want = spec.get("model", "")
            if want and want not in have:
                # Cocokkan nama tag persis. Pencocokan "keluarga" (qwen3.5:9b dianggap ada
                # karena qwen3.5:4b terpasang) melapor 'siap' untuk mesin yang pasti gagal.
                return False, f"server hidup, model '{want}' belum di-pull (ada: {', '.join(sorted(h for h in have if h)) or '-'})"
            return True, ""
        except Exception as exc:  # noqa: BLE001
            return False, f"server tidak menjawab ({type(exc).__name__})"
    return True, ""


def build_prompt(subject, focus):
    parts = [REVIEW_BRIEF]
    if focus:
        parts.append(f"Fokus pemeriksaan: {focus}\n\n")
    parts.append(subject)
    return "".join(parts)


def daemon_get(path, timeout=10):
    url = f"http://127.0.0.1:{os.environ.get('STATION_PORT', '8999')}{path}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def daemon_post(path, payload, timeout=10):
    url = f"http://127.0.0.1:{os.environ.get('STATION_PORT', '8999')}{path}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07|\r")


def strip_ansi(text):
    return ANSI_RE.sub("", text or "")



SPINNER_CHARS = "⠁⠂⠄⡀⢀⠐⠈⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⢿⣟⣷"


def last_frame(raw):
    """Ambil frame render TERAKHIR dari scrollback.

    Antigravity tidak mencetak teks secara kumulatif; ia menggambar ulang barisnya
    dengan ESC[2A (naik 2 baris). Menggabungkan seluruh history menghasilkan ribuan
    potongan kata yang sama berulang, dan panjangnya akan terus berubah sehingga
    deteksi 'teks sudah stabil' tidak pernah selesai.
    """
    parts = re.split(r"\x1b\[2A|\x1b\[1A", raw)
    return parts[-1] if parts else raw




def pane_is_busy(frame):
    return any(ch in frame for ch in SPINNER_CHARS) or "esc to cancel" in frame.lower()


def capture_pane(tab):
    """Baca layar pane lewat tmux capture-pane.

    Jauh lebih andal daripada mengurai /api/term/history: buffer itu berisi ribuan frame
    redraw berisi ANSI mentah, sedangkan capture-pane mengembalikan TAMPILAN TERAKHIR
    sebagai teks bersih. Ini juga tidak menggeser offset baca daemon, jadi GUI tidak
    kehilangan byte-nya.
    """
    try:
        res = subprocess.run(
            ["tmux", "-L", os.environ.get("STATION_TMUX_SOCKET", "irsofka"),
             "capture-pane", "-p", "-t", f"station-{tab}"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15)
        if res.returncode != 0:
            return None
        return res.stdout
    except Exception:  # noqa: BLE001
        return None


def pane_idle(screen):
    """Pane dianggap selesai kalau tidak ada spinner dan prompt input kosong terlihat."""
    if not screen:
        return False
    if any(ch in screen for ch in SPINNER_CHARS) or "esc to cancel" in screen.lower():
        return False
    return ("? for shortcuts" in screen) or bool(re.search(r"^\s*>\s*$", screen, re.M))


def tail_answer(screen, marker):
    """Ambil isi setelah prompt terkirim, buang kerangka UI pane."""
    lines = [l.rstrip() for l in screen.splitlines()]
    idx = max((i for i, l in enumerate(lines) if marker in l), default=-1)
    body = lines[idx + 1:] if idx >= 0 else lines
    out = []
    for l in body:
        s = l.strip()
        if not s or s.startswith("? for shortcuts") or set(s) <= set("─━>-│ "):
            continue
        if re.match(r"^(>\s*|.*·\s*(Gemini|Qwen|Claude)[\w .·-]*$)", s) and len(s) < 60 and "VERDICT" not in s.upper():
            continue
        out.append(s)
    return "\n".join(out).strip()


def run_via_pane(tab, prompt, timeout):
    """Kirim tugas ke CLI yang SUDAH HIDUP di pane tmux, lalu baca jawabannya.

    Kenapa tidak spawns `agy -p` baru: proses dingin butuh >100 detik hanya untuk login dan
    membangun konteks (sumber kegagalan dispatch Antigravity selama ini), dan yang lebih
    penting: kehilangan sesi hangat yang sudah punya riwayat percakapan. Submission memakai
    /api/cli/run yang sejak 2026-10-06 mengirim \r, bukan \n — \n tidak pernah dianggap
    Enter oleh TUI, sehingga prompt hanya menumpuk di kotak input.
    """
    import uuid
    marker = f"XV-{uuid.uuid4().hex[:10]}"
    body = f"{marker} {prompt}"[:6000]
    try:
        daemon_post("/api/cli/run", {"prompt": body, "target_cli": tab})
    except Exception as exc:  # noqa: BLE001
        return "UNAVAILABLE", (f"Tidak bisa mengirim ke pane '{tab}' (daemon tidak menjawab): {exc}. "
                               "Hasil ini BELUM diverifikasi mesin kedua.")

    started = time.time()
    seen_prompt = False
    quiet_since, best = None, ""
    while time.time() - started < timeout:
        time.sleep(1.5)
        screen = capture_pane(tab)
        if screen is None:
            return "UNAVAILABLE", f"tmux capture-pane gagal untuk tab {tab}."
        if marker in screen:
            seen_prompt = True
        if not seen_prompt:
            continue
        if not pane_idle(screen):
            quiet_since = None
            continue
        answer = tail_answer(screen, marker)
        if not answer:
            continue
        best = answer
        if quiet_since is None:
            quiet_since = time.time()
        elif time.time() - quiet_since >= 3:
            return "COMPLETED", answer[:8000]
    if best:
        return "TIMEOUT", "Jawaban belum selesai. Terakhir terbaca:\n\n" + best[:4000]
    return "TIMEOUT", (f"Pane '{tab}' tidak menampilkan jawaban dalam {timeout}s. "
                       "Prompt sudah dikirim; cek tab Antigravity untuk melihat statusnya.")


def clean_answer(frame):
    """Rapikan frame terminal: buang garis tepi UI dan baris duplikat hasil redraw."""
    out, seen = [], set()
    for line in frame.splitlines():
        line = line.strip(" │┃┏┓┗┛┃─═")
        if not line or line.startswith("[?") or line.startswith("[9"):
            continue
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return "\n".join(out).strip()


def run_engine(spec, prompt, timeout):
    """Jalankan satu mesin. Status jujur: UNAVAILABLE kalau memang tidak ada."""
    started = time.time()
    kind = spec.get("kind", "cli")

    if kind == "pane":
        status, out = run_via_pane(spec.get("tab", "antigravity"), prompt, timeout)
        return status, out

    if kind == "ollama":
        try:
            # num_ctx TIDAK boleh diwarisi dari default Ollama (4096). Pemeriksaan kode
            # menghasilkan jawaban panjang + konteks berkas, dan kehabisan konteks tampil
            # sebagai "jawaban kosong" yang mudah disalahartikan sebagai model bodoh.
            opts = {"num_ctx": int(spec.get("num_ctx", 12288)),
                    "num_predict": int(spec.get("num_predict", 1200)),
                    "temperature": float(spec.get("temperature", 0.2))}
            req = urllib.request.Request(
                spec.get("url") or "http://127.0.0.1:11434/api/generate",
                data=json.dumps({"model": spec.get("model"), "prompt": prompt,
                                 "stream": False, "options": opts,
                                 # qwen3.5 adalah model berpikir: tanpa think=False, seluruh
                                 # anggaran token habis di kolom `thinking` dan `response`
                                 # kembali KOSONG — terlihat seperti model bodoh padahal
                                 # kehabisan ruang. Pemeriksa kode butuh jawaban, bukan monolog.
                                 "think": bool(spec.get("think", False)),
                                 # Aturan GPU workstation: jangan menahan VRAM. Tanpa ini
                                 # model tinggal di kartu sampai 5 menit setelah pemeriksaan.
                                 "keep_alive": spec.get("keep_alive", 0)}).encode(),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode())
            out = (data.get("response") or "").strip()
            if out:
                return "COMPLETED", out
            # Jawaban kosong bukan "gagal periksa" tanpa sebab: sertakan apa yang server
            # bilang, supaya mesin/pembaca tahu bedanya model menolak, kehabisan token,
            # atau endpoint tidak cocok.
            sebab = data.get("error") or ", ".join(f"{k}={str(v)[:40]}" for k, v in data.items()
                                                   if k in ("done_reason", "prompt_eval_count", "eval_count"))
            cuma_pikir = len((data.get("thinking") or "").strip())
            if cuma_pikir and not out:
                sebab += f" · model hanya menghasilkan monolog berpikir ({cuma_pikir} karakter) tanpa jawaban"
            return "FAILED", (f"Model {spec.get('model')} menjawab kosong"
                              + (f" ({sebab})" if sebab else "") + ". Hasil ini BELUM diverifikasi.")
        except Exception as exc:  # noqa: BLE001
            return "UNAVAILABLE", (f"Engine lokal tidak aktif ({type(exc).__name__}: {exc}). "
                                   "Hasil ini BELUM diverifikasi oleh mesin kedua.")

    binary = spec.get("binary", "")
    if not shutil.which(binary):
        return "UNAVAILABLE", f"Perintah '{binary}' tidak ditemukan di PATH."

    cmd = [c.replace("{model}", str(spec.get("model", "")))
             .replace("{prompt}", prompt) for c in spec["template"]]
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, timeout=timeout)
        out = (res.stdout or "").strip()
        if res.returncode != 0:
            return "FAILED", out or f"exit {res.returncode} tanpa output"
        if not out:
            return "FAILED", "(mesin tidak menghasilkan output)"
        # 'exit 0 + teks' BUKAN bukti pemeriksaan. Terukur 2026-10-07: `agy -p` headless
        # menolak tool yang minta izin, mengembalikan response berisi penolakannya, dan
        # exit 0 — audit seperti itu tercatat COMPLETED padahal tidak ada yang diperiksa.
        # Format VERDICT adalah satu-satunya tanda mesin itu benar-benar menjawab brief.
        reply = unwrap_cli_response(out)
        if "VERDICT" not in reply.upper():
            return "FAILED", (out + "\n\n(mesin menjawab tanpa baris VERDICT: tidak ada "
                              "pemeriksaan yang bisa dicatat sebagai terverifikasi)")
        return "COMPLETED", out
    except subprocess.TimeoutExpired:
        return "TIMEOUT", f"Batas {timeout}s terlampaui; pemeriksaan tidak selesai."
    except Exception as exc:  # noqa: BLE001
        return "FAILED", f"{type(exc).__name__}: {exc}"


def unwrap_cli_response(text):
    """Ambil isi jawaban dari sampul yang dicetak CLI (--output-format json).

    Hanya dibuka kalau teksnya memang JSON dengan kunci 'response'; teks polos lewat utuh.
    """
    try:
        first = text.split("\n", 1)[0]
        d = json.loads(first) if first.lstrip().startswith("{") else None
    except Exception:  # noqa: BLE001
        return text
    if isinstance(d, dict) and isinstance(d.get("response"), str):
        return d["response"]
    return text


def parse_verdict(text):
    """Ambi verdict TERAKHIR, bukan pertama.

    Layar pane menyimpan jawaban sebelumnya di scrollback. Mengambil kecocokan pertama
    membuat audit mencatat verdict dari pemeriksaan lama — salah catat yang tampak meyakinkan.
    """
    found = "TIDAK_YAKIN"
    for line in (text or "").splitlines():
        u = line.upper()
        if not u.startswith("VERDICT"):
            continue
        if "PERLU_KOREKSI" in u:
            found = "PERLU_KOREKSI"
        elif "TIDAK_YAKIN" in u:
            found = "TIDAK_YAKIN"
        elif "SETUJU" in u:
            found = "SETUJU"
    return found


def record(subject_ref, engine, model, status, verdict, text, duration_ms):
    """Catat hasil verifikasi ke action_log supaya ketidaksepakatan antar-mesin jadi memori."""
    try:
        sys.path.insert(0, str(AI_STATION / "tools"))
        from session_ingestor import Store
        store = Store()
        store.insert_event({
            "ts": datetime.now().isoformat(), "engine": "station", "session_id": None,
            "turn_id": None, "tab": engine, "kind": "cross_verify", "model": model,
            "effort": "", "cwd": os.getcwd(), "tool": "cross_verify",
            "summary": f"verifikasi oleh {engine}: {status} verdict={verdict} :: "
                       f"{(text or '')[:200]}",
            "exit_code": 0 if status == "COMPLETED" else 1,
            "duration_ms": duration_ms, "raw_ref": str(subject_ref),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] hasil tidak tercatat ke SQL: {exc}", file=sys.stderr)
        return False


def main():
    ap = argparse.ArgumentParser(description="Verifikasi lintas mesin")
    ap.add_argument("engine", nargs="?",
                    help="kunci mesin dari config/engines.json — jalankan `--list` untuk melihat "
                         "yang terdaftar dan mana yang sedang terpasang")
    # nargs='*' supaya subjek boleh ditulis sebelum ATAU sesudah opsi (--focus/--timeout);
    # argparse menolak dua posisi opsional yang disela opsi.
    ap.add_argument("subject", nargs="*", default=[], help="klaim/teks yang diperiksa")
    ap.add_argument("--file", help="berkas yang diperiksa (isinya dikirim sebagai subjek)")
    ap.add_argument("--focus", default="", help="sudut yang harus diperiksa")
    ap.add_argument("--timeout", type=int, default=TIMEOUT)
    ap.add_argument("--remember", action="store_true", help="catat ke action_log (default: ya)")
    ap.add_argument("--list", action="store_true")
    args, extra = ap.parse_known_args()
    # argparse menghabiskan pola posisi pada kelompok pertama, lalu sisa teks jadi 'extra'.
    # Kita serap kembali token non-flag sebagai subjek; flag asing tetap error.
    leftovers = [t for t in extra if not t.startswith("-")]
    flags = [t for t in extra if t.startswith("-")]
    if flags:
        ap.error(f"opsi tak dikenal: {' '.join(flags)}")
    if leftovers:
        args.subject = list(args.subject) + leftovers

    reg = load_registry()
    if args.list or not args.engine:
        print("=== mesin verifikasi tersedia ===")
        for name, spec in sorted(reg.items()):
            ok, why = engine_present(spec)
            state = why if why else ("ada" if ok else "TIDAK terpasang")
            print(f"  {name:<14} {spec.get('role','?'):<10} {spec.get('model',''):<24} "
                  f"{state:<34} {spec.get('note','')}")
        print(f"\n  registry tambahan: {REGISTRY_PATH} "
              f"({'ada' if REGISTRY_PATH.exists() else 'belum ada — menambah mesin baru tidak perlu ubah kode'})")
        return 0

    spec = reg.get(args.engine)
    if not spec:
        print(f"❌ mesin '{args.engine}' tidak ada di registry. Pilihan: {', '.join(sorted(reg))}")
        return 2

    if args.file:
        path = Path(args.file).expanduser()
        if not path.is_file():
            print(f"❌ berkas tidak ditemukan: {path}")
            return 2
        body = path.read_text(errors="replace")
        if len(body) > 120000:
            body = body[:120000] + "\n...[dipotong; kirim bagian tertentu lewat --focus]"
        subject = f"--- {path} ({len(body)} karakter) ---\n{body}"
        ref = str(path)
    else:
        subject = " ".join(args.subject).strip()
        ref = "klaim-tekstual"
    if not subject.strip():
        print("❌ beri subjek: teks klaim atau --file <path>")
        return 2

    blocked = quota_gate_reason(spec)
    if blocked:
        print(f"❌ {args.engine} tidak diberangkatkan: {blocked}")
        record(ref, args.engine, spec.get("model", ""), "UNAVAILABLE", "UNAVAILABLE",
               blocked, 0)
        print("\n⚠ Klaim ini BELUM diverifikasi mesin kedua. Jangan catat sebagai terverifikasi.")
        return 3

    started = time.time()
    status, text = run_engine(spec, build_prompt(subject, args.focus), args.timeout)
    verdict = parse_verdict(text) if status == "COMPLETED" else status
    dur = int((time.time() - started) * 1000)
    saved = record(ref, args.engine, spec.get("model", ""), status, verdict, text, dur)

    print(f"=== hasil verifikasi oleh {args.engine} ({spec.get('model')}) ===")
    print(f"  status  : {status}")
    print(f"  verdict : {verdict}")
    print(f"  durasi  : {dur} ms   tercatat di SQL: {'ya' if saved else 'TIDAK'}")
    print("  ---")
    print(text[:4000])
    if status == "UNAVAILABLE":
        print("\n⚠ Klaim ini BELUM diverifikasi mesin kedua. Jangan catat sebagai terverifikasi.")
        return 3
    return 0 if verdict == "SETUJU" else 1


if __name__ == "__main__":
    sys.exit(main())
