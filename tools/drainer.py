"""Drainer F10.3 — satu tick deterministik tanpa LLM yang memilih pekerjaan berikutnya dari ledger.

Roadmap F10.3 menuntut sebuah systemd timer yang menjalankan skrip tetap: ambil item PENDING
berikutnya, jalankan `check_command`-nya sebagai gerbang, lalu baru memanggil engine. Yang
menahan modul ini sampai sekarang bukan kemalasan — tangannya. Timer yang bisa mengetik ke pane
milik orang lain adalah timer yang bisa menggunting sesi kerja operator, jadi F10.8 (sakelar) dan
F10.7 (heartbeat) harus selesai lebih dulu supaya timer tahu kapan wajib diam.

Tangga tangan (`work_order.drain_level`) adalah satu-satunya penentu seberapa jauh tick ini boleh
bergerak, dan modul ini tidak pernah menaikkannya: menaiki tangga butuh terminal pengendali milik
manusia (`/dev/tty`), jadi seorang AI yang ditinggal sendirian secara struktural tidak bisa
mempersenjatai dirinya sendiri. Menurunkan tangan tetap bisa dari mana saja — invarian 6 bilang
jalan mati tidak boleh bergantung pada kerja sama AI.

    observe   membaca dan mencatat; tidak menjalankan apa pun, tidak menulis ledger
    dispatch  + menjalankan gerbang shell (grup proses sendiri, timeout) dan menulis hasil Angka
                keluar yang terukur; + menyerahkan item baru ke engine yang benar-benar menganggur
    resume    + mengetik CONTINUE ke engine yang memegang klaim dan terbukti AT_REST oleh F10.7

Pembagian itu bukan kenyamanan. `AT_REST` adalah kesimpulan dua sinyal independent; mendefinisikan
ulang "istirahat" di dalam timer adalah cara mesin berakhir mengetik ke sesi yang sedang di tengah
kalimat (§12). Karena itu modul ini memakai `wo.heartbeat()` apa adanya dan dilarang keras
menyentuh pembacaan pane mana pun — cek sumber di dalam selftest menegaskannya.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import work_order as wo  # noqa: E402

REPO = Path.home() / ".ai-station"
LOG_DIR = REPO / "logs"

CHECK_TIMEOUT = 240      # detik; gerbang yang lebih lambat dari ini bukan "sedang bekerja", tapi macet
GATE_COOLDOWN = 1800     # detik; kegagalan barusan tidak ditanya ulang setiap tick
CLAIM_COOLDOWN = 300     # detik; klaim yang ditolak dicoba lagi setelah jeda pendek
HOLDER = "drainer"       # pemegang klaim tanpa pane — alasannya di `hand_new_work`

# Invarian 3: tindakan yang tidak bisa dibatalkan berada di luar jangkauan tick mana pun. Daftar
# ini dipakai untuk MENOLAK, bukan untuk menjalankan dengan hati-hati, jadi kelebihan tangkap hanya
# membuat sebuah item naik ke antrean manusia — kegagalan yang masih bisa diterima. Celah di sini
# adalah kehilangan pekerjaan seseorang, jadi daftar yang longgar adalah harga yang benar.
UNDOABLE = (
    "rm -rf", "rm -fr", "sudo rm", "drop table", "drop database", "drop schema", "truncate ",
    "delete from", "mkfs", "dd if=", "> /dev/sd", "push --force", "push -f", "reset --hard",
    "clean -fd", "clean -df", "checkout --", "rebase", "kill-server", "pkill", "shutdown",
    "reboot", "stop postgresql", "uninstall", "destroy", "format",
)

# Yang dikirim ke engine yang memegang klaim dan terbukti istirahat. Arahan di dalamnya adalah
# kontrak F10.1 yang ditulis ulang di setiap sentuhan: pilihan level preferensi tidak boleh
# dilempar ke operator — mesin yang menjawab atas namanya, dan menyebut model yang menjawab.
CONTINUE_PROMPT = (
    "Lanjut: item {id} masih WORKING atas nama {who}, dan dua sinyal independent menyimpulkan "
    "kamu beristirahat ({why}). Kerjakan sampai gerbangnya lolos: "
    "`python3 tools/work_order.py complete {id} --evidence \"<perintah yang benar-benar lolos>\"`. "
    "Selama Autopilot ON jangan meminta saya memilih: untuk keputusan level preferensi jalankan "
    "`python3 tools/work_order.py decide ...` lalu `route`, biar resolver terdaftar yang menjawab. "
    "Hanya keadaan genting (data hilang, tidak bisa dibatalkan, kuota habis) yang boleh menunggu "
    "saya; kalau terhalang, lewati dan ambil item berikutnya lewat `next`."
)

# Yang dikirim ke engine pengangguran untuk item yang baru saja diklaim drainer. Item ini belum
# pernah disentuh siapa pun, jadi isinya adalah perintah mulai, bukan teguran.
START_PROMPT = (
    "Tugas baru dari drainer (Autopilot ON): item {id} — {title}. "
    "Klaim dulu supaya engine lain tidak ganda mengerjakannya, tapi drainer memegang lease item "
    "ini sampai kamu mulai: perpanjang dengan `python3 tools/work_order.py lease {id}`. "
    "Gerbang penerimaannya: `{gate}`. Kerja, lalu tutup dengan bukti angka keluar, bukan dengan "
    "prosa. Kalau tersangkut keputusan preferensi, jangan tanya saya — `decide` + `route`."
)


# --- pembacaan ----------------------------------------------------------------
def read_gate(conn, engine):
    """Tiga pembacaan yang menentukan apa yang boleh dilakukan tick ini — semuanya turunan."""
    return {"level": wo.drain_level(conn, engine),
            "autopilot": wo.autopilot(conn, engine),
            "night": wo.night_state(conn, engine)}


def undoable_in(*texts):
    """Kata kerja mana yang tak bisa dibatalkan, atau None. Mengembalikan bukti, bukan bool."""
    blob = " ".join((t or "").lower() for t in texts)
    return next((verb for verb in UNDOABLE if verb in blob), None)


def open_claims(conn, engine, holder=None):
    """Klaim yang masih berstatus kerja, dibaca sebagai baris — bukan dari ingatan tick sebelumnya."""
    if holder is None:
        return wo.run(conn, engine,
                      "SELECT id, title, status, claimed_by, check_command, resume_count, "
                      "lease_epoch, cli_engine, due_epoch FROM quest_tasks "
                      "WHERE status IN ('WORKING','CLAIMED') ORDER BY id")
    return wo.run(conn, engine,
                  "SELECT id, title, status, claimed_by, check_command, resume_count, "
                  "lease_epoch, cli_engine, due_epoch FROM quest_tasks "
                  f"WHERE status IN ('WORKING','CLAIMED') AND claimed_by={wo_p(engine)} ORDER BY id",
                  (holder,))


def wo_p(engine):
    """Placeholder per backend. Dua backend adalah dua dialek; yang menulis SQL dua kali pasti
    akan salah satu di antaranya, jadi bentuknya disatukan di sini."""
    return "%s" if engine == "POSTGRESQL" else "?"


def next_ready(conn, engine):
    return wo.next_item(conn, engine, respect_due=True)


# --- tulisan ------------------------------------------------------------------
def cooldown(conn, engine, item_id, seconds):
    """Tandai `due_epoch`; antrean patuh pada kolom ini lewat `next_item(respect_due=True)`."""
    wo.run(conn, engine,
           f"UPDATE quest_tasks SET due_epoch={wo_p(engine)} WHERE id={wo_p(engine)}",
           (wo.now() + int(seconds), item_id))


def default_runner(cmd, timeout=CHECK_TIMEOUT):
    """Jalankan gerbang lewat call-worker yang berjanji mati bersama pemanggilnya (kontrak §4).

    Jalur subprocess biasa dengan timeout hanya membunuh anak langsung; satu panggilan yang
    menggantung pernah terbukti meninggalkan cucu yang tetap memegang RSS. `run_grouped` memulai
    perintah di sesi proses sendiri dan menyapu seluruh grupnya — termasuk pada jalur sukses.
    """
    import call_workers as cw
    return cw.run_grouped(["bash", "-lc", f"cd {REPO} && {cmd}"], timeout=timeout,
                          text=True, merge_stderr=True)


def default_sender(target, prompt):
    """Satu-satunya jalur tik ke pane: API daemon yang sudah ada.

    Modul ini tidak punya jalur kirim kedua — tidak ada tombol yang dikirim lewat mekanisme di luar
    API ini, karena tiga jalur kirim adalah tiga perilaku berbeda dan hanya satu yang bisa dihentikan
    sakelar. Balasan daemon adalah klaim daemon, BUKAN bukti: `/api/cli/run` mengembalikan
    "dispatched" walaupun sesi targetnya tidak ada, jadi yang dicatat di log adalah balasannya apa
    adanya, bukan "pasti sampai".
    """
    import cross_verify as cv
    return cv.daemon_post("/api/cli/run", {"prompt": prompt, "target_cli": target}, timeout=10)


# --- gerbang ------------------------------------------------------------------
def gate_item(conn, engine, item, runner=None, stage="after", emit=None):
    """Jalankan `check_command` satu item dan catat angkanya. `stage` menentukan artinya.

    `before` adalah baseline: item yang belum dikerjakan memang HARUS gagal di sini, jadi kegagalan
    tidak ditulis sebagai FAILED — itu akan meracuni ledger dengan angka yang tidak berarti dan,
    lebih parah, memicu circuit breaker atas pekerjaan yang belum dimulai. Kelulusan di sini berarti
    item sudah terpenuhi: ditutup dengan bukti terukur, tanpa memanggil engine mana pun.

    `after` adalah vonis: kelulusan menutup item, kegagalan menutupnya sebagai FAILED dan memberi
    jeda, karena mengulang gerbang yang sama setiap lima menit adalah cara kuota habis dalam sedetik.
    """
    runner = runner or default_runner
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    command = (item.get("check_command") or "").strip()
    item_id = int(item["id"])
    if not command:
        emit("GATE_NO_COMMAND", item=item_id, title=item.get("title"))
        return {"item": item_id, "exit": None, "why": "tanpa check_command"}

    verb = undoable_in(command, item.get("title"))
    if verb:
        # Invarian 3. Ditolak SEBELUM satu proses pun dibuat, dan item dinaikkan ke antrean manusia
        # supaya kepalanya tidak memacetkan antrean selamanya. Bukan PARKED: PARKED dihitung sebagai
        # kegagalan malam ini, padahal ini bukan kegagalan — ini penolakan yang benar.
        ok, msg = wo.finish(conn, engine, item_id, "HUMAN",
                            reason=f"invarian 3: gerbang memuat '{verb}' — tidak bisa dibatalkan")
        emit("REFUSED_UNDOABLE", item=item_id, verb=verb, escalated=bool(ok), why=msg)
        return {"item": item_id, "exit": None, "refused": verb}

    try:
        done = runner(command, CHECK_TIMEOUT)
        code = int(getattr(done, "returncode", -1))
        tail = (getattr(done, "stdout", "") or "")[-200:].strip()
    except Exception as exc:  # noqa: BLE001 — gerbang yang meledak adalah gerbang yang gagal
        code, tail = -1, f"{type(exc).__name__}: {exc}"

    if code == 0:
        wo.finish(conn, engine, item_id, "COMPLETED",
                  evidence=f"drainer gate ({stage}) exit 0: {command[:180]}")
        emit("GATE_PASSED", item=item_id, exit=code, stage=stage, title=item.get("title"))
    elif stage == "before":
        emit("GATE_BASELINE", item=item_id, exit=code, stage=stage, out=tail)
    else:
        wo.finish(conn, engine, item_id, "FAILED",
                  reason=f"drainer gate exit {code}: {command[:180]}")
        cooldown(conn, engine, item_id, GATE_COOLDOWN)
        emit("GATE_FAILED", item=item_id, exit=code, stage=stage, out=tail)
    return {"item": item_id, "exit": code}


def reconcile(conn, engine, runner=None, emit=None):
    """Vonis atas klaim drainer yang masih hidup: gerbang diulang, angka keluar yang bicara."""
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    for row in open_claims(conn, engine, holder=HOLDER):
        if int(row.get("lease_epoch") or 0) > wo.now():
            # Lease masih dipegang: pekerjaan sedang berjalan atau engine belum mulai. Vonis
            # dijatuhkan setelah lease habis — lebih awal dari itu artinya menyebut pekerjaan orang
            # gagal hanya karena ia belum selesai.
            wo.renew_lease(conn, engine, int(row["id"]))
            emit("HELD", item=int(row["id"]), lease_left=int(row["lease_epoch"]) - wo.now())
            continue
        gate_item(conn, engine, row, runner=runner, stage="after", emit=emit)


def hand_new_work(conn, engine, runner=None, sender=None, emit=None):
    """Serahkan satu item baru ke engine yang ditunjuk barisnya. Tidak pernah menebak tujuan.

    Mesin mana yang mengerjakan adalah milik kolom `cli_engine` pada baris itu — operator menyuntingnya
    dari Station, dan kontrak melarang memilih engine dari ingatan (§12). Baris tanpa tujuan ditolak,
    bukan diisi dengan favorit tick ini.

    Drainer mengklaim atas namanya sendiri supaya tidak ada dua klaim atas item yang sama malam itu;
    HOLDER tidak punya pane, jadi `heartbeat` selalu menjawab UNKNOWN untuknya dan jalur resume tidak
    akan pernah bisa mengetuknya — aman secara konstruksi, bukan karena ada yang ingat menjaga.
    """
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    item = next_ready(conn, engine)
    if not item:
        emit("QUEUE_EMPTY")
        return None
    full = wo.get_item(conn, engine, int(item["id"])) or item
    # Baseline dulu, sebelum klaim: item yang gerbangnya sudah lolos tidak pernah perlu dikerjakan.
    verdict = gate_item(conn, engine, full, runner=runner, stage="before", emit=emit)
    if verdict.get("refused") or verdict.get("exit") == 0:
        return verdict
    ok, msg = wo.claim(conn, engine, int(item["id"]), HOLDER)
    if not ok:
        emit("CLAIM_REFUSED", item=int(item["id"]), why=msg)
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        return None
    target = (full.get("cli_engine") or "").strip()
    if not target:
        # Tidak ada tujuan: klaim drainer ditarik kembali, bukan dibiarkan menggantung. Baris yang
        # statusnya WORKING tanpa pemegang adalah laporan bahwa ada yang mengerjakan padahal tidak
        # ada — jadi item ini dikembalikan persis seperti keadaannya sebelum tick ini menyentuhnya.
        wo.run(conn, engine,
               f"UPDATE quest_tasks SET status='PENDING', claimed_by=NULL, lease_epoch=0 "
               f"WHERE id={wo_p(engine)}", (int(item["id"]),))
        emit("NO_TARGET", item=int(item["id"]), why="kolom cli_engine kosong — memilih engine "
             "dari ingatan dilarang, jadi item ini menunggu orang mengisinya")
        return None
    prompt = START_PROMPT.format(id=item["id"], title=item["title"],
                                 gate=(full.get("check_command") or "tanpa gerbang"))
    try:
        reply = (sender or default_sender)(target, prompt)
        emit("WORK_DISPATCHED", item=int(item["id"]), target=target, reply=str(reply)[:200])
    except Exception as exc:  # noqa: BLE001 — kegagalan kirim dicatat, tidak dikarang ulang
        emit("SEND_FAILED", item=int(item["id"]), target=target,
             why=f"{type(exc).__name__}: {exc}")
    return {"item": int(item["id"]), "target": target}


def nudge_resting(conn, engine, beats=None, sender=None, emit=None):
    """CONTINUE untuk klaim yang terbukti istirahat — anggaran dihitung oleh `wo.nudge`."""
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    sender = sender or default_sender
    if beats is None:
        beats = wo.heartbeat(conn, engine)
    for b in beats:
        if b["state"] != "AT_REST":
            emit("SKIP_NOT_RESTING", item=b["id"], who=b["claimed_by"], state=b["state"],
                 why=b["why"])
            continue
        ok, msg = wo.nudge(conn, engine, int(b["id"]))
        if not ok:
            wo.finish(conn, engine, int(b["id"]), "PARKED", reason=f"budget resume habis: {msg}")
            emit("STUCK_PARKED", item=int(b["id"]), who=b["claimed_by"], why=msg)
            continue
        prompt = CONTINUE_PROMPT.format(id=b["id"], who=b["claimed_by"], why=b["why"])
        try:
            reply = sender(b["claimed_by"], prompt)
            emit("CONTINUE_SENT", item=int(b["id"]), who=b["claimed_by"], budget=msg,
                 reply=str(reply)[:200])
        except Exception as exc:  # noqa: BLE001
            emit("SEND_FAILED", item=int(b["id"]), who=b["claimed_by"],
                 why=f"{type(exc).__name__}: {exc}")


# --- satu tick ----------------------------------------------------------------
def tick(conn, engine, gate=None, beats=None, runner=None, sender=None, log=None):
    """Satu putaran timer. Mengembalikan kejadian yang juga dituliskan ke log malam.

    Semua yang bisa mengetik atau mengeksekusi (`beats`, `runner`, `sender`) bisa disuntik, jadi
    selftest tidak menyentuh satu pane pun dan tidak membuat satu proses pun — karena itu file ini
    bisa dibaca sebagai bukti, bukan sebagai maksud.
    """
    events = []
    emit = log or events.append
    gate = gate or read_gate(conn, engine)
    lvl, ap, night = gate["level"], gate["autopilot"], gate["night"]

    def ev(action, **rest):
        row = {"ts": wo.now(), "action": action, "level": lvl.get("level"),
               "mode": ap.get("mode"), **rest}
        emit(row)
        return row

    # Pintu pertama ditutup oleh ketidaktahuan, bukan oleh keadaan "aman". Empat keadaan tak terbaca
    # adalah UNKNOWN (invarian 5) dan UNKNOWN tidak pernah berarti boleh bergerak.
    if lvl.get("readable") is not True:
        ev("CLOSED_UNKNOWN", why=lvl.get("why"))
        return events
    if ap.get("mode") != "ON":
        ev("CLOSED_OFF", why=ap.get("off_reason") or "sakelar OFF")
        return events
    if not night.get("ok"):
        ev("CLOSED_NIGHT", why=night.get("why"))
        return events
    if lvl["level"] == "observe":
        ev("OBSERVED", claims=len(open_claims(conn, engine)),
           next=(next_ready(conn, engine) or {}).get("id"), rest=lvl.get("why"))
        return events

    if lvl["allows_dispatch"]:
        reconcile(conn, engine, runner=runner, emit=ev)
        hand_new_work(conn, engine, runner=runner, sender=sender, emit=ev)
    rest = [b for b in (beats if beats is not None else wo.heartbeat(conn, engine))
            if b["state"] == "AT_REST"]
    if lvl["allows_resume"]:
        nudge_resting(conn, engine, beats=beats, sender=sender, emit=ev)
    elif rest:
        ev("HELD_BY_LEVEL", items=[b["id"] for b in rest],
           why="AT_REST terbukti, tangan tidak diangkat — naikkan dari terminal: arm resume")
    return events


# --- log malam ----------------------------------------------------------------
def night_log_path(day=None):
    return LOG_DIR / f"drainer-{day or datetime.now().strftime('%Y-%m-%d')}.jsonl"


def write_events(events, path=None):
    path = Path(path or night_log_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for e in events:
            fh.write(json.dumps(e, default=str) + "\n")
    return path


# --- selftest -----------------------------------------------------------------
def selftest():
    """Tick diuji dengan indera dan tangan suntikan di SQLite sementara: nol proses, nol ketikan,
    nol koneksi daemon — dan nol tulisan ke basis data produksi."""
    import sqlite3
    results = []

    def check(name, cond, detail=""):
        results.append((bool(cond), name, detail))
        print(f"[{'ok    ' if cond else 'FAIL  '}] {name}" + ("" if cond else f"  <- {detail}"))

    src = Path(__file__).read_text()
    # Sumber modul adalah bagian dari klaim. Ini tangan: ia tidak boleh bisa menyimpulkan
    # "istirahat" sendiri (dua definisi = dua laporan yang boleh berbeda) dan tidak boleh punya
    # jalur kirim kedua di luar API daemon.
    #
    # Yang dipindai hanya kode, bukan selftest-nya sendiri: daftar token terlarang di bawah ini
    # menyebut token-token itu secara literal, jadi memindai seluruh berkas akan menemukan daftarnya
    # sendiri dan gagal. Klaimnya adalah "tidak ada satu pun jalur eksekusi di modul ini yang
    # memakai itu", dan jalur eksekusi semuanya berada sebelum fungsi test.
    code = src.partition("def selftest(")[0]
    for banned in ("capture_pane", "pane_is_busy", "action_log", "ydotool", "send-keys",
                   "set_level(", "subprocess.run", "check_output", "Popen", "tmux"):
        check(f"kode tidak memakai {banned!r}", banned not in code)
    for needed in ("wo.heartbeat", "wo.drain_level", "wo.nudge", "wo.next_item", "cw.run_grouped",
                   "cv.daemon_post"):
        check(f"memakai {needed}, bukan menduakalinya", needed.split(".", 1)[1] in code)

    tmp = Path(f"/tmp/drainer_selftest_{wo.now()}.db")
    conn = sqlite3.connect(str(tmp))
    conn.row_factory = sqlite3.Row
    wo.ensure_schema(conn, "SQLITE")
    sent, ran = [], []

    def sender(target, prompt):
        sent.append((target, prompt))
        return json.dumps({"status": "dispatched", "target_cli": target})

    def runner(cmd, timeout=CHECK_TIMEOUT):
        ran.append(cmd)
        return type("R", (), {"returncode": 1, "stdout": "sengaja gagal dalam selftest"})()

    def passing_runner(cmd, timeout=CHECK_TIMEOUT):
        ran.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "lolos"})()

    def sink(out):
        """Penampung kejadian berbentuk seperti `ev` di dalam tick: (action, **isi) -> satu dict.

        Dipakai oleh pemanggilan langsung ke `gate_item`/`reconcile` tanpa lewat `tick`, supaya tes
        menguji fungsi yang sama dengan bentuk pemanggilan yang sama — bukan varian lemahnya.
        """
        def _emit(action, **rest):
            row = {"action": action, **rest}
            out.append(row)
            return row
        return _emit

    def make(title, status="PENDING", gate="true", engine_key=None, holder=None, lease=None):
        wo.run(conn, "SQLITE",
               "INSERT INTO quest_tasks (title, status, check_command, cli_engine, claimed_by, "
               "lease_epoch) VALUES (?,?,?,?,?,?)",
               (title, status, gate, engine_key, holder, lease))
        return int(wo.run(conn, "SQLITE",
                          "SELECT id FROM quest_tasks WHERE title=?", (title,))[0]["id"])

    try:
        lvl0 = wo.drain_level(conn, "SQLITE")
        assert lvl0["level"] == "observe", "fixture harus lahir dengan tangan tertutup"

        def gate_for(level, mode="ON", ok=True, why=None, dispatch=None, resume=None):
            allows_d = dispatch if dispatch is not None else level in ("dispatch", "resume")
            allows_r = resume if resume is not None else level == "resume"
            return {"level": {**lvl0, "level": level, "readable": True,
                              "allows_dispatch": allows_d, "allows_resume": allows_r, "why": why or []},
                    "autopilot": {"mode": mode, "off_reason": "selftest"},
                    "night": {"ok": ok, "why": why or []}}

        out = tick(conn, "SQLITE", gate=gate_for("observe", mode="OFF"), beats=[],
                   sender=sender, runner=runner)
        check("sakelar OFF = tidak ada yang bergerak sama sekali",
              [e["action"] for e in out] == ["CLOSED_OFF"] and not sent and not ran, str(out))

        unreadable = {"level": {"readable": False, "level": None, "allows_dispatch": False,
                                "allows_resume": False, "why": ["tidak terbaca"]},
                      "autopilot": {"mode": "ON"}, "night": {"ok": True, "why": []}}
        out = tick(conn, "SQLITE", gate=unreadable, beats=[], sender=sender, runner=runner)
        check("gerbang tak terbaca menutup sebagai UNKNOWN, bukan sebagai observe",
              [e["action"] for e in out] == ["CLOSED_UNKNOWN"], str(out))

        out = tick(conn, "SQLITE", gate=gate_for("observe", ok=False, why=["circuit breaker"]),
                   beats=[], sender=sender, runner=runner)
        check("circuit breaker menutup tick sebelum satu klaim pun",
              [e["action"] for e in out] == ["CLOSED_NIGHT"], str(out))

        ran.clear()
        out = tick(conn, "SQLITE", gate=gate_for("observe"), beats=[], sender=sender, runner=runner)
        check("observe = indera saja: nol gerbang, nol kiriman, nol tulisan",
              [e["action"] for e in out] == ["OBSERVED"] and not sent and not ran, str(out))

        # --- invarian 3 diuji pada baris sungguhan, dengan canary yang membuktikan penolakan ---
        canary = "/tmp/drainer_canary_selftest"
        Path(canary).unlink(missing_ok=True)
        poison = make("selftest-poison", gate=f"touch {canary} && rm -rf /tmp/drainer_target")
        ran.clear()
        verdict = gate_item(conn, "SQLITE", wo.get_item(conn, "SQLITE", poison), runner=runner,
                            stage="before", emit=sink([]))
        check("item mengandung 'rm -rf' ditolak sebelum satu proses pun dibuat",
              verdict.get("refused") and not ran, str((verdict, ran)))
        check("canary-nya benar-benar tidak pernah dieksekusi",
              not Path(canary).exists(), "berkas canary ada = gerbang tetap dijalankan")
        check("yang ditolak naik ke antrean manusia, tidak hilang dan tidak PARKED",
              wo.norm((wo.get_item(conn, "SQLITE", poison) or {}).get("status")) == "HUMAN",
              str((wo.get_item(conn, "SQLITE", poison) or {}).get("status")))

        # --- kegagalan baseline TIDAK boleh ditulis sebagai FAILED ---
        base = make("selftest-baseline", gate="false", engine_key="qoder")
        ran.clear()
        out = []
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[], sender=sender, runner=runner,
             log=out.append)
        check("dispatch menjalankan gerbang dan mencatat angka keluar",
              ran and any(e["action"] == "GATE_BASELINE" and e["exit"] == 1 for e in out), str(out))
        after_base = wo.get_item(conn, "SQLITE", base) or {}
        check("kegagalan baseline tidak ditulis sebagai FAILED: itu belum dimulai",
              wo.norm(after_base.get("status")) in ("WORKING", "CLAIMED"), str(after_base.get("status")))
        check("item yang dikerjakan drainer dipegang atas nama drainer, bukan atas nama pane",
              after_base.get("claimed_by") == HOLDER, str(after_base.get("claimed_by")))
        check("kirim pekerjaan baru memakai API daemon ke tujuan dari baris, bukan tebakan",
              [e["action"] for e in out if e["action"] == "WORK_DISPATCHED"] and
              any(e.get("target") == "qoder" for e in out), str(out))

        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET cli_engine=NULL WHERE id=?", (base,))
        fresh = make("selftest-tanpa-tujuan", gate="false")
        sent.clear()
        out = []
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[], sender=sender, runner=runner,
             log=out.append)
        check("baris tanpa cli_engine ditolak NO_TARGET, tidak dikirim ke engine favorit tick ini",
              any(e["action"] == "NO_TARGET" and e["item"] == fresh for e in out)
              and not any(e["action"] == "WORK_DISPATCHED" and e.get("item") == fresh for e in out),
              str(out))
        freed = wo.get_item(conn, "SQLITE", fresh) or {}
        check("yang ditolak NO_TARGET melepas lease drainer, tidak menahan item untuk dirinya",
              not freed.get("claimed_by"), str(freed.get("claimed_by")))

        # --- vonis: gerbang lulus = COMPLETED dengan bukti terukur, tanpa prosa ---
        ready = make("selftest-sudah-selesai", gate="true")
        ran.clear()
        verdict = gate_item(conn, "SQLITE", wo.get_item(conn, "SQLITE", ready),
                            runner=passing_runner, stage="before", emit=sink([]))
        row = wo.get_item(conn, "SQLITE", ready) or {}
        check("gerbang yang lolos saat baseline menutup item dengan bukti angka, bukan panggilan engine",
              verdict["exit"] == 0 and wo.norm(row.get("status")) == "COMPLETED"
              and "exit 0" in (row.get("evidence") or ""), str(row))

        # --- after-gate + cooldown ---
        held = make("selftest-dipegang", status="WORKING", gate="false", holder=HOLDER,
                    lease=wo.now() + 600)
        ran.clear()
        out = []
        reconcile(conn, "SQLITE", runner=runner, emit=sink(out))
        check("klaim drainer yang lease-nya masih hidup divonis nanti, bukan sekarang",
              any(e["action"] == "HELD" and e["item"] == held for e in out)
              and not any(e["item"] == held and e["action"] == "GATE_FAILED" for e in out)
              and wo.norm((wo.get_item(conn, "SQLITE", held) or {}).get("status")) == "WORKING",
              str(out))
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET lease_epoch=? WHERE id=?",
               (wo.now() - 1, held))
        ran.clear()
        out = []
        reconcile(conn, "SQLITE", runner=runner, emit=sink(out))
        row = wo.get_item(conn, "SQLITE", held) or {}
        check("lease habis + gerbang gagal = FAILED dengan angka keluar yang terukur",
              any(e["action"] == "GATE_FAILED" and e["item"] == held and e["exit"] == 1
                  for e in out) and wo.norm(row.get("status")) == "FAILED", str(out))
        check("yang gagal diberi jeda, supaya tick berikutnya tidak mengunyah baris yang sama",
              int(row.get("due_epoch") or 0) > wo.now(), str(row.get("due_epoch")))

        # --- tangan: hanya pada resume, hanya pada AT_REST, dan selalu dihitung ---
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET status='WORKING', claimed_by='qoder', "
               "resume_count=0, due_epoch=0, lease_epoch=? WHERE id=?", (wo.now() + 600, base))
        beat = {"id": base, "state": "AT_REST", "claimed_by": "qoder", "why": "dua sinyal setuju"}
        sent.clear()
        disp_out = []
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[beat], sender=sender, runner=runner,
             log=disp_out.append)
        check("di tingkat dispatch, AT_REST ditahan dan tidak ada satu pun ketikan",
              not sent and any(e["action"] == "HELD_BY_LEVEL" and e["items"] == [base]
                               for e in disp_out), str((sent, disp_out)))

        resume_out = []
        tick(conn, "SQLITE", gate=gate_for("resume"), beats=[beat], sender=sender, runner=runner,
             log=resume_out.append)
        check("resume + AT_REST = satu kiriman CONTINUE, dan anggaran ikut naik",
              len(sent) == 1 and sent[0][0] == "qoder"
              and int((wo.get_item(conn, "SQLITE", base) or {}).get("resume_count") or 0) == 1,
              str(sent))
        check("prompt yang dikirim membawa arahan broker, bukan pertanyaan kepada operator",
              bool(sent) and "jangan meminta saya memilih" in sent[0][1], str(sent[:1]))
        check("kiriman dicatat dengan balasan daemon apa adanya, bukan sebagai 'pasti sampai'",
              any(e["action"] == "CONTINUE_SENT" and "dispatched" in (e.get("reply") or "")
                  for e in resume_out), str(resume_out))

        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET resume_count=? WHERE id=?",
               (wo.MAX_RESUME, base))
        sent.clear()
        stuck = []
        tick(conn, "SQLITE", gate=gate_for("resume"), beats=[beat], sender=sender, runner=runner,
             log=stuck.append)
        check("anggaran habis memarkir item dan tidak mengirim apa pun",
              not sent and any(e["action"] == "STUCK_PARKED" and e["item"] == base
                               for e in stuck), str(stuck))

        sent.clear()
        out = []
        tick(conn, "SQLITE", gate=gate_for("resume"),
             beats=[{"id": base, "state": "WORKING", "claimed_by": "qoder", "why": "spinner"},
                    {"id": base, "state": "UNKNOWN", "claimed_by": "qoder", "why": "pane mati"}],
             sender=sender, runner=runner, log=out.append)
        skipped = [e for e in out if e["action"] == "SKIP_NOT_RESTING"]
        check("WORKING dan UNKNOWN tidak pernah disentuh — UNKNOWN bukan berarti diam",
              not sent and len(skipped) == 2
              and {e["state"] for e in skipped} == {"WORKING", "UNKNOWN"}, str(out))

        # Jendela ON dibaca ulang pada saat klaim, bukan diwarisi dari awal tick. Timer yang mulai
        # pukul 01:55 untuk jendela yang mati pukul 02:00 tidak boleh masih mengklaim pada 02:05
        # hanya karena ia sudah terlanjur bangun (F10.8: expiry ditegakkan di `claim`, bukan di timer).
        wo.run(conn, "SQLITE",
               "UPDATE autopilot_state SET mode='ON', on_epoch=?, until_epoch=?, item_budget=12",
               (wo.now() - 7200, wo.now() - 60))
        sent.clear()
        out = []
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[], sender=sender, runner=runner,
             log=out.append)
        refused = [e for e in out if e["action"] == "CLAIM_REFUSED"]
        check("jendela yang lewat ketahuan saat klaim, bukan dipercaya dari awal tick",
              bool(refused) and "jendela ON lewat" in " ".join(str(e.get("why")) for e in refused),
              str(out))
        check("klaim yang ditolak ikut diberi jeda pendek, tidak langsung ditanya ulang",
              all(int((wo.get_item(conn, "SQLITE", e["item"]) or {}).get("due_epoch") or 0) > wo.now()
                  for e in refused), str(refused))
        wo.run(conn, "SQLITE", "UPDATE autopilot_state SET mode='OFF', until_epoch=NULL")

        # Klaim atas nama HOLDER tidak boleh bisa diketuki: tanpa pane, heartbeat menjawab UNKNOWN.
        dr = make("selftest-drainer-held", status="WORKING", gate="true", holder=HOLDER,
                  lease=wo.now() + 600)
        beats = wo.heartbeat(conn, "SQLITE", capture=lambda t: None, busy=lambda t: False,
                             sample_gap=0)
        mine = [b for b in beats if b["claimed_by"] == HOLDER]
        check("klaim drainer selalu UNKNOWN: jalur resume secara struktur tidak menjangkaunya",
              any(b["id"] == dr for b in mine) and all(b["state"] == "UNKNOWN" for b in mine),
              str([(b["id"], b["state"]) for b in mine]))

        path = write_events([{"ts": 1, "action": "SELFTEST"}],
                            path=Path("/tmp/drainer_selftest.jsonl"))
        check("log malam ditulis sebagai JSONL yang dibaca baris per baris",
              path.exists() and json.loads(path.read_text().splitlines()[-1])["action"] == "SELFTEST")
    finally:
        Path("/tmp/drainer_selftest.jsonl").unlink(missing_ok=True)
        Path("/tmp/drainer_canary_selftest").unlink(missing_ok=True)
        conn.close()
        tmp.unlink(missing_ok=True)
    bad = [r for r in results if not r[0]]
    print(f"\ndrainer selftest: {len(results) - len(bad)}/{len(results)} sesuai")
    for _ok, name, detail in bad:
        print(f"  FAIL {name} {detail}")
    return 1 if bad else 0


def main(argv=None):
    p = argparse.ArgumentParser(description="F10.3 drainer — satu tick tanpa LLM")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("tick")
    sub.add_parser("gate")
    sub.add_parser("selftest")
    a = p.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    conn, engine = wo.connect()
    wo.ensure_schema(conn, engine)
    if a.cmd == "gate":
        print(json.dumps(read_gate(conn, engine), default=str, indent=2))
        return 0
    events = []
    tick(conn, engine, log=events.append)
    path = write_events(events)
    print(json.dumps(events, default=str, indent=2))
    print(f"\nlog: {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
