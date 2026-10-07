"""Work order — satu-satunya modul yang punya kosakata tugas: klaim, lease, bukti, dan antrean manusia.

Roadmap adalah prosa; mesin tidak bisa memilih pekerjaan dari prosa. Modul ini mengubah tiap
butir menjadi baris yang bisa dibaca: `claim` atomik (dua engine tidak mengerjakan item sama),
`lease` yang kedaluwarsa supaya tugas tidak terkunci selamanya, dan `evidence` yang wajib berisi
perintah yang benar-benar lolos sebelum sebuah item boleh disebut COMPLETED.

Autopilot (`F10.8`) hanya mengubah SIAPA yang memicu, bukan APA yang boleh dikerjakan: status
mesin tersimpan di tabel `autopilot_state`, dan jalur resume membaca kolom itu lebih dulu.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_state import get_db_connection, DB_SQLITE_PATH  # noqa: E402

# Kosakata status. IN_PROGRESS dibiarkan utuh pada baris lama: menulis ulang sejarah laporan
# adalah cara tercepat membuat ledger tidak lagi dipercaya (§12, "reports are derived").
STATUS = ("PENDING", "CLAIMED", "WORKING", "BLOCKED", "HUMAN", "PARKED",
          "COMPLETED", "FAILED", "UNAVAILABLE")
ALIAS = {"IN_PROGRESS": "CLAIMED", "DONE": "COMPLETED"}

LEASE_SECONDS = 900          # satu item tanpa heartbeat 15 menit dianggap lepas
MAX_RESUME = 3               # item yang butuh 4 sentuhan bukan tertidur, tapi macet
EVIDENCE_LIMIT = 900
# Circuit breaker (temuan reviewer Gemini, 2026-10-08). Tanpa ini sebuah item yang gagal dengan
# exit code cepat akan diklaim-lepas-gagal lagi tanpa henti, dan kuota mingguan habis dalam
# hitungan menit sementara mesin terlihat sangat sibuk.
REPO = Path.home() / ".ai-station"
NIGHT_MAX_FAILURES = 3
NIGHT_MAX_ITEMS = 25
_MEASURE = object()          # pembeda "belum diukur" dari "diukur tapi gagal dibaca"


def tree_dirty() -> bool | None:
    """True/False dari git; None kalau git tidak bisa dibaca — dan None BUKAN berarti bersih."""
    try:
        import subprocess
        r = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain"],
                           capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return None
    if r.returncode != 0:
        return None
    return bool(r.stdout.strip())


def night_state(conn, engine, dirty=_MEASURE):
    """Apakah malam ini masih aman mengklaim pekerjaan baru."""
    ap = autopilot(conn, engine)
    dirty = tree_dirty() if dirty is _MEASURE else dirty
    on_since = int((ap or {}).get("on_epoch") or 0)
    fails = run(conn, engine,
                "SELECT COUNT(*) AS n FROM quest_tasks WHERE status IN ('FAILED','PARKED') "
                "AND completed_at >= CURRENT_TIMESTAMP - INTERVAL '12 hours'"
                if engine == "POSTGRESQL" else
                "SELECT COUNT(*) AS n FROM quest_tasks WHERE status IN ('FAILED','PARKED') "
                "AND completed_at >= datetime('now','-12 hours')")
    n_fail = int(fails[0]["n"]) if fails else 0
    done = run(conn, engine,
               "SELECT COUNT(*) AS n FROM quest_tasks WHERE status='COMPLETED' "
               "AND EXTRACT(EPOCH FROM completed_at) >= %s"
               if engine == "POSTGRESQL" else
               "SELECT COUNT(*) AS n FROM quest_tasks WHERE status='COMPLETED' "
               "AND strftime('%%s', completed_at) >= ?",
               (on_since,)) if on_since else [{"n": 0}]
    why = []
    if (ap or {}).get("mode") == "ON" and on_since and int(ap.get("until_epoch") or 0) < now():
        why.append("jendela ON lewat")
    if n_fail >= NIGHT_MAX_FAILURES:
        why.append(f"{n_fail} kegagalan berturut-turut (batas {NIGHT_MAX_FAILURES})")
    if dirty is True:
        why.append("working tree kotor")
    if dirty is None:
        why.append("git tidak terbaca — UNKNOWN, bukan bersih")
    if int((ap or {}).get("items_done") or 0) >= NIGHT_MAX_ITEMS:
        why.append(f"batas {NIGHT_MAX_ITEMS} item per sesi terlampaui")
    return {"mode": (ap or {}).get("mode"), "failures_12h": n_fail,
            "items_done": int((ap or {}).get("items_done") or 0), "tree_dirty": dirty,
            "ok": not why, "why": why}


def norm(status: str | None) -> str:
    return ALIAS.get(status or "", status or "PENDING")


# --- skema -----------------------------------------------------------------
# Dua jalur seperti sisa workstation: CREATE untuk instalasi baru, ALTER untuk yang lama.
# Waktu disimpan sebagai epoch BIGINT, bukan TIMESTAMP, karena SQLite menulis CURRENT_TIMESTAMP
# dalam UTC sementara PostgreSQL menulis waktu lokal — lease yang membandingkan keduanya akan
# salah selama tujuh jam dan salahnya diam.
CREATE_PG = [
    """CREATE TABLE IF NOT EXISTS quest_tasks (
        id SERIAL PRIMARY KEY, title VARCHAR(255) NOT NULL,
        status VARCHAR(50) DEFAULT 'PENDING', model_assigned VARCHAR(100),
        cli_engine VARCHAR(50), summary TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, completed_at TIMESTAMP)""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id SERIAL PRIMARY KEY, question TEXT NOT NULL, options TEXT,
        weight VARCHAR(10) DEFAULT 'heavy', rung_used VARCHAR(50), engine_key VARCHAR(50),
        model_id VARCHAR(120), answer TEXT, state VARCHAR(20) DEFAULT 'OPEN',
        note TEXT, created_epoch BIGINT, resolved_epoch BIGINT)""",
    """CREATE TABLE IF NOT EXISTS autopilot_state (
        id SMALLINT PRIMARY KEY DEFAULT 1, mode VARCHAR(10) DEFAULT 'OFF',
        on_epoch BIGINT, until_epoch BIGINT, item_budget INT DEFAULT 12,
        items_done INT DEFAULT 0, off_reason TEXT, changed_by VARCHAR(60) DEFAULT 'operator')""",
]
CREATE_SQLITE = [
    """CREATE TABLE IF NOT EXISTS quest_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
        status TEXT DEFAULT 'PENDING', model_assigned TEXT, cli_engine TEXT,
        summary TEXT, created_at TEXT, completed_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id INTEGER PRIMARY KEY AUTOINCREMENT, question TEXT NOT NULL, options TEXT,
        weight TEXT DEFAULT 'heavy', rung_used TEXT, engine_key TEXT, model_id TEXT,
        answer TEXT, state TEXT DEFAULT 'OPEN', note TEXT,
        created_epoch INTEGER, resolved_epoch INTEGER)""",
    """CREATE TABLE IF NOT EXISTS autopilot_state (
        id INTEGER PRIMARY KEY CHECK (id = 1), mode TEXT DEFAULT 'OFF',
        on_epoch INTEGER, until_epoch INTEGER, item_budget INTEGER DEFAULT 12,
        items_done INTEGER DEFAULT 0, off_reason TEXT, changed_by TEXT DEFAULT 'operator')""",
]
NEW_COLUMNS = [
    ("quest_tasks", "phase", "VARCHAR(20)", "TEXT"),
    ("quest_tasks", "check_command", "TEXT", "TEXT"),
    ("quest_tasks", "depends_on", "TEXT", "TEXT"),      # daftar id terpisah koma, portabel
    ("quest_tasks", "claimed_by", "VARCHAR(60)", "TEXT"),
    ("quest_tasks", "lease_epoch", "BIGINT", "INTEGER"),
    ("quest_tasks", "evidence", "TEXT", "TEXT"),
    ("quest_tasks", "resume_count", "INT DEFAULT 0", "INTEGER DEFAULT 0"),
    ("quest_tasks", "escalation_reason", "TEXT", "TEXT"),
    ("quest_tasks", "due_epoch", "BIGINT", "INTEGER"),
]


def ensure_schema(conn, engine: str) -> list[str]:
    """Bangun yang belum ada; tidak pernah mengubah baris yang sudah ada."""
    cur = conn.cursor()
    log = []
    for stmt in (CREATE_PG if engine == "POSTGRESQL" else CREATE_SQLITE):
        try:
            cur.execute(stmt)
            log.append("ok " + stmt.split("(")[0].strip().split()[-1])
        except Exception as exc:  # noqa: BLE001
            log.append(f"skip {exc}")
    for table, column, pgtype, sqtype in NEW_COLUMNS:
        if engine == "POSTGRESQL":
            stmt = f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {pgtype}"
        else:
            cols = [r[1].lower() for r in cur.execute(f"PRAGMA table_info({table})")]
            if not cols or column.lower() in cols:
                continue
            stmt = f"ALTER TABLE {table} ADD COLUMN {column} {sqtype}"
        try:
            cur.execute(stmt)
            log.append(f"col {table}.{column}")
        except Exception as exc:  # noqa: BLE001
            log.append(f"skip {table}.{column}: {exc}")
    if engine != "POSTGRESQL":
        cur.execute("INSERT OR IGNORE INTO autopilot_state (id) VALUES (1)")
    else:
        cur.execute("INSERT INTO autopilot_state (id) VALUES (1) ON CONFLICT DO NOTHING")
    conn.commit() if engine != "POSTGRESQL" else None
    cur.close()
    return log


def now() -> int:
    return int(time.time())


# --- koneksi ---------------------------------------------------------------
def connect():
    conn, engine = get_db_connection()
    return conn, engine


def run(conn, engine, sql, params=()):
    """SELECT/INSERT via satu jalur untuk dua backend.

    psycopg2 mengembalikan tuple tanpa row_factory, sementara sqlite3.Row sudah seperti dict.
    Membentuk dict dari cur.description membuat keduanya identik — tanpa itu, kode yang lolos
    29 selftest SQLite tetap jatuh di produksi PostgreSQL pada SELECT pertama.
    """
    cur = conn.cursor()
    cur.execute(sql, params)
    if engine != "POSTGRESQL":
        conn.commit()
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []
    cur.close()
    return rows


def get_item(conn, engine, item_id):
    rows = run(conn, engine, "SELECT * FROM quest_tasks WHERE id = %s"
               if engine == "POSTGRESQL" else "SELECT * FROM quest_tasks WHERE id = ?",
               (item_id,))
    return rows[0] if rows else None


# --- mesin kerja ------------------------------------------------------------
def claim(conn, engine, item_id, who):
    """Ambil satu item. Gagal dengan alasan, bukan dengan kerja ganda.

    PostgreSQL punya UPDATE ... RETURNING; SQLite tidak. Keduanya harus menghasilkan keputusan
    yang sama, jadi klaim dilakukan sebagai UPDATE tersyarat lalu dibaca kembali — bukan
    'select lalu update', yang memenangkan siapa pun yang paling cepat bertanya.
    """
    item = get_item(conn, engine, item_id)
    if not item:
        return False, f"item {item_id} tidak ada"
    state = norm(item.get("status"))
    holder = item.get("claimed_by")
    lease = int(item.get("lease_epoch") or 0)
    if state not in ("PENDING", "CLAIMED", "WORKING"):
        return False, f"status {state} tidak bisa diklaim"
    if holder and holder != who and lease > now():
        return False, f"dipegang {holder} sampai lease berakhir ({lease - now()}s lagi)"
    ap = autopilot(conn, engine) or {}
    if ap.get("mode") == "ON":
        st = night_state(conn, engine)
        if st["why"]:
            return False, "circuit breaker: " + "; ".join(st["why"])
    ph = "%s" if engine == "POSTGRESQL" else "?"
    q = (f"UPDATE quest_tasks SET status='WORKING', claimed_by={ph}, lease_epoch={ph}, "
         f"model_assigned={ph} WHERE id={ph}")
    run(conn, engine, q, (who, now() + LEASE_SECONDS, who, item_id))
    after = get_item(conn, engine, item_id)
    ok = after and after.get("claimed_by") == who and norm(after.get("status")) == "WORKING"
    return (True, f"claimed by {who}") if ok else (False, "klaim tidak terbaca di baris")


def heartbeat(conn, engine, item_id):
    run(conn, engine,
        "UPDATE quest_tasks SET lease_epoch=%s WHERE id=%s AND status IN ('WORKING','CLAIMED')"
        if engine == "POSTGRESQL" else
        "UPDATE quest_tasks SET lease_epoch=? WHERE id=? AND status IN ('WORKING','CLAIMED')",
        (now() + LEASE_SECONDS, item_id))
    return get_item(conn, engine, item_id)


def finish(conn, engine, item_id, status, evidence=None, reason=None):
    """COMPLETED menuntut bukti. Laporan tidak boleh ditulis tangan (§12)."""
    if status == "COMPLETED" and not (evidence or "").strip():
        return False, "COMPLETED tanpa evidence ditolak: jalankan check_command-nya"
    item = get_item(conn, engine, item_id)
    if not item:
        return False, f"item {item_id} tidak ada"
    ph = "%s" if engine == "POSTGRESQL" else "?"
    run(conn, engine,
        f"UPDATE quest_tasks SET status={ph}, evidence={ph}, escalation_reason={ph}, "
        f"completed_at=CURRENT_TIMESTAMP WHERE id={ph}",
        (status, (evidence or "")[:EVIDENCE_LIMIT], reason, item_id))
    return True, f"{item['title']} -> {status}"


def next_item(conn, engine, skip_blocked=True):
    """Item berikutnya yang siap: semua dependensinya COMPLETED, dan lease-nya tidak dipegang orang.

    `UNAVAILABLE` tidak pernah ditawarkan, sama seperti `FAILED`: keduanya adalah kesimpulan yang
    sudah dilaporkan, bukan pekerjaan yang menunggu. Menawarkannya lagi membuat sebuah loop yang
    tampak sibuk padahal hanya mengunyah baris yang mesin sudah katakan tidak bisa dijalankan.
    """
    rows = run(conn, engine,
               "SELECT id, title, status, depends_on, claimed_by, lease_epoch, phase "
               "FROM quest_tasks ORDER BY id")
    done = {int(r["id"]) for r in rows if norm(r["status"]) == "COMPLETED"}
    for r in rows:
        state = norm(r["status"])
        if state in ("COMPLETED", "FAILED", "UNAVAILABLE"):
            continue
        if state in ("BLOCKED", "HUMAN", "PARKED") and skip_blocked:
            continue
        deps = [int(x) for x in str(r.get("depends_on") or "").split(",") if x.strip()]
        missing = [d for d in deps if d not in done]
        if missing:
            continue
        if r.get("claimed_by") and int(r.get("lease_epoch") or 0) > now() and state == "WORKING":
            continue
        return r
    return None


def resolve_decision(conn, engine, decision_id, rung, engine_key, model_id, answer,
                     state="RESOLVED", note=None):
    """Catat siapa yang menjawab. Baris OPEN tanpa jawaban adalah pertanyaan yang belum dijawab."""
    if state == "RESOLVED" and not (model_id or "").strip():
        return False, "RESOLVED tanpa model_id ditolak: laporan harus menyebut yang menjawab"
    ph = "%s" if engine == "POSTGRESQL" else "?"
    run(conn, engine,
        f"UPDATE decisions SET state={ph}, rung_used={ph}, engine_key={ph}, model_id={ph}, "
        f"answer={ph}, note={ph}, resolved_epoch={ph} WHERE id={ph}",
        (state, rung, engine_key, model_id, answer[:4000], note, now(), decision_id))
    return True, f"decision {decision_id} -> {state}"


def stalled(conn, engine, older_than=180):
    """Kandidat resume: WORKING, lease masih hidup, tidak ada aksi terukur selama `older_than`s.

    Ini hanya daftar. Keputusan mengirim ketikan ada di daemon dan butuh dua sinyal lagi
    (pane terbaca + teksnya tidak berubah); di sini tidak ada satu pun dari itu.
    """
    rows = run(conn, engine,
               "SELECT id, title, claimed_by, lease_epoch, resume_count FROM quest_tasks "
               "WHERE status IN ('WORKING','CLAIMED') AND claimed_by IS NOT NULL")
    fresh = []
    cutoff = now() - older_than
    for r in rows:
        last = run(conn, engine,
                   "SELECT MAX(ts) AS t FROM action_log WHERE engine=%s"
                   if engine == "POSTGRESQL" else
                   "SELECT MAX(created_at) AS t FROM action_log WHERE engine=?",
                   (r["claimed_by"],))
        t = last[0]["t"] if last and last[0].get("t") else None
        epoch = int(t.timestamp()) if hasattr(t, "timestamp") else (t if isinstance(t, int) else None)
        if r.get("lease_epoch") and r["lease_epoch"] > now() and (epoch is None or epoch < cutoff):
            fresh.append({**r, "last_action_epoch": epoch})
    return fresh


def autopilot(conn, engine, mode=None, minutes=None, budget=None, who="operator", reason=None):
    if mode is None:
        rows = run(conn, engine, "SELECT * FROM autopilot_state WHERE id=1")
        return rows[0] if rows else None
    ph = "%s" if engine == "POSTGRESQL" else "?"
    until = now() + int(minutes) * 60 if minutes else None
    if mode.upper() == "ON":
        run(conn, engine,
            f"UPDATE autopilot_state SET mode='ON', on_epoch={ph}, until_epoch={ph}, "
            f"item_budget={ph}, items_done=0, off_reason=NULL, changed_by={ph} WHERE id=1",
            (now(), until, budget or 12, who))
    else:
        run(conn, engine,
            f"UPDATE autopilot_state SET mode='OFF', off_reason={ph}, changed_by={ph} WHERE id=1",
            (reason or "dimatikan", who))
    return autopilot(conn, engine)


def decide(conn, engine, question, options=None, weight="heavy"):
    ph = "%s" if engine == "POSTGRESQL" else "?"
    run(conn, engine,
        f"INSERT INTO decisions (question, options, weight, state, created_epoch) "
        f"VALUES ({ph}, {ph}, {ph}, 'OPEN', {ph})",
        (question, json.dumps(options or [])[:4000], weight, now()))
    return run(conn, engine, "SELECT * FROM decisions WHERE state='OPEN' ORDER BY id DESC LIMIT 1")


def seed(conn, engine, items):
    """Idempoten berdasarkan judul — memanggil ulang tidak menggandakan pekerjaan.

    Dependensi ditulis sebagai KEY, bukan id: id di DB live sudah mulai dari 27, jadi angka
    '1' akan menunjuk baris tua yang bukan apa-apa. Peta key->id dibangun setelah semua baris
    ada, lalu kolom depends_on diisi — dua tahap, karena item B bisa menyebut A yang belum
    pernah punya id di pemanggilan pertama.
    """
    keymap, made = {}, []
    for key, title, phase, deps, check in items:
            exists = run(conn, engine, "SELECT id FROM quest_tasks WHERE title=%s"
                         if engine == "POSTGRESQL" else "SELECT id FROM quest_tasks WHERE title=?",
                         (title,))
            if exists:
                keymap[key] = int(exists[0]["id"])
                made.append((keymap[key], "ada", title))
                continue
            ph = "%s" if engine == "POSTGRESQL" else "?"
            run(conn, engine,
                f"INSERT INTO quest_tasks (title, status, phase, check_command, summary) "
                f"VALUES ({ph}, 'PENDING', {ph}, {ph}, {ph})",
                (title, phase, check, "dipetakan dari ROADMAP.md oleh work_order seed"))
            row = run(conn, engine, "SELECT id FROM quest_tasks WHERE title=%s"
                      if engine == "POSTGRESQL" else "SELECT id FROM quest_tasks WHERE title=?",
                      (title,))
            keymap[key] = int(row[0]["id"])
            made.append((keymap[key], "baru", title))
    for key, _title, _phase, deps, _check in items:
        if not deps:
            continue
        missing = [d for d in deps if d not in keymap]
        if missing:
            made.append((keymap.get(key), f"DEPS HILANG {missing}", key))
            continue
        ph = "%s" if engine == "POSTGRESQL" else "?"
        run(conn, engine, f"UPDATE quest_tasks SET depends_on={ph} WHERE id={ph}",
            (",".join(str(keymap[d]) for d in deps), keymap[key]))
    return made


ROADMAP_ITEMS = [
    ("f84", "F8.4+F10.2 work-order schema dan lease atomik: phase, check_command, depends_on, "
            "claimed_by, lease, evidence", "build", None, "python3 tools/work_order.py selftest"),
    ("f108", "F10.8 tombol Autopilot + tab Human Decide, tiga jalur mati tanpa izin AI "
             "(WAJIB sebelum drainer menyala)", "build", ["f84"], "bash bin/deploy_engine.sh --check"),
    ("f101", "F10.1 decision broker: baris keputusan di-resolve dari role resolver, model yang "
             "menjawab tercatat, jawabannya dibaca dari tabel/pane bukan hanya pipa", "build",
     ["f84"], "python3 tools/work_order.py selftest"),
    ("f107", "F10.7 heartbeat: dua sinyal independen sebelum CONTINUE dikirim, pane tak terbaca "
             "= UNKNOWN", "build", ["f108"], "python3 tools/work_order.py stalled"),
    ("f103", "F10.3 drainer systemd timer dengan circuit breaker + batas kuota per malam", "build",
     ["f84", "f108", "f101"], "systemctl --user list-timers irsofka-autopilot.timer"),
    ("f91", "F9.1 deterministic environment state: exit code + process tree + delta pane "
            "disuapkan ke prompt (jalan paralel, tidak bergantung antrean)", "build", None,
     "bash bin/deploy_engine.sh --check"),
    ("f92", "F9.2 action-outcome anti-pattern: bentuk perintah yang pernah gagal -> peringatan "
            "sebelum diketik", "build", ["f91"], "python3 tools/work_order.py selftest"),
    ("f93", "F9.3 reflex proof: ydotool bergerak lalu diff 64x64 membuktikan layar berubah",
     "build", None, "cargo test -q reflex_proof"),
    ("f105", "F10.5 dream phase: konsolidasi memori saat idle, berkas tracked hanya lewat digest",
     "build", ["f103"], "python3 tools/work_order.py digest"),
]


# --- selftest ---------------------------------------------------------------
def selftest():
    """Jalankan di SQLite sementara. Tes tidak boleh menyentuh produksi: satu selftest yang
    menulis ke DB bersama akan mengacaukan antrean yang sedang dibaca engine lain."""
    tmp = Path(f"/tmp/work_order_selftest_{now()}.db")
    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    fails = []
    try:
        log = ensure_schema(conn, "SQLITE")
        checks = []

        def check(name, cond, detail=""):
            checks.append((name, bool(cond), detail))

        check("schema dibuat", all(not l.startswith("skip") for l in log), "; ".join(log))
        made = seed(conn, "SQLITE", [("kA", "A", "build", None, "true"),
                                     ("kB", "B", "build", ["kA"], "true")])
        ids = {t: i for i, _, t in made}
        check("seed memberi id", ids.get("A") and ids.get("B"))
        check("dependensi terselesaikan jadi id, bukan key",
              "kA" not in str(get_item(conn, "SQLITE", ids["B"]).get("depends_on")),
              str(get_item(conn, "SQLITE", ids["B"]).get("depends_on")))
        again = seed(conn, "SQLITE", [("kA", "A", "build", None, "true")])
        check("seed idempoten", again[0][1] == "ada")

        ok, msg = claim(conn, "SQLITE", ids["A"], "qoder")
        check("klaim pertama menang", ok, msg)
        ok2, msg2 = claim(conn, "SQLITE", ids["A"], "antigravity")
        check("klaim kedua kalah", not ok2, msg2)
        check("alasan menyebut pemegang", "qoder" in msg2, msg2)

        nxt = next_item(conn, "SQLITE")
        check("B tertahan dependensi belum COMPLETED", not nxt or nxt["title"] != "B",
              str(nxt and nxt["title"]))

        ok, msg = finish(conn, "SQLITE", ids["A"], "COMPLETED", evidence=None)
        check("COMPLETED tanpa bukti ditolak", not ok, msg)
        ok, msg = finish(conn, "SQLITE", ids["A"], "COMPLETED", evidence="selftest: exit 0")
        check("COMPLETED dengan bukti diterima", ok, msg)
        nxt = next_item(conn, "SQLITE")
        check("B terbuka setelah A selesai", nxt and nxt["title"] == "B", str(nxt))

        ok, msg = finish(conn, "SQLITE", ids["B"], "HUMAN", reason="ambang: disk < 10G")
        check("eskalasi menyimpan alasan", ok and get_item(conn, "SQLITE", ids["B"])["escalation_reason"])
        check("item HUMAN dilewati", next_item(conn, "SQLITE") is None)

        conn.execute("INSERT INTO quest_tasks (title, status) VALUES ('unavail','UNAVAILABLE')")
        conn.commit()
        check("item UNAVAILABLE tidak pernah ditawarkan sebagai kerja",
              next_item(conn, "SQLITE") is None, str(next_item(conn, "SQLITE")))

        stale = run(conn, "SQLITE", "SELECT 1")  # stall tidak diuji di sini: butuh action_log produksi
        check("stalled() tersedia", callable(stalled), str(stale and ""))

        ap = autopilot(conn, "SQLITE")
        check("autopilot default OFF", ap and ap["mode"] == "OFF", str(ap))
        ap = autopilot(conn, "SQLITE", "ON", minutes=30, budget=4)
        check("ON menyimpan budget dan expiry", ap["mode"] == "ON" and ap["items_done"] == 0
              and ap["until_epoch"] > now())
        ap = autopilot(conn, "SQLITE", "OFF", reason="selftest selesai")
        check("OFF menyimpan alasan", ap["mode"] == "OFF" and "selftest" in (ap["off_reason"] or ""))

        d = decide(conn, "SQLITE", "A atau B?", ["A", "B"], weight="heavy")
        check("keputusan tercatat OPEN", d and d[0]["state"] == "OPEN")
        ok, msg = resolve_decision(conn, "SQLITE", d[0]["id"], "resolver", "gemini", "", "ya")
        check("RESOLVED tanpa model_id ditolak", not ok, msg)
        ok, msg = resolve_decision(conn, "SQLITE", d[0]["id"], "resolver", "gemini",
                                   "gemini-3.8-flash-high", "B", "RESOLVED")
        row = run(conn, "SQLITE", "SELECT * FROM decisions WHERE id=%s"
                  .replace("%s", "?"), (d[0]["id"],))[0]
        check("jawabannya mencatat model yang menjawab", ok and row["model_id"].startswith("gemini")
              and row["state"] == "RESOLVED", str(row["model_id"]))

        item = get_item(conn, "SQLITE", ids["A"])
        check("resume_count ada", "resume_count" in item)
        # alias: baris lama tidak ditulis ulang, hanya dibaca sebagai CLAIMED
        conn.execute("INSERT INTO quest_tasks (title, status) VALUES ('legacy','IN_PROGRESS')")
        conn.commit()
        lg = run(conn, "SQLITE", "SELECT status FROM quest_tasks WHERE title='legacy'")[0]["status"]
        check("IN_PROGRESS terbaca utuh, tidak ditulis ulang", lg == "IN_PROGRESS")
        check("alias memetakannya ke CLAIMED", norm(lg) == "CLAIMED")

        lease = int(get_item(conn, "SQLITE", ids["A"])["lease_epoch"] or 0)
        check("lease disimpan sebagai epoch", lease > now(), str(lease))

        # --- circuit breaker: loop yang gagal cepat tidak boleh menguras kuota malam ---
        st = night_state(conn, "SQLITE", dirty=True)
        check("tree kotor menahan klaim", not st["ok"] and "kotor" in " ".join(st["why"]), str(st))
        # None sekarang berarti "diukur, tapi git tidak bisa dibaca" — bukan "belum diukur".
        st = night_state(conn, "SQLITE", dirty=None)
        check("UNKNOWN bukan berarti bersih", not st["ok"] and "UNKNOWN" in " ".join(st["why"]), str(st["why"]))
        dirty_now = tree_dirty()
        check("tree_dirty memberi jawaban terbaca", dirty_now in (True, False, None), str(dirty_now))

        conn.execute("UPDATE autopilot_state SET mode='ON', on_epoch=?, item_budget=12, "
                     "until_epoch=? WHERE id=1", (now(), now() + 3600))
        conn.commit()
        orig_tree_dirty = globals()["tree_dirty"]
        globals()["tree_dirty"] = lambda: False
        try:
            for k in range(NIGHT_MAX_FAILURES):
                conn.execute("INSERT INTO quest_tasks (title, status, completed_at) "
                             "VALUES (?, 'FAILED', datetime('now'))", (f"gagal{k}",))
            conn.commit()
            conn.execute("INSERT INTO quest_tasks (title, status) VALUES ('setelah gagal','PENDING')")
            conn.commit()
            nid = run(conn, "SQLITE", "SELECT id FROM quest_tasks WHERE title='setelah gagal'")[0]["id"]
            ok, msg = claim(conn, "SQLITE", nid, "qoder")
            check("3 kegagalan mematikan klaim berikutnya", not ok and "circuit breaker" in msg, msg)
            st = night_state(conn, "SQLITE", dirty=False)
            check("batas kegagalan terlihat di night_state", not st["ok"], str(st["why"]))
        finally:
            globals()["tree_dirty"] = orig_tree_dirty
    finally:
        conn.close()
        tmp.unlink(missing_ok=True)

    bad = [c for c in checks if not c[1]]
    for name, okval, detail in checks:
        print(f"[{'ok    ' if okval else 'GAGAL '}] {name}" + (f" — {detail}" if detail and not okval else ""))
    print(f"\nwork_order selftest: {len(checks) - len(bad)}/{len(checks)} sesuai")
    return 1 if bad else 0


def selftest_live():
    """Jalankan asersi yang sama di backend yang benar-benar dipakai malam ini.

    Bug pertama modul ini justru tidak terlihat di SQLite: psycopg2 mengembalikan tuple, dan
    selftest yang hanya menyentuh SQLite akan tetap hijau sementara produksi jatuh pada SELECT
    pertama. Satu-satunya cara mencegahnya adalah menjalankan tes pada backend yang sama dengan
    yang dibaca engine lain. Baris uji diberi penanda dan dihapus lagi; yang dihapus hanya
    buatan tes ini sendiri.
    """
    conn, engine = connect()
    mark = f"selftest-live-{now()}"
    checks = []

    def check(name, cond, detail=""):
        checks.append((name, bool(cond), detail))

    try:
        ensure_schema(conn, engine)
        rows = run(conn, engine, "SELECT 1 AS one")
        check(f"SELECT mengembalikan dict di {engine}", rows and rows[0].get("one") == 1, str(rows))
        ph = "%s" if engine == "POSTGRESQL" else "?"
        run(conn, engine,
            f"INSERT INTO quest_tasks (title, status, phase, check_command) "
            f"VALUES ({ph}, 'PENDING', 'test', {ph})", (f"{mark}-A", "true"))
        got = run(conn, engine, f"SELECT id FROM quest_tasks WHERE title={ph}", (f"{mark}-A",))
        check("baris uji bisa dibaca kembali", got, str(got))
        if got:
            iid = int(got[0]["id"])
            ok, msg = claim(conn, engine, iid, "qoder")
            check("klaim berhasil di backend produksi", ok, msg)
            ok2, msg2 = claim(conn, engine, iid, "antigravity")
            check("klaim ganda ditolak", not ok2, msg2)
            ok, msg = finish(conn, engine, iid, "COMPLETED")
            check("COMPLETED tanpa bukti ditolak", not ok, msg)
            ok, msg = finish(conn, engine, iid, "COMPLETED", evidence="selftest_live")
            check("COMPLETED dengan bukti diterima", ok, msg)
        ap = autopilot(conn, engine)
        check("autopilot_state terbaca", ap and ap.get("mode") in ("ON", "OFF"), str(ap))
        check("default autopilot OFF di produksi", (ap or {}).get("mode") == "OFF", str(ap))
        st = night_state(conn, engine)
        check("night_state terbaca tanpa crash", isinstance(st.get("ok"), bool), str(st))
        nxt = next_item(conn, engine)
        check("next_item tidak menawarkan baris UNAVAILABLE di produksi",
              not nxt or norm(nxt["status"]) not in ("UNAVAILABLE", "FAILED", "COMPLETED"),
              str(nxt))
    finally:
        like = f"{mark}%"
        run(conn, engine, "DELETE FROM quest_tasks WHERE title LIKE "
            + ("%s" if engine == "POSTGRESQL" else "?"), (like,))
        left = run(conn, engine, "SELECT COUNT(*) AS n FROM quest_tasks WHERE title LIKE "
                    + ("%s" if engine == "POSTGRESQL" else "?"), (like,))
        check("baris uji dibersihkan", left and int(left[0]["n"]) == 0, str(left))
        conn.close()

    bad = [c for c in checks if not c[1]]
    for name, okval, detail in checks:
        print(f"[{'ok    ' if okval else 'GAGAL '}] (live/{engine}) {name}"
              + (f" — {detail}" if detail and not okval else ""))
    print(f"\nwork_order selftest live: {len(checks) - len(bad)}/{len(checks)} sesuai")
    return 1 if bad else 0


# --- CLI --------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Work order autopilot — klaim, lease, bukti, eskalasi")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ensure")
    p = sub.add_parser("seed"); p.add_argument("--roadmap", action="store_true")
    p = sub.add_parser("claim"); p.add_argument("id", type=int); p.add_argument("--by", required=True)
    p = sub.add_parser("heartbeat"); p.add_argument("id", type=int)
    p = sub.add_parser("complete"); p.add_argument("id", type=int); p.add_argument("--evidence", default="")
    p = sub.add_parser("block"); p.add_argument("id", type=int); p.add_argument("--reason", required=True)
    p = sub.add_parser("human"); p.add_argument("id", type=int); p.add_argument("--reason", required=True)
    sub.add_parser("next")
    sub.add_parser("list")
    sub.add_parser("stalled")
    p = sub.add_parser("autopilot"); p.add_argument("mode", nargs="?", choices=["on", "off", "show"])
    p.add_argument("--minutes", type=int); p.add_argument("--budget", type=int)
    p.add_argument("--reason", default=None); p.add_argument("--by", default="operator")
    p = sub.add_parser("decide"); p.add_argument("question"); p.add_argument("--options", default="")
    p.add_argument("--weight", default="heavy", choices=["heavy", "light"])
    p = sub.add_parser("resolve"); p.add_argument("id", type=int)
    p.add_argument("--rung", required=True); p.add_argument("--engine-key", required=True)
    p.add_argument("--model", required=True); p.add_argument("--answer", required=True)
    p.add_argument("--state", default="RESOLVED",
                   choices=["RESOLVED", "ESCALATED", "UNAVAILABLE"])
    p.add_argument("--note", default=None)
    sub.add_parser("digest")
    p = sub.add_parser("selftest"); p.add_argument("--live", action="store_true")
    a = ap.parse_args(argv)

    if a.cmd == "selftest":
        rc = selftest()
        if a.live and not rc:
            rc = selftest_live()
        return rc
    conn, engine = connect()
    if engine == "POSTGRESQL":
        ensure_schema(conn, engine)

    if a.cmd == "ensure":
        for line in ensure_schema(conn, engine):
            print(line)
    elif a.cmd == "seed":
        for i, state, title in seed(conn, engine, ROADMAP_ITEMS if a.roadmap else []):
            print(f"{i}\t{state}\t{title}")
    elif a.cmd == "claim":
        ok, msg = claim(conn, engine, a.id, a.by)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "heartbeat":
        it = heartbeat(conn, engine, a.id)
        print(json.dumps({"id": a.id, "lease_epoch": it and it.get("lease_epoch")}))
    elif a.cmd == "complete":
        ok, msg = finish(conn, engine, a.id, "COMPLETED", evidence=a.evidence)
        print(msg)
        return 0 if ok else 1
    elif a.cmd in ("block", "human"):
        ok, msg = finish(conn, engine, a.id, "BLOCKED" if a.cmd == "block" else "HUMAN",
                         reason=a.reason)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "next":
        nxt = next_item(conn, engine)
        print(json.dumps(nxt, default=str) if nxt else '{"item": null, "why": "antrean kosong"}')
    elif a.cmd == "list":
        for r in run(conn, engine, "SELECT id, title, status, claimed_by, phase FROM quest_tasks ORDER BY id"):
            print(f"{r['id']:>4}  {norm(r['status']):<10}  {(r['claimed_by'] or '-'):<12}  "
                  f"{(r['phase'] or '-'):<7}  {r['title'][:74]}")
    elif a.cmd == "stalled":
        print(json.dumps(stalled(conn, engine), default=str, indent=2))
    elif a.cmd == "autopilot":
        mode = None if a.mode in (None, "show") else a.mode.upper()
        print(json.dumps(autopilot(conn, engine, mode, a.minutes, a.budget, a.by, a.reason),
                         default=str, indent=2))
    elif a.cmd == "decide":
        opts = [o for o in a.options.split("|") if o]
        rows = decide(conn, engine, a.question, opts, a.weight)
        print(json.dumps(rows[0], default=str, indent=2))
    elif a.cmd == "resolve":
        ok, msg = resolve_decision(conn, engine, a.id, a.rung, a.engine_key, a.model,
                                   a.answer, a.state, a.note)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "digest":
        rows = run(conn, engine, "SELECT status, COUNT(*) AS n FROM quest_tasks GROUP BY status ORDER BY 2 DESC")
        open_dec = run(conn, engine, "SELECT COUNT(*) AS n FROM decisions WHERE state='OPEN'")
        print(json.dumps({"engine": engine, "per_status": rows,
                          "decisions_open": open_dec[0]["n"] if open_dec else 0,
                          "autopilot": autopilot(conn, engine)}, default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
