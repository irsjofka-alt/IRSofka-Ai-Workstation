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


def read_input() -> dict:
    raw = (sys.stdin.read() or "").strip()
    try:
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def build_note(cwd: str) -> str | None:
    """Satu kalimat laporan antrean, atau None kalau memang tidak ada yang perlu dilaporkan."""
    import work_order as wo
    conn, engine = wo.connect()
    ap = wo.autopilot(conn, engine) or {}
    rows = wo.run(conn, engine,
                  "SELECT id, title, cli_engine, ws_path, origin FROM quest_tasks "
                  "WHERE origin=%s AND status='PENDING' ORDER BY id"
                  if engine == "POSTGRESQL" else
                  "SELECT id, title, cli_engine, ws_path, origin FROM quest_tasks "
                  "WHERE origin=? AND status='PENDING' ORDER BY id",
                  (wo.ORIGIN_OPERATOR,))
    mine = [r for r in rows if not (r.get("ws_path") or "") or cwd and Path(r["ws_path"]) == Path(cwd)]
    other = [r for r in rows if r not in mine]
    if not rows:
        return None
    lines = [f"ANTREAN OPERATOR (mode {ap.get('mode')}, tangan {ap.get('level')}):"]
    for r in (mine[:MAX_LISTED]):
        lines.append(f"  #{r['id']} {str(r['title'])[:90]} → {r.get('cli_engine') or 'tanpa mesin'}")
    if len(mine) > MAX_LISTED:
        lines.append(f"  ... {len(mine) - MAX_LISTED} lagi di proyek ini")
    if other:
        lines.append(f"  ({len(other)} baris lain menunggu untuk PROYEK LAIN — jangan dikerjakan di sini)")
    lines.append("Kerjakan yang untukmu lewat SEMI; jangan ambil item roadmap saat mode SEMI. "
                 "Untuk keputusan level preferensi: `work_order.py decide` lalu `route` — "
                 "jangan tanyakan ke operator.")
    return "\n".join(lines)


def main() -> int:
    data = read_input()
    cwd = data.get("cwd") or ""
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
