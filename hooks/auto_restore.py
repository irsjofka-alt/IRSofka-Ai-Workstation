#!/usr/bin/env python3
"""PostCompact hook: setelah konteks diringkas, isi kembali ingatannya OTOMATIS.

Kenapa perlu: PreCompact menulis handoff supaya jejak tidak hilang — tapi tidak ada yang
mengembalikannya ke dalam konteks sesi. Akibatnya sesi yang baru saja dikompaksi "tahu" ia
pernah mengerjakan sesuatu, tanpa tahu apa. Hook ini menutup lubang itu: ia menyusun
briefing pendek dari PostgreSQL + berkas handoff, dan menyerahkannya sebagai additionalContext.

Briefing sengaja DIPANGGAS ketat. Ia masuk ke konteks yang baru saja dibersihkan — kalau
isinya gemuk, hook ini justru mengulang penyakit yang mau disembuhkannya.
"""
import json
import os
import sys
from pathlib import Path

STATION = Path.home() / ".ai-station"
sys.path.insert(0, str(STATION / "tools"))

MAX_CHARS = 2800


def db_lines():
    out = []
    try:
        from db_state import get_db_connection
    except Exception as exc:  # noqa: BLE001
        return [f"(db_state tidak terbaca: {exc})"]
    try:
        conn, engine = get_db_connection()
        cur = conn.cursor()
        ph = "%s" if str(engine).upper().startswith("POSTG") else "?"

        cur.execute(f"SELECT id, title, model_assigned FROM quest_tasks "
                    f"WHERE status = {ph} ORDER BY id DESC LIMIT 5", ("IN_PROGRESS",))
        quests = cur.fetchall()
        out.append("Quest berjalan: " + ("; ".join(f"#{q[0]} {q[1]}" for q in quests) if quests else "tidak ada"))

        cur.execute(f"SELECT key, substr(content,1,160) FROM world_memory ORDER BY updated_at DESC LIMIT 4")
        for key, content in cur.fetchall():
            out.append(f"Memori `{key}`: {str(content).strip()[:150]}")

        cur.execute(f"SELECT kind, tab, substr(summary,1,90) FROM action_log ORDER BY id DESC LIMIT 8")
        acts = " | ".join(f"{k}/{t}:{s.strip()[:60]}" for k, t, s in cur.fetchall())
        out.append("8 aksi terakhir: " + (acts or "-"))
        conn.close()
    except Exception as exc:  # noqa: BLE001
        out.append(f"(basis data tidak terbaca: {type(exc).__name__}: {str(exc)[:90]})")
    return out


def handoff_lines():
    folder = STATION / "brain" / "memory" / "projects"
    try:
        files = sorted(folder.glob("handoff_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    if not files:
        return []
    newest = files[0]
    try:
        body = newest.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        return []
    ambil, n = [], 0
    for line in body:
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        ambil.append(t[:180])
        n += 1
        if n >= 12:
            break
    return [f"Handoff terbaru ({newest.name}):"] + ["  " + a for a in ambil]


def index_lines():
    """Hanya judul indeks — aturan selective loading: jangan sedot seluruh skill ke konteks."""
    out = []
    for label, path in (("Skill", STATION / "brain/skills/SKILLS.md"),
                        ("Memori", STATION / "brain/memory/MEMORY.md")):
        try:
            heads = [l.strip().lstrip("#").strip() for l in path.read_text(encoding="utf-8").splitlines()
                     if l.strip().startswith("###")][:8]
            out.append(f"{label} terdaftar: " + ("; ".join(heads) or "-"))
        except OSError:
            continue
    return out


def build():
    parts = ["KONTEKS SESI INI BARU SAJA DIRINGKAS. Ini ingatan yang masih tersimpan — "
             "baca dulu, jangan mulai kerja sebelum cocokkan dengan database.", ""]
    parts += db_lines() + [""] + handoff_lines() + [""] + index_lines()
    parts += ["", "Perintah lengkap kalau perlu gali lebih dalam: `ai-station recovery 40`, "
              "`ai-station handoff`, `ai-station recall 10`. Riwayat tidak tercatat = tidak pernah terjadi."]
    text = "\n".join(p for p in parts if p is not None)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n… (dipangkas — gali utuhnya dengan `ai-station recovery 40`)"
    return text


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        event = {}
    if os.environ.get("STATION_AUTO_RESTORE") == "0":
        return 0
    context = build()
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PostCompact",
            "additionalContext": context,
        }
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
