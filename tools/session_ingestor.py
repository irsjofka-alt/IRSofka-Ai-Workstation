#!/usr/bin/env python3
"""Session Ingestor — rekam jejak nyata AI Workstation ke SQL (PostgreSQL primer, SQLite fallback).

Sumber yang dibaca (hanya tail file yang sudah ditulis CLI sendiri, tidak menyadap):
  1. segmen sesi Qoder    : ~/.ai-station/engines/qoder/logs/sessions/*/*/segments/*.jsonl
  2. transkrip sesi Qoder : ~/.ai-station/engines/qoder/projects/*/*.jsonl
  3. ringkasan Antigravity: ~/.gemini/antigravity/conversation_summaries.db

Prinsip desain: simpan METADATA + POINTER (raw_ref), BUKAN blob output tool.
Log ini untuk audit & pemulihan sesi, bukan untuk disuntikkan mentah ke konteks model.
Menjejalkan seluruh hasil perintah ke memori AI menurunkan kualitas nalar; ringkasan + pointer
memungkinkan sesi baru menarik detail hanya ketika dibutuhkan.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".ai-station" / "tools"))
from db_state import get_db_connection  # noqa: E402  adapter dual-engine resmi workstation

HOME = Path.home()
# Batas penyimpanan jejak, bukan batas tampilan. 400 membuat jawaban panjang terpotong
# sebelum sempat dibaca siapa pun — kolomnya `text`, jadi yang membatasi cuma angka ini.
SUMMARY_LIMIT = 8000
QODER_SEG_GLOB = str(HOME / ".ai-station/engines/qoder/logs/sessions/*/*/segments/*.jsonl")
QODER_PROJ_GLOB = str(HOME / ".ai-station/engines/qoder/projects/*/*.jsonl")
AGY_DB = HOME / ".gemini/antigravity/conversation_summaries.db"
PROFILES = HOME / ".ai-station/config/cli_profiles.json"
ENGINES_JSON = HOME / ".ai-station/config/engines.json"

SECRET_RE = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key|authorization|bearer|cookie)\b"
    r"(\s*[:=]\s*)(\S+)"
)


def redact(text) -> str:
    if not text:
        return ""
    clean = SECRET_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}[redacted]", str(text))
    return " ".join(clean.split())[:SUMMARY_LIMIT]


def is_pg(engine: str) -> bool:
    return engine == "POSTGRESQL"


LOCAL_TZ = datetime.now().astimezone().tzinfo


def parse_ts(raw):
    """Parsa ISO 8601 (mendukung 'Z' dan offset) lalu normalisasi ke zona lokal mesin."""
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "+00:00").replace("T", " ")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    dt = dt.replace(tzinfo=LOCAL_TZ) if dt.tzinfo is None else dt.astimezone(LOCAL_TZ)
    return dt


def ts_for_tstz(raw) -> str | None:
    dt = parse_ts(raw)
    return dt.isoformat() if dt else None


def ts_for_naive(raw) -> str | None:
    dt = parse_ts(raw)
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else None


def ph(n: int, engine: str) -> str:
    return ", ".join(["%s"] * n) if is_pg(engine) else ", ".join(["?"] * n)


DDL_PG = """
CREATE TABLE IF NOT EXISTS action_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    engine TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    tab TEXT,
    kind TEXT NOT NULL,
    model TEXT,
    effort TEXT,
    cwd TEXT,
    tool TEXT,
    summary TEXT,
    exit_code INT,
    duration_ms INT,
    input_tokens INT,
    output_tokens INT,
    cache_read_tokens INT,
    credits NUMERIC(12,4),
    raw_ref TEXT
);
CREATE TABLE IF NOT EXISTS engine_quota (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    engine TEXT NOT NULL,
    scope TEXT NOT NULL,
    limit_window TEXT NOT NULL,
    state TEXT NOT NULL,
    used NUMERIC,
    total NUMERIC,
    remaining_fraction NUMERIC,
    -- Kosakata satu (§12). remaining_fraction = pecahan 0..1 yang dilaporkan meter, dan
    -- itu SISA, bukan terpakai. remaining = jumlah absolut dalam satuan meter itu sendiri
    -- (kredit G1, dolar, apa pun). Keduanya NULL berarti meter tidak melaporkannya — nol
    -- punya makna sendiri, jadi tidak boleh dipakai sebagai pengganti "tidak ada angka".
    remaining NUMERIC(12,4),
    reset_at TIMESTAMPTZ,
    source TEXT NOT NULL,
    detail TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS sync_cursor (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    file_offset BIGINT DEFAULT 0,
    last_key TEXT,
    updated_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (source, path)
);
"""

DDL_SQLITE = """
CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    engine TEXT NOT NULL,
    session_id TEXT,
    turn_id TEXT,
    tab TEXT,
    kind TEXT NOT NULL,
    model TEXT,
    effort TEXT,
    cwd TEXT,
    tool TEXT,
    summary TEXT,
    exit_code INTEGER,
    duration_ms INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    credits REAL,
    raw_ref TEXT
);
CREATE TABLE IF NOT EXISTS engine_quota (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    engine TEXT NOT NULL,
    scope TEXT NOT NULL,
    limit_window TEXT NOT NULL,
    state TEXT NOT NULL,
    used REAL,
    total REAL,
    remaining_fraction REAL,
    -- sama artinya seperti di DDL_PG di atas; satu kosakata untuk dua backend
    remaining REAL,
    reset_at TIMESTAMP,
    source TEXT NOT NULL,
    detail TEXT,
    raw TEXT
);
CREATE TABLE IF NOT EXISTS sync_cursor (
    source TEXT NOT NULL,
    path TEXT NOT NULL,
    file_offset INTEGER DEFAULT 0,
    last_key TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (source, path)
);
"""

DDL_INDEX = [
    "CREATE INDEX IF NOT EXISTS idx_action_log_ts ON action_log (ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_action_log_session ON action_log (engine, session_id)",
    "CREATE INDEX IF NOT EXISTS idx_action_log_kind ON action_log (kind)",
    # Treasury: kolom credit jarang terisi (hanya engine yang melaporkannya), jadi indeksnya
    # parsial — memindai seluruh action_log untuk menjumlahkan kredit adalah cara termudah
    # membuat laporan biaya melambat tepat saat table itu terbesar.
    "CREATE INDEX IF NOT EXISTS idx_action_log_credits ON action_log (engine, ts DESC) "
    "WHERE credits IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_engine_quota_engine_ts ON engine_quota (engine, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_engine_quota_state ON engine_quota (state)",
]

# Kolom baru ada di kedua jalur: bagian CREATE TABLE untuk instalasi baru, ALTER untuk
# instalasi lama. IF NOT EXISTS membuat keduanya sama-sama aman.
DDL_MIGRATE_PG = [
    "ALTER TABLE action_log ADD COLUMN IF NOT EXISTS credits NUMERIC(12,4)",
    "ALTER TABLE engine_quota ADD COLUMN IF NOT EXISTS remaining NUMERIC(12,4)",
]



class Store:
    def __init__(self):
        self.conn, self.engine = get_db_connection()
        cur = self.conn.cursor()
        ddl = DDL_PG if is_pg(self.engine) else DDL_SQLITE
        # Komentar dibuang SEBELUM pemecahan pada ";": komentar penjelasan yang kebetulan
        # memakai titik-koma akan memotong CREATE TABLE di tengah dan gagal sebagai
        # "[ddl skip]" yang tidak dibaca siapa pun.
        for stmt in filter(None, (s.strip() for s in
                                  re.sub(r"--[^\n]*", "", ddl).split(";"))):
            try:
                cur.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                print(f"[ddl skip] {exc}", file=sys.stderr)
        # Kolom baru harus ada SEBELUM indeksnya: CREATE INDEX … WHERE credits IS NOT NULL
        # gagal diam-diam di basis data lama kalau migrasinya dijalankan belakangan, dan
        # indeks yang hilang tidak dilaporkan siapa pun sampai laporan biaya melambat.
        if is_pg(self.engine):
            for stmt in DDL_MIGRATE_PG:
                try:
                    cur.execute(stmt)
                except Exception as exc:  # noqa: BLE001
                    print(f"[migrate skip] {exc}", file=sys.stderr)
        else:
            try:
                self.migrate_sqlite_credits(cur)
            except Exception as exc:  # noqa: BLE001
                print(f"[migrate skip] {exc}", file=sys.stderr)
        for stmt in DDL_INDEX:
            try:
                cur.execute(stmt)
            except Exception:  # noqa: BLE001
                pass
        self.conn.commit()
        cur.close()

    def migrate_sqlite_credits(self, cur):
        """SQLite tidak mengenal ADD COLUMN IF NOT EXISTS: tanya dulu ke pragma."""
        for table, column, sqltype in (("action_log", "credits", "REAL"),
                                       ("engine_quota", "remaining", "REAL")):
            cols = [r[1].lower() for r in cur.execute(f"PRAGMA table_info({table})")]
            if cols and column not in cols:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {sqltype}")

    def execute(self, sql: str, params=()):
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            self.conn.commit()
            return cur
        except Exception as exc:  # noqa: BLE001
            print(f"[sql skip] {exc}", file=sys.stderr)
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            cur.close()
            return None

    def query(self, sql: str, params=()):
        cur = self.execute(sql, params)
        if cur is None:
            return []
        rows = cur.fetchall()
        cur.close()
        return rows

    def insert_event(self, ev: dict):
        cols = ["ts", "engine", "session_id", "turn_id", "tab", "kind", "model", "effort",
                "cwd", "tool", "summary", "exit_code", "duration_ms", "input_tokens",
                "output_tokens", "cache_read_tokens", "credits", "raw_ref"]
        vals = [ev.get(c) for c in cols]
        self.execute(
            f"INSERT INTO action_log ({', '.join(cols)}) VALUES ({ph(len(cols), self.engine)})",
            vals,
        )

    def cursor_get(self, source: str, path: str) -> int:
        rows = self.query(
            f"SELECT file_offset FROM sync_cursor WHERE source = {ph(1, self.engine)} "
            f"AND path = {ph(1, self.engine)}", (source, path))
        return int(rows[0][0] or 0) if rows else 0

    def cursor_set(self, source: str, path: str, offset: int, last_key: str = None):
        if is_pg(self.engine):
            self.execute(
                """INSERT INTO sync_cursor (source, path, file_offset, last_key, updated_at)
                   VALUES (%s, %s, %s, %s, now())
                   ON CONFLICT (source, path) DO UPDATE SET
                     file_offset = EXCLUDED.file_offset,
                     last_key = COALESCE(EXCLUDED.last_key, sync_cursor.last_key),
                     updated_at = now()""",
                (source, path, offset, last_key),
            )
        else:
            self.execute(
                """INSERT INTO sync_cursor (source, path, file_offset, last_key, updated_at)
                   VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT (source, path) DO UPDATE SET
                     file_offset = excluded.file_offset,
                     last_key = COALESCE(excluded.last_key, sync_cursor.last_key),
                     updated_at = CURRENT_TIMESTAMP""",
                (source, path, offset, last_key),
            )


def qoder_runtime_profile() -> dict:
    try:
        prof = json.loads(PROFILES.read_text()).get("qoder", {})
    except Exception:  # noqa: BLE001
        prof = {}
    return {"model": prof.get("model") or "Qwen3.8-Flash",
            "effort": prof.get("effort") or "",
            "context": int(prof.get("context_window") or 1_000_000),
            "tab": "qoder"}


def session_id_of(path: str) -> str:
    parts = Path(path).parts
    if "segments" in parts:
        return parts[parts.index("segments") - 1]
    return Path(path).stem


def pid_of_segment_name(path: str) -> str:
    stem = Path(path).stem
    return stem.rsplit("-p", 1)[1] if "-p" in stem else ""


def workspace_of(path: str) -> str:
    for part in reversed(Path(path).parts):
        if part.startswith("-"):
            return "/" + part.lstrip("-").replace("-", "/")
    return ""


def iter_lines_from(path: str, offset: int):
    """Yield (new_offset, parsed_json). Baca biner agar offset byte tetap akurat."""
    with open(path, "rb") as fh:
        fh.seek(offset)
        while True:
            line = fh.readline()
            if not line:
                break
            offset += len(line)
            try:
                yield offset, json.loads(line.decode("utf-8", "ignore"))
            except Exception:  # noqa: BLE001
                continue


def log_event(store, base, **over):
    ev = dict(base)
    ev.update(over)
    store.insert_event(ev)


def ingest_qoder_segments(store: Store, meta: dict) -> int:
    count = 0
    for path in sorted(glob.glob(QODER_SEG_GLOB)):
        size = os.path.getsize(path)
        offset = store.cursor_get("qoder_segment", path)
        if offset > size:
            offset = 0
        if offset == size:
            continue
        sid = session_id_of(path)
        cwd_hint = workspace_of(path)
        turn_prompt: dict[str, str] = {}
        turn_row: dict[str, int] = {}
        for new_off, ev in iter_lines_from(path, offset):
            offset = new_off
            count += emit_qoder_event(store, ev, sid, path, meta, cwd_hint, turn_prompt, turn_row)
        store.cursor_set("qoder_segment", path, min(offset, size))
    return count


def emit_qoder_event(store, ev, sid, path, meta, cwd_hint, turn_prompt, turn_row) -> int:
    etype = ev.get("type", "")
    data = ev.get("data") or {}
    base = {
        "ts": ts_for_tstz(ev.get("ts")),
        "engine": "qoder", "session_id": sid, "turn_id": ev.get("turn_id") or "",
        "tab": meta["tab"], "model": meta["model"], "effort": meta["effort"],
        "cwd": data.get("cwd") or cwd_hint, "tool": None, "summary": None,
        "exit_code": None, "duration_ms": None, "input_tokens": None,
        "output_tokens": None, "cache_read_tokens": None, "raw_ref": path,
    }

    if etype == "session.config.loaded":
        log_event(store, base, kind="session_start", cwd=data.get("project_root"),
                  model=data.get("model") or meta["model"],
                  summary=redact(f"permission_mode={data.get('permission_mode')} "
                                 f"target_dir={data.get('target_dir')}"))
    elif etype == "input.prompt.received":
        tid = ev.get("turn_id") or ""
        prompt = redact(data.get("text_preview", ""))
        turn_prompt[tid] = prompt
        log_event(store, base, kind="prompt", summary=prompt,
                  raw_ref=f"{path}#seq={ev.get('seq')}")
        row_id = record_turn(store, meta, ev.get("ts"), prompt)
        if row_id:
            turn_row[tid] = row_id
    elif etype == "tool.requested":
        args = data.get("args") or {}
        # Qoder MEMOTONG argumen tool yang besar menjadi {"truncated":true,"preview":...} —
        # dan potongan itu tidak lagi menyisakan file_path. Rantai fallback yang berakhir di
        # json.dumps(args) lalu menjadikan seluruh cuplikan isi berkas sebagai "jejak", yang
        # di halaman Artefacts tampil sebagai satu kartu raksasa berisi CSS. Sebuah artefak
        # harus bernama berkas: kalau namanya tidak ada, lebih baik tidak dicatat sama sekali.
        brief = (args.get("command") or args.get("prompt") or args.get("file_path")
                 or args.get("path") or args.get("notebook_path") or args.get("subject")
                 or ("[args terpotong]" if args.get("truncated") else json.dumps(args, default=str)))
        log_event(store, base, kind="tool_call", tool=data.get("tool_name"),
                  summary=redact(brief))
    elif etype == "tool.shell.started":
        log_event(store, base, kind="command", cwd=data.get("cwd"), tool="Bash",
                  summary=redact(data.get("command", "")), raw_ref=f"{path}#seq={ev.get('seq')}")
    elif etype == "tool.shell.finished":
        log_event(store, base, kind="command_exit", tool="Bash",
                  exit_code=data.get("exit_code"),
                  summary=redact(f"aborted={data.get('aborted')} out_chars={data.get('output_length')}"),
                  raw_ref=data.get("output_path") or path)
    elif etype == "model.response.completed":
        log_event(store, base, kind="model_response", model=data.get("model") or meta["model"],
                  summary=redact(f"stop_reason={data.get('stop_reason')} "
                                 f"blocks={data.get('content_block_count')}"),
                  input_tokens=data.get("input_tokens"),
                  output_tokens=data.get("output_tokens"),
                  cache_read_tokens=data.get("cache_read_input_tokens"))
    elif etype == "turn.finished":
        tid = ev.get("turn_id") or ""
        log_event(store, base, kind="turn", duration_ms=data.get("duration_ms"),
                  input_tokens=data.get("input_tokens"),
                  output_tokens=data.get("output_tokens"),
                  cache_read_tokens=data.get("cache_read_input_tokens"),
                  summary=redact(f"reason={data.get('reason')} loop_iters={data.get('num_turns')}"))
        row_id = turn_row.get(tid)
        if row_id:
            store.execute(
                f"UPDATE session_turns SET duration_ms = {ph(1, store.engine)}, "
                f"context_window = {ph(1, store.engine)} WHERE id = {ph(1, store.engine)}",
                (data.get("duration_ms"), meta["context"], row_id))
        turn_row.pop(tid, None)
    else:
        return 0
    return 1


def record_turn(store, meta, ts, prompt) -> int:
    if not prompt:
        return 0
    cols = ["timestamp", "cli_engine", "model", "reasoning_effort", "context_window",
            "prompt", "auto_approved"]
    params = [ts_for_naive(ts), "qoder", meta["model"], meta["effort"],
              meta["context"], prompt, True]
    if is_pg(store.engine):
        rows = store.query(
            f"INSERT INTO session_turns ({', '.join(cols)}) "
            f"VALUES ({ph(len(cols), store.engine)}) RETURNING id", params)
        return int(rows[0][0]) if rows else 0
    cur = store.execute(
        f"INSERT INTO session_turns ({', '.join(cols)}) "
        f"VALUES ({ph(len(cols), store.engine)})", params)
    return int(cur.lastrowid) if cur is not None else 0


def ingest_qoder_transcripts(store: Store, meta: dict) -> int:
    count = 0
    for path in sorted(glob.glob(QODER_PROJ_GLOB)):
        size = os.path.getsize(path)
        offset = store.cursor_get("qoder_transcript", path)
        if offset > size:
            offset = 0
        if offset == size:
            continue
        sid = Path(path).stem
        cwd = workspace_of(path)
        for new_off, rec in iter_lines_from(path, offset):
            offset = new_off
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            blocks = msg.get("content") or []
            texts = [b.get("text", "") for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text" and b.get("text", "").strip()]
            if not texts:
                continue
            store.insert_event({
                "ts": ts_for_tstz(rec.get("timestamp")), "engine": "qoder", "session_id": sid,
                "turn_id": rec.get("uuid") or "", "tab": meta["tab"], "kind": "response",
                "model": msg.get("model") or meta["model"], "effort": meta["effort"],
                "cwd": cwd, "tool": None, "summary": redact(" ".join(texts)),
                "exit_code": None, "duration_ms": None, "input_tokens": None,
                "output_tokens": None, "cache_read_tokens": None,
                "raw_ref": f"{path}#{rec.get('uuid', '')}",
            })
            count += 1
        store.cursor_set("qoder_transcript", path, min(offset, size))
    return count


def ingest_antigravity(store: Store) -> int:
    if not AGY_DB.exists():
        return 0
    _, _, marker = get_cursor_state(store, str(AGY_DB), "agy_summaries")
    try:
        conn = sqlite3.connect(f"file:{AGY_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "select conversation_id, title, step_count, last_modified_time, workspace_uris, "
            "status, project_id, agent_name from conversation_summaries "
            "order by last_modified_time desc limit 20")]
        conn.close()
    except Exception:  # noqa: BLE001
        return 0
    if not rows:
        return 0
    head = f"{rows[0]['conversation_id']}:{rows[0]['last_modified_time']}:{rows[0]['step_count']}"
    if head == marker:
        return 0
    for d in rows:
        store.insert_event({
            "ts": ts_for_naive(d.get("last_modified_time")),
            "engine": "antigravity", "session_id": str(d.get("conversation_id") or ""),
            "turn_id": "", "tab": "antigravity", "kind": "agy_conversation",
            "model": str(d.get("agent_name") or ""), "effort": "",
            "cwd": redact(d.get("workspace_uris"))[:180] or None, "tool": None,
            "summary": redact(f"{d.get('title','')} | steps={d.get('step_count',0)} | "
                              f"status={str(d.get('status','')).replace('CASCADE_RUN_STATUS_','').lower()}"),
            "exit_code": None, "duration_ms": None, "input_tokens": None,
            "output_tokens": None, "cache_read_tokens": None,
            "raw_ref": f"{AGY_DB}#{d.get('project_id')}",
        })
    store.cursor_set("agy_summaries", str(AGY_DB), 0, head)
    return len(rows)


def get_cursor_state(store, path, source):
    rows = store.query(
        f"SELECT file_offset, last_key FROM sync_cursor WHERE source = {ph(1, store.engine)} "
        f"AND path = {ph(1, store.engine)}", (source, path))
    if not rows:
        return 0, None, ""
    return int(rows[0][0] or 0), rows[0][1], (rows[0][1] or "")


def recent(store: Store, limit: int, engine: str = "", kind: str = ""):
    where, params = [], []
    if engine:
        where.append(f"engine = {ph(1, store.engine)}")
        params.append(engine)
    if kind:
        where.append(f"kind = {ph(1, store.engine)}")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = store.query(
        f"SELECT ts, engine, kind, tool, cwd, summary, exit_code, duration_ms, session_id "
        f"FROM action_log {clause} ORDER BY ts DESC, id DESC LIMIT {ph(1, store.engine)}",
        tuple(params) + (limit,))
    for ts, eng, k, tool, cwd, summary, exit_code, dur, sid in reversed(rows):
        flag = "" if exit_code in (None, 0) else f"  exit={exit_code}"
        durtxt = f"  {dur}ms" if dur else ""
        print(f"[{str(ts)[:19]}] {str(eng):<11} {str(k):<14} {str(tool or '-'):<12} "
              f"{str(summary or '')[:120]}{flag}{durtxt}")
    return len(rows)


def stats(store: Store):
    print(f"DB engine: {store.engine}")
    rows = store.query("SELECT engine, kind, COUNT(*), MAX(ts) FROM action_log "
                       "GROUP BY engine, kind ORDER BY engine, kind")
    for eng, kind, n, last in rows:
        print(f"  {eng:<12} {kind:<16} {n:>6}  terakhir: {str(last)[:19]}")
    turns = store.query("SELECT COUNT(*) FROM session_turns")[0][0]
    mem = store.query("SELECT COUNT(*) FROM world_memory")[0][0]
    quest = store.query("SELECT COUNT(*) FROM quest_tasks WHERE status = "
                        f"{ph(1, store.engine)}", ("IN_PROGRESS",))[0][0]
    print(f"  session_turns={turns}  world_memory={mem}  quest IN_PROGRESS={quest}")


def handoff(store: Store):
    rows = store.query(
        f"SELECT key, category, substr(content,1,2600), updated_at FROM world_memory "
        f"WHERE key LIKE {ph(1, store.engine)} OR category = {ph(1, store.engine)} "
        f"ORDER BY updated_at DESC LIMIT 6", ("%handoff%", "rule"))
    for key, cat, content, upd in rows:
        print(f"\n### {key}  [{cat}]  ({str(upd)[:19]})\n{content}")
    rows = store.query(
        f"SELECT id, title, status, model_assigned, substr(summary,1,400) FROM quest_tasks "
        f"WHERE status = {ph(1, store.engine)} ORDER BY id DESC LIMIT 5", ("IN_PROGRESS",))
    for rid, title, status, model, summ in rows:
        print(f"\n> QUEST AKTIF #{rid} [{status}] {title} ({model})\n  {summ}")


BRAIN_PROJECTS = HOME / ".ai-station/brain/memory/projects"


def _probe(cmd: list, timeout: int = 4) -> str:
    """Jalankan perintah inspeksi; kembalikan teks error ALIH-ALIH menelan diam-diam."""
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, timeout=timeout)
        out = (res.stdout or "").strip()
        return out if res.returncode == 0 else f"(exit {res.returncode}) {out[:300]}"
    except FileNotFoundError:
        return "(perintah tidak tersedia)"
    except Exception as exc:  # noqa: BLE001
        return f"(gagal: {exc})"


def snapshot(store: Store, session_id: str = "", limit: int = 25) -> str:
    """Tulis handoff MEKANIS saat sesi berakhir. Dipanggil hook SessionEnd.

    Alasan: seluruh disiplin "tulis handoff sebelum berhenti" bergantung pada AI yang
    masih sadar dan ingat. Saat konteks terputus, justru di situlah handoff paling
    dibutuhkan dan paling tidak mungkin ditulis. Rekaman ini tidak mengarang ringkasan —
    ia menyalin jejak yang benar-benar tereksekusi, sehingga sesi berikutnya punya
    titik lanjut yang jujur.
    """
    sid = session_id or os.environ.get("QODER_SESSION_ID") or ""
    stamp = datetime.now()

    def last_rows(kind: str, n: int):
        where, args = [f"kind = {ph(1, store.engine)}"], [kind]
        if sid:
            where.append(f"session_id = {ph(1, store.engine)}")
            args.append(sid)
        rows = store.query(
            f"SELECT ts, summary, cwd, exit_code, raw_ref FROM action_log "
            f"WHERE {' AND '.join(where)} ORDER BY ts DESC, id DESC "
            f"LIMIT {ph(1, store.engine)}", tuple(args) + (n,))
        return list(reversed(rows))

    prompts = last_rows("prompt", 3)
    replies = last_rows("response", 2)
    commands = last_rows("command", limit)
    quests = store.query(
        f"SELECT id, title, status, substr(summary,1,300) FROM quest_tasks "
        f"WHERE status = {ph(1, store.engine)} ORDER BY id DESC LIMIT 8", ("IN_PROGRESS",))

    if not (prompts or replies or commands or quests):
        # Handoff kosong lebih berbahaya daripada tidak ada handoff sama sekali: ia tercatat
        # sebagai "sesi terakhir" dan membaca isinya "tidak mengerjakan apa pun", padahal
        # yang benar jejaknya belum sempat masuk SQL. Sesi berikutnya akan percaya itu.
        return "handoff dilewati: tidak ada satu pun baris terekam untuk sesi ini"

    lines = [
        f"# Auto-Handoff — {stamp:%Y-%m-%d %H:%M:%S}",
        "",
        f"Sesi: `{sid or 'tanpa id — baris di bawah adalah jejak terbaru apa pun sesinya'}`  •  "
        "Ditulis otomatis oleh hook SessionEnd, "
        "bukan karangan AI. Isinya jejak perintah yang benar-benar tereksekusi.",
        "",
        "## Permintaan terakhir pengguna",
    ]
    for ts, summary, _cwd, _ec, ref in prompts or []:
        lines.append(f"- `[{str(ts)[:19]}]` {summary}")
    if not prompts:
        lines.append("- (tidak ada prompt terekam untuk sesi ini)")

    lines += ["", "## Jawaban terakhir AI"]
    for ts, summary, _cwd, _ec, ref in replies or []:
        lines.append(f"- `[{str(ts)[:19]}]` {summary}")
    if not replies:
        lines.append("- (tidak ada jawaban terekam)")

    lines += ["", f"## {len(commands)} perintah terakhir (exit code terlihat)"]
    for ts, summary, cwd, ec, _ref in commands or []:
        flag = "" if ec in (None, 0) else f"  **exit={ec}**"
        one = (summary or "").replace("\n", " ")[:160]
        lines.append(f"- `[{str(ts)[:19]}]` `{cwd or '-'}` :: {one}{flag}")
    if not commands:
        lines.append("- (tidak ada perintah terekam)")

    lines += ["", "## Quest yang masih terbuka"]
    for rid, title, status, summ in quests or []:
        lines.append(f"- #{rid} [{status}] {title} :: {summ}")
    if not quests:
        lines.append("- (tidak ada quest IN_PROGRESS)")

    # Repo-nya SATU di ~/.ai-station. Dulu probe diarahkan ke engine-rust/ dan dilabeli
    # "git engine-rust" — direktori itu tidak punya .git sendiri, jadi git hanya naik ke
    # repo induk dan hasilnya salah dibaca sebagai repo terpisah.
    station = HOME / ".ai-station"
    git_branch = _probe(['git', '-C', str(station), 'rev-parse', '--abbrev-ref', 'HEAD']).strip()[:30]
    git_head = _probe(['git', '-C', str(station), 'log', '--oneline', '-1']).strip()[:120]
    dirty_lines = [l for l in _probe(['git', '-C', str(station), 'status', '--porcelain']).splitlines() if l.strip()]
    contoh = ", ".join(l.split()[-1].split("/")[-1] for l in dirty_lines[:4])

    lines += [
        "",
        "## Keadaan mesin saat sesi mati",
        f"- waktu: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`",
        f"- repo `~/.ai-station` · branch `{git_branch or '?'}` · `{git_head or 'tidak terdeteksi'}`",
        f"- belum di-commit: **{len(dirty_lines)} berkas**"
        + (f" (mis. {contoh})" if dirty_lines else " — pohon kerja bersih"),
        f"- tab tmux: `{_probe(['tmux', '-L', 'irsofka', 'list-sessions'])[:300]}`",
        f"- daemon: `{_probe(['systemctl', '--user', 'is-active',
                             'irsofka-ai-workstation.service'])[:60]}`",
        "",
        "## Cara melanjutkan",
        "Jalankan `ai-station recovery 40` untuk riwayat penuh, lalu baca berkas ini. "
        "Jangan menyimpulkan keadaan dari ingatan — verifikasi `md5sum` biner dan "
        "`/api/workspace` sebelum mengulang pekerjaan.",
    ]
    content = "\n".join(lines)

    safe_sid = re.sub(r"[^0-9a-zA-Z_-]", "", sid)[:8] or "sesi"
    key = f"handoff_auto_{stamp:%Y%m%d-%H%M}_{safe_sid}"
    store.execute(
        f"INSERT INTO world_memory (key, category, content, tags, updated_at) "
        f"VALUES ({ph(5, store.engine)}) "
        "ON CONFLICT (key) DO UPDATE SET content = EXCLUDED.content, updated_at = EXCLUDED.updated_at",
        (key, "session_recovery", content, "auto,session_end", stamp.strftime("%Y-%m-%d %H:%M:%S")))

    try:
        BRAIN_PROJECTS.mkdir(parents=True, exist_ok=True)
        path = BRAIN_PROJECTS / f"handoff_auto_{stamp:%Y-%m-%d_%H%M}_{safe_sid}.md"
        path.write_text(content + "\n", encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001
        print(f"[snapshot] berkas handoff gagal ditulis: {exc}", file=sys.stderr)
        return "(hanya tersimpan di world_memory)"


def ingest_station_spool(store: Store) -> int:
    """Spool kejadian dari daemon Rust: ~/.ai-station/logs/station_events.jsonl -> action_log.

    Daemon tidak menyentuh SQL langsung; ia menulis baris JSON ke file spool, dan ingestor
    ini yang memipanya ke PostgreSQL (atau SQLite saat PostgreSQL tumbang). Satu jalur SQL,
    satu tempat dialek.
    """
    path = HOME / ".ai-station/logs/station_events.jsonl"
    if not path.exists():
        return 0
    size = path.stat().st_size
    offset = store.cursor_get("station_spool", str(path))
    if offset > size:
        offset = 0
    count = 0
    for new_off, ev in iter_lines_from(str(path), offset):
        offset = new_off
        store.insert_event({
            "ts": ts_for_tstz(ev.get("ts")), "engine": "station",
            "session_id": ev.get("session_id"), "turn_id": "", "tab": ev.get("tab"),
            "kind": ev.get("kind") or "event", "model": ev.get("model"),
            "effort": ev.get("effort"), "cwd": ev.get("cwd"), "tool": ev.get("tool"),
            "summary": redact(ev.get("summary")), "exit_code": ev.get("exit_code"),
            "duration_ms": ev.get("duration_ms"), "input_tokens": None,
            "output_tokens": None, "cache_read_tokens": None,
            "raw_ref": f"{path}#seq={ev.get('seq', '')}",
        })
        count += 1
    store.cursor_set("station_spool", str(path), min(offset, size))
    return count


def error_signature(command_text: str, exit_code) -> str:
    """Signature stabil dari perintah gagal: nama alat + argumen pendek, TANPA jejak path.

    Normalisasi ini yang membuat self-healing mungkin terjadi. Kalau signature memuat path
    lengkap, `cd ~/.ai-station/tools && python3 ...` dan variasi lain dari kegagalan yang
    sama tercatat sebagai insiden berbeda, ambang 5x tidak pernah tercapai, dan daftar
    insiden berubah menjadi sampah yang tidak dibaca siapa pun.
    """
    text = " ".join(str(command_text or "").split())
    # lewati pengantar yang bukan inti kegagalan (cd/export/source/set) dan potong operator
    for chunk in re.split(r"&&|;|\||\n", text):
        chunk = chunk.strip()
        if not chunk or re.match(r"^(cd|export|source|\.)\b", chunk):
            continue
        text = chunk
        break
    parts = []
    for tok in text.split(" "):
        if len(parts) >= 3:
            break
        if not tok or tok.startswith("-") or tok.startswith("2>") or tok in (">", ">>", "|"):
            continue
        if "/" in tok or tok.startswith("~"):
            tok = tok.rstrip("/").split("/")[-1]      # path -> nama terakhir
        tok = re.sub(r"[^0-9A-Za-z_.]", "", tok)
        if tok:
            parts.append(tok[:20])
    head = "_".join(parts) or "perintah"
    return f"exit{exit_code}:{head}"[:255]


def sweep_failures(store: Store) -> int:
    """Ubah kegagalan perintah yang sudah terekam menjadi insiden.

    Ini jalur yang menghubungkan 'AI gagal' menjadi 'AI belajar': incident_recorder
    membentuk skill otomatis pada ambang 5x, tapi selama ini tidak ada yang pernah
    memanggilnya. Event shell.finished dari CLI tidak membawa teks perintahnya, jadi
    pemasangan dilakukan di SQL: perintah terdekat sebelum exit_code != 0 pada sesi sama.
    """
    try:
        from incident_recorder import record_incident
    except Exception as exc:  # noqa: BLE001
        print(f"[sweep] incident_recorder tidak tersedia: {exc}", file=sys.stderr)
        return 0

    last = store.cursor_get("incident_sweep", "action_log") or 0
    # CATATAN: ph(n) menghasilkan n placeholder yang digabung, BUKAN placeholder ke-n.
    rows = store.query(
        f"SELECT id, session_id, exit_code, summary FROM action_log "
        f"WHERE kind = 'command_exit' AND exit_code IS NOT NULL AND exit_code <> 0 "
        f"AND id > {ph(1, store.engine)} ORDER BY id ASC LIMIT {ph(1, store.engine)}",
        (last, 200))
    if not rows:
        return 0

    newest = last
    for rid, sid, exit_code, summary in rows:
        cmd = ""
        if sid:
            found = store.query(
                f"SELECT summary FROM action_log WHERE kind = 'command' "
                f"AND session_id = {ph(1, store.engine)} AND id < {ph(1, store.engine)} "
                f"ORDER BY id DESC LIMIT 1", (sid, rid))
            cmd = found[0][0] if found else ""
        if not cmd:
            cmd = summary or ""
        sig = error_signature(cmd, exit_code)
        try:
            record_incident(sig, f"perintah gagal (exit {exit_code}): {str(cmd)[:200]}",
                            "Periksa pesan error pada baris ini, lalu putuskan apakah "
                            "perlu skill baru atau cukup koreksi sekali jalan.")
        except Exception as exc:  # noqa: BLE001
            print(f"[sweep] gagal mencatat insiden {sig}: {exc}", file=sys.stderr)
        newest = max(newest, int(rid))
    store.cursor_set("incident_sweep", "action_log", newest)
    return len(rows)


# ─────────────────────────── Treasury (F8.2): membaca meter tiap mesin ───────────────────────────
#
# Dua hal berbeda dan keduanya ada di sini, karena mencampur keduanya adalah cara
# paling cepat membuat laporan yang terlihat benar:
#   KUOTA  = sisa jatah akun. Hanya CLI yang tahu angka ini, jadi ia dibaca dari meternya
#            dan disimpan sebagai pembacaan (baris engine_quota). Tidak pernah dikurangi
#            dengan asumsi.
#   BIAYA  = yang habis dipakai hari ini. Sumbernya transaksi (action_log). Kalau kolom
#            kredit/token tidak terisi, laporannya UNAVAILABLE dengan alasan faktual —
#            bukan nol, bukan taksiran.
#
# Instalasi lain (mis. orang yang meng-clone repo ini tanpa Qoder/Antigravity) tidak perlu
# mengubah kode: blok "usage" di engines.json yang berubah. probe null / parser "none"
# berarti mesin itu memang tidak punya meter, dan Treasury menuliskan UNSUPPORTED.

TREASURY_SOCKET = "treasury"        # socket tmux KHUSUS meter — operator memakai "irsofka"
TREASURY_TAB = "usage"
TREASURY_DIR = HOME / "runtime/treasury-probe"
TREASURY_EVERY_S = int(os.environ.get("STATION_TREASURY_EVERY", "900"))
TREASURY_DETAIL_MAX = 400

# Hanya kunci meter yang boleh keluar dari layar panel. Yang lain adalah data akun
# (nama, login, session id, working directory) — panel /usage menampilkannya bersebelahan,
# jadi menyipan seluruh layar ke kolom raw akan membocorkan identitas ke basis data.
QODER_PANEL_METERS = {
    "Plan Credits Used": "plan-credits",
    "Add-on Credits Used": "add-on-credits",
    "Org Resource Package": "org-resource-package",
}
QODER_PANEL_WINDOW_KEY = "Plan Expires At"
# Kunci yang bukan meter, tapi bukti bahwa sesi probe tidak sedang mengerjakan apa pun.
QODER_PANEL_PROOF = ("Total Duration (API)", "Total Code Changes")
PANEL_BOOT_S = 12         # detik minimum sebelum probe boleh mengetik apa pun
PANEL_SETTLE_TICKS = 3    # layar harus identik 3 capture berturut-turut (1 detik tiap capture)
PANEL_TRIES = 2           # lebih dari sekali mengetik ke TUI yang tidak merespons = risiko turn
# Yang membuktikan sesi ini benar-benar kosong dan benar-benar milik probe.
PANEL_FLAGS = ["--tools=", "--strict-mcp-config"]
PANEL_ENV_UNSET = ("QODER_PID", "QODER_CLI", "__QODER_BASH_PATH_PREFIX",
                   "QODER_SECURITY_SCAN_SETTINGS_JSON")
# Tanda di layar bahwa perintah garis miring diperlakukan sebagai prompt dan model sudah
# mulai bekerja. Keduanya terukur saat insiden, bukan tebakan.
PANEL_TURN_MARKS = ("esc to cancel", "Thinking", "Writing")


def _panel_ready(screen: str) -> bool:
    """Kolom input terlihat, baris status bawah tergambar, dan konteks masih 0%.

    Syarat ketiga bukan seremonial. Ketika probe sempat membawa percakapan sesi aktif,
    baris statusnya menunjukkan ctx 2%/14% — dan perintah /usage yang diketik ke sesi itu
    berubah menjadi turn model sungguhan: kredit terpakai dan tab shell operator ikut
    diketiki. Konteks nol + footer siap adalah satu-satunya cara melihat dari luar bahwa
    TUI sudah hidup DAN sesi ini benar-benar kosong.
    """
    return (_panel_prompt(screen)
            and ("skills" in screen or "MCP servers" in screen)
            and " 0%" in screen)


def _panel_echo(screen: str, panel: str) -> bool:
    """Apakah teks perintah benar-benar masuk ke kolom input (baris "> ...")."""
    for line in screen.splitlines():
        stripped = line.strip()
        if stripped.startswith(">") and panel in stripped:
            return True
    return False


def _turn_started(screen: str) -> bool:
    return any(mark in screen for mark in PANEL_TURN_MARKS)


def _proof_worked(proof: dict) -> bool:
    """Apakah panel membuktikan sesi probe sempat BEKERJA (bukan hanya membaca meternya).

    Panel /usage menghitung sesi yang berjalan, jadi "0.0s API" dan "0 lines" adalah bukti
    bahwa pembacaan meter tidak memakan kuota. Salah satu Probe di workstation ini pernah
    menjalankan satu turn model sungguhan; angka dari sesi semacam itu bukan angka kuota.
    """
    return any(float(n or 0) > 0
               for value in proof.values()
               for n in re.findall(r"\d+(?:\.\d+)?", str(value)))


def _panel_prompt(screen: str) -> bool:
    """Apakah kolom input Qoder sudah kelihatan di layar."""
    return "Type your message" in screen or "? for shortcuts" in screen


def registry_usage() -> dict:
    """Blok 'usage' per mesin dari engines.json: satu-satunya tempat meter dinyatakan."""
    try:
        reg = json.loads(ENGINES_JSON.read_text()).get("engines") or {}
    except Exception:  # noqa: BLE001
        return {}
    return {name: spec["usage"] for name, spec in reg.items()
            if isinstance(spec, dict) and spec.get("usage")}


def _probe_argv(probe) -> list:
    """Argv meter dari registry, dalam dua bentuk yang wajar ditulis orang.

    List adalah bentuk di engines.json workstation ini. String adalah bentuk yang
    akan ditulis orang lain yang mengedit konfigurasi dari git — dan tanpa normalizer
    ini string di-iter per KARAKTER, sehingga "agy -p /usage" menjadi argv ['a','g','y',...]
    dan kegagalan yang dilaporkan menyebut perintah 'a'. Bentuknya salah, angkanya hilang.
    """
    if isinstance(probe, (list, tuple)):
        return [str(x) for x in probe]
    return shlex.split(str(probe))


METER_SEARCH_GLOBS = (".local/bin", "bin", ".cargo/bin", "runtime/local/bin",
                      ".gemini/*/bin", ".gemini/*/*/bin")


def _meter_search_path(extra=()) -> str:
    """PATH tempat perintah meter benar-benar dicari.

    Layanan systemd dan server tmux mewarisi PATH sistem saja (/usr/bin, /bin), sementara
    biner CLI dipasang di luar itu. Terukur di mesin ini: agy = ~/.local/bin (symlink ke
    ~/runtime/local/bin), qoder = ~/.gemini/antigravity/bin (symlink ke
    engines/qoder/bin/qodercli). Keduanya ELF, jadi shebang `env node` tidak menjadi soal di
    sini — tapi PATH yang sama tetap ikut diberikan ke proses anak, karena asumsi "biner
    statis" tidak boleh diwarisi oleh mesin lain yang meng-clone repo ini. GLOBS hanyalah
    kenyamanan bawaan; host lain menuliskan "search_path" di blok usage-nya sendiri.
    """
    home = str(Path.home())
    found = [p for pat in METER_SEARCH_GLOBS for p in glob.glob(os.path.join(home, pat))]
    dirs = [str(p) for p in list(found) + list(extra) if os.path.isdir(str(p))]
    base = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    return os.pathsep.join(dict.fromkeys(base + dirs))


def _resolve_argv(cmd: list, extra=()) -> list:
    """Ganti nama perintah meter dengan path absolutnya bila PATH saat ini tidak melihatnya."""
    if not cmd or os.path.sep in str(cmd[0]):
        return cmd
    if shutil.which(cmd[0]):
        return cmd
    found = shutil.which(cmd[0], path=_meter_search_path(extra))
    return [found or cmd[0], *cmd[1:]]


def _run(cmd: list, timeout: int = 30, extra_path=()) -> tuple[int, str]:
    """Jalankan perintah meter. rc != 0 tetap mengembalikan teksnya: alasannya perlu dilaporkan."""
    cmd = _resolve_argv(list(cmd), extra_path)
    env = {**os.environ, "PATH": _meter_search_path(extra_path)}
    try:
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, timeout=timeout, env=env)
        return res.returncode, (res.stdout or "").strip()
    except FileNotFoundError:
        return 127, (f"(command {cmd[0]!r} is not installed on this machine; "
                     f"searched {env['PATH']!r})")
    except subprocess.TimeoutExpired:
        return 124, f"(meter did not answer within {timeout}s)"
    except Exception as exc:  # noqa: BLE001
        return 1, f"(gagal: {type(exc).__name__}: {exc})"


def _tmux(*args: str) -> tuple[int, str]:
    """Semua tmux Treasury lewat socket sendiri.

    Penjaga di bawah ini bukan hiasan: konfigurasi yang salah socket membuat daemon
    mengetik "/usage" ke pane tempat operator sedang bekerja.
    """
    if TREASURY_SOCKET == "irsofka":
        return 1, "(probe ditolak: socket meter tidak boleh sama dengan socket operator)"
    return _run(["tmux", "-L", TREASURY_SOCKET, *[str(a) for a in args]], 15)


def _first_json(text: str):
    start = text.find("{")
    if start < 0:
        return None
    for candidate in (text[start:], text[start:text.rfind("}") + 1]):
        try:
            return json.loads(candidate)
        except Exception:  # noqa: BLE001
            continue
    return None


def _frac(v):
    """Pecahan hanya kalau CLI memang melaporkannya sebagai pecahan."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if 0.0 <= f <= 1.0 else None


def parse_agy_usage_json(text: str) -> tuple[list[dict], str]:
    """`agy -p /usage --output-format json` → satu baris kuota per kelompok model × jendela.

    Struktur `{command.data.groups[].buckets[]}` adalah bentuk resmi CLI-nya, jadi tidak ada
    layar yang perlu dibaca. Bentuk lama/berbeda masih bisa dipakai lewat teks `response`
    (TSV: kelompok, nama jendela, persen, waktu reset) — kalau keduanya tidak dikenali,
    yang dilaporkan adalah UNAVAILABLE, bukan nol.
    """
    payload = _first_json(text)
    if payload is None:
        return [], "output is not JSON"
    groups = (((payload.get("command") or {}).get("data") or {}).get("groups")) or []
    rows = []
    for g in groups:
        scope = str(g.get("name") or "model group")
        for b in g.get("buckets") or []:
            frac = _frac(b.get("remaining_fraction"))
            rows.append({
                "scope": scope,
                "limit_window": str(b.get("window") or b.get("name") or ""),
                "state": "REPORTED" if frac is not None else "UNAVAILABLE",
                "used": None,
                "total": None,
                "remaining_fraction": frac,
                "reset_at": ts_for_tstz(b.get("reset_time")),
                "detail": str(b.get("description") or b.get("name") or ""),
                "raw": json.dumps({k: v for k, v in b.items()
                                   if k in ("id", "name", "window", "remaining_fraction",
                                            "reset_time")}, ensure_ascii=False),
            })
    if rows:
        return rows, ""

    for line in str(payload.get("response") or "").splitlines():
        parts = [p.strip() for p in line.split("\t")]
        if len(parts) < 4 or "Limit" not in parts[1]:
            continue
        try:
            frac = float(parts[2].rstrip("%")) / 100.0
        except ValueError:
            continue
        rows.append({
            "scope": parts[0], "limit_window": parts[1],
            "state": "REPORTED", "used": None, "total": None,
            "remaining_fraction": _frac(frac), "reset_at": ts_for_tstz(parts[3]),
            "detail": "read from the response text (no structured payload)",
            "raw": json.dumps({"line": line}, ensure_ascii=False),
        })
    if rows:
        return rows, ""
    return [], "no quota group in the CLI output (did its shape change?)"


def parse_agy_credits_json(text: str) -> tuple[list[dict], str]:
    """`agy -p /credits --output-format json` → satu baris SALDO kredit akun.

    Antigravity tidak mengenal biaya harian maupun jendela harian — hanya 5h dan weekly,
    dan itu sudah dibaca meter `usage`. Yang tersisa dari CLI ini adalah saldo absolut, jadi
    kolomnya `remaining`, bukan `remaining_fraction`: CLI tidak pernah menyebut penyebutnya,
    dan mengarang penyebut (mis. 100) adalah cara tercepat membuat laporan yang salah.
    Saldo 0 adalah angka sungguhan — Free plan memang nol — jadi state-nya REPORTED.
    """
    payload = _first_json(text)
    if payload is None:
        return [], "output is not JSON"
    data = (payload.get("command") or {}).get("data") or {}
    value = data.get("remaining_credits")
    if value is None:
        for line in str(payload.get("response") or "").splitlines():
            parts = [p.strip() for p in line.split("\t")]
            if len(parts) >= 2 and parts[0].lower().startswith("remaining credit"):
                value = parts[1]
                break
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return [], "the credit balance is not readable as a number in the CLI output"
    return [{
        "scope": "credits",
        "limit_window": "balance",
        "state": "REPORTED",
        "used": None,
        "total": None,
        "remaining_fraction": None,
        "remaining": amount,
        "reset_at": None,
        "detail": "account credit balance (absolute), not daily consumption",
        "raw": json.dumps({"remaining_credits": value,
                           "upgrade_uri": data.get("upgrade_uri")}, ensure_ascii=False),
    }], ""


def parse_qoder_usage_panel(screen: str) -> tuple[list[dict], str]:
    """Panel /usage Qoder (teks layar tmux) → baris kredit akun `used/total`.

    `used` di sini kumulatif se-akun, bukan per sesi: CLI tidak memisahkan konsumsi per
    jendela. Itu sebabnya biaya harian mesin ini dilaporkan sebagai selisih pembacaan
    meter, dan bukan sebagai angka harian yang CLI cetak.
    """
    pairs, proof, expires = {}, {}, None
    for raw_line in screen.splitlines():
        line = " ".join(raw_line.split())
        if " : " not in line:
            continue
        label, value = [p.strip() for p in line.split(" : ", 1)]
        if label in QODER_PANEL_METERS:
            pairs[label] = value
        elif label in QODER_PANEL_PROOF:
            proof[label] = value
        elif label == QODER_PANEL_WINDOW_KEY and value not in ("", "N/A"):
            expires = ts_for_tstz(value)
    rows = []
    for label, value in pairs.items():
        used = total = None
        if "/" in value:
            left, right = value.split("/", 1)
            try:
                used, total = float(left.strip()), float(right.strip())
            except ValueError:
                pass
        reported = used is not None and total not in (None, 0)
        if reported and _proof_worked(proof):
            reported = False
        note = ""
        if not reported:
            note = (" — probe sempat menjalankan turn model, pembacaan ditolak"
                    if used is not None and _proof_worked(proof)
                    else " — tidak ada alokasi/jatah")
        rows.append({
            "scope": "account",
            "limit_window": QODER_PANEL_METERS[label],
            "state": "REPORTED" if reported else "UNAVAILABLE",
            "used": used if reported else None,
            "total": total if reported else None,
            # Qoder tidak melaporkan pecahan, jadi kolomnya tetap kosong — fraksi akan
            # jadi karangan kalau dihitung di sini.
            "remaining_fraction": None,
            "reset_at": expires if label == "Plan Credits Used" else None,
            "detail": f"{label}: {value}{note}",
            "raw": json.dumps({"meter": {label: value}, "probe_session": proof},
                              ensure_ascii=False),
        })
    if rows:
        return rows, ""
    return [], "the /usage panel holds no recognised meter key (did the CLI version change?)"


PARSERS = {
    "agy_usage_json": parse_agy_usage_json,
    "agy_credits_json": parse_agy_credits_json,
    "qoder_usage_panel": parse_qoder_usage_panel,
}


def read_qoder_panel(cfg: dict) -> tuple[str, str]:
    """Ketikan /usage ke qoder milik sendiri di socket tmux terpisah, lalu baca layarnya.

    Meter kredit Qoder hanya hidup di TUI. Yang sudah diukur (bukan diasumsikan):
    `qoder status -o json` tidak punya field kredit, dan `qoder -p "/usage"` mengembalikan
    hasil kosong (num_turns 0, total_credits 0). Jadi satu-satunya sumber adalah sesi yang
    benar-benar berjalan — dan sesi itu tidak boleh menjadi pane operator.

    Bahayanya nyata dan sudah terjadi sekali di workstation ini: mengetik "/usage" sebelum
    TUI selesai bangun membuat teksnya diperlakukan sebagai PROMPT, bukan perintah garis
    miring. Sesi probe lalu menjalankan turn model sungguhan — dan selama MCP workstation
    termuat, turn itu sempat mengetik perintah ke tab shell operator. Karena itu probe ini
    (1) hidup di socket + cwd sendiri, (2) mematikan MCP dan tools, (3) menolak mengetik
    sebelum layar benar-benar siap, (4) membatalkan apa pun yang berubah menjadi turn, dan
    (5) menolak pembacaan yang terjadi saat sebuah turn berjalan. Angka yang datang dari
    sesi yang sedang bekerja bukan angka kuota, jadi lebih baik UNAVAILABLE.
    """
    cmd = _probe_argv(cfg.get("probe") or ["qoder"]) + list(PANEL_FLAGS)
    # Variabel identitas sesi dibuang lewat `env -u`, bukan lewat env proses kita: tmux
    # mewariskan env SERVER, dan server itu bisa saja duluan dibangun oleh proses yang
    # env-nya penuh. Yang dibuang justru QODER_PID/QODER_CLI — penanda "ini anak dari sesi
    # X", dan probe tidak boleh jadi anak siapa pun.
    cmd = ["env", *(f"-u{k}" for k in PANEL_ENV_UNSET),
           f"PATH={_meter_search_path(cfg.get('search_path') or ())}", *cmd]
    mcp_file = cfg.get("mcp_config") or ""
    if mcp_file:
        # Path relatif di engines.json dihitung dari akar workstation, bukan dari cwd
        # proses — daemon, systemd dan `ai-station treasury` punya cwd yang berbeda-beda.
        target = Path(mcp_file)
        if not target.is_absolute():
            target = HOME / ".ai-station" / target
        cmd += ["--mcp-config", str(target)]
    panel = str(cfg.get("panel_command") or "/usage")
    timeout = int(cfg.get("timeout_s", 180))
    try:
        TREASURY_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        return "", f"folder probe tidak bisa dibuat: {exc}"
    _tmux("kill-session", "-t", TREASURY_TAB)
    rc, out = _tmux("new-session", "-d", "-s", TREASURY_TAB, "-x", "220", "-y", "50",
                    "-c", str(TREASURY_DIR), *cmd)
    if rc != 0:
        return "", f"probe could not be started: {out[:TREASURY_DETAIL_MAX]}"

    screen = ""
    previous = None
    steady = 0
    typed = False
    tries = 0
    settle_at = time.time() + PANEL_BOOT_S
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            time.sleep(1.0)
            rc, screen = _tmux("capture-pane", "-t", TREASURY_TAB, "-p")
            if rc != 0:
                return "", f"capture-pane failed: {screen[:TREASURY_DETAIL_MAX]}"

            if _turn_started(screen):
                _tmux("send-keys", "-t", TREASURY_TAB, "Escape")
                return "", ("the probe command became a model turn and was cancelled; "
                            "the TUI was not ready when it was typed — reading rejected")

            if any(lbl in screen for lbl in QODER_PANEL_METERS):
                return screen, ""

            if "trust the files in this folder" in screen.lower():
                # Enter hanya untuk folder probe buatan kita sendiri. Folder lain berarti
                # cwd salah dan probe harus berhenti, bukan setuju-setuju.
                if str(TREASURY_DIR) not in screen:
                    return "", "the trust window names another folder; probe stopped"
                typed, steady, previous = False, 0, None
                settle_at = time.time() + PANEL_BOOT_S
                _tmux("send-keys", "-t", TREASURY_TAB, "Enter")
                continue

            steady = steady + 1 if screen == previous else 0
            previous = screen
            if typed or time.time() < settle_at or tries >= PANEL_TRIES:
                continue
            if steady < PANEL_SETTLE_TICKS or not _panel_ready(screen):
                continue
            _tmux("send-keys", "-t", TREASURY_TAB, "-l", panel)
            time.sleep(0.8)
            _, typed_screen = _tmux("capture-pane", "-t", TREASURY_TAB, "-p")
            tries += 1
            if _panel_echo(typed_screen, panel):
                _tmux("send-keys", "-t", TREASURY_TAB, "Enter")
                typed = True
                steady, previous = 0, None
            else:
                # Echo tidak muncul = teks tidak masuk ke kolom input. Enter tidak dikirim;
                # mengirim Enter buta adalah cara probe mengubah keadaan pane tidak keruan.
                steady, previous = 0, None
    finally:
        _tmux("send-keys", "-t", TREASURY_TAB, "Escape")
        _tmux("kill-session", "-t", TREASURY_TAB)
    return "", (f"panel {panel} did not appear within {timeout}s "
                f"(command tried {tries} time(s), reached the input box: {int(typed)})")


def read_meter(reg_key: str, cfg: dict) -> tuple[list[dict], str, str]:
    """Satu siklus baca SEMUA meter satu mesin: yang utama plus `extra` bila ada.

    Satu slot registry boleh punya lebih dari satu meter karena satu CLI boleh memisahkan
    laporan: antigravity mis. menulis kuota jendela di `/usage` dan saldo kredit di
    `/credits`. Tanpa `extra`, orang yang menambahkan meter kedua harus mengedit kode
    ingestor — dan konfigurasi yang tidak bisa dibaca tanpa membaca kode bukan konfigurasi.
    Kegagalan sebagian tetap dilaporkan apa adanya: baris yang berhasil masuk, sisanya
    membawa pesan CLI-nya sendiri.
    """
    meters = [cfg] + list(cfg.get("extra") or [])
    rows, sources, errs = [], [], []
    for meter in meters:
        r, source, err = _read_one_meter(reg_key, meter)
        # Baris membawa sumbernya sendiri. Tanpa ini, satu kolom 'source' untuk semua baris
        # hasil beberapa meter akan mengklaim bahwa baris kuota_window berasal dari perintah
        # /credits — dan audit angka tidak bisa lagi ditelusuri ke perintah yang mencetaknya.
        for item in r:
            item["_source"] = source
        rows.extend(r)
        if source:
            sources.append(source)
        if err:
            errs.append(err)
    return rows, " + ".join(sources), "; ".join(errs)


def _read_one_meter(reg_key: str, cfg: dict) -> tuple[list[dict], str, str]:
    """Satu siklus baca satu meter. Kembalikan (baris, nama sumber, pesan gagal).

    err == "UNSUPPORTED" adalah sentinel, bukan kalimat: mesin tanpa meter di registry
    tidak sedang gagal diukur, memang tidak ada yang bisa diukur.
    """
    name = str(cfg.get("parser") or "none")
    probe = cfg.get("probe")
    if name in ("none", "") or not probe:
        return [], "", "UNSUPPORTED"
    parser = PARSERS.get(name)
    if parser is None:
        return [], "", f"parser {name!r} is unknown to this ingestor"
    if name == "qoder_usage_panel":
        text, err = read_qoder_panel(cfg)
        source = "qoder /usage panel"
    else:
        argv = _probe_argv(probe)
        rc, text = _run(argv, int(cfg.get("timeout_s", 60)), cfg.get("search_path") or ())
        err = "" if rc == 0 else text[:TREASURY_DETAIL_MAX]
        source = " ".join(argv)
    if err:
        return [], source, err
    rows, perr = parser(text)
    return rows, source, perr


def record_quota(store: Store, rows: list[dict], source: str, engine: str):
    cols = ["ts", "engine", "scope", "limit_window", "state", "used", "total",
            "remaining_fraction", "remaining", "reset_at", "source", "detail", "raw"]
    now_iso = datetime.now().astimezone().isoformat(timespec="seconds")
    for r in rows:
        vals = [now_iso, engine, r.get("scope") or "-", r.get("limit_window") or "-",
                r.get("state") or "UNAVAILABLE", r.get("used"), r.get("total"),
                r.get("remaining_fraction"), r.get("remaining"), r.get("reset_at"),
                redact(str(r.get("_source") or source))[:120],
                redact(str(r.get("detail") or ""))[:TREASURY_DETAIL_MAX],
                redact(str(r.get("raw") or ""))[:SUMMARY_LIMIT]]
        store.execute(
            f"INSERT INTO engine_quota ({', '.join(cols)}) VALUES ({ph(len(cols), store.engine)})",
            vals)


def probe_treasury(store: Store, only: tuple = (), force: bool = False) -> dict:
    """Baca meter setiap mesin yang terdaftar dan simpan pembacaannya sebagai fakta."""
    usage = registry_usage()
    if not usage:
        return {"engines": [], "readings": 0,
                "note": "engines.json has no 'usage' block: there is no meter to read"}
    report, readings = [], 0
    for key, cfg in usage.items():
        if only and key not in only:
            continue
        engine = str(cfg.get("engine") or key)
        if not force and store_treasury_age(store, key) < TREASURY_EVERY_S:
            continue
        rows, source, err = read_meter(key, cfg)
        if not rows:
            # Kunci "probe/probe" menandai baris ini sebagai hasil percobaan baca, bukan
            # jendela kuota yang benar-benar dimiliki mesin itu.
            rows = [{"scope": "probe", "limit_window": "probe",
                     "state": "UNSUPPORTED" if err == "UNSUPPORTED" else "UNAVAILABLE",
                     "detail": err or "the meter produced no rows at all"}]
        record_quota(store, rows, source or str(cfg.get("parser") or "-"), engine)
        store.cursor_set("treasury", f"meter/{key}", int(time.time()), redact(err or source)[:200])
        readings += len(rows)
        report.append({"key": key, "engine": engine, "rows": len(rows),
                       "state": rows[0]["state"] if len(rows) == 1 else "MIXED",
                       "reason": err})
    return {"engines": report, "readings": readings, "interval_s": TREASURY_EVERY_S}


def store_treasury_age(store: Store, reg_key: str) -> float:
    off, _, _ = get_cursor_state(store, f"meter/{reg_key}", "treasury")
    return time.time() - int(off or 0) if off else float("inf")


TREASURY_LOCK = threading.Lock()


def treasury_watch(store: Store):
    """Siklus Treasury di dalam watch loop, tanpa memblokir ingest.

    Membaca meter bisa makan menit (probe TUI Qoder menunggu CLI bangun dulu). Kalau ini
    berjalan sinkron di loop 3 detik, jejak transaksi berhenti selama itu — jadi pekerjaannya
    dilempar ke thread dengan koneksi DB sendiri, dan siklus berikutnya hanya memicu
    pekerjaannya kalau siklus sebelumnya sudah selesai.
    """
    if not TREASURY_LOCK.acquire(blocking=False):
        return False
    due = [k for k in registry_usage() if store_treasury_age(store, k) >= TREASURY_EVERY_S]
    if not due:
        TREASURY_LOCK.release()
        return False

    def worker():
        try:
            probe_treasury(Store(), only=tuple(due), force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[treasury] {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            TREASURY_LOCK.release()

    # Daemon thread: kalau service mati di tengah probe, thread ikut mati dan sesi tmux
    # probe tertinggal. Tidak berbahaya (bukan socket operator) dan dibersihkan oleh
    # kill-session di awal probe berikutnya.
    threading.Thread(target=worker, daemon=True).start()
    return True


def _opt_num(v):
    """Sama seperti _num, tapi NULL tetap NULL.

    Di Treasury bedanya bukan kosmetik: 0 berarti "meter melaporkan nol", None berarti
    "mesin itu tidak melaporkan angka ini sama sekali". Memakai _num() pada kolom kuota
    akan mengubah 'tidak ada' menjadi 'nol' — laporan yang terlihat bersih dan salah.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return int(f) if f.is_integer() else round(f, 6)


def _day_bounds() -> tuple[str, str]:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat(
        timespec="seconds"), now.isoformat(timespec="seconds")


def latest_quota(store: Store) -> list[dict]:
    """Pembacaan TERAKHIR per (mesin, scope, jendela). Query join, bukan DISTINCT ON, supaya
    berjalan sama di PostgreSQL maupun SQLite."""
    cols = ["engine", "scope", "limit_window", "state", "used", "total",
            "remaining_fraction", "remaining", "reset_at", "source", "detail", "ts"]
    rows = store.query(
        f"SELECT DISTINCT q.{', q.'.join(cols)} FROM engine_quota q "
        f"JOIN (SELECT engine, scope, limit_window, MAX(ts) AS mts FROM engine_quota "
        f"      GROUP BY engine, scope, limit_window) t "
        f"  ON t.engine = q.engine AND t.scope = q.scope "
        f" AND t.limit_window = q.limit_window AND t.mts = q.ts "
        f"ORDER BY q.engine, q.scope, q.limit_window")
    out = []
    for r in rows:
        d = dict(zip(cols, r))
        d["ts"] = str(d["ts"])
        d["reset_at"] = str(d["reset_at"]) if d["reset_at"] else None
        for k in ("used", "total", "remaining_fraction", "remaining"):
            d[k] = _opt_num(d[k])
        # Laporan boleh menurunkan angka, bukan menggantikannya: kalau meter sendiri
        # melaporkan sisa, itu yang shown; total−used hanya dipakai untuk meter yang
        # hanya memberi tahu terpakai/batas (mis. kredit Qoder 451/1500).
        if d["remaining"] is None and d["used"] is not None and d["total"] is not None:
            d["remaining"] = d["total"] - d["used"]
        out.append(d)
    return out


def meter_deltas(store: Store) -> list[dict]:
    """Konsumsi hari ini dari selisih dua pembacaan meter — bukan angka taksiran.

    Yang dipakai adalah pembacaan PERTAMA dan TERAKHIR hari ini, bukan min/max. Bedanya
    penting saat kuota me-reset di tengah hari: min/max akan menyebut seluruh jatah lama
    sebagai "terpakai hari ini", padahal yang terjadi adalah meter yang mulai dari awal.
    """
    start, _end = _day_bounds()
    rows = store.query(
        "SELECT engine, scope, limit_window, ts, used, remaining_fraction, source "
        " FROM engine_quota WHERE ts >= "
        f"{ph(1, store.engine)} AND state = 'REPORTED' "
        " ORDER BY engine, scope, limit_window, ts", (start,))
    buckets: dict = {}
    for eng, scope, window, ts, used, frac, source in rows:
        buckets.setdefault((eng, scope, window), []).append(
            (str(ts), _opt_num(used), _opt_num(frac), source))

    out = []
    for (eng, scope, window), readings in buckets.items():
        first, last = readings[0], readings[-1]
        entry = {"engine": eng, "scope": scope, "limit_window": window,
                 "readings": len(readings), "from": first[0], "to": last[0], "source": last[3]}
        if len(readings) < 2:
            # Satu pembacaan hanya menjelaskan POSISI hari ini, bukan berapa yang terpakai.
            # Menyimpulkan "terpakai 0" dari satu pembacaan adalah tebakan.
            entry.update({"state": "UNAVAILABLE", "consumed": None, "unit": None,
                          "detail": f"only 1 reading since midnight; daily consumption "
                                    f"needs both a first and a last reading"})
        elif first[1] is not None and last[1] is not None:
            entry.update({"state": "REPORTED", "consumed": last[1] - first[1],
                          "unit": "account credits (cumulative; the CLI does not split them per session)",
                          "detail": f"meter moved {first[1]} → {last[1]}"})
        elif first[2] is not None and last[2] is not None:
            entry.update({"state": "REPORTED", "consumed": first[2] - last[2],
                          # Sisa kuota TURUN saat dipakai, jadi yang terpakai = awal - akhir.
                          "unit": "fraction of this window's allowance",
                          "detail": f"remaining fell {first[2]:.4f} → {last[2]:.4f}"})
        else:
            entry.update({"state": "UNAVAILABLE", "consumed": None, "unit": None,
                          "detail": "the first and last reading carry no comparable "
                                    "used or remaining_fraction"})
        out.append(entry)
    return out


def ledger_cost(store: Store) -> list[dict]:
    """Biaya harian dari transaksi. Kalau kolomnya kosong, itu yang dilaporkan — bukan nol."""
    start, _end = _day_bounds()
    rows = store.query(
        "SELECT engine, COUNT(*), COUNT(credits), COALESCE(SUM(credits),0), "
        " COUNT(input_tokens), COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0), "
        " COALESCE(SUM(cache_read_tokens),0), COALESCE(SUM(duration_ms),0) "
        f" FROM action_log WHERE ts >= {ph(1, store.engine)} GROUP BY engine ORDER BY engine",
        (start,))
    out = []
    for (eng, rows_n, cred_n, cred_sum, tok_n, in_sum, out_sum, cache_sum, dur) in rows:
        tokens = _num(in_sum) + _num(out_sum)
        # Alasan dilaporkan sebagai hasil hitungan, bukan kalimat tetap per mesin: instalasi
        # lain punya mesin berbeda dan angkanya harus menjelaskan dirinya sendiri.
        detail = (f"{_num(rows_n)} transaction rows today: {_num(cred_n)} carry credits, "
                  f"{_num(tok_n)} carry tokens (input {_num(in_sum)}, output {_num(out_sum)})")
        if _num(cred_n):
            state, reason = "REPORTED", detail
        elif tokens:
            state, reason = "TOKENS_ONLY", detail + " — per-transaction credits are absent from the log"
        else:
            state, reason = "UNAVAILABLE", detail + " — the log source records no real number"
        out.append({"engine": eng, "state": state, "reason": reason,
                    "transactions": _num(rows_n),
                    "credits": _num(cred_sum) if _num(cred_n) else None,
                    "credit_rows": _num(cred_n), "token_rows": _num(tok_n),
                    "input_tokens": _num(in_sum), "output_tokens": _num(out_sum),
                    "cache_read_tokens": _num(cache_sum), "duration_ms": _num(dur)})
    return out


def treasury_report(store: Store) -> dict:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "day": _day_bounds()[0][:10],
        "interval_s": TREASURY_EVERY_S,
        "quota": latest_quota(store),
        "consumption": meter_deltas(store),
        "ledger_cost": ledger_cost(store),
        "registry": {k: {"engine": v.get("engine") or k, "parser": v.get("parser"),
                         "has_meter": bool(v.get("probe"))}
                     for k, v in registry_usage().items()},
        "invariant": "every number here is a CLI reading or a transaction query; nothing is written by hand",
    }


def treasury(store: Store, probe: bool = False, only: tuple = ()) -> None:
    if probe:
        print(json.dumps(probe_treasury(store, only=only, force=True),
                         ensure_ascii=False, default=str), file=sys.stderr)
    print(json.dumps(treasury_report(store), ensure_ascii=False, default=str))


def run_once(store: Store, meta: dict) -> int:
    n = ingest_qoder_segments(store, meta)
    n += ingest_qoder_transcripts(store, meta)
    n += ingest_antigravity(store)
    n += ingest_station_spool(store)
    # Setelah baris kegagalan masuk, ubah menjadi insiden (satu siklus di belakang,
    # supaya perintah pemicunya sudah pasti ada di tabel).
    try:
        sweep_failures(store)
    except Exception as exc:  # noqa: BLE001
        print(f"[sweep] dilewati: {exc}", file=sys.stderr)
    return n


def api(store: Store, limit: int, engine: str = "", kind: str = ""):
    """Satu payload JSON untuk daemon Rust (panel recovery & log)."""
    where, params = [], []
    if engine:
        where.append(f"engine = {ph(1, store.engine)}")
        params.append(engine)
    if kind:
        where.append(f"kind = {ph(1, store.engine)}")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    events = store.query(
        f"SELECT ts, engine, kind, tool, cwd, summary, exit_code, duration_ms, session_id, tab, raw_ref "
        f"FROM action_log {clause} ORDER BY id DESC LIMIT {ph(1, store.engine)}",
        tuple(params) + (limit,))
    memories = store.query(
        f"SELECT key, category, content, updated_at FROM world_memory "
        f"WHERE key LIKE {ph(1, store.engine)} OR category = {ph(1, store.engine)} "
        f"ORDER BY updated_at DESC LIMIT 8", ("%handoff%", "rule"))
    quests = store.query(
        f"SELECT id, title, status, model_assigned, cli_engine, summary, created_at FROM quest_tasks "
        f"WHERE status = {ph(1, store.engine)} ORDER BY id DESC LIMIT 8", ("IN_PROGRESS",))
    turns = store.query(
        "SELECT timestamp, cli_engine, model, reasoning_effort, prompt, response, duration_ms "
        "FROM session_turns ORDER BY id DESC LIMIT " + ph(1, store.engine), (min(limit, 20),))

    def iso(v):
        return str(v) if v is not None else None

    print(json.dumps({
        "db_engine": store.engine,
        "events": [dict(zip(["ts", "engine", "kind", "tool", "cwd", "summary", "exit_code",
                             "duration_ms", "session_id", "tab", "raw_ref"], [iso(e[0]), *e[1:]]))
                   for e in events],
        "handoff_memory": [dict(zip(["key", "category", "content", "updated_at"],
                                    [m[0], m[1], m[2], iso(m[3])])) for m in memories],
        "active_quests": [dict(zip(["id", "title", "status", "model", "cli_engine", "summary",
                                    "created_at"], [q[0], q[1], q[2], q[3], q[4], q[5], iso(q[6])]))
                          for q in quests],
        "recent_turns": [dict(zip(["timestamp", "engine", "model", "effort", "prompt", "response",
                                   "duration_ms"], [iso(t[0]), t[1], t[2], t[3], t[4], t[5], t[6]]))
                         for t in turns],
    }, default=str))


STATION_POSTS_VIEW = """
CREATE OR REPLACE VIEW station_posts AS
WITH pr AS (
  SELECT id, ts, session_id, tab, model, cwd, summary
  FROM action_log
  WHERE kind = 'prompt'
    -- Prompt yang masuk lewat pintu mesin bukan permintaan operator. Notifikasi task
    -- dan sisipan sistem tercatat dengan kind yang sama, dan kalau dibiarkan ia jadi
    -- postingan yang seolah-olah kamu perintahkan.
    AND summary NOT LIKE '<task-notification>%'
    AND summary NOT LIKE '<system-reminder>%'
), bnd AS (
  SELECT pr.id,
         pr.ts AS opened_at,
         pr.session_id, pr.tab, pr.model, pr.cwd, pr.summary,
         (SELECT t.ts FROM action_log t
           WHERE t.kind = 'turn' AND t.session_id = pr.session_id AND t.ts > pr.ts
           ORDER BY t.ts LIMIT 1) AS closed_at,
         (SELECT t.summary FROM action_log t
           WHERE t.kind = 'turn' AND t.session_id = pr.session_id AND t.ts > pr.ts
           ORDER BY t.ts LIMIT 1) AS turn_note
  FROM pr
)
SELECT bnd.id AS post_id,
       bnd.session_id,
       bnd.opened_at,
       bnd.closed_at,
       bnd.tab,
       bnd.model,
       bnd.cwd,
       left(bnd.summary, 240) AS command,
       EXTRACT(EPOCH FROM (COALESCE(bnd.closed_at, now()) - bnd.opened_at))::int AS duration_s,
       (SELECT count(*) FROM action_log a WHERE a.kind='command'      AND a.session_id=bnd.session_id AND a.ts>=bnd.opened_at AND (bnd.closed_at IS NULL OR a.ts<=bnd.closed_at)) AS commands,
       (SELECT count(*) FROM action_log a WHERE a.kind='tool_call'    AND a.session_id=bnd.session_id AND a.ts>=bnd.opened_at AND (bnd.closed_at IS NULL OR a.ts<=bnd.closed_at)) AS tool_calls,
       (SELECT count(*) FROM action_log a WHERE a.kind='command_exit' AND a.exit_code <> 0 AND a.session_id=bnd.session_id AND a.ts>=bnd.opened_at AND (bnd.closed_at IS NULL OR a.ts<=bnd.closed_at)) AS failed,
       CASE WHEN bnd.closed_at IS NULL THEN 'IN_PROGRESS' ELSE 'CLOSED' END AS state,
       bnd.turn_note
FROM bnd
"""


STATION_ARTIFACTS_VIEW = """
CREATE OR REPLACE VIEW station_artifacts AS
SELECT a.id AS artifact_id,
       a.ts,
       a.session_id,
       a.tab,
       a.engine,
       a.tool,
       a.summary AS path,
       CASE WHEN lower(a.summary) ~ '\\.(png|jpe?g|webp|gif|svg|mp4|blend|fbx)$'
            THEN 'media' ELSE 'file' END AS kind,
       (SELECT p.id FROM action_log p
         WHERE p.kind = 'prompt' AND p.session_id = a.session_id AND p.ts <= a.ts
         ORDER BY p.ts DESC LIMIT 1) AS post_id
FROM action_log a
WHERE a.kind = 'tool_call' AND a.tool IN ('Write', 'Edit')
  -- Lapis kedua: yang boleh disebut artefak hanyalah yang berbentuk jalur. Baris lama yang
  -- tercatat sebelum penjagaan ini ada tetap tertahan di sini, tidak dibuang dari log.
  AND length(a.summary) <= 512
  AND a.summary !~ '[\r\n]'
  AND a.summary ~ '^(~?/[[:print:]]+|[^/[:space:]]+(/[[:print:]]+)*)$'
"""


def ensure_station_view(store: Store) -> bool:
    """Bangunkan view `station_posts` — satu baris = satu postingan.

    Postingan TIDAK ditulis siapa pun. Ia diturunkan dari action_log setiap kali dibaca, jadi
    tidak bisa basi, tidak bisa disunting, dan tidak memakan token satu byte pun: yang menutup
    postingan adalah baris `turn` yang ditulis watcher, bukan laporan dari AI yang selesai kerja.

    Hanya PostgreSQL. View SQLite fallback sengaja tidak dibuat: DDL-nya memakai EXTRACT(EPOCH)
    dan cast `::int`, dan fallback itu ada supaya workstation tetap hidup saat Postgres mati —
    bukan supaya ia punya fitur yang sama.
    """
    if not str(store.engine).upper().startswith("POSTG"):
        return False
    ok = True
    for name, ddl in (("station_posts", STATION_POSTS_VIEW), ("station_artifacts", STATION_ARTIFACTS_VIEW)):
        try:
            with store.conn.cursor() as cur:
                # CREATE OR REPLACE VIEW menolak kalau daftar kolomnya berubah nama atau
                # urutannya — Postgres memandangnya view berbeda, bukan view yang sama.
                # View ini diturunkan penuh dari action_log, jadi menjatuhkannya dan
                # membangun ulang lebih benar daripada memelihara dua definisi.
                cur.execute("DROP VIEW IF EXISTS %s" % name)
                cur.execute(ddl)
            store.conn.commit()
        except Exception as exc:  # noqa: BLE001
            print(f"[ingestor] view {name} gagal: {exc}", file=sys.stderr)
            ok = False
    return ok


def posts(store: Store, limit: int = 20) -> int:
    """Cetak postingan terakhir — bukti bahwa rekam jejak menutup dirinya sendiri."""
    # `ts` bertipe timestamptz: to_char sudah memakai timezone koneksi. Konversi ganda
    # ("at time zone 'UTC' at time zone ...") justru menggeser jam 7 ke belakang.
    rows = store.query(
        "SELECT to_char(opened_at,'MM-DD HH24:MI'),"
        " state, duration_s, commands, tool_calls, failed, left(command,70)"
        " FROM station_posts ORDER BY opened_at DESC LIMIT %s", (limit,))
    rows = list(reversed(rows))
    for t, state, dur, cmd, tool, fail, cmd_text in rows:
        flag = "!" if fail else " "
        print(f" {flag} {t}  {state:<11} {dur:>5}s  cmd={cmd:<3} tool={tool:<3} fail={fail:<2} {cmd_text}")
    return len(rows)


def _num(v):
    """Postgres mengembalikan sum() sebagai Decimal; JSON tidak punya tipe itu."""
    if v is None:
        return 0
    try:
        return int(v)
    except (TypeError, ValueError):
        return v


def station(store: Store, limit: int = 30) -> None:
    """Satu payload JSON untuk halaman Station: postingan + artefak + ringkasan hari ini.

    Semuanya hasil query. Tidak ada satu pun angka di sini yang ditulis oleh AI yang
    mengerjakan pekerjaannya — itu persis yang membuat halaman ini bisa dipercaya.
    """
    posts_rows = store.query(
        "SELECT post_id, to_char(opened_at,'YYYY-MM-DD\"T\"HH24:MI:SS\"+07\"'), "
        " to_char(closed_at,'YYYY-MM-DD\"T\"HH24:MI:SS\"+07\"'), state, duration_s, "
        " commands, tool_calls, failed, tab, model, cwd, command "
        "FROM station_posts ORDER BY opened_at DESC LIMIT %s", (limit,))
    arts = store.query(
        "SELECT post_id, path, kind, to_char(ts,'HH24:MI') FROM station_artifacts "
        "WHERE ts >= (SELECT COALESCE(min(opened_at), now()) FROM "
        "  (SELECT opened_at FROM station_posts ORDER BY opened_at DESC LIMIT %s) t) "
        "ORDER BY ts", (limit,))
    per_post: dict = {}
    for pid, path, kind, hhmm in arts:
        bucket = per_post.setdefault(int(pid or 0), {})
        entry = bucket.get(path)
        if entry:
            # Berkas yang sama boleh disentuh lima kali dalam satu postingan; yang ingin
            # dibaca manusia itu "berkas apa, berapa kali", bukan lima baris identik.
            entry["edits"] += 1
            entry["last"] = hhmm
        else:
            bucket[path] = {"path": path, "kind": kind, "at": hhmm, "last": hhmm, "edits": 1}

    ids = [p[0] for p in posts_rows]
    steps: dict = {}
    outcomes: dict = {}
    if ids:
        # Satu query untuk semua postingan. `response` hanya menyimpan 400 karakter pertama
        # (caps di insert_event), jadi yang bisa ditampilkan jujur adalah potongan akhirnya —
        # teks utuhnya ada di transkrip yang ditunjuk raw_ref, bukan hasil karangan ulang.
        rows = store.query(
            "SELECT p.post_id, a.kind, a.tool, left(a.summary,1200), to_char(a.ts,'HH24:MI') "
            "FROM station_posts p JOIN action_log a "
            "  ON a.session_id = p.session_id AND a.ts >= p.opened_at "
            " AND a.ts <= COALESCE(p.closed_at, now()) "
            "WHERE a.kind IN ('command','response') AND p.post_id = ANY(%s) ORDER BY a.ts",
            (ids,))
        for pid, kind, tool, text, hhmm in rows:
            pid = int(pid)
            if kind == "response":
                outcomes[pid] = {"at": hhmm, "text": text}
            else:
                steps.setdefault(pid, []).append({"at": hhmm, "cmd": text})

    out = []
    for (pid, opened, closed, state, dur, cmd, tool, fail, tab, model, cwd, command) in posts_rows:
        out.append({
            "id": pid, "opened_at": opened, "closed_at": closed, "state": state,
            "duration_s": _num(dur), "commands": _num(cmd), "tool_calls": _num(tool),
            "failed": _num(fail), "tab": tab, "model": model, "cwd": cwd,
            "command": command, "artifacts": list(per_post.get(pid, {}).values()),
            "outcome": outcomes.get(pid), "steps": steps.get(pid, [])[:24],
        })

    today = store.query(
        "SELECT count(*), COALESCE(sum(commands),0), COALESCE(sum(tool_calls),0), "
        " COALESCE(sum(failed),0), COALESCE(sum(duration_s),0) "
        "FROM station_posts WHERE opened_at::date = CURRENT_DATE")
    pcount, pcm, ptl, pfl, pdur = today[0] if today else (0, 0, 0, 0, 0)
    acount = store.query(
        "SELECT count(*) FROM station_artifacts WHERE ts::date = CURRENT_DATE")[0][0]

    print(json.dumps({
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "totals": {"posts": _num(pcount), "commands": _num(pcm), "tool_calls": _num(ptl),
                   "failed": _num(pfl), "work_seconds": _num(pdur), "artifacts": _num(acount)},
        "posts": out,
    }, ensure_ascii=False, default=str))


def main():
    ap = argparse.ArgumentParser(description="Ingestor log sesi AI Workstation ke SQL")
    ap.add_argument("mode", nargs="?", default="once",
                    choices=["once", "watch", "recent", "handoff", "snapshot", "stats",
                             "recovery", "api", "posts", "station", "treasury"])
    ap.add_argument("limit", nargs="?", type=int, default=40)
    ap.add_argument("--engine", default="")
    ap.add_argument("--kind", default="")
    ap.add_argument("--session", default="", help="batasi snapshot ke satu session_id")
    ap.add_argument("--interval", type=int, default=3)
    ap.add_argument("--probe", action="store_true",
                    help="treasury: baca meter CLI sekarang juga (bisa memakan menit)")
    ap.add_argument("--only", default="",
                    help="treasury: batasi probe ke kunci registry, mis. --only qoder,gemini")
    args = ap.parse_args()

    store = Store()
    ensure_station_view(store)
    meta = qoder_runtime_profile()

    if args.mode == "watch":
        while True:
            try:
                # Profil dibaca ULANG tiap siklus. Operator mengganti model/effort/context
                # dari GUI kapan saja; menempel nilai saat service start ke seluruh hari itu
                # membuat baris biaya menyebut model yang tidak benar-benar dipakai, dan itu
                # langsung membatalkan acceptance F8.2 (angka = yang CLI laporkan).
                run_once(store, qoder_runtime_profile())
                treasury_watch(store)
            except Exception as exc:  # noqa: BLE001
                print(f"[ingestor error] {exc}", file=sys.stderr)
                try:
                    store = Store()
                    ensure_station_view(store)
                except Exception:  # noqa: BLE001
                    time.sleep(args.interval * 4)
            time.sleep(args.interval)
        return

    if args.mode == "api":
        run_once(store, meta)
        api(store, args.limit, args.engine, args.kind)
    elif args.mode == "recent":
        run_once(store, meta)
        if not recent(store, args.limit, args.engine, args.kind):
            print("(action_log masih kosong)")
    elif args.mode == "stats":
        run_once(store, meta)
        stats(store)
    elif args.mode == "handoff":
        handoff(store)
    elif args.mode == "snapshot":
        run_once(store, meta)
        where = snapshot(store, args.session, min(args.limit, 40))
        print(f"[snapshot] tersimpan: {where}", file=sys.stderr)
    elif args.mode == "station":
        if not ensure_station_view(store):
            print(json.dumps({"error": "halaman Station butuh PostgreSQL", "posts": [],
                              "totals": {}}))
        else:
            station(store, args.limit)
    elif args.mode == "posts":
        if not ensure_station_view(store):
            print("(view postingan hanya tersedia di PostgreSQL; fallback SQLite sedang aktif)")
        elif not posts(store, args.limit):
            print("(belum ada postingan)")
    elif args.mode == "treasury":
        treasury(store, probe=args.probe,
                 only=tuple(x.strip() for x in args.only.split(",") if x.strip()))
    elif args.mode == "recovery":
        run_once(store, meta)
        print("=== TERAKHIR DARI action_log ===")
        recent(store, args.limit, args.engine, args.kind)
        print("\n=== SERAH-TERIMA (world_memory) ===")
        handoff(store)
    else:
        print(f"DB engine: {store.engine} | event baru: {run_once(store, meta)}")


if __name__ == "__main__":
    main()
