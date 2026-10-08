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
    """Baca antrean dan rendahkan menjadi catatan. None berarti tidak ada yang perlu dikatakan."""
    if not cwd:
        # Bukan "tidak ada yang untukmu" — itu pernyataan tentang proyek yang tidak kita ketahui.
        # Yang jujur adalah tidak berkata apa pun.
        return None
    import work_order as wo
    conn, engine = wo.connect()
    try:
        return _note_from(conn, engine, cwd, wo)
    finally:
        # Ditemukan audit Gemini 2026-10-08 12:4x: hook ini dipanggil SETIAP prompt dan setiap
        # panggilan membuka koneksi yang tidak pernah ditutup. Tidak meledak hari ini karena
        # pool PostgreSQL sabar; ia akan meledak di sesi yang panjang, dan itu jenis kegagalan
        # yang datang sendiri sambil kita tidak melihatnya.
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _note_from(conn, engine, cwd, wo):
    ap = wo.autopilot(conn, engine) or {}
    # `ready_items`, bukan SQL sendiri. Ini kesalahan yang baru saja dikutuk putaran lima dan
    # kulakukan lagi di berkas yang sama: query `status='PENDING'` milik sendiri, tanpa `norm()`,
    # tanpa lease, tanpa cooldown. Bedanya kali ini ketahuan HIDUP — penasihat melaporkan baris
    # #86 dan #88 yang sudah COMPLETED ke sesi ini, persis saat ia dipasang sebagai pagar.
    ready = wo.ready_items(conn, engine, respect_due=True, origin=wo.ORIGIN_OPERATOR)
    mine_all = [x for x in ready if norm_ok(x)]
    if not mine_all:
        return None
    mine = select_rows(mine_all, cwd)
    # Dua alasan berbeda tidak boleh dapat satu label: baris milik MESIN lain dan baris milik
    # mesin ini tapi PROYEK lain adalah dua hal berbeda, dan menyebut keduanya "MESIN LAIN"
    # membuat operator menyalahkan mesin yang salah.
    other_engine = [x for x in mine_all if (x.get("cli_engine") or "").strip() != THIS_ENGINE]
    other_project = [x for x in mine_all
                     if (x.get("cli_engine") or "").strip() == THIS_ENGINE and x not in mine]
    return render_note((ap.get("mode") or "OFF").upper(), ap.get("level"),
                       mine, other_engine, other_project)


def norm_ok(row):
    """Baris siap yang benar-benar menunggu manusia, bukan yang sedang dikerjakan mesin."""
    return (row.get("status") or "").upper() == "PENDING"


def render_note(mode, level, mine, other_engine, other_project):
    """Bentuk kalimatnya. Terpisah dan murni supaya bisa diuji tanpa menyentuh database.

    `build_note` membuka koneksi sungguhan; mengujinya dari selftest berarti hasilnya bergantung
    pada apa yang kebetulan ada di antrean produksi hari ini.
    """
    lines = [f"ANTREAN OPERATOR untuk {THIS_ENGINE} (mode {mode}, tangan {level}):"]
    if not mine:
        lines.append("  tidak ada yang untukmu")
    for row in mine[:MAX_LISTED]:
        lines.append(f"  #{row['id']} {str(row['title'])[:90]}")
    if len(mine) > MAX_LISTED:
        lines.append(f"  ... {len(mine) - MAX_LISTED} lagi untuk {THIS_ENGINE}")
    # Dua alasan berbeda tidak boleh dapat satu label: baris milik MESIN lain dan baris milik
    # mesin ini tapi PROYEK lain adalah dua hal berbeda, dan menyebut keduanya "MESIN LAIN"
    # membuat operator menyalahkan mesin yang salah.
    if other_engine:
        lines.append(f"  ({len(other_engine)} baris menunggu untuk MESIN LAIN — jangan diambil)")
    if other_project:
        lines.append(f"  ({len(other_project)} baris milik mesin ini tapi PROYEK LAIN — "
                     "akan ditolak hook, bukan hilang)")
    # LAPORAN, bukan perintah. Versi sebelumnya menyuruh "kerjakan yang untukmu" untuk baris yang
    # belum diklaim siapa pun — jalur samping di luar disiplin: tidak ada lease, tidak ada cek
    # pemutus, tidak ada cek working-tree kotor. Yang menyalurkan pekerjaan adalah klaim.
    lines.append("Ini LAPORAN, bukan perintah: jangan mulai mengerjakan baris mana pun dari sini. "
                 "Yang menyalurkan pekerjaan adalah hook pada batas giliran, dan itu hanya lewat "
                 "jalur klaim — pemutus malam, aturan tree kotor, dan satu-perintah-aktif "
                 "ditegakkan di sana, bukan di catatan ini.")
    if mode == "OFF":
        lines.append("Mode OFF: tidak ada yang akan disalurkan sampai sakelar dipindahkan "
                     "oleh manusia dari Dashboard.")
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
