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

MAX_CHARS = 3200


def db_lines():
    out = []
    try:
        from db_state import get_db_connection
    except Exception as exc:  # noqa: BLE001
        return [f"(db_state import failed: {exc})"]
    try:
        conn, engine = get_db_connection()
        cur = conn.cursor()
        ph = "%s" if str(engine).upper().startswith("POSTG") else "?"

        cur.execute(f"SELECT id, title, model_assigned FROM quest_tasks "
                    f"WHERE status = {ph} ORDER BY id DESC LIMIT 5", ("IN_PROGRESS",))
        quests = cur.fetchall()
        out.append("Active quests: " + ("; ".join(f"#{q[0]} {q[1]}" for q in quests) if quests else "tidak ada"))

        # handoff_auto_* ditulis hook SessionEnd — sudah 62 baris dan isinya seragam.
        # Dibiarkan masuk, empat slot memori ini hanya menampilkan salinan handoff yang
        # sudah dibacakan utuh di bagian bawah briefing, dan memori sungguhan tersingkir.
        cur.execute("SELECT key, substr(content,1,160) FROM world_memory "
                    "WHERE key NOT LIKE 'handoff%' ORDER BY updated_at DESC LIMIT 4")
        for key, content in cur.fetchall():
            out.append(f"Memory `{key}`: {str(content).strip()[:150]}")

        cur.execute(f"SELECT kind, tab, substr(summary,1,90) FROM action_log ORDER BY id DESC LIMIT 8")
        acts = " | ".join(f"{k}/{t}:{s.strip()[:60]}" for k, t, s in cur.fetchall())
        out.append("Last 8 actions: " + (acts or "-"))
        conn.close()
    except Exception as exc:  # noqa: BLE001
        out.append(f"(database unreadable: {type(exc).__name__}: {str(exc)[:90]})")
    return out


def handoff_lines():
    folder = STATION / "brain" / "memory" / "projects"
    try:
        files = sorted(folder.glob("handoff_*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return []
    newest, body = None, []
    for cand in files[:6]:
        try:
            txt = cand.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        # Handoff tanpa satu baris pun tercatat (biasanya SessionEnd yang mendahului
        # ingestor) bukan "sesi terakhir yang penting" — lewati, jangan tampilkan.
        if "tidak ada prompt terekam" in txt and "tidak ada perintah terekam" in txt:
            continue
        newest, body = cand, txt.splitlines()
        break
    if newest is None:
        return []
    ambil, n = [], 0
    for line in body:
        t = line.strip()
        if not t or t.startswith("#"):
            continue
        ambil.append(t[:120])
        n += 1
        if n >= 5:
            break
    older = [p.name for p in files if p != newest]
    tail = "  (%d older handoffs archived — `ai-station recovery 40` walks the chain)" % len(older) if len(older) > 2 else ""
    return [f"Latest handoff ({newest.name}):"] + ["  " + a for a in ambil] + ([tail] if tail else [])


def index_lines():
    """Hanya judul indeks — aturan selective loading: jangan sedot seluruh skill ke konteks."""
    out = []
    for label, path in (("Skills", STATION / "brain/skills/SKILLS.md"),
                        ("Memory", STATION / "brain/memory/MEMORY.md")):
        try:
            heads = [l.strip().lstrip("#").strip() for l in path.read_text(encoding="utf-8").splitlines()
                     if l.strip().startswith("###")][:8]
            out.append(f"{label} registered: " + ("; ".join(heads) or "-"))
        except OSError:
            continue
    return out


def build():
    parts = ["THIS SESSION'S CONTEXT WAS JUST COMPRESSED. This is the memory that survived — "
             "read it before acting, and cross-check the database instead of trusting recall.", ""]
    parts += db_lines() + [""] + handoff_lines() + [""] + index_lines()
    parts += ["", "File map (generated): brain/memory/projects/ARCHITECTURE.md — folder -> file -> purpose. "
              "Open the section that owns the behaviour, then grep only that directory."]
    parts += ["", "Full commands when you need to dig deeper: `ai-station recovery 40`, "
              "`ai-station handoff`, `ai-station recall 10`. Unrecorded history never happened."]
    text = "\n".join(p for p in parts if p is not None)
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n… (truncated — read it in full with `ai-station recovery 40`)"
    return text


def main():
    try:
        event = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        event = {}
    if os.environ.get("STATION_AUTO_RESTORE") == "0":
        return 0
    context = build()
    # Nama event HARUS sama dengan event pemicunya: Qoder menolak hookSpecificOutput yang
    # hookEventName-nya tidak cocok, jadi menulis "PostCompact" saat dipanggil SessionStart
    # membuat jalur refresh tidak pernah menyalurkan isinya sama sekali.
    event_name = event.get("hook_event_name") or "PostCompact"
    json.dump({
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
