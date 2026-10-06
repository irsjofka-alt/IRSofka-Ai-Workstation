#!/usr/bin/env python3
"""
Incident Recorder & Auto-Skill Synthesizer
Mencatat kegagalan, dan jika pola error terulang >= 5x,
secara otomatis menciptakan SKILL baru di brain/skills/learned/
dan memperbarui catalog SKILLS.md.
"""

import os
import re
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".ai-station" / "tools"))
from db_state import get_db_connection  # noqa: E402


def is_pg_engine(engine):
    return str(engine).upper() == "POSTGRESQL"

AI_STATION = Path.home() / ".ai-station"
BRAIN_DIR = AI_STATION / "brain"
INCIDENT_FILE = BRAIN_DIR / "memory" / "incidents" / "error_tracker.json"
SKILLS_DIR = BRAIN_DIR / "skills"
LEARNED_DIR = SKILLS_DIR / "learned"
SKILLS_CATALOG = SKILLS_DIR / "SKILLS.md"
THRESHOLD = 5


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sql_record(signature, description, count_at_least=1):
    """Catat insiden ke tabel incident_log (sumber kebenaran, bukan JSON saja).

    Fungsi ini dulunya hanya menulis error_tracker.json, sementara GUI dan semua alat
    membaca tabel incident_log di PostgreSQL — itulah sebabnya tabel itu tetap kosong
    meskipun fitur 'self-healing' diklaim sudah jadi.
    """
    try:
        conn, engine = get_db_connection()
        cur = conn.cursor()
        if is_pg_engine(engine):
            cur.execute(
                """INSERT INTO incident_log (error_signature, first_seen, last_seen,
                                            repeat_count, last_error_text, resolved)
                   VALUES (%s, %s, %s, %s, %s, FALSE)
                   ON CONFLICT (error_signature) DO UPDATE SET
                       last_seen = EXCLUDED.last_seen,
                       repeat_count = incident_log.repeat_count + 1,
                       last_error_text = EXCLUDED.last_error_text""",
                (signature[:255], now(), now(), count_at_least, (description or "")[:4000]))
            cur.execute("SELECT repeat_count FROM incident_log WHERE error_signature = %s",
                        (signature[:255],))
            row = cur.fetchone()
        else:
            cur.execute(
                """INSERT INTO incident_log (error_signature, first_seen, last_seen,
                                            repeat_count, last_error_text, resolved)
                   VALUES (?, ?, ?, ?, ?, 0)
                   ON CONFLICT (error_signature) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       repeat_count = incident_log.repeat_count + 1,
                       last_error_text = excluded.last_error_text""",
                (signature[:255], now(), now(), count_at_least, (description or "")[:4000]))
            cur.execute("SELECT repeat_count FROM incident_log WHERE error_signature = ?",
                        (signature[:255],))
            row = cur.fetchone()
        conn.commit()
        conn.close()
        return int(row[0]) if row else count_at_least
    except Exception as exc:  # noqa: BLE001
        print(f"[incident] GAGAL mencatat ke SQL: {exc}", file=sys.stderr)
        return None


def sql_register_skill(skill_id, name, description, file_path):
    """Daftarkan skill hasil belajar ke skills_inventory supaya benar-benar terhitung."""
    try:
        conn, engine = get_db_connection()
        cur = conn.cursor()
        ph = "%s" if is_pg_engine(engine) else "?"
        cur.execute(
            f"""INSERT INTO skills_inventory (id, name, category, level, description,
                                             file_path, auto_learned, use_count, created_at, updated_at)
                VALUES ({ph}, {ph}, 'learned', 1, {ph}, {ph}, TRUE, 0, {ph}, {ph})
                ON CONFLICT (id) DO NOTHING""",
            (skill_id[:50], name[:120], description[:1000], str(file_path), now(), now()))
        conn.commit()
        conn.close()
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[incident] GAGAL mendaftarkan skill ke SQL: {exc}", file=sys.stderr)
        return False


def load_tracker():
    if INCIDENT_FILE.exists():
        try:
            with open(INCIDENT_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"version": "1.0", "threshold_auto_skill": THRESHOLD, "incidents": {}}


def save_tracker(data):
    INCIDENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(INCIDENT_FILE, "w") as f:
        json.dump(data, f, indent=2)


def safe_name(text):
    """Nama yang aman untuk berkas, URL, dan id SQL.

    Signature insiden mengandung ':' (mis. 'exit1:python3_PY_import'). ext4 memang
    menerima titik dua, tapi shell glob, URL file://, dan sebagian alat tidak — jadi
    dibersihkan sekali di sini, di tempat nama itu lahir.
    """
    return re.sub(r"[^0-9A-Za-z_.-]", "_", str(text)).strip("_")[:60] or "insiden"


def generate_skill(incident_id, incident_data):
    LEARNED_DIR.mkdir(parents=True, exist_ok=True)
    skill_file = LEARNED_DIR / f"{safe_name(incident_id)}.md"

    title = incident_id.replace("_", " ").title()
    content = f"""# Learned Skill: {title}

Dokumen skill ini di-generate secara otomatis oleh AI Workstation setelah error terulang {incident_data['count']} kali.

## 1. Gejala / Masalah yang Terjadi
{incident_data.get('description', 'Deskripsi masalah tidak tersedia.')}

## 2. Solusi Permanen / Best Practice
{incident_data.get('solution', 'Lakukan verifikasi environment dan jalankan prosedur mitigasi.')}

## 3. Metadata Insiden
- Terakhir Terjadi: {incident_data.get('last_seen')}
- Total Terulang: {incident_data.get('count')} kali
- Status: Auto-Mitigated ke Master Skills
"""
    skill_file.write_text(content)
    print(f"[+] Berhasil membuat file skill baru: {skill_file}")

    # Update SKILLS.md catalog
    if SKILLS_CATALOG.exists():
        catalog_text = SKILLS_CATALOG.read_text()
        entry = f"- [{incident_id}.md](file://{skill_file}) — Solusi auto-generated untuk: {incident_data.get('description', title)[:80]}...\n"
        
        target_marker = "### 5. Learned Skills (Self-Generated) (`skills/learned/`)"
        if target_marker in catalog_text and str(skill_file.name) not in catalog_text:
            parts = catalog_text.split(target_marker)
            new_catalog = parts[0] + target_marker + "\n" + entry + parts[1]
            SKILLS_CATALOG.write_text(new_catalog)
            print(f"[+] Master SKILLS.md berhasil diperbarui!")

    # Daftarkan ke skills_inventory supaya skill ini TERHITUNG sebagai memori yang belajar.
    # Tanpa langkah ini, klaim 'self-healing' tidak pernah bisa dibuktikan dari database.
    skill_id = f"learned_{safe_name(incident_id)}"[:50]
    if sql_register_skill(skill_id, title,
                          incident_data.get("description", title)[:1000], skill_file):
        print("[+] skills_inventory diperbarui (auto_learned=true).")

    try:
        conn, engine = get_db_connection()
        cur = conn.cursor()
        ph = "%s" if str(engine).upper() == "POSTGRESQL" else "?"
        cur.execute(f"UPDATE incident_log SET resolved = {'TRUE' if str(engine).upper() == 'POSTGRESQL' else '1'}, "
                    f"auto_skill_id = {ph} WHERE error_signature = {ph}",
                    (skill_id, incident_id[:255]))
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        print(f"[incident] gagal menandai insiden terselesaikan: {exc}", file=sys.stderr)

    # Kirim notifikasi desktop ke user di Pop!_OS
    try:
        subprocess.Popen([
            "notify-send", "-a", "AI Workstation Core",
            "🧠 Skill Baru Terbentuk Otomatis!",
            f"Error '{incident_id}' terulang 5x dan telah dipelajari menjadi skill permanen."
        ])
    except Exception:
        pass


def record_incident(incident_id, description, solution=""):
    tracker = load_tracker()
    incidents = tracker.setdefault("incidents", {})

    # SQL adalah sumber kebenaran jumlah; JSON hanya cermin yang bisa dibaca manusia.
    sql_count = sql_record(incident_id, description)

    if incident_id not in incidents:
        incidents[incident_id] = {
            "count": 1,
            "description": description,
            "solution": solution,
            "first_seen": now(),
            "last_seen": now(),
            "converted_to_skill": False
        }
    else:
        incidents[incident_id]["count"] += 1
        incidents[incident_id]["last_seen"] = now()
        if description:
            incidents[incident_id]["description"] = description
        if solution:
            incidents[incident_id]["solution"] = solution

    count = sql_count if sql_count else incidents[incident_id]["count"]
    incidents[incident_id]["count"] = count
    print(f"[*] Insiden '{incident_id}' tercatat. (Frekuensi: {count}/{THRESHOLD}"
          f"{' dari SQL' if sql_count else ', SQL tidak terjangkau'})")

    if count >= THRESHOLD and not incidents[incident_id].get("converted_to_skill", False):
        print(f"[!] AMBANG BATAS {THRESHOLD}X TERCAPAI: Memicu pembentukan skill otomatis...")
        generate_skill(incident_id, incidents[incident_id])
        incidents[incident_id]["converted_to_skill"] = True

    save_tracker(tracker)


if __name__ == "__main__":
    if len(sys.argv) >= 3:
        i_id = sys.argv[1]
        i_desc = sys.argv[2]
        i_sol = sys.argv[3] if len(sys.argv) > 3 else "Gunakan metode fallback yang sesuai."
        record_incident(i_id, i_desc, i_sol)
    elif len(sys.argv) == 2 and sys.argv[1] == "list":
        t = load_tracker()
        print(json.dumps(t, indent=2))
    else:
        print("Usage: incident_recorder.py <incident_id> <description> [solution]")
        print("       incident_recorder.py list")
