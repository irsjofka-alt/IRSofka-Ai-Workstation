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
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from call_workers import run_grouped  # noqa: E402  (worker panggilan mati bersama pemanggilnya)

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
    if not isinstance(payload, dict):
        return None, None
    try:
        interval = int(payload.get("interval_s") or 900)
    except (TypeError, ValueError):
        # interval_s datang dari daemon, bukan dari config kita. Daemon yang melaporkan
        # angka raksasa tidak boleh memperlonggar batas kesegaran tanpa batas.
        interval = 900
    return payload.get("quota") or [], min(max(2 * interval, 1800), 6 * 3600)


def _finite(value):
    """float hanya untuk angka yang benar-benar angka: None, teks, NaN dan inf semua gagal."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _parse_ts(ts):
    """Cap waktu meter sebagai datetime sadar zona, atau None kalau tidak bisa dibaca."""
    if not ts:
        return None
    try:
        when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return when.astimezone() if when.tzinfo else when.replace(tzinfo=LocalZone())


def LocalZone():
    """Zona lokal, dipakai hanya untuk cap waktu yang ditulis tanpa offset."""
    return datetime.now().astimezone().tzinfo


def _rank_row(row):
    """Urutkan pembacaan per jendela: yang terbaru, dan pada detik sama yang LEMAH menang.

    Dua baris bertimestamp identik dengan state REPORTED dan UNAVAILABLE bukan kasus reka-reka:
    ia terjadi ketika satu siklus menulis dua meter untuk jendela yang sama. Menganggap yang
   REPORTED otomatis lebih baru berarti bukti yang tidak bisa dipercaya kalah diam-diam —
    fail-open persis yang dikira sudah ditutup.
    """
    when = _parse_ts(row.get("ts"))
    return (when is not None, when or datetime(1970, 1, 1, tzinfo=LocalZone()),
            str(row.get("state")) != "REPORTED")


def quota_gate_reason(spec):
    """Alasan mesin ini TIDAK boleh diberangkatkan, atau None kalau jendelanya masih ada.

    Pemilik mesin memutuskan kolam Anthropic/GPT boleh dihabiskan untuk verifikasi — tapi
    keputusannya bersyarat: 'selama kuota 5 hours ada dan weekly nya ada'. Setiap jalur yang
    membuat kita tidak punya bukti — gate tanpa jendela, ambang tidak terbaca, state bukan
    laporan, fraksi di luar 0..1, cap waktu hilang atau dari masa depan, pembacaan lebih tua
    dari 2x interval — berujung penolakan. Aturan yang gagal dibaca tidak boleh berubah
    menjadi 'boleh lewat'.
    """
    gate = spec.get("quota_gate")
    if not gate:
        return None
    if not isinstance(gate, dict):
        return "quota_gate bukan objek: tidak ada yang bisa diperiksa"
    engine, scope = str(gate.get("engine") or ""), str(gate.get("scope") or "")
    windows = [str(w) for w in (gate.get("windows") or [])]
    if not windows:
        return ("quota_gate tanpa daftar 'windows': tidak ada satu jendela pun yang bisa "
                "dibuktikan, jadi tidak ada dasar untuk memberangkatkan mesin ini")
    floor = _finite(gate.get("min_fraction", 0.0))
    if floor is None or not 0.0 <= floor <= 1.0:
        return (f"ambang 'min_fraction' {gate.get('min_fraction')!r} bukan pecahan 0..1 — "
                "aturan ambangnya sendiri rusak, dan gerbang tidak menebak")
    if not engine or not scope:
        return "quota_gate tanpa 'engine' atau 'scope': baris meter mana yang harus dipercaya?"
    rows, fresh_s = treasury_quota()
    if rows is None:
        return (f"kuota {engine}/{scope} tidak terbaca: daemon mati dan ingestor tidak "
                "menghasilkan angka — tidak ada bukti jendela ini masih ada")
    candidates = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get("engine")) == engine and str(r.get("scope")) == scope:
            candidates.setdefault(str(r.get("limit_window")), []).append(r)
    for w in windows:
        group = candidates.get(w)
        if not group:
            return (f"meter tidak pernah melaporkan jendela {w} untuk {engine}/{scope} — "
                    "tidak ada dasar untuk memberangkatkan mesin ini")
        r = max(group, key=_rank_row)
        state = str(r.get("state") or "")
        if state != "REPORTED":
            return (f"pembacaan terakhir jendela {w} berstate {state or 'kosong'}, bukan "
                    "REPORTED: tidak ada bukti kuota")
        frac = _finite(r.get("remaining_fraction"))
        if frac is None:
            return f"jendela {w} dilaporkan tanpa angka sisa yang bisa dibaca"
        if not 0.0 <= frac <= 1.0:
            return (f"sisa jendela {w} dilaporkan {frac} — di luar 0..1, jadi angka ini bukan "
                    "pecahan sisa dan tidak bisa dipakai sebagai bukti")
        if frac <= floor:
            return f"jendela {w} tinggal {frac:.3f} (habis)"
        when = _parse_ts(r.get("ts"))
        if when is None:
            return f"pembacaan jendela {w} tidak punya cap waktu yang bisa dibaca"
        age = (datetime.now().astimezone() - when).total_seconds()
        if age < 0:
            return (f"pembacaan jendela {w} berumur {age / 60:.0f} menit — jam mesin dan "
                    "cap meter tidak sepakat, jadi kesegarannya tidak bisa dibuktikan")
        if age > fresh_s:
            return (f"pembacaan jendela {w} berumur {age / 60:.0f} menit, lebih tua dari "
                    f"batas kesegaran meter ({fresh_s / 60:.0f} menit)")
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
        res = run_grouped(cmd, timeout=timeout, text=True, merge_stderr=True)
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


def _gate_selftest_cases():
    """Deretan keadaan meter yang WAJIB berujung penolakan, ditambah yang boleh lewat.

    Setiap kasus di sini lahir dari lubang nyata yang ditemukan audit mesin lain terhadap
    gate ini: jendela kosong, ambang tidak terbaca, state bukan laporan, cap waktu hilang
    atau dari masa depan, fraksi di luar 0..1, pembacaan basi. Fungsinya bukan menguji
    matematika — fungsinya menjaga supaya "aturan yang gagal dibaca" jangan pernah kembali
    menjadi "boleh lewat".
    """
    now = datetime.now().astimezone()
    utc_now = now.astimezone(timezone.utc)

    def iso(hours_from_now: float) -> str:
        return (now + timedelta(hours=hours_from_now)).isoformat()

    def zulu() -> str:
        return utc_now.isoformat().replace("+00:00", "Z")

    base_gate = {"engine": "antigravity", "scope": "Claude and GPT models",
                 "windows": ["5h", "weekly"]}

    def row(window, frac, state="REPORTED", ts=None, **over):
        r = {"engine": "antigravity", "scope": "Claude and GPT models",
             "limit_window": window, "remaining_fraction": frac, "state": state,
             "ts": iso(0) if ts is None else ts}
        r.update(over)
        return r

    def ok_rows():
        return [row("5h", 0.94), row("weekly", 0.97)]

    # (nama, baris meter, override gate, harus_ditolak)
    return [
        ("kedua jendela segar dan REPORTED", ok_rows(), {}, False),
        ("baris asing dicampur, sisanya sehat", [row("5h", 0.94), "sampah", row("weekly", 0.97)],
         {}, False),
        ("windows kosong", ok_rows(), {"windows": []}, True),
        ("min_fraction teks", ok_rows(), {"min_fraction": "abc"}, True),
        ("min_fraction 1.5", ok_rows(), {"min_fraction": 1.5}, True),
        ("min_fraction negatif", ok_rows(), {"min_fraction": -3}, True),
        ("gate tanpa 'engine'", ok_rows(), {"engine": ""}, True),
        ("scope salah ketik ('Anthropic')", ok_rows(), {"scope": "Anthropic"}, True),
        ("quota_gate bukan objek", ok_rows(), {"__selftest_scalar__": True}, True),
        ("meter tidak ada sama sekali", [], {}, True),
        ("hanya satu jendela dilaporkan", [row("5h", 0.94)], {}, True),
        ("hanya baris bukan dict", ["sampah"], {}, True),
        ("state UNAVAILABLE", [row("5h", 0.94), row("weekly", 0.97, state="UNAVAILABLE")],
         {}, True),
        ("remaining_fraction None", [row("5h", None), row("weekly", 0.97)], {}, True),
        ("frac 1.4 — bukan pecahan", [row("5h", 1.4), row("weekly", 0.97)], {}, True),
        ("frac -0.2", [row("5h", -0.2), row("weekly", 0.97)], {}, True),
        ("frac NaN", [row("5h", float("nan"), ), row("weekly", 0.97)], {}, True),
        ("ts dari masa depan", [row("5h", 0.94, ts=iso(2)), row("weekly", 0.97)], {}, True),
        ("tanpa cap waktu", [row("5h", 0.94, ts=""), row("weekly", 0.97, ts="")], {}, True),
        ("pembacaan basi 7 jam", [row("5h", 0.94, ts=iso(-7)), row("weekly", 0.97)], {}, True),
        ("ts detik sama: REPORTED vs UNAVAILABLE -> yang lemah menang",
         [row("5h", 0.94), row("5h", 0.94, state="UNAVAILABLE"), row("weekly", 0.97)], {}, True),
        ("ts format Z (UTC) tetap dikenali dan dianggap segar",
         [row("5h", 0.94, ts=zulu()), row("weekly", 0.97, ts=zulu())], {}, False),
    ], base_gate


def gate_selftest() -> int:
    """Jalankan tabel kasus gate tanpa menyentuh daemon maupun database."""
    cases, base_gate = _gate_selftest_cases()
    failures = 0
    real_quota = globals()["treasury_quota"]
    try:
        for name, rows, gate_over, must_refuse in cases:
            scalar = gate_over.pop("__selftest_scalar__", False)
            gate = dict(base_gate, **gate_over)
            globals()["treasury_quota"] = lambda *a, _r=rows: (_r, 3600)
            spec = {"quota_gate": 12345 if scalar else gate}
            reason = quota_gate_reason(spec)
            refused = reason is not None
            if refused != must_refuse:
                failures += 1
            print(f"  [{'GAGAL' if refused != must_refuse else 'ok  '}] "
                  f"harus {'DITOLAK' if must_refuse else 'LEWAT '}: {name}")
            if refused:
                print(f"          -> {reason}")
    finally:
        globals()["treasury_quota"] = real_quota
    print(f"\n=== gate selftest: {len(cases) - failures}/{len(cases)} sesuai ===")
    return 1 if failures else 0


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
    ap.add_argument("--gate-selftest", action="store_true",
                    help="uji tabel keadaan meter terhadap quota gate: tanpa daemon, "
                         "tanpa database, tanpa mengirim prompt ke mana-mana")
    args, extra = ap.parse_known_args()
    # argparse menghabiskan pola posisi pada kelompok pertama, lalu sisa teks jadi 'extra'.
    # Kita serap kembali token non-flag sebagai subjek; flag asing tetap error.
    leftovers = [t for t in extra if not t.startswith("-")]
    flags = [t for t in extra if t.startswith("-")]
    if flags:
        ap.error(f"opsi tak dikenal: {' '.join(flags)}")
    if leftovers:
        args.subject = list(args.subject) + leftovers

    if args.gate_selftest:
        return gate_selftest()

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
