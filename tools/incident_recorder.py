#!/usr/bin/env python3
"""
Incident Recorder & Auto-Skill Synthesizer
Mencatat kegagalan, dan jika pola error terulang >= 5x,
secara otomatis menciptakan SKILL baru di brain/skills/learned/
dan memperbarui catalog SKILLS.md.
"""

import os
import sys
import json
import subprocess
from datetime import datetime
from pathlib import Path

AI_STATION = Path.home() / ".ai-station"
BRAIN_DIR = AI_STATION / "brain"
INCIDENT_FILE = BRAIN_DIR / "memory" / "incidents" / "error_tracker.json"
SKILLS_DIR = BRAIN_DIR / "skills"
LEARNED_DIR = SKILLS_DIR / "learned"
SKILLS_CATALOG = SKILLS_DIR / "SKILLS.md"
THRESHOLD = 5


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


def generate_skill(incident_id, incident_data):
    LEARNED_DIR.mkdir(parents=True, exist_ok=True)
    skill_file = LEARNED_DIR / f"{incident_id}.md"

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

    if incident_id not in incidents:
        incidents[incident_id] = {
            "count": 1,
            "description": description,
            "solution": solution,
            "first_seen": datetime.utcnow().isoformat(),
            "last_seen": datetime.utcnow().isoformat(),
            "converted_to_skill": False
        }
    else:
        incidents[incident_id]["count"] += 1
        incidents[incident_id]["last_seen"] = datetime.utcnow().isoformat()
        if description:
            incidents[incident_id]["description"] = description
        if solution:
            incidents[incident_id]["solution"] = solution

    count = incidents[incident_id]["count"]
    print(f"[*] Insiden '{incident_id}' tercatat. (Frekuensi: {count}/{THRESHOLD})")

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
