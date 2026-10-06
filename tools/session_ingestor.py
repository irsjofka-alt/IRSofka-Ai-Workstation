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
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path.home() / ".ai-station" / "tools"))
from db_state import get_db_connection  # noqa: E402  adapter dual-engine resmi workstation

HOME = Path.home()
SUMMARY_LIMIT = 400
QODER_SEG_GLOB = str(HOME / ".ai-station/engines/qoder/logs/sessions/*/*/segments/*.jsonl")
QODER_PROJ_GLOB = str(HOME / ".ai-station/engines/qoder/projects/*/*.jsonl")
AGY_DB = HOME / ".gemini/antigravity/conversation_summaries.db"
PROFILES = HOME / ".ai-station/config/cli_profiles.json"

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
    raw_ref TEXT
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
    raw_ref TEXT
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
]


class Store:
    def __init__(self):
        self.conn, self.engine = get_db_connection()
        cur = self.conn.cursor()
        ddl = DDL_PG if is_pg(self.engine) else DDL_SQLITE
        for stmt in filter(None, (s.strip() for s in ddl.split(";"))):
            try:
                cur.execute(stmt)
            except Exception as exc:  # noqa: BLE001
                print(f"[ddl skip] {exc}", file=sys.stderr)
        for stmt in DDL_INDEX:
            try:
                cur.execute(stmt)
            except Exception:  # noqa: BLE001
                pass
        self.conn.commit()
        cur.close()

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
                "output_tokens", "cache_read_tokens", "raw_ref"]
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
        brief = (args.get("command") or args.get("prompt") or args.get("file_path")
                 or args.get("subject") or json.dumps(args, default=str))
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
        f"SELECT key, category, substr(content,1,700), updated_at FROM world_memory "
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

    lines = [
        f"# Auto-Handoff — {stamp:%Y-%m-%d %H:%M:%S}",
        "",
        f"Sesi: `{sid or 'tidak diketahui'}`  •  Ditulis otomatis oleh hook SessionEnd, "
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

    lines += [
        "",
        "## Keadaan mesin saat sesi mati",
        f"- git engine-rust: `{_probe(['git', '-C', str(HOME / '.ai-station/engine-rust'),
                                     'log', '--oneline', '-1'])[:120]}`",
        f"- suntingan belum di-commit: "
        f"`{_probe(['git', '-C', str(HOME / '.ai-station/engine-rust'), 'status', '--porcelain'])[:300]}`",
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


def main():
    ap = argparse.ArgumentParser(description="Ingestor log sesi AI Workstation ke SQL")
    ap.add_argument("mode", nargs="?", default="once",
                    choices=["once", "watch", "recent", "handoff", "snapshot", "stats",
                             "recovery", "api"])
    ap.add_argument("limit", nargs="?", type=int, default=40)
    ap.add_argument("--engine", default="")
    ap.add_argument("--kind", default="")
    ap.add_argument("--session", default="", help="batasi snapshot ke satu session_id")
    ap.add_argument("--interval", type=int, default=3)
    args = ap.parse_args()

    store = Store()
    meta = qoder_runtime_profile()

    if args.mode == "watch":
        while True:
            try:
                run_once(store, meta)
            except Exception as exc:  # noqa: BLE001
                print(f"[ingestor error] {exc}", file=sys.stderr)
                try:
                    store = Store()
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
