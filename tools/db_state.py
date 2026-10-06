#!/usr/bin/env python3
"""
Irsofka AI Workstation - Dual-Engine RPG Save State (PostgreSQL Primary + SQLite Fallback)
Menyimpan Player Profile, Skill Inventory, Quest/Task Log, World Memory, dan Incident Tracker
secara otomatis ke PostgreSQL Enterprise atau SQLite dengan Auto-Migration.
"""

import os
import sys
import json
import time
import sqlite3
from pathlib import Path

DB_SQLITE_PATH = Path.home() / ".ai-station" / "brain" / "workstation.db"

# Identitas pemilik disemai ke player_profile HANYA saat basis data masih kosong.
# Nilai aslinya dulu tertulis langsung di kode; karena repo ini public, ia dibaca dari
# lingkungan dengan placeholder netral supaya hasil clone tidak membawa data pribadi.
OWNER_NAME = os.environ.get("STATION_OWNER_NAME", "Owner")
OWNER_EMAIL_QODER = os.environ.get("STATION_EMAIL_QODER", "owner@example.com")
OWNER_EMAIL_ANTIGRAVITY = os.environ.get("STATION_EMAIL_ANTIGRAVITY", "owner@example.com")
OWNER_GPU = os.environ.get("STATION_OWNER_GPU", "NVIDIA GeForce RTX 3060 (12GB VRAM)")
OWNER_RAM = os.environ.get("STATION_OWNER_RAM", "32 GB RAM DDR4")
OWNER_OS = os.environ.get("STATION_OWNER_OS", "Pop!_OS 24.04 LTS (COSMIC Wayland)")
OWNER_TITLE = os.environ.get("STATION_OWNER_TITLE", "Chief AI Architect & Game Developer")
DB_LOCAL_PATH = Path(os.environ.get("STATION_PG_LOCAL")
                     or (Path(__file__).resolve().parent.parent / "config" / "db_local.json"))

def _pg_config():
    """Kredensial PostgreSQL: nilai bawaan -> config/db_local.json -> variabel lingkungan.

    Password dulu di-hardcode di berkas ini. Karena repo workstation kini PUBLIC,
    kredensial dipindah ke config/db_local.json yang di-ignore git. Hasil clone yang bersih
    akan gagal terhubung secara terang-terangan, bukan ikut membawa password milik orang lain.
    """
    cfg = {
        "dbname": "irsofka_ai_workstation",
        "user": "irsofka",
        "password": "",
        "host": "localhost",
        "port": 5432,
    }
    try:
        if DB_LOCAL_PATH.exists():
            with open(DB_LOCAL_PATH) as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                cfg.update({k: v for k, v in loaded.items() if k in cfg})
    except Exception as exc:  # noqa: BLE001
        print(f"db_state: gagal membaca {DB_LOCAL_PATH}: {exc}", file=sys.stderr)
    env_map = {
        "host": "STATION_PG_HOST", "port": "STATION_PG_PORT", "user": "STATION_PG_USER",
        "password": "STATION_PG_PASSWORD", "dbname": "STATION_PG_DB",
    }
    for key, var in env_map.items():
        value = os.environ.get(var)
        if value:
            cfg[key] = int(value) if key == "port" else value
    return cfg

# PostgreSQL Connection Params
PG_CONFIG = _pg_config()

def get_db_connection():
    # 1. Coba koneksi ke PostgreSQL terlebih dahulu (Enterprise Buff)
    try:
        import psycopg2
        import psycopg2.extras
        conn = psycopg2.connect(**PG_CONFIG, connect_timeout=1)
        conn.autocommit = True
        return conn, "POSTGRESQL"
    except Exception:
        pass

    # 2. Fallback mulus ke SQLite jika PostgreSQL tidak tersedia
    DB_SQLITE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_SQLITE_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn, "SQLITE"

def init_db():
    conn, engine_type = get_db_connection()
    if engine_type == "POSTGRESQL":
        with conn.cursor() as cur:
            # Seed profile if empty
            cur.execute("SELECT COUNT(*) FROM player_profile;")
            if cur.fetchone()[0] == 0:
                cur.execute("""
                INSERT INTO player_profile (id, username, email_qoder, email_antigravity, hardware_gpu, hardware_ram, os_info, active_title)
                VALUES ('main_user', %s, %s, %s, %s, %s, %s, %s);
                """, (OWNER_NAME, OWNER_EMAIL_QODER, OWNER_EMAIL_ANTIGRAVITY,
                      OWNER_GPU, OWNER_RAM, OWNER_OS, OWNER_TITLE))
            # Seed skills if empty
            cur.execute("SELECT COUNT(*) FROM skills_inventory;")
            if cur.fetchone()[0] == 0:
                core_skills = [
                    ("qwen_1m_code", "Qwen 3.8 Flash 1M Context Code Synthesizer", "coding", 5, "Menganalisis dan menulis kode proyek skala besar dengan 1M tokens (0 Points)", 10),
                    ("gemini_vision", "Gemini Visual Eye Inspector", "visual", 4, "Inspeksi screenshot desktop Pop!_OS via cosmic-screenshot", 6),
                    ("wayland_actor", "Wayland Eye & Hand Actuator", "sysadmin", 3, "Navigasi desktop dan simulasi input otomatis", 4),
                    ("self_healer", "Self-Healing Auto Synthesizer", "learned", 5, "Deteksi error berulang 5x dan sintesis skill otomatis", 12)
                ]
                for s_id, s_name, s_cat, s_lvl, s_desc, s_cnt in core_skills:
                    cur.execute("""
                    INSERT INTO skills_inventory (id, name, category, level, description, use_count)
                    VALUES (%s, %s, %s, %s, %s, %s);
                    """, (s_id, s_name, s_cat, s_lvl, s_desc, s_cnt))
        conn.close()
    else:
        # SQLite Initialization
        with conn:
            conn.execute("""
            INSERT OR IGNORE INTO player_profile (id, username, email_qoder, email_antigravity, hardware_gpu, hardware_ram, os_info, active_title)
            VALUES ('main_user', ?, ?, ?, ?, ?, ?, ?);
            """, (OWNER_NAME, OWNER_EMAIL_QODER, OWNER_EMAIL_ANTIGRAVITY,
                  OWNER_GPU, OWNER_RAM, OWNER_OS, OWNER_TITLE))
        conn.close()

def log_session_turn(cli_engine, model, effort, ctx_window, prompt, response="", auto_approved=True, duration_ms=0):
    conn, engine_type = get_db_connection()
    try:
        if engine_type == "POSTGRESQL":
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO session_turns (cli_engine, model, reasoning_effort, context_window, prompt, response, auto_approved, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s);
                """, (cli_engine, model, effort, ctx_window, prompt, response, auto_approved, duration_ms))
        else:
            with conn:
                conn.execute("""
                INSERT INTO session_turns (cli_engine, model, reasoning_effort, context_window, prompt, response, auto_approved, duration_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """, (cli_engine, model, effort, ctx_window, prompt, response, auto_approved, duration_ms))
    finally:
        conn.close()

def record_quest_task(cli_engine, model, title, status, summary):
    conn, engine_type = get_db_connection()
    try:
        if engine_type == "POSTGRESQL":
            with conn.cursor() as cur:
                cur.execute("""
                INSERT INTO quest_tasks (title, status, model_assigned, cli_engine, summary, completed_at)
                VALUES (%s, %s, %s, %s, %s, CURRENT_TIMESTAMP);
                """, (title, status, model, cli_engine, summary))
        else:
            with conn:
                conn.execute("""
                INSERT INTO quest_tasks (title, status, model_assigned, cli_engine, summary, completed_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP);
                """, (title, status, model, cli_engine, summary))
    finally:
        conn.close()

def get_stats_summary():
    conn, engine_type = get_db_connection()
    try:
        if engine_type == "POSTGRESQL":
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM skills_inventory;")
                skills_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM world_memory;")
                mem_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM incident_log WHERE resolved=FALSE;")
                incidents_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM session_turns;")
                turns_cnt = cur.fetchone()[0]
                cur.execute("SELECT COUNT(*) FROM quest_tasks;")
                quests_cnt = cur.fetchone()[0]
        else:
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM skills_inventory;")
            skills_cnt = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM world_memory;")
            mem_cnt = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM incident_log WHERE resolved=0;")
            incidents_cnt = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM session_turns;")
            turns_cnt = c.fetchone()[0]
            c.execute("SELECT COUNT(*) FROM quest_tasks;")
            quests_cnt = c.fetchone()[0]
    finally:
        conn.close()

    return {
        "engine": engine_type,
        "skills_count": skills_cnt,
        "memories_count": mem_cnt,
        "active_incidents": incidents_cnt,
        "total_turns": turns_cnt,
        "total_quests": quests_cnt
    }

if __name__ == "__main__":
    init_db()
    stats = get_stats_summary()
    print(f"✓ AI Workstation Save State Initialized with Engine: {stats['engine']}")
    print("✓ Stats Summary:", stats)
