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
import inspect
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db_state import get_db_connection, DB_SQLITE_PATH  # noqa: E402

# Kosakata status. IN_PROGRESS dibiarkan utuh pada baris lama: menulis ulang sejarah laporan
# adalah cara tercepat membuat ledger tidak lagi dipercaya (§12, "reports are derived").
STATUS = ("PENDING", "CLAIMED", "WORKING", "BLOCKED", "HUMAN", "PARKED",
          "COMPLETED", "FAILED", "UNAVAILABLE")
ALIAS = {"IN_PROGRESS": "CLAIMED", "DONE": "COMPLETED"}

LEASE_SECONDS = 900          # satu item tanpa perpanjangan lease 15 menit dianggap lepas
MIN_SAMPLE_GAP = 1           # detik; di bawahnya "pane tidak berubah" bukan pembacaan apa pun
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


def items_done_in_window(conn, engine, on_epoch):
    """Berapa item COMPLETED sejak jendela ON dibuka — diturunkan dari ledger, bukan dihitung orang.

    Bug yang diikat di sini diukur dua kali, karena keduanya lolos tanpa suara: `%%s` menghasilkan
    teks `'%s'`, dan `strftime()` sendiri juga mengembalikan TEKS. SQLite membandingkan teks dengan
    integer memakai aturan "integer selalu lebih kecil dari teks", jadi `strftime('%s', …) >= 12345`
    benar untuk SEMUA baris — jendela yang tampak menyaring padahal tidak menyaring apa pun. CAST
    memaksa perbandingan terjadi di tanah angka; `unixepoch()` setara di SQLite >= 3.38 tapi tidak
    ada jaminan versi sistem yang jadi fallback, jadi bentuk lama yang dipaksa yang dipakai.

    PostgreSQL menyimpan jebakan yang sama dengan ukuran berbeda, dan jebakannya justru yang
    ditemukan terakhir: `completed_at` adalah TIMESTAMP tanpa zona — ditulis lewat
    CURRENT_TIMESTAMP dalam waktu lokal — dan `EXTRACT(EPOCH FROM <timestamp>)` memperlakukan
    nilai tanpa zona sebagai UTC. Terukur 2026-10-08: selisihnya +25 200 detik, tepat sebesar
    offset Asia/Jakarta; bentuk `::timestamptz` menyamakannya dengan `time.time()` sampai detik.
    Cast itu bukan hiasan — tanpanya tiap item yang baru selesai malam ini terlihat berada di
    masa depan, dan anggaran item per jendela membaca angka yang salah tanpa pernah bilang.

    Batas yang diterima apa adanya: `completed_at` beresolusi satu detik, jadi baris yang selesai
    pada detik yang sama dengan dibukanya jendela ikut terhitung (>=). Anggaran semalam adalah
    bilangan puluhan, dan selisih satu item pada detik perataan tidak mengubah keputusan apa pun;
    membuat jendela terlihat lebih tajam dari alat ukurnya sendiri hanyalah cara lain mengarang
    laporan.
    """
    if not on_epoch:
        return 0
    rows = run(conn, engine,
               "SELECT COUNT(*) AS n FROM quest_tasks WHERE status='COMPLETED' "
               "AND EXTRACT(EPOCH FROM completed_at::timestamptz) >= %s"
               if engine == "POSTGRESQL" else
               "SELECT COUNT(*) AS n FROM quest_tasks WHERE status='COMPLETED' "
               "AND CAST(strftime('%s', completed_at) AS INTEGER) >= ?",
               (int(on_epoch),))
    return int(rows[0]["n"]) if rows else 0


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
    done = int((ap or {}).get("items_done") or 0)
    why = []
    if (ap or {}).get("mode") == "ON" and on_since and int(ap.get("until_epoch") or 0) < now():
        why.append("jendela ON lewat")
    if n_fail >= NIGHT_MAX_FAILURES:
        why.append(f"{n_fail} kegagalan berturut-turut (batas {NIGHT_MAX_FAILURES})")
    if dirty is True:
        why.append("working tree kotor")
    if dirty is None:
        why.append("git tidak terbaca — UNKNOWN, bukan bersih")
    if done >= NIGHT_MAX_ITEMS:
        why.append(f"batas {NIGHT_MAX_ITEMS} item per sesi terlampaui")
    return {"mode": (ap or {}).get("mode"), "failures_12h": n_fail, "items_done": done,
            "tree_dirty": dirty, "ok": not why, "why": why}


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
        weight VARCHAR(20) DEFAULT 'heavy', rung_used VARCHAR(50), engine_key VARCHAR(50),
        model_id VARCHAR(120), answer TEXT, state VARCHAR(20) DEFAULT 'OPEN',
        note TEXT, created_epoch BIGINT, resolved_epoch BIGINT)""",
    """CREATE TABLE IF NOT EXISTS autopilot_state (
        id SMALLINT PRIMARY KEY DEFAULT 1, mode VARCHAR(10) DEFAULT 'OFF',
        on_epoch BIGINT, until_epoch BIGINT, item_budget INT DEFAULT 12,
        items_done INT DEFAULT 0, off_reason TEXT, changed_by VARCHAR(60) DEFAULT 'operator',
        level VARCHAR(16) DEFAULT 'observe', level_until BIGINT)""",
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
    # Tangga tangan drainer (F10.3) tinggal di baris autopilot yang sama dengan sakelarnya:
    # `mode` menjawab SIAPA yang memegang mesin, `level` menjawab JAUH MANA tangan boleh
    # bergerak. Dua tabel untuk satu sakelar adalah dua laporan yang boleh berbeda (§12).
    ("autopilot_state", "level", "VARCHAR(16)", "TEXT"),
    ("autopilot_state", "level_until", "BIGINT", "INTEGER"),
]

# Lebar kolom yang ditumbuhkan, bukan diganti isinya. `weight` lahir sebagai VARCHAR(10) pada
# saat kosakatanya masih muat di situ; bobot ketiga dari invarian 3 — `irreversible`, 12 huruf —
# meledak di PostgreSQL dengan StringDataRightTruncation sementara SQLite tidak pernah memeriksa
# panjang, jadi fixture offline tetap hijau. Kolom dibaca dulu; hanya yang sempit yang dilebarkan,
# supaya pemanggilan ulang tidak menulis ulang tabel yang sudah benar.
WIDENINGS = [
    ("decisions", "weight", 20),
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
    if engine == "POSTGRESQL":
        # Lewat run(), bukan cur.execute().fetchone(): cursor psycopg2 mengembalikan None dari
        # execute, jadi bentuk sqlite3 di sini akan AttributeError tepat saat schema sedang
        # diperbaiki — dan ensure_schema dipanggil pada setiap start, bukan saat sepi.
        for table, column, want in WIDENINGS:
            have = run(conn, engine,
                       "SELECT character_maximum_length AS n FROM information_schema.columns "
                       "WHERE table_name=%s AND column_name=%s", (table, column))
            width = have[0]["n"] if have else None
            if width and width < want:
                cur.execute(f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({want})")
                log.append(f"widen {table}.{column} {width}->{want}")
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
    seluruh selftest SQLite tetap jatuh di produksi PostgreSQL pada SELECT pertama.
    """
    cur = conn.cursor()
    # `params` tidak boleh diteruskan kalau tidak ada: psycopg2 menafsirkan setiap `%`
    # dalam SQL sebagai placeholder begitu ia menerima tuple parameter, jadi satu `LIKE
    # 'autopilot%'` tanpa parameter akan meledak dengan "tuple index out of range" — di
    # PostgreSQL saja, karena SQLite tidak mengenal interpolation itu sama sekali.
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
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


def renew_lease(conn, engine, item_id):
    """Perpanjang lease. Fungsi ini pernah bernama `heartbeat` — satu kata untuk dua hal berbeda;
    §12 melarang itu, jadi nama itu sekarang hanya punya indera F10.7 di bawah."""
    run(conn, engine,
        "UPDATE quest_tasks SET lease_epoch=%s WHERE id=%s AND status IN ('WORKING','CLAIMED')"
        if engine == "POSTGRESQL" else
        "UPDATE quest_tasks SET lease_epoch=? WHERE id=? AND status IN ('WORKING','CLAIMED')",
        (now() + LEASE_SECONDS, item_id))
    return get_item(conn, engine, item_id)


def nudge(conn, engine, item_id):
    """Naikkan `resume_count` satu kali — dan tolak kenaikan yang melewati batas.

    Batasnya ditegakkan di sini, bukan di pemanggil, mengikuti preseden yang sama dengan circuit
    breaker yang hidup di dalam `claim()`: manusia yang mengetik perintah yang sama malam ini
    menabrak angka yang sama. Tulisan dilakukan sebagai `COALESCE(...)+1` lalu dibaca kembali,
    karena yang menentukan adalah barisnya, bukan siapa yang paling cepat bertanya (§12, F10.2).

    Yang TIDAK dilakukan fungsi ini: mengirim apa pun. Ia menghitung sentuhan, bukan menyentuh.
    """
    item = get_item(conn, engine, item_id)
    if not item:
        return False, f"item {item_id} tidak ada"
    used = int(item.get("resume_count") or 0)
    if used >= MAX_RESUME:
        return False, (f"budget resume habis ({used}/{MAX_RESUME}) — item ini macet, bukan "
                       "tertidur; empat sentuhan tidak membuatnya bergerak")
    ph = "%s" if engine == "POSTGRESQL" else "?"
    run(conn, engine,
        f"UPDATE quest_tasks SET resume_count=COALESCE(resume_count, 0) + 1 WHERE id={ph}",
        (item_id,))
    after = int((get_item(conn, engine, item_id) or {}).get("resume_count") or 0)
    if after > MAX_RESUME:
        return False, (f"naik ke {after} melewati {MAX_RESUME}: pemanggil lain sudah menyentuh "
                       "item ini lebih dulu")
    if after <= used:
        return False, f"kenaikan tidak terbaca kembali ({used} -> {after})"
    return True, f"resume {after}/{MAX_RESUME}"


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


def next_item(conn, engine, skip_blocked=True, respect_due=False):
    """Item berikutnya yang siap: semua dependensinya COMPLETED, dan lease-nya tidak dipegang orang.

    `UNAVAILABLE` tidak pernah ditawarkan, sama seperti `FAILED`: keduanya adalah kesimpulan yang
    sudah dilaporkan, bukan pekerjaan yang menunggu. Menawarkannya lagi membuat sebuah loop yang
    tampak sibuk padahal hanya mengunyah baris yang mesin sudah katakan tidak bisa dijalankan.

    `respect_due` menambahkan satu syarat lagi atas nama pemanggil yang punya ritme: `due_epoch`
    adalah kolom antrean tertunda, dan yang berhak menuliskannya adalah yang menahan dirinya sendiri
    (drainer, F10.3). Filternya tinggal di sini karena menentukan "apa berikutnya" adalah bagian
    dari kosakata antrean — kalau drainer menyusun urutannya sendiri, ada dua definisi "berikutnya"
    dan tidak ada yang bisa mengatakan yang mana yang dibaca GUI (§12).
    """
    rows = run(conn, engine,
               "SELECT id, title, status, depends_on, claimed_by, lease_epoch, phase, due_epoch "
               "FROM quest_tasks ORDER BY id")
    done = {int(r["id"]) for r in rows if norm(r["status"]) == "COMPLETED"}
    for r in rows:
        state = norm(r["status"])
        if state in ("COMPLETED", "FAILED", "UNAVAILABLE"):
            continue
        if state in ("BLOCKED", "HUMAN", "PARKED") and skip_blocked:
            continue
        if respect_due and int(r.get("due_epoch") or 0) > now():
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
    """Catat siapa yang menjawab. Baris OPEN tanpa jawaban adalah pertanyaan yang belum dijawab.

    `answer` boleh None: jalur ESCALATED memang tidak punya jawaban, dan di sinilah pertanyaan
    yang tidak bisa diselesaikan mesin berakhir di antrean orang. Membedah None dengan slicing
    akan membuat kegagalan pertama yang paling penting justru crash alih-alih melaporkan.
    """
    if state == "RESOLVED" and not (model_id or "").strip():
        return False, "RESOLVED tanpa model_id ditolak: laporan harus menyebut yang menjawab"
    ph = "%s" if engine == "POSTGRESQL" else "?"
    run(conn, engine,
        f"UPDATE decisions SET state={ph}, rung_used={ph}, engine_key={ph}, model_id={ph}, "
        f"answer={ph}, note={ph}, resolved_epoch={ph} WHERE id={ph}",
        (state, rung, engine_key, model_id, answer[:4000] if answer else None,
         note, now(), decision_id))
    return True, f"decision {decision_id} -> {state}"


def _last_action(conn, engine, who):
    """`(epoch, terbaca)` aksi terukur terakhir satu engine — dua nilai, bukan satu.

    Satu angka tidak boleh menyamar jadi dua hal. Tabel yang tidak terbaca dan engine yang memang
    belum pernah mencatat sama-sama kembali sebagai None, dan kalau keduanya berarti "tidak ada
    aksi", sinyal yang HILANG justru terbaca sebagai bukti terkuat bahwa sebuah pane menganggur.
    Invarian 5 berlaku ke dua sisi, bukan cuma sisi tmux. Yang TIDAK bisa dibedakan dari sini adalah
    "belum pernah mencatat" dan "mencatat dengan nama lain" — `claimed_by` dan kolom `engine` punya
    ruang penamaan masing-masing — jadi `heartbeat()` memperlakukan None sebagai sinyal yang tidak
    mendukung kesimpulan apa pun, bukan sebagai bukti menganggur.

    Kolomnya `ts` di kedua backend (DDL milik `session_ingestor.py`: `TIMESTAMPTZ` di Postgres,
    `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` di SQLite), tapi bentuknya beda: Postgres mengirim datetime
    ber-zona, SQLite mengirim teks UTC. Teks yang diperlakukan sebagai waktu lokal membuat umur aksi
    meleset sejauh zona waktu mesin — tujuh jam di sini — dan meleset ke masa lalu adalah arah yang
    melahirkan `AT_REST` palsu.
    """
    try:
        rows = run(conn, engine,
                   "SELECT MAX(ts) AS t FROM action_log WHERE engine=" +
                   ("%s" if engine == "POSTGRESQL" else "?"),
                   (who,))
    except Exception:  # noqa: BLE001 — tabel tidak ada / koneksi seret: itu UNKNOWN, bukan diam
        # Koneksi ini autocommit (db_state.py: conn.autocommit = True), jadi kegagalan SELECT tidak
        # meninggalkan transaksi aborted yang membuat pembacaan berikutnya ikut gagal.
        return None, False
    v = rows[0]["t"] if rows and rows[0].get("t") else None
    if v is None:
        return None, True
    if isinstance(v, str):
        try:
            v = datetime.strptime(v[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None, False
    if hasattr(v, "timestamp"):
        return int(v.timestamp()), True
    # Float / Decimal / date bukan bentuk yang dijanjikan DDL mana pun. Membacanya sebagai
    # "terbaca, cuma None" akan menyamar jadi "engine ini belum berbuat apa-apa".
    return (v if isinstance(v, int) else None), isinstance(v, int)


def stalled(conn, engine, older_than=180):
    """Kandidat resume: WORKING, lease masih hidup, tidak ada aksi terukur selama `older_than`s.

    Ini hanya daftar satu sinyal. `heartbeat()` yang menambahkan sinyal kedua dan aturan UNKNOWN,
    dan hanya bacaannya yang boleh dipakai. Karena itu tiap baris membawa `action_log_readable`:
    daftar yang menyamar jadi kesimpulan adalah cara tercepat mengetik ke tengah kerja orang lain.
    """
    rows = run(conn, engine,
               "SELECT id, title, claimed_by, lease_epoch, resume_count FROM quest_tasks "
               "WHERE status IN ('WORKING','CLAIMED') AND claimed_by IS NOT NULL")
    fresh = []
    cutoff = now() - older_than
    for r in rows:
        epoch, readable = _last_action(conn, engine, r["claimed_by"])
        if readable and r.get("lease_epoch") and r["lease_epoch"] > now() \
                and (epoch is None or epoch < cutoff):
            fresh.append({**r, "last_action_epoch": epoch, "action_log_readable": True})
    return fresh


def _pane_text(capture, tab):
    """Satu pembacaan pane. `capture` milik orang lain (tmux, nanti SSH, nanti mesin lain), jadi
    bentuk kegagalannya bukan hanya None: fungsi yang melempar adalah pembacaan yang mati, dan
    PEMBACAAN YANG MATI adalah UNKNOWN — bukan pane kosong, bukan pane diam, bukan crash."""
    try:
        return capture(tab)
    except Exception:  # noqa: BLE001
        return None


def heartbeat(conn, engine, quiet_seconds=180, sample_gap=3, capture=None, busy=None):
    """WORKING / AT_REST / UNKNOWN per klaim terbuka — dua sinyal harus setuju dulu.

    Yang ditolak di sini adalah cara termudah: menyimpulkan "diam" dari satu sinyal. Sebuah CLI
    yang sedang berpikir panjang tidak menulis baris `action_log`, dan satu yang baru saja selesai
    menulis juga tidak. Jadi dibutuhkan dua pembacaan yang saling bebas — umur aksi di database dan
    teks pane yang berubah di antara dua sampel — dan `AT_REST` hanya keluar kalau KEDUANYA setuju.
    Salah menyimpulkan `AT_REST` berarti mengetik `CONTINUE` ke tengah pekerjaan orang lain.

    Invarian 5 menentukan sisanya, untuk KEDUA sisi, dan ia kejam: EMPAT keadaan berbeda semuanya
    menghasilkan nol pembacaan dan keempatnya `UNKNOWN`, bukan "diam" — pane yang tidak terbaca, tabel
    yang tidak terbaca, tabel yang terbaca tapi KOSONG untuk nama engine ini, dan dua pembacaan yang
    diambil tanpa jeda. Yang ketiga itu temuan reviewer (Oct 8, 02:05): `claimed_by` dan kolom `engine`
    di ledger adalah dua ruang penamaan yang tidak dijamin sama, jadi "tidak ada baris atas nama qoder"
    bisa berarti "ia belum berbuat apa-apa" ATAU "ledger menyimpannya dengan nama lain" — dan satu-
    satunya pembacaan yang tersisa lalu menyimpulkan diam dari satu sinyal.

    Interval karena itu ikut menjadi bukti, bukan bentuk: `sample_gap` punya lantai di jalur produksi
    (`MIN_SAMPLE_GAP`), dan 0 hanya honored untuk `capture` suntikan — tes tidak boleh tidur, produksi
    tidak boleh mengambil dua sampel di waktu yang sama. Karena itu tidak ada satu pun jalur lewat
    fungsi ini yang mengirim apa pun — fungsi ini hanya indera. Tangan milik F10.3, dan ia belum boleh
    dipakai malam ini.

    `capture`/`busy` bisa diganti supaya tes tidak membaca pane operator yang sedang hidup.
    """
    injected = capture is not None or busy is not None
    if capture is None or busy is None:
        import cross_verify as cv
        capture = capture or cv.capture_pane
        busy = busy or cv.pane_is_busy
    if sample_gap < MIN_SAMPLE_GAP and not injected:
        sample_gap = MIN_SAMPLE_GAP
    rows = run(conn, engine,
               "SELECT id, title, claimed_by, lease_epoch, resume_count FROM quest_tasks "
               "WHERE status IN ('WORKING','CLAIMED') AND claimed_by IS NOT NULL")
    if not rows:
        return []
    tabs = sorted({r["claimed_by"] for r in rows})
    before = {t: _pane_text(capture, t) for t in tabs}
    if sample_gap:
        time.sleep(sample_gap)
    after = {t: _pane_text(capture, t) for t in tabs}
    interval = sample_gap > 0

    out = []
    for r in rows:
        who = r["claimed_by"]
        epoch, log_readable = _last_action(conn, engine, who)
        quiet_s = None if epoch is None else now() - epoch
        b, a = before.get(who), after.get(who)
        pane_readable = bool(b) and bool(a)
        if not pane_readable and not log_readable:
            state, why = "UNKNOWN", "kedua sinyal mati: pane dan action_log tidak terbaca"
        elif not pane_readable:
            state, why = "UNKNOWN", "pane tidak terbaca — bukan berarti diam"
        elif not log_readable:
            state, why = "UNKNOWN", "action_log tidak terbaca: satu sinyal hilang, dua belum setuju"
        elif busy(a):
            state, why = "WORKING", "pane menampilkan spinner/esc-to-cancel"
        elif b != a:
            # Perubahan adalah bukti dengan sendirinya: dua pembacaan yang BERBEDA membuktikan gerak
            # walau diambil beruntun. Yang butuh jeda hanya kesimpulan "tidak ada perubahan".
            state, why = "WORKING", "teks pane berubah di antara dua sampel"
        elif not interval:
            state, why = "UNKNOWN", "dua pembacaan tanpa jeda: 'tidak berubah' bukan bukti"
        elif epoch is None:
            state, why = "UNKNOWN", "ledger tidak punya baris atas nama ini: diam atau nama lain"
        elif quiet_s < quiet_seconds:
            state, why = "WORKING", f"aksi terukur {quiet_s}s lalu"
        else:
            state, why = "AT_REST", f"dua sinyal setuju: aksi terakhir {quiet_s}s lalu, pane diam {sample_gap}s"
        out.append({**{k: r[k] for k in ("id", "title", "claimed_by", "resume_count")},
                    "state": state, "why": why,
                    "pane_readable": pane_readable, "action_log_readable": log_readable,
                    "last_action_epoch": epoch, "quiet_seconds": quiet_s,
                    "sample_gap": sample_gap,
                    "lease_alive": bool(r.get("lease_epoch")) and r["lease_epoch"] > now(),
                    "resume_budget_left": max(0, MAX_RESUME - int(r.get("resume_count") or 0))})
    return out


def autopilot(conn, engine, mode=None, minutes=None, budget=None, who="operator", reason=None):
    if mode is None:
        rows = run(conn, engine, "SELECT * FROM autopilot_state WHERE id=1")
        if not rows:
            return None
        row = rows[0]
        # `items_done` tidak pernah diincrement siapa pun, dan kedua permukaan membaca kolom itu
        # apa adanya: laporan "0 item malam ini" yang tetap nol walaupun ada pekerjaan selesai.
        # §12 melarang laporan ditulis tangan, jadi angka yang ditampilkan diturunkan dari ledger;
        # nilai tersimpan dilaporkan terpisah supaya perbedaan keduanya terlihat, bukan ditutupi.
        row["items_done_stored"] = row.get("items_done")
        row["items_done"] = items_done_in_window(conn, engine, int(row.get("on_epoch") or 0))
        return row
    ph = "%s" if engine == "POSTGRESQL" else "?"
    until = now() + int(minutes) * 60 if minutes else None
    if mode.upper() == "ON":
        run(conn, engine,
            f"UPDATE autopilot_state SET mode='ON', on_epoch={ph}, until_epoch={ph}, "
            f"item_budget={ph}, items_done=0, off_reason=NULL, changed_by={ph} WHERE id=1",
            (now(), until, budget or 12, who))
    else:
        # OFF juga menutup tangan. Level `resume` yang dibiarkan menggantung akan dibuka lagi
        # oleh ON berikutnya tanpa ada orang yang menaikkannya malam itu.
        run(conn, engine,
            f"UPDATE autopilot_state SET mode='OFF', off_reason={ph}, changed_by={ph}, "
            f"level='observe', level_until=NULL WHERE id=1",
            (reason or "dimatikan", who))
    return autopilot(conn, engine)


# --- F10.3: tangga tangan drainer -------------------------------------------
# `mode` (ON/OFF) menjawab SIAPA yang memegang mesin; `level` menjawab JAUH MANA tangan boleh
# bergerak. Keduanya tinggal di baris yang sama, supaya tidak ada dua laporan yang boleh
# berbeda (§12).


LEVELS = ("observe", "dispatch", "resume")
ARM_DEFAULT_MINUTES = 45
ARM_MAX_MINUTES = 240


def has_tty() -> bool:
    """Ada terminal pengendali di belakang pemanggil ini — diukur lewat /dev/tty, bukan ditebak.

    Terukur 2026-10-08: `systemd-run --user --wait` (jalur timer) dan Bash tool milik Qoder sama-sama
    gagal dengan ENXIO, sementara tiap pane `station-*` memegang pts sendiri. Jadi tidak ada jalur
    tak-berpengawas yang bisa mempersenjatai dirinya sendiri. Yang TIDAK dijaga pagar ini: mengetik
    ke pane operator lewat send-keys — dan itu justru tangan yang sedang dijaga, jadi kontrak F10.8
    sudah melarangnya lebih dulu. Pagar ini membuat kelalaian tidak mungkin, bukan pembangkangan
    tidak mungkin; menyebutnya batas keamanan akan lebih berbahaya daripada tanpa pagar sama sekali.
    """
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
    except OSError:
        return False
    os.close(fd)
    return True


def drain_level(conn, engine):
    """Tingkat tangan sebagai turunan: apa yang berlaku MENIT INI, bukan apa yang pernah ditulis."""
    try:
        rows = run(conn, engine,
                   "SELECT level, level_until, changed_by FROM autopilot_state WHERE id=1")
    except Exception:  # noqa: BLE001
        return {"level": None, "requested": None, "readable": False, "expired": None,
                "seconds_left": 0, "changed_by": None, "allows_dispatch": False,
                "allows_resume": False,
                "why": "baris autopilot tidak terbaca — tertutup karena UNKNOWN, bukan karena observe"}
    row = rows[0] if rows else {}
    want = row.get("level") or "observe"
    until = row.get("level_until")
    effective, why = want, []
    if want not in LEVELS:
        effective = "observe"
        why.append(f"level '{want}' bukan kosakata — tangga terkecil yang dipakai")
    elif want != "observe":
        if until is None:
            effective = "observe"
            why.append(f"{want} ditulis tanpa batas waktu; sakelar yang tinggal ON karena lupa "
                       "bukan sakelar (F10.8 §2)")
        elif int(until) <= now():
            effective = "observe"
            why.append(f"{want} kedaluwarsa {now() - int(until)}s lalu")
    return {"level": effective, "requested": want, "readable": True,
            "expired": effective != want, "changed_by": row.get("changed_by"),
            "seconds_left": max(0, int(until) - now()) if (until and effective != "observe") else 0,
            "allows_dispatch": effective in ("dispatch", "resume"),
            "allows_resume": effective == "resume", "why": why}


def set_level(conn, engine, level, minutes=ARM_DEFAULT_MINUTES, who="operator", tty=_MEASURE):
    """Naikkan tangan — butuh terminal pengendali. Menurunkan tangan tidak pernah butuh apa pun.

    Asimetrinya intinya, bukan kenyamanan: invarian 6 bilang jalan mati tidak boleh bergantung pada
    kerja sama AI, jadi `observe` bisa ditulis dari mana saja — termasuk timer yang sedang berlari
    dan selftest yang tidak punya tty. Yang naik tangga-lah yang harus diketik orang.
    """
    if level not in LEVELS:
        return False, f"level '{level}' bukan kosakata: {'/'.join(LEVELS)}"
    if minutes < 1 or minutes > ARM_MAX_MINUTES:
        return False, f"menit harus 1..{ARM_MAX_MINUTES} (dapat {minutes}); tanpa batas waktu ditolak"
    tty = has_tty() if tty is _MEASURE else tty
    if level != "observe" and not tty:
        return False, (f"menaikkan tangan ke '{level}' butuh terminal pengendali — /dev/tty tidak "
                       "terbuka dari proses ini. Menurunkan (`disarm`) tidak butuh tty.")
    until = None if level == "observe" else now() + int(minutes) * 60
    ph = "%s" if engine == "POSTGRESQL" else "?"
    run(conn, engine,
        f"UPDATE autopilot_state SET level={ph}, level_until={ph}, changed_by={ph} WHERE id=1",
        (level, until, who))
    return True, json.dumps(drain_level(conn, engine), default=str)


def decide(conn, engine, question, options=None, weight="heavy"):
    """Catat satu pertanyaan. `options` boleh label polos atau objek dengan bukti + biaya.

    Bentuknya {label, evidence, cost} karena kontrak F10.1 menuntut resolver melihat bukti
    per pilihan, bukan hanya namanya: pertanyaan "A atau B" tanpa angka di belakangnya hanya
    memindahkan tebakan ke mesin lain.

    `weight='irreversible'` tidak pernah dikirim ke resolver — invarian 3 outranks the broker,
    dan gerbangnya ada di sini supaya pemanggil mana pun mendapatinya, bukan hanya yang ingat.
    """
    ph = "%s" if engine == "POSTGRESQL" else "?"
    state = "ESCALATED" if weight == "irreversible" else "OPEN"
    run(conn, engine,
        f"INSERT INTO decisions (question, options, weight, state, rung_used, note, created_epoch) "
        f"VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})",
        (question, json.dumps(normalise_options(options))[:4000], weight, state,
         "human-only" if state == "ESCALATED" else None,
         "invarian 3: pilihan yang tidak bisa dibatalkan tidak didelegasikan ke mesin mana pun"
         if state == "ESCALATED" else None,
         now()))
    return run(conn, engine, "SELECT * FROM decisions ORDER BY id DESC LIMIT 1")


# --- F10.1 decision broker ---------------------------------------------------

RESOLVER_ROLES = ("resolver", "resolver-alt", "resolver-local")


def normalise_options(options):
    """Label polos dan objek {label, evidence, cost} masuk ke satu bentuk yang sama."""
    out = []
    for o in (options or []):
        if isinstance(o, str):
            out.append({"label": o})
        elif isinstance(o, dict):
            out.append({k: v for k, v in o.items() if k in ("label", "evidence", "cost")})
    return out


def parse_options_arg(raw):
    """`--options` menerima label polos (A|B) dan JSON ([{"label":...,"evidence":...}])."""
    s = (raw or "").strip()
    if not s:
        return []
    if s.startswith("["):
        return json.loads(s)
    return [o for o in s.split("|") if o.strip()]


def resolver_rungs():
    """Tangga resolver dibaca dari config/slots.yaml pada saat dipakai, bukan dari ingatan.

    Kontrak §12 dan aturan 'never select a model from memory': berkas itu yang jadi kontrak,
    dan operator menyuntingnya dari Station. Yang dikembalikan adalah (role_id, registry_key) —
    nama model, kolam kuota dan jendelanya tetap milik config/engines.json, satu definisi.
    """
    import yaml
    slots = REPO / "config" / "slots.yaml"
    doc = yaml.safe_load(slots.read_text(encoding="utf-8")) or {}
    want = {r.get("role_id"): r.get("registry_key")
            for t in doc.get("teams", []) if t.get("id") == doc.get("active_team")
            for r in t.get("roles", [])}
    return [(role, want[role]) for role in RESOLVER_ROLES if want.get(role)]


def decision_prompt(question, options):
    """Pertanyaan yang berdiri sendiri: headless tidak bisa mencari berkasnya sendiri."""
    lines = [
        "Kamu adalah RESOLVER keputusan di workstation ini (kontrak §4). Sebuah pilihan level "
        "preferensi tidak boleh dilempar ke manusia, jadi kamu yang memilih.",
        "",
        f"PERTANYAAN: {question}",
        "",
        "PILIHAN (bukti dan biaya ikut ditulis; yang kosong berarti memang tidak diukur):",
    ]
    for i, o in enumerate(options, 1):
        lines.append(f"{i}. {o.get('label', '(tanpa label)')}")
        lines.append(f"   bukti: {o.get('evidence') or '(tidak ada bukti terukur yang dilampirkan)'}")
        lines.append(f"   biaya: {o.get('cost') or '(tidak diukur)'}")
    lines += [
        "",
        "Aturan yang mengikat kamu:",
        "- Pilih salah satu nomor. Kalau semua pilihan buruk, pilih yang paling sedikit "
        "merusak dan tulis alasannya; jangan menolak memilih.",
        "- Jangan menambah pilihan ketiga, jangan mengubah pertanyaan.",
        "- Keputusan yang tidak bisa dibatalkan (hapus data, push paksa, kirim uang, pasang "
        "perangkat) tidak pernah jadi tugasmu: jawab `PILIHAN: TIDAK` dan sebutkan kenapa.",
        "",
        "Balas PERSIS dengan tiga baris ini, tanpa apa pun sebelumnya:",
        "PILIHAN: <nomor>",
        "ALASAN: <satu atau dua kalimat>",
        "BUKTI: <baris atau angka yang kamu pakai>",
    ]
    return "\n".join(lines)


def answer_tail(text, prompt):
    """Buang gema prompt sebelum jawabannya dibaca.

    Pane menampilkan kembali apa yang diketik ke dalamnya, jadi baris contoh format
    (`PILIHAN: <nomor>`, `ALASAN: <satu atau dua kalimat>`) dan kalimat aturan
    `jawab PILIHAN: TIDAK` ikut berada di layar SEBELUM jawaban yang sebenarnya. Parser yang
    mencari dari awal akan membaca instruksinya sendiri sebagai jawaban — dan yang paling
    berbahaya bukan nomor yang salah, melainkan sebuah penolakan yang tidak pernah diucapkan
    siapa pun. Yang dipakai di sini adalah teks setelah kemunculan TERAKHIR baris terakhir
    prompt: batas antara apa yang kita kirim dan apa yang mereka balas.
    """
    lines = [l.strip() for l in (prompt or "").splitlines() if l.strip()]
    if not lines or not text:
        return text or ""
    marker = lines[-1]
    i = text.rfind(marker)
    return text[i + len(marker):] if i >= 0 else text


def parse_choice(text, count):
    """Ambil nomor pilihan dari jawaban. Tidak ada baris PILIHAN = tidak ada keputusan."""
    if not text:
        return None, "jawaban kosong"
    m = re.search(r"PILIHAN\s*[:=]\s*(?:nomor\s*)?(\d+)", text, re.I)
    if not m:
        if re.search(r"PILIHAN\s*[:=]\s*TIDAK", text, re.I):
            return None, "resolver menolak memilih (PILIHAN: TIDAK)"
        return None, "tidak ada baris 'PILIHAN: <nomor>' — jawaban tidak bisa dibaca sebagai keputusan"
    idx = int(m.group(1))
    if not 1 <= idx <= count:
        return None, f"nomor {idx} di luar 1..{count}"
    reason = re.search(r"ALASAN\s*[:=]\s*(.+)", text, re.I)
    return idx, (reason.group(1).strip() if reason else "(tanpa alasan)")


def broker(conn, engine, decision_id, timeout=240):
    """Kiratkan satu pertanyaan OPEN ke tangga resolver, catat yang menjawab.

    Tiga hal yang membuat fungsi ini ada, semuanya hasil pengukuran, bukan selera:

    1. Model dipilih dari config pada saat dipakai, dan setiap anak panah dispatch adalah
       `cross_verify.run_engine` — registry, pagar kuota, dan pembacaan pane sudah milik di
       sana. Menyalinnya ke sini menghasilkan dua pagar yang boleh berbeda.
    2. Pane diperiksa sebelum diketik. `run_via_pane` mengirim lewat /api/cli/run, jadi
       mengetik ke pane yang sedang bekerja atau penuh teks belum terkirim akan menumpuk
       prompt ke dalam pekerjaan orang lain. Tingkatnya dilewati, bukan dipaksakan.
    3. Jawaban dibaca walau statusnya TIMEOUT. Satu dispatch gemini terukur TIMEOUT oleh
       harness padahal pane sudah menjawab; broker yang hanya percaya pipa akan membuang
       keputusan yang nyata-nyata sudah ada.
    4. Gema prompt dibuang sebelum parsing (`answer_tail`). Yang diketemukan saat uji pertama
       malam ini: ALASAN terbaca dari baris contoh di instruksi sendiri, dan teks aturan
       memuat `PILIHAN: TIDAK` — pane yang hanya menggemakan prompt akan tercatat sebagai
       resolver yang menolak memilih. Kesimpulan palsu dari kalimat kita sendiri.
    """
    import cross_verify as cv
    ph = "%s" if engine == "POSTGRESQL" else "?"
    rows = run(conn, engine, f"SELECT * FROM decisions WHERE id={ph}", (decision_id,))
    if not rows:
        return False, f"decision {decision_id} tidak ada"
    d = rows[0]
    if d["state"] != "OPEN":
        return False, f"decision {decision_id} sudah {d['state']} — broker tidak menulis ulang kesimpulan"
    opts = normalise_options(json.loads(d["options"] or "[]"))
    if not opts:
        return False, "pertanyaan tanpa pilihan — broker bukan tempat menebak apa opsinya"
    prompt = decision_prompt(d["question"], opts)

    rungs = resolver_rungs()
    if not rungs:
        return False, ("tidak satu pun role resolver ada di config/slots.yaml — pertanyaan "
                       "dibiarkan OPEN, bukan dijawab oleh mesin yang dipilih di tempat")
    registry = cv.load_registry()
    attempts = []
    for role_id, key in rungs:
        spec = registry.get(key)
        if not spec:
            attempts.append(f"{role_id}->{key}: tidak ada di config/engines.json")
            continue
        present, why = cv.engine_present(spec)
        if not present:
            attempts.append(f"{role_id}->{key}: UNAVAILABLE — {why or 'tidak hadir di mesin ini'}")
            continue
        if spec.get("kind") == "pane":
            tab = spec.get("tab", "antigravity")
            screen = cv.capture_pane(tab)
            if screen is None or not cv.pane_idle(screen):
                attempts.append(f"{role_id}->{key}: pane '{tab}' sedang bekerja atau ada teks "
                                "belum terkirim — prompt tidak diketik ke dalamnya")
                continue
        status, out = cv.run_engine(spec, prompt, timeout)
        idx, detail = parse_choice(answer_tail(out, prompt), len(opts))
        if idx:
            chosen = opts[idx - 1].get("label", f"pilihan {idx}")
            answer = (f"PILIHAN {idx}: {chosen}\nALASAN: {detail}\n\n"
                      f"--- jawaban mentah ({status}) ---\n{(out or '')[:2500]}")
            ok, msg = resolve_decision(conn, engine, decision_id, role_id, key,
                                       spec.get("model", ""), answer, "RESOLVED",
                                       note=f"{status}; {len(attempts)} tingkat dicoba sebelum ini")
            return ok, msg
        attempts.append(f"{role_id}->{key}: {status} — {detail}")

    note = "semua tingkat habis: " + " | ".join(attempts)
    ok, msg = resolve_decision(conn, engine, decision_id, None, None, None, None,
                               "ESCALATED", note[:3000])
    return ok, f"{msg} — {note}"



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
             "= UNKNOWN", "build", ["f108"],
             "python3 tools/work_order.py selftest && python3 tools/work_order.py heartbeat"),
    ("f103", "F10.3 drainer systemd timer dengan circuit breaker + batas kuota per malam", "build",
     ["f84", "f108", "f101"],
     # Gerbang ini pernah `systemctl --user list-timers irsofka-autopilot.timer`, dan diukur
     # 2026-10-08 02:58: perintah itu keluar 0 dengan tabel KOSONG untuk unit yang tidak ada sama
     # sekali. Gerbang yang selalu benar adalah undangan bagi drainer untuk menandai itemnya sendiri
     # COMPLETED tanpa bukti — bentuk kegagalan yang justru dilarang §12. Bentuk sekarang hanya
     # benar kalau berkasnya terpasang DAN timer-nya di-enable; hari ini keduanya belum, dan itu
     # memang sisanya: seorang yang bangun yang menyalakannya.
     "test -f ~/.config/systemd/user/irsofka-autopilot.timer && "
     "systemctl --user is-enabled irsofka-autopilot.timer"),
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

        # --- F10.7 heartbeat: dua sinyal harus setuju sebelum ada yang boleh disebut diam ---
        # capture/busy disuntik: tes tidak pernah membaca pane operator yang sedang hidup.
        hb_id = seed(conn, "SQLITE", [("kHB", "HB", "build", None, "true")])[0][0]
        claim(conn, "SQLITE", hb_id, "qoder")

        GAP = 0.02   # jeda yang benar-benar diambil; produksi dilantai oleh MIN_SAMPLE_GAP

        def still(_tab):
            return "prompt kosong"

        def shifting(_tab):
            shifting.n += 1
            return f"frame {shifting.n}"

        shifting.n = 0

        def explode(_tab):
            raise RuntimeError("tmux hilang")

        calm = lambda _screen: False          # noqa: E731
        spinner = lambda _screen: True        # noqa: E731

        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=lambda _t: None, busy=calm)
        check("pane tidak terbaca = UNKNOWN, bukan AT_REST",
              rows and rows[0]["state"] == "UNKNOWN" and rows[0]["pane_readable"] is False,
              str(rows and rows[0]))
        check("sinyal database yang tidak terbaca ikut menjadi UNKNOWN",
              rows and rows[0]["action_log_readable"] is False, str(rows and rows[0]))
        check("satu sinyal hilang cukup untuk menolak kesimpulan",
              rows and rows[0]["state"] == "UNKNOWN" and "AT_REST" not in str(rows), str(rows))
        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=explode, busy=calm)
        check("capture yang melempar bukan crash dan bukan diam — UNKNOWN",
              rows[0]["state"] == "UNKNOWN" and rows[0]["pane_readable"] is False, str(rows[0]))
        check("stalled() tidak mengarang kandidat dari tabel yang tak terbaca",
              stalled(conn, "SQLITE", older_than=0) == [], str(stalled(conn, "SQLITE", 0)))

        conn.execute("CREATE TABLE action_log (engine TEXT, ts TEXT)")
        conn.commit()
        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=still, busy=calm)
        check("tabel terbaca tapi kosong atas nama pemegang = UNKNOWN, bukan AT_REST",
              rows[0]["state"] == "UNKNOWN" and rows[0]["last_action_epoch"] is None
              and rows[0]["action_log_readable"] is True, str(rows[0]))

        conn.execute("INSERT INTO action_log (engine, ts) VALUES ('qoder', 'bukan-tanggal')")
        conn.commit()
        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=still, busy=calm)
        check("tanggal yang tak bisa dibaca = sinyal mati, bukan sinyal kosong",
              rows[0]["state"] == "UNKNOWN" and rows[0]["action_log_readable"] is False,
              str(rows[0]))

        conn.execute("DELETE FROM action_log")
        conn.execute("INSERT INTO action_log (engine, ts) VALUES ('qoder', datetime('now','-1 hour'))")
        conn.commit()
        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=still, busy=calm)
        check("teks UTC dibaca sebagai UTC: umur aksi ≈ 3600s",
              3540 <= (rows[0]["quiet_seconds"] or 0) <= 3660, str(rows[0]["quiet_seconds"]))
        check("dua sinyal setuju = AT_REST", rows[0]["state"] == "AT_REST", str(rows[0]))
        check("lease dan sisa budget resume ikut dilaporkan",
              rows[0]["lease_alive"] is True and rows[0]["resume_budget_left"] == MAX_RESUME,
              str(rows[0]))
        rows = heartbeat(conn, "SQLITE", 180, 0, capture=still, busy=calm)
        check("dua pembacaan tanpa jeda tidak boleh menyimpulkan diam",
              rows[0]["state"] == "UNKNOWN" and "jeda" in rows[0]["why"], str(rows[0]))

        conn.execute("INSERT INTO action_log (engine, ts) VALUES ('qoder', datetime('now'))")
        conn.commit()
        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=still, busy=calm)
        check("satu aksi terukur membatalkan kesimpulan diam",
              rows[0]["state"] == "WORKING" and "aksi terukur" in rows[0]["why"], str(rows[0]))

        rows = heartbeat(conn, "SQLITE", 180, GAP, capture=shifting, busy=calm)
        check("teks pane yang berubah = WORKING walau database diam",
              rows[0]["state"] == "WORKING" and "berubah" in rows[0]["why"], str(rows[0]))
        rows = heartbeat(conn, "SQLITE", 180, 0, capture=shifting, busy=calm)
        check("perubahan tetap bukti walau tanpa jeda; yang butuh jeda hanya ketiadaan perubahan",
              rows[0]["state"] == "WORKING", str(rows[0]))

        rows = heartbeat(conn, "SQLITE", 0, GAP, capture=still, busy=spinner)
        check("spinner lebih dipercaya daripada dua sinyal diam",
              rows[0]["state"] == "WORKING" and "spinner" in rows[0]["why"], str(rows[0]))

        finish(conn, "SQLITE", hb_id, "COMPLETED", evidence="selftest: heartbeat dua sinyal")
        check("tanpa klaim terbuka heartbeat tidak mengarang baris",
              heartbeat(conn, "SQLITE", 180, 0, capture=still, busy=calm) == [], "ada baris")
        # Invarian yang tidak boleh lulus tanpa diperiksa: jalur baca ini hanya indera. Cara
        # tercepat menjebolnya adalah orang berikutnya menambah "sedikit" pengiriman di ujung yang
        # sama — jadi yang diperiksa bukan hanya heartbeat(), tapi setiap fungsi yang ia panggil.
        seen = ""
        for fn in (heartbeat, _pane_text, _last_action, stalled):
            seen += inspect.getsource(fn)
        check("jalur baca tidak punya tangan: nol jalur ketik/spawn di heartbeat dan setiap bagiannya",
              not any(w in seen for w in ("send-keys", "send_to_terminal", "ydotool",
                                          "subprocess", "run_grouped", "run_bounded")),
              "ada jalur pengiriman di jalur baca")
        # Klaimnya harus sebatas yang benar: membaca pane memang memakai subprocess, tapi di
        # cross_verify.capture_pane — satu-satunya nama yang boleh dipinjam heartbeat.
        borrowed = [n for n in ("capture_pane", "pane_is_busy", "run_engine", "engine_present",
                                "send_keys") if f"cv.{n}" in inspect.getsource(heartbeat)]
        check("yang dipinjam dari cross_verify hanya dua pembacaan, bukan pengiriman",
              borrowed == ["capture_pane", "pane_is_busy"], str(borrowed))

        ap = autopilot(conn, "SQLITE")
        check("autopilot default OFF", ap and ap["mode"] == "OFF", str(ap))
        ap = autopilot(conn, "SQLITE", "ON", minutes=30, budget=4)
        check("ON menyimpan budget dan expiry", ap["mode"] == "ON" and ap["item_budget"] == 4
              and ap["until_epoch"] > now(), str(ap))
        ap = autopilot(conn, "SQLITE", "OFF", reason="selftest selesai")
        check("OFF menyimpan alasan", ap["mode"] == "OFF" and "selftest" in (ap["off_reason"] or ""))

        # --- F10.3: tangga tangan, dan pagar yang membuatnya tak bisa dinaikkan sendiri ---
        lvl = drain_level(conn, "SQLITE")
        check("tangga lahir tertutup: observe, nol izin dispatch dan nol izin resume",
              lvl["level"] == "observe" and not lvl["allows_dispatch"]
              and not lvl["allows_resume"], str(lvl))
        check("turun-naik tangan ditulis oleh `arm`, tapi default tidak pernah menulis apa pun",
              lvl["readable"] is True and lvl["level"] == "observe", str(lvl))
        ok, msg = set_level(conn, "SQLITE", "resume", 30, "selftest", tty=False)
        check("naik tangga tanpa terminal pengendali ditolak", not ok and "/dev/tty" in msg, msg)
        check("penolakan tidak mengubah keadaan", drain_level(conn, "SQLITE")["level"] == "observe")
        ok, msg = set_level(conn, "SQLITE", "observe", 30, "selftest", tty=False)
        check("menurunkan tangan tidak butuh tty — jalan mati tidak minta izin", ok, msg)
        ok, msg = set_level(conn, "SQLITE", "shoot", 30, "selftest", tty=True)
        check("kata di luar kosakata ditolak", not ok and "kosakata" in msg, msg)
        ok, msg = set_level(conn, "SQLITE", "dispatch", 0, "selftest", tty=True)
        check("naik tangga tanpa batas waktu ditolak", not ok and "batas waktu" in msg, msg)
        ok, msg = set_level(conn, "SQLITE", "dispatch", ARM_MAX_MINUTES + 1, "selftest", tty=True)
        check("batas menit atas ditegakkan, bukan dipotong diam-diam",
              not ok and str(ARM_MAX_MINUTES) in msg, msg)
        ok, msg = set_level(conn, "SQLITE", "dispatch", 30, "selftest", tty=True)
        lvl = drain_level(conn, "SQLITE")
        check("dispatch boleh mulai engine tapi tidak boleh mengetik",
              ok and lvl["level"] == "dispatch" and lvl["allows_dispatch"]
              and not lvl["allows_resume"], str(lvl))
        check("sisa menit dilaporkan dalam detik, bukan hanya statusnya",
              1770 <= lvl["seconds_left"] <= 1800, str(lvl))
        ok, msg = set_level(conn, "SQLITE", "resume", 30, "selftest", tty=True)
        check("resume membuka tangan", ok and drain_level(conn, "SQLITE")["allows_resume"], msg)
        conn.execute("UPDATE autopilot_state SET level_until=? WHERE id=1", (now() - 5,))
        lvl = drain_level(conn, "SQLITE")
        check("kedaluwarsa menutup tangan tanpa ada yang mengetik disarm",
              lvl["level"] == "observe" and lvl["expired"] and not lvl["allows_resume"], str(lvl))
        check("alasan penutupan ikut dilaporkan, tidak hilang bersama tutupnya",
              "kedaluwarsa" in " ".join(lvl["why"]), str(lvl["why"]))
        conn.execute("UPDATE autopilot_state SET level='teleport', level_until=? WHERE id=1",
                     (now() + 600,))
        lvl = drain_level(conn, "SQLITE")
        check("level asing diturunkan ke tangga terkecil, bukan dipercaya",
              lvl["level"] == "observe" and lvl["requested"] == "teleport", str(lvl))
        conn.execute("UPDATE autopilot_state SET level='resume', level_until=NULL WHERE id=1")
        lvl = drain_level(conn, "SQLITE")
        check("resume tanpa batas waktu terbaca tertutup: lupa bukan izin",
              lvl["level"] == "observe" and not lvl["allows_resume"], str(lvl))
        # OFF pada sakelar ikut menutup tangan; kalau tidak, ON berikutnya menemukan level yang
        # diangkat orang lain pada malam yang berbeda.
        set_level(conn, "SQLITE", "resume", 30, "selftest", tty=True)
        ap = autopilot(conn, "SQLITE", "OFF", reason="selftest: pagar OFF menutup tangan")
        check("OFF pada sakelar ikut menutup tangan",
              drain_level(conn, "SQLITE")["level"] == "observe" and ap["mode"] == "OFF")
        check("has_tty adalah ukuran, bukan dugaan", isinstance(has_tty(), bool), str(has_tty()))

        # --- F10.3: anggaran sentuhan ditegakkan di tempat tulisannya terjadi, bukan di timer.
        # Presedennya circuit breaker di `claim()`: manusia yang mengetik perintah yang sama
        # menabrak angka yang sama. Selama batasnya hidup di pemanggil, pemanggil berikutnya
        # boleh lupa membawa batasnya.
        conn.execute("INSERT INTO quest_tasks (title, status) VALUES ('nudge-target','PENDING')")
        conn.commit()
        nid = run(conn, "SQLITE", "SELECT id FROM quest_tasks WHERE title='nudge-target'")[0]["id"]
        for k in range(MAX_RESUME):
            ok, msg = nudge(conn, "SQLITE", nid)
            check(f"sentuhan {k + 1} dari {MAX_RESUME} diizinkan dan terbaca kembali",
                  ok and int(get_item(conn, "SQLITE", nid)["resume_count"] or 0) == k + 1, msg)
        ok, msg = nudge(conn, "SQLITE", nid)
        check("sentuhan berikutnya ditolak: item ini macet, bukan tertidur",
              not ok and "habis" in msg and str(MAX_RESUME) in msg, msg)
        check("penolakan tidak menaikkan angka diam-diam",
              int(get_item(conn, "SQLITE", nid)["resume_count"] or 0) == MAX_RESUME,
              str(get_item(conn, "SQLITE", nid)["resume_count"]))
        check("nudge hanya menghitung — status item tidak ikut ditulis",
              get_item(conn, "SQLITE", nid)["status"] == "PENDING",
              str(get_item(conn, "SQLITE", nid)["status"]))
        ok, msg = nudge(conn, "SQLITE", 999999)
        check("item yang tidak ada ditolak, tidak dibuatkan angka", not ok and "tidak ada" in msg, msg)

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

        # --- F10.1 decision broker: yang diuji adalah gerbangnya, BUKAN dispatch-nya.
        # Satu panggilan run_engine di dalam selftest akan membakar kuota asli dan mengetik
        # ke pane orang, jadi yang diverifikasi di sini adalah jalur yang berhenti sendiri.
        irr = decide(conn, "SQLITE", "Hapus seluruh tabel?", ["ya", "tidak"], weight="irreversible")
        check("pilihan tak bisa dibatalkan tidak pernah dikirim ke resolver",
              irr[0]["state"] == "ESCALATED" and irr[0]["rung_used"] == "human-only",
              f"{irr[0]['state']}/{irr[0]['rung_used']}")
        ok, msg = broker(conn, "SQLITE", irr[0]["id"])
        check("broker menolak menulis ulang baris yang sudah selesai",
              not ok and "kesimpulan" in msg, msg)
        empty = decide(conn, "SQLITE", "A atau B?", [], weight="light")
        ok, msg = broker(conn, "SQLITE", empty[0]["id"])
        check("pertanyaan tanpa pilihan ditolak, tidak ditebak", not ok and "pilihan" in msg, msg)

        # Anak tangga yang tidak bisa dibaca harus dilewati, dan kalau semuanya habis
        # pertanyaannya mendarat ESCALATED — BUKAN dijawab oleh mesin lain yang kebetulan ada.
        # Kunci palsu dipilih sengaja: ia gagal sebelum dispatch, jadi tes ini tidak bisa
        # membakar kuota maupun mengetik ke pane siapa pun.
        orig_rungs = globals()["resolver_rungs"]
        globals()["resolver_rungs"] = lambda: [("resolver", "mesin-yang-tidak-ada")]
        try:
            un = decide(conn, "SQLITE", "A atau B?", ["A", "B"], weight="light")
            ok, msg = broker(conn, "SQLITE", un[0]["id"])
            ur = run(conn, "SQLITE", "SELECT state, model_id, note FROM decisions WHERE id=?",
                     (un[0]["id"],))[0]
            check("resolver tak terbaca -> ESCALATED tanpa model penjawab",
                  ur["state"] == "ESCALATED" and not ur["model_id"]
                  and "engines.json" in (ur["note"] or ""), str(ur))
        finally:
            globals()["resolver_rungs"] = orig_rungs

        rungs = resolver_rungs()
        check("tangga resolver dibaca dari config, urut seperti di slots.yaml",
              [r for r, _k in rungs] == list(RESOLVER_ROLES) and all(k for _r, k in rungs),
              str(rungs))
        import cross_verify as _cv
        reg = _cv.load_registry()
        check("setiap anak tangga menunjuk registry key yang benar-benar ada",
              all(k in reg for _r, k in rungs), str([k for _r, k in rungs if k not in reg]))

        p = decision_prompt("A atau B?", [{"label": "A", "evidence": "ukur 3ms", "cost": "1 GB"},
                                          {"label": "B"}])
        check("prompt membawa bukti dan biaya per pilihan",
              "ukur 3ms" in p and "1 GB" in p and "tidak diukur" in p)
        check("prompt memuat aturan menolak yang tak bisa dibatalkan", "PILIHAN: TIDAK" in p)
        check("prosa tanpa baris PILIHAN bukan keputusan",
              parse_choice("menurut saya dua-duanya bagus", 2)[0] is None)
        check("nomor di luar jangkauan ditolak", parse_choice("PILIHAN: 7", 2)[0] is None)
        check("penolakan eksplisit terbaca sebagai penolakan",
              "menolak" in (parse_choice("PILIHAN: TIDAK", 2)[1] or ""))

        # Regresi nyata dari uji pertama broker (decision 3, pane antigravity): teks yang
        # kembali adalah prompt + jawaban, dan parser membaca baris contoh di prompt sebagai
        # jawaban. Yang berbahaya bukan nomornya — yang berbahaya adalah penolakan palsu.
        pr = decision_prompt("A atau B?", [{"label": "A"}, {"label": "B"}])
        echoed = pr + "\n▸ Thought for 10s, 714 tokens\nPILIHAN: 2\nALASAN: lebih hemat\nBUKTI: 8x4=32\n"
        i2, d2 = parse_choice(answer_tail(echoed, pr), 2)
        check("gema prompt dibuang: nomor dan alasan datang dari jawaban, bukan instruksi",
              i2 == 2 and d2.startswith("lebih hemat"), f"{i2}/{d2}")
        i3, d3 = parse_choice(answer_tail(pr + "\n", pr), 2)
        check("pane yang hanya menggemakan prompt bukan keputusan, dan bukan penolakan",
              i3 is None and "TIDAK" not in (d3 or ""), str(d3))
        check("tanpa pemotongan, kalimat aturan kita sendiri terbaca sebagai penolakan",
              "menolak" in (parse_choice(pr, 2)[1] or ""), str(parse_choice(pr, 2)))
        stt_irr = status(conn, "SQLITE")
        check("baris ESCALATED muncul di antrean yang dibaca GUI",
              any(x["id"] == irr[0]["id"] for x in stt_irr["decisions_open"]),
              str([x["id"] for x in stt_irr["decisions_open"]]))
        # Tab Human Decide membaca kolom ini satu per satu. Kalau query di status() berhenti
        # menyertainya, halaman tidak error — ia cuma menampilkan baris kosong, dan yang
        # dilihat operator adalah pertanyaan tanpa alasan. Jadi kolomnya diassert.
        need = {"id", "question", "options", "weight", "state", "rung_used", "model_id", "note"}
        sample = next((x for x in stt_irr["decisions_open"] if x["state"] != "OPEN"), None)
        check("setiap kolom yang dibaca tab Human Decide ikut terkirim",
              sample is not None and need <= set(sample), str(sorted(set(sample or {}))))

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

        stt = status(conn, "SQLITE")
        check("status punya semua bagian yang dibaca GUI",
              all(k in stt for k in ("autopilot", "drain", "night", "limits", "per_status",
                                     "human_queue", "decisions_open", "stalled", "next")),
              str(sorted(stt)))
        check("laporan GUI turunan, bukan karangan: tree kotor berarti night.ok false",
              tree_dirty() is not True or not stt["night"]["ok"], str(stt["night"]))

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

        # --- anggaran item per jendela: diturunkan, dan jendelanya benar-benar dipakai ---
        # Jebakan yang diikat di sini terukur, bukan dicurigai: `strftime('%%s', ...)` menghasilkan
        # TEKS '%s' di SQLite (parameternya tidak diinterpolasi di sana), dan SQLite membandingkan
        # teks melawan integer dengan aturan "teks selalu lebih besar" — filter jendela yang begitu
        # menjadi benar untuk semua baris, selamanya, tanpa pernah menampilkan kesalahan apa pun.
        check("strftime('%%s') menghasilkan teks, bukan angka",
              run(conn, "SQLITE", "SELECT strftime('%%s', '2026-01-01 00:00:00') AS v")[0]["v"] == "%s",
              str(run(conn, "SQLITE", "SELECT strftime('%%s', '2026-01-01 00:00:00') AS v")))
        check("strftime yang BENAR pun mengembalikan teks — tanpa CAST jendela mana pun selalu benar",
              run(conn, "SQLITE",
                  "SELECT typeof(strftime('%s', '2026-01-01 00:00:00')) AS t")[0]["t"] == "text")
        conn.execute("INSERT INTO quest_tasks (title, status, completed_at) "
                     "VALUES ('selesai-lama', 'COMPLETED', datetime('now','-3 days'))")
        conn.commit()
        total = run(conn, "SQLITE",
                    "SELECT COUNT(*) AS n FROM quest_tasks WHERE status='COMPLETED'")[0]["n"]
        check("jendela di masa depan tidak menampung apa pun — pembuktian filternya hidup",
              items_done_in_window(conn, "SQLITE", now() + 3600) == 0,
              str(items_done_in_window(conn, "SQLITE", now() + 3600)))
        check("item yang selesai tiga hari lalu tidak dihitung untuk jendela yang baru dibuka",
              total > 0 and items_done_in_window(conn, "SQLITE", now()) < total,
              f"total={total} in_window={items_done_in_window(conn, 'SQLITE', now())}")
        check("tanpa jendela ON, tidak ada angka yang dikarang",
              items_done_in_window(conn, "SQLITE", 0) == 0)
        before = items_done_in_window(conn, "SQLITE", now() - 30)
        conn.execute("INSERT INTO quest_tasks (title, status, completed_at) "
                     "VALUES ('selesai-baru', 'COMPLETED', datetime('now'))")
        conn.commit()
        check("item yang baru selesai menambah hitungan satu, tidak lebih",
              items_done_in_window(conn, "SQLITE", now() - 30) == before + 1,
              f"before={before} after={items_done_in_window(conn, 'SQLITE', now() - 30)}")
        before_ap = autopilot(conn, "SQLITE")["items_done"]
        conn.execute("INSERT INTO quest_tasks (title, status, completed_at) "
                     "VALUES ('selesai-lagi', 'COMPLETED', datetime('now'))")
        conn.commit()
        ap = autopilot(conn, "SQLITE")
        check("angka yang dibaca kedua permukaan bergerak bersama ledger, bukan kolom mati",
              ap["items_done"] == before_ap + 1 and ap["items_done_stored"] == 0,
              f"before={before_ap} {ap}")

        # --- bentuk gerbang yang terbukti tidak membuktikan apa pun ---
        # Diukur 2026-10-08 02:58 di mesin ini: `systemctl --user list-timers irsofka-tidak-ada.timer`
        # keluar 0 dengan tabel kosong. Karena drainer menutup item atas angka keluar, gerbang model
        # ini akan menandai pekerjaannya sendiri COMPLETED tanpa melakukan apa pun — laporan yang
        # ditulis tangan tanpa mengaku menulis tangan (§12). Jadi bentuknya yang dilarang, dicek.
        vacuous = [title for _k, title, _p, _d, gate in ROADMAP_ITEMS
                   if "list-timers" in (gate or "")]
        check("tidak ada gerbang ROADMAP yang berupa list-timers telanjang", not vacuous, str(vacuous))
        no_gate = [title for _k, title, _p, _d, gate in ROADMAP_ITEMS if not (gate or "").strip()]
        check("setiap butir ROADMAP yang di-seed membawa gerbang", not no_gate, str(no_gate))
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
        try:
            lit = run(conn, engine,
                      f"SELECT COUNT(*) AS n FROM quest_tasks WHERE title LIKE '{mark}%'")
            check("LIKE dengan % literal dan tanpa parameter tidak meledak",
                  lit and int(lit[0]["n"]) >= 1, str(lit))
        except Exception as exc:  # noqa: BLE001
            check("LIKE dengan % literal dan tanpa parameter tidak meledak", False, repr(exc))
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
        # decide() yang baru menyebut kolom rung_used/note, dan status() yang baru menyebut
        # rung_used di SELECT. Keduanya sudah ada di SQLite fixture, jadi fixture tidak bisa
        # membuktikan PostgreSQL punya kolom yang sama — dan itu persis kelas bug yang sudah
        # dua kali lolos ke produksi dari modul ini.
        irr = decide(conn, engine, f"{mark} hapus tabel?", [{"label": "ya"}, {"label": "tidak"}],
                     weight="irreversible")
        check("decide() bentuk baru tertulis di backend produksi",
              irr and irr[0]["state"] == "ESCALATED" and irr[0]["rung_used"] == "human-only",
              str(irr[0] if irr else None))
        stt = status(conn, engine)
        check("status() membaca kolom baru decisions di produksi",
              any(x["id"] == irr[0]["id"] for x in stt["decisions_open"])
              and "rung_used" in (stt["decisions_open"][0] if stt["decisions_open"] else {}),
              str([x.get("id") for x in stt["decisions_open"]][:5]))
        # Kelas bug yang sama untuk F10.7: pertanyaan lama memakai `created_at` di jalur SQLite,
        # padahal kolomnya bernama `ts` di KEDUA backend (DDL milik session_ingestor.py). Nama yang
        # salah hanya meledak saat tabelnya benar-benar dibaca, jadi ia diperiksa di produksi.
        epoch, readable = _last_action(conn, engine, "qoder")
        check("aksi engine terbaca di backend produksi", readable, str(epoch))
        check("umur aksi berupa epoch bilangan, bukan teks",
              epoch is None or isinstance(epoch, int), f"{type(epoch).__name__}: {epoch}")
        # heartbeat produksi: hanya membaca (capture-pane + SELECT). Tidak ada satu byte pun yang
        # dikirim ke pane mana pun, jadi aman dijalankan sementara operator memegang terminalnya.
        beats = heartbeat(conn, engine, sample_gap=1)
        check("heartbeat atas klaim produksi mengembalikan keadaan tiga nilai",
              all(b["state"] in ("WORKING", "AT_REST", "UNKNOWN") for b in beats),
              str([(b["claimed_by"], b["state"]) for b in beats]))
        check("heartbeat menyebut pane yang memang terbaca",
              all(isinstance(b["pane_readable"], bool) for b in beats), str(beats[:1]))
        check("UNKNOWN tidak pernah dibuat dari pane yang terbaca dan log yang terbaca",
              all(b["state"] != "UNKNOWN" or not (b["pane_readable"] and b["action_log_readable"])
                  for b in beats),
              str([b for b in beats if b["state"] == "UNKNOWN"]))
        # F10.3 di backend produksi, dengan pengukuran NYATA: proses ini tidak punya terminal
        # pengendali, jadi naik tangga harus ditolak di sini. Semua cek di atas menyuntik tty=False
        # supaya bisa diuji; yang satu ini membuktikan suntikannya cocok dengan dunia.
        lvl = drain_level(conn, engine)
        check("drain_level terbaca dan kosakatanya tertutup rapi di produksi",
              lvl["readable"] is True and lvl["level"] in LEVELS
              and (not lvl["allows_resume"] or lvl["level"] == "resume"), str(lvl))
        ok, msg = set_level(conn, engine, "dispatch", 30, "selftest-live")
        check("proses tanpa tty ditolak NAIK tangga di produksi, tanpa menulis apa pun",
              not ok and "/dev/tty" in msg, msg)
        check("penolakan di produksi tidak mengubah level yang terbaca",
              drain_level(conn, engine)["level"] == lvl["level"], str(drain_level(conn, engine)))
        # `nudge()` adalah satu-satunya tempat anggaran sentuhan ditulis. Placeholder-nya berbeda
        # per backend (%s vs ?), dan kesalahan placeholder tidak bersuara di SQLite — ia cuma
        # menghasilkan tulisan ke baris yang salah. Jadi dihitung di backend nyata.
        run(conn, engine, f"INSERT INTO quest_tasks (title, status) VALUES ({ph}, 'PENDING')",
            (f"{mark}-nudge",))
        nud = run(conn, engine, f"SELECT id FROM quest_tasks WHERE title={ph}",
                  (f"{mark}-nudge",))
        nudge_id = int(nud[0]["id"])
        for k in range(MAX_RESUME):
            ok, msg = nudge(conn, engine, nudge_id)
            check(f"produksi: sentuhan {k + 1}/{MAX_RESUME} diterima dan terbaca kembali",
                  ok and int((get_item(conn, engine, nudge_id) or {}).get("resume_count") or 0) == k + 1,
                  msg)
        ok, msg = nudge(conn, engine, nudge_id)
        check("produksi: sentuhan ke-4 ditolak, dan tidak ada yang mengetik karenanya",
              not ok and "habis" in msg, msg)
        # Anggaran item per jendela di backend NYATA. Bentuk SQLite-nya salah dua kali dan kedua
        # salah itu tidak bersuara, jadi asersi yang sama dijalankan di sini dengan dua baris
        # bertanda yang dihapus lagi: satu selesai sekarang, satu selesai tiga hari lalu.
        old_stamp = (f"{mark}-lama", )
        run(conn, engine,
            f"INSERT INTO quest_tasks (title, status, completed_at) VALUES ({ph}, 'COMPLETED', "
            f"CURRENT_TIMESTAMP - INTERVAL '3 days')", old_stamp)
        run(conn, engine,
            f"INSERT INTO quest_tasks (title, status, completed_at) VALUES ({ph}, 'COMPLETED', "
            "CURRENT_TIMESTAMP)", (f"{mark}-baru",))
        try:
            future = items_done_in_window(conn, engine, now() + 3600)
            wide = items_done_in_window(conn, engine, now() - 86400 * 7)
            tight = items_done_in_window(conn, engine, now() - 30)
            check("produksi: jendela masa depan kosong — filter tipe benar di backend ini",
                  future == 0, str(future))
            check("produksi: baris tiga hari lalu hanya masuk jendela yang lebar",
                  wide - tight >= 1, f"wide={wide} tight={tight} selisih={wide - tight}")
            check("produksi: baris yang baru selesai masuk jendela 30 detik",
                  tight >= 1, str(tight))
            if engine == "POSTGRESQL":
                # Asumsi zona waktu diuji, bukan diasumsikan. `completed_at` tanpa zona dibaca
                # EXTRACT sebagai UTC, dan selisihnya persis offset Asia/Jakarta: +25 200 detik.
                # Baris yang baru ditulis ini epoch-nya diketahui dalam sedetik, jadi bentuk
                # `::timestamptz` harus cocok — kalau tidak, seluruh jendela malam ini salah.
                seen = run(conn, engine,
                           "SELECT EXTRACT(EPOCH FROM completed_at::timestamptz)::bigint AS e "
                           f"FROM quest_tasks WHERE title={ph}", (f"{mark}-baru",))
                check("produksi: epoch yang dibaca PG sama dengan jam Python (selisih < 5s)",
                      seen and abs(int(seen[0]["e"]) - now()) < 5,
                      f"{seen and int(seen[0]['e']) - now()}s")
            ap = autopilot(conn, engine)
            check("produksi: items_done yang dibaca GUI adalah turunan, dan nilai tersimpan "
                  "dipisahkan", isinstance(ap.get("items_done"), int)
                  and "items_done_stored" in ap, str({k: ap.get(k) for k in ("items_done", "items_done_stored")}))
        finally:
            for t in (f"{mark}-lama", f"{mark}-baru"):
                run(conn, engine, f"DELETE FROM quest_tasks WHERE title={ph}", (t,))
    finally:
        like = f"{mark}%"
        run(conn, engine, "DELETE FROM quest_tasks WHERE title LIKE "
            + ("%s" if engine == "POSTGRESQL" else "?"), (like,))
        run(conn, engine, "DELETE FROM decisions WHERE question LIKE "
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


# --- muka untuk GUI ---------------------------------------------------------


def status(conn, engine, older_than=180):
    """Satu amplop JSON untuk sakelar Autopilot dan tab Human Decide.

    Semua isinya turunan: baris database, angka pemutus, dan satu pembacaan git. Tidak ada
    satu pun field yang boleh ditulis tangan — §12 melarang laporan yang dikarang, dan muka
    GUI yang mengarang adalah cara tercepat membuat operator percaya angka yang salah.

    `decisions_open` adalah "belum selesai", bukan "berstatus OPEN": baris ESCALATED dan
    UNAVAILABLE juga muncul di sini, karena keduanya adalah pertanyaan yang mesin tidak bisa
    tutup sendiri dan artinya menunggu orang.
    """
    ap = autopilot(conn, engine) or {}
    per_status = run(conn, engine,
                     "SELECT status, COUNT(*) AS n FROM quest_tasks GROUP BY status ORDER BY 2 DESC")
    human = run(conn, engine,
                "SELECT id, title, status, escalation_reason, phase FROM quest_tasks "
                "WHERE status='HUMAN' ORDER BY id")
    dec = run(conn, engine,
              "SELECT id, question, options, weight, state, rung_used, engine_key, model_id, "
              "note, created_epoch FROM decisions WHERE state <> 'RESOLVED' ORDER BY id")
    nxt = next_item(conn, engine)
    return {
        "engine": engine,
        "autopilot": ap,
        "drain": drain_level(conn, engine),
        "night": night_state(conn, engine),
        "limits": {"max_failures": NIGHT_MAX_FAILURES, "max_items": NIGHT_MAX_ITEMS,
                   "lease_seconds": LEASE_SECONDS, "max_resume": MAX_RESUME,
                   "arm_default_minutes": ARM_DEFAULT_MINUTES, "arm_max_minutes": ARM_MAX_MINUTES,
                   "levels": list(LEVELS)},
        "per_status": per_status,
        "human_queue": human,
        "decisions_open": dec,
        "stalled": stalled(conn, engine, older_than),
        "next": nxt,
    }


# --- CLI --------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="Work order autopilot — klaim, lease, bukti, eskalasi")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ensure")
    p = sub.add_parser("seed"); p.add_argument("--roadmap", action="store_true")
    p = sub.add_parser("claim"); p.add_argument("id", type=int); p.add_argument("--by", required=True)
    p = sub.add_parser("lease"); p.add_argument("id", type=int)
    p = sub.add_parser("heartbeat")
    p.add_argument("--quiet", type=int, default=180,
                   help="berapa detik tanpa aksi terukur baru dianggap diam")
    p.add_argument("--gap", type=float, default=float(MIN_SAMPLE_GAP),
                   help=f"detik antara dua pembacaan pane (lantai {MIN_SAMPLE_GAP}s di produksi)")
    p = sub.add_parser("complete"); p.add_argument("id", type=int); p.add_argument("--evidence", default="")
    p = sub.add_parser("block"); p.add_argument("id", type=int); p.add_argument("--reason", required=True)
    p = sub.add_parser("human"); p.add_argument("id", type=int); p.add_argument("--reason", required=True)
    sub.add_parser("next")
    sub.add_parser("list")
    sub.add_parser("stalled")
    p = sub.add_parser("autopilot"); p.add_argument("mode", nargs="?", choices=["on", "off", "show"])
    p.add_argument("--minutes", type=int); p.add_argument("--budget", type=int)
    p.add_argument("--reason", default=None); p.add_argument("--by", default="operator")
    p = sub.add_parser("arm"); p.add_argument("level", choices=list(LEVELS))
    p.add_argument("--minutes", type=int, default=ARM_DEFAULT_MINUTES)
    p.add_argument("--by", default="operator")
    p = sub.add_parser("disarm"); p.add_argument("--by", default="operator")
    sub.add_parser("drain")
    p = sub.add_parser("decide"); p.add_argument("question"); p.add_argument("--options", default="")
    p.add_argument("--weight", default="heavy", choices=["heavy", "light", "irreversible"])
    p.add_argument("--route", action="store_true", help="langsung kiratkan ke tangga resolver")
    p.add_argument("--timeout", type=int, default=240)
    p = sub.add_parser("route"); p.add_argument("id", type=int)
    p.add_argument("--timeout", type=int, default=240)
    p.add_argument("--dry-run", action="store_true",
                   help="tunjukkan tangga + prompt tanpa mengirim apa pun")
    p = sub.add_parser("decisions"); p.add_argument("--limit", type=int, default=15)
    p = sub.add_parser("resolve"); p.add_argument("id", type=int)
    p.add_argument("--rung", required=True); p.add_argument("--engine-key", required=True)
    p.add_argument("--model", required=True); p.add_argument("--answer", required=True)
    p.add_argument("--state", default="RESOLVED",
                   choices=["RESOLVED", "ESCALATED", "UNAVAILABLE"])
    p.add_argument("--note", default=None)
    sub.add_parser("digest")
    sub.add_parser("status")
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
    elif a.cmd == "lease":
        it = renew_lease(conn, engine, a.id)
        print(json.dumps({"id": a.id, "lease_epoch": it and it.get("lease_epoch")}))
    elif a.cmd == "heartbeat":
        # Hanya indera. Tidak ada satu baris pun di bawah yang mengetik ke pane mana pun.
        print(json.dumps(heartbeat(conn, engine, a.quiet, a.gap), default=str, indent=2))
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
    elif a.cmd == "arm":
        ok, msg = set_level(conn, engine, a.level, a.minutes, a.by)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "disarm":
        # Jalan mati tidak pernah minta izin — invarian 6, dan karena itu disarm sengaja tidak
        # memeriksa tty: kalau pagar yang sama menutup jalur kembali, ia bukan jalan mati.
        ok, msg = set_level(conn, engine, "observe", ARM_DEFAULT_MINUTES, a.by)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "drain":
        print(json.dumps(drain_level(conn, engine), default=str, indent=2))
    elif a.cmd == "decide":
        rows = decide(conn, engine, a.question, parse_options_arg(a.options), a.weight)
        row = rows[0]
        print(json.dumps(row, default=str, indent=2))
        if a.weight == "irreversible":
            print("-> ESCALATED: invarian 3, tidak dikirim ke resolver mana pun")
        elif a.route:
            ok, msg = broker(conn, engine, row["id"], a.timeout)
            print(msg)
            return 0 if ok else 1
    elif a.cmd == "route":
        if a.dry_run:
            import cross_verify as cv
            rows = run(conn, engine, "SELECT * FROM decisions WHERE id=%s"
                       if engine == "POSTGRESQL" else "SELECT * FROM decisions WHERE id=?",
                       (a.id,))
            if not rows:
                print(f"decision {a.id} tidak ada")
                return 1
            d = rows[0]
            registry = cv.load_registry()
            opts = normalise_options(json.loads(d["options"] or "[]"))
            print(json.dumps({
                "state": d["state"], "weight": d["weight"],
                "rungs": [{"role": r, "registry_key": k,
                           "model": registry.get(k, {}).get("model"),
                           "kind": registry.get(k, {}).get("kind")}
                          for r, k in resolver_rungs()],
                "prompt": decision_prompt(d["question"], opts),
            }, indent=2, ensure_ascii=False))
            return 0
        ok, msg = broker(conn, engine, a.id, a.timeout)
        print(msg)
        return 0 if ok else 1
    elif a.cmd == "decisions":
        rows = run(conn, engine, "SELECT id, state, weight, rung_used, model_id, question, answer, "
                                 "note FROM decisions ORDER BY id DESC LIMIT "
                   + ("%s" if engine == "POSTGRESQL" else "?"), (a.limit,))
        if not rows:
            print("belum ada keputusan — antrean ini kosong artinya normal")
        for r in rows:
            who = r["model_id"] or ("MENUNGGU ORANG" if r["state"] != "RESOLVED" else "?")
            pick = ""
            if r["answer"]:
                first = r["answer"].splitlines()[0].strip()
                pick = first[:70]
            print(f"#{r['id']:<4} {r['state']:<10} {r['weight']:<12} "
                  f"{(r['rung_used'] or '-'):<14} {who:<28} {r['question'][:44]}")
            if pick:
                print(f"      ↳ {pick}")
            if r["state"] != "RESOLVED" and r["note"]:
                print(f"      ↳ {r['note'][:110]}")
        return 0
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
    elif a.cmd == "status":
        print(json.dumps(status(conn, engine), default=str, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
