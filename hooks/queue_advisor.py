"""Penasehat antrean — memberi tahu CLI apa yang sedang menunggu di SQL.

Alasannya konkret, bukan arsitektural: perintah operator masuk lewat Dashboard ke PostgreSQL, dan
sebuah sesi yang sudah TERBUKA tidak memuat hook baru — hook dibaca saat sesi mulai. Itu terukur
2026-10-08 11:05: item 86 antre jam 11:00:59 dan tidak ada satu pun tembakan hook tercatat setelah
10:50:25, padahal sesi ini berhenti beberapa kali. Jadi "IDLE" yang sebenarnya adalah *sesi yang
tidak tahu ada pekerjaan*, bukan mesin yang tidak bisa mengirim.

Fungsi ini menutup bagian itu tanpa tangan: ia hanya MEMBACA dan menempelkan hasilnya sebagai
`additionalContext` pada peristiwa `SessionStart` dan `UserPromptSubmit`. Tidak ada klaim, tidak ada
kirim, tidak ada `decision` — sebuah nasihat yang bisa mengubah keadaan adalah tangan yang menyamar
menjadi catatan.

IDLE/WORKING tidak lagi disimpulkan di jalur ini: CLI yang memakainya yang melaporkan. Deteksi
dari LUAR tetap milik `work_order.py heartbeat()` (dua sinyal independent, dan `UNKNOWN` bukan
`idle`), dan jalur itu yang berhak mengetuk pane — sesudah di-`arm` dari TTY.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".ai-station" / "tools"))

MAX_LISTED = 5
# Mesin milik sesi yang memanggil, dibaca dari argv supaya satu berkas bisa terdaftar di dua CLI.
# Dipakai untuk MEMILAH laporan, bukan untuk memilih pekerjaan: menampilkan antrean milik mesin lain
# membuat engine ini seolah-olah punya sesuatu untuk dikerjakan.
THIS_ENGINE = (sys.argv[1] if len(sys.argv) > 1 else "qoder")


def read_input() -> dict:
    raw = (sys.stdin.read() or "").strip()
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def session_cwd(data: dict):
    raw = data.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    paths = data.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0].strip():
        return paths[0].strip()
    return None


def select_rows(rows, cwd):
    """Baris yang memang untuk sesi ini — memakai resolver yang sama dengan hook dan drainer.

    Dipisah dan murni supaya bisa diuji dengan daftar sungguhan. Versi sebelumnya hanya
    MEMBERI TANDA "[proyek lain]" dan memperlakukan `ws_path` kosong sebagai "milik semua proyek",
    sementara hook menolak baris seperti itu di proyek mana pun selain REPO: nasihat yang bilang
    "ini untukmu" lalu ditolak mesin adalah cara membuat operator mengira perintahnya hilang.
    """
    import work_order as wo
    here = None
    if cwd:
        try:
            here = Path(cwd).expanduser().resolve(strict=False)
        except OSError:
            here = None
    if here is None:
        # Sesi tidak melaporkan proyeknya. Melaporkan semua baris milik mesin ini akan menyuruh
        # engine mengerjakan antrean yang akan ditolak hook-nya sendiri, dan dari kursinya itu
        # terlihat seperti perintah yang hilang. Yang jujur adalah diam.
        return []
    keep = []
    for r in rows:
        if (r.get("cli_engine") or "").strip() != THIS_ENGINE:
            continue
        if wo.resolve_workspace(r) != here:
            continue
        keep.append(r)
    return keep


def build_note(cwd: str) -> str | None:
    """Satu kalimat laporan antrean, atau None kalau memang tidak ada yang perlu dilaporkan."""
    if not cwd:
        # Bukan "tidak ada yang untukmu" — itu pernyataan tentang proyek yang tidak kita ketahui.
        # Yang jujur adalah tidak berkata apa pun.
        return None
    import work_order as wo
    conn, engine = wo.connect()
    ap = wo.autopilot(conn, engine) or {}
    mine_all = wo.run(conn, engine,
                      "SELECT id, title, cli_engine, ws_path, status FROM quest_tasks "
                      "WHERE origin=%s AND status='PENDING' ORDER BY id"
                      if engine == "POSTGRESQL" else
                      "SELECT id, title, cli_engine, ws_path, status FROM quest_tasks "
                      "WHERE origin=? AND status='PENDING' ORDER BY id",
                      (wo.ORIGIN_OPERATOR,))
    mine = select_rows(mine_all, cwd)
    # Dua alasan berbeda tidak boleh dapat satu label. Baris milik MESIN lain dan baris milik
  # mesin ini tapi milik PROYEK lain adalah dua hal yang berbeda, dan menyebut keduanya
  # "MESIN LAIN" membuat operator mengira mesin yang salah yang menahan perintahnya.
    other_engine = [r for r in mine_all if (r.get("cli_engine") or "").strip() != THIS_ENGINE]
    other_project = [r for r in mine_all
                     if (r.get("cli_engine") or "").strip() == THIS_ENGINE and r not in mine]
    if not mine_all:
        return None
    mode = (ap.get("mode") or "OFF").upper()
    lines = [f"ANTREAN OPERATOR untuk {THIS_ENGINE} (mode {mode}, tangan {ap.get('level')}):"]
    if not mine:
        lines.append("  tidak ada yang untukmu")
    for r in mine[:MAX_LISTED]:
        lines.append(f"  #{r['id']} {str(r['title'])[:90]}")
    if len(mine) > MAX_LISTED:
        lines.append(f"  ... {len(mine) - MAX_LISTED} lagi untuk {THIS_ENGINE}")
    if other_engine:
        lines.append(f"  ({len(other_engine)} baris menunggu untuk MESIN LAIN — jangan diambil)")
    if other_project:
        lines.append(f"  ({len(other_project)} baris milik mesin ini tapi PROYEK LAIN — "
                     "akan ditolak hook, bukan hilang)")
    # Nasihat harus sesuai mode. Versi pertama selalu bilang "kerjakan lewat SEMI" bahkan ketika
    # sakelarnya OFF — dan menaati itu berarti mengerjakan pekerjaan yang sedang dimatikan.
    if mode == "SEMI":
        lines.append("Mode SEMI: kerjakan yang untukmu; JANGAN ambil item roadmap.")
    elif mode == "ON":
        lines.append("Mode ON: antrean roadmap juga terbuka; perintahmu tetap punya prioritas.")
    else:
        lines.append("Mode OFF: jangan kerjakan apa pun dari sini — ini hanya laporan, dan "
                     "yang menyalakan mesin adalah manusia.")
    lines.append("Untuk keputusan level preferensi: `work_order.py decide` lalu `route` — "
                 "jangan tanyakan ke operator.")
    return "\n".join(lines)


def main() -> int:
    data = read_input()
    cwd = session_cwd(data) or ""
    try:
        note = build_note(cwd)
    except Exception:  # noqa: BLE001 — nasihat yang gagal tidak boleh menghentikan seseorang
        return 0
    if not note:
        return 0
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": data.get("hook_event_name") or "SessionStart",
        "additionalContext": note}}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
