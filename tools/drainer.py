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
import shlex
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

# Yang dikirim ke engine ketika yang memegang mesin adalah perintah orang, bukan antrean roadmap.
# Bedanya dengan START_PROMPT bukan hiasan: SEMI berarti ada manusia yang sedang mengawasi, jadi
# yang dikerjakan SATU perintah yang dia tulis sendiri, dan mengambil item roadmap adalah perluasan
# mandat yang tidak pernah dia berikan.
SEMI_PROMPT = (
    "Perintah operator dari Dashboard (Semi Autopilot): item {id} — {title}. "
    "Proyek baris ini: `{ws}` — kerjakan di direktori itu, bukan di ~/.ai-station. Drainer memegang "
    "lease sampai kamu mulai: perpanjang dengan `python3 tools/work_order.py lease {id}`. "
    "Gerbang penerimaannya: `{gate}`. Ada manusia di meja yang sedang melihat, jadi selesaikan SATU "
    "perintah ini lalu tutup dengan `complete {id} --evidence \"<perintah yang benar-benar lolos>\"` "
    "— bukti menyebut angka keluar dan direktori, bukan prosa. Jangan ambil item roadmap: SEMI hanya "
    "mengerjakan apa yang operator tulis sendiri ke SQL. Keputusan level preferensi tetap jangan "
    "ditanyakan ke dia: `decide` lalu `route`."
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


def next_ready(conn, engine, origin=None):
    """Item berikutnya yang siap, dengan batas asal yang diminta pemanggil.

    `origin` diteruskan, tidak disaring di sini: "apa berikutnya" punya satu pemilik (`next_item`)
    dan drainer hanya meminjamnya. Menyaring di sisi drainer membuat dua definisi antrean.
    """
    return wo.next_item(conn, engine, respect_due=True, origin=origin)


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

    Direktori TIDAK lagi diputuskan di sini. `cd {REPO}` yang lama membuat setiap gerbang di dunia
    mengukur workstation, termasuk gerbang milik proyek lain — dan engine yang mengerjakan proyek A
    sementara vonisnya dibaca dari tree B adalah laporan tentang orang lain, bukan tentang dirinya.
    Yang memilih direktori sekarang hanya `gate_command`, dan satu baris hanya punya satu jawaban.
    """
    import call_workers as cw
    return cw.run_grouped(["bash", "-lc", cmd], timeout=timeout, text=True, merge_stderr=True)


def gate_command(item, command):
    """Bungkus gerbang dengan direktori milik baris ini — di-kuip, bukan disambungkan mentah.

    `ws_path` kosong berarti `REPO`, persis seperti sebelum kolom itu ada, sehingga enam puluh baris
    lama tidak perlu dipaksa punya proyek sebelum mereka memang punya satu.
    """
    cwd = shlex.quote(str(wo.resolve_workspace(item)))
    return f"cd {cwd} && {command}"


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
        done = runner(gate_command(item, command), CHECK_TIMEOUT)
        code = int(getattr(done, "returncode", -1))
        tail = (getattr(done, "stdout", "") or "")[-200:].strip()
    except Exception as exc:  # noqa: BLE001 — gerbang yang meledak adalah gerbang yang gagal
        code, tail = -1, f"{type(exc).__name__}: {exc}"

    if code == 0:
        # Bukti menyebut direktori yang sebenarnya diukur. "exit 0: cargo test" tanpa tempat adalah
        # laporan yang bisa dibaca sebagai kelulusan tree mana pun — dan setelah kolom ws_path ada,
        # itu justru tree yang salah.
        wo.finish(conn, engine, item_id, "COMPLETED",
                  evidence=f"drainer gate ({stage}) exit 0: {gate_command(item, command)[:180]}")
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


def _release_claim(conn, engine, item_id):
    """Tarik kembali klaim drainer: baris tanpa pemegang adalah laporan bahwa ada yang mengerjakan
    padahal tidak ada — kembalikan persis seperti keadaan sebelum tick ini menyentuhnya."""
    wo.run(conn, engine,
           f"UPDATE quest_tasks SET status='PENDING', claimed_by=NULL, lease_epoch=0 "
           f"WHERE id={wo_p(engine)}", (int(item_id),))


def hand_new_work(conn, engine, runner=None, sender=None, emit=None, mode="ON", origin=None):
    """Serahkan satu item baru ke engine yang ditunjuk barisnya. Tidak pernah menebak tujuan.

    Mesin mana yang mengerjakan adalah milik kolom `cli_engine` pada baris itu — operator menyuntingnya
    dari Station, dan kontrak melarang memilih engine dari ingatan (§12). Baris tanpa tujuan ditolak,
    bukan diisi dengan favorit tick ini.

    Drainer mengklaim atas namanya sendiri supaya tidak ada dua klaim atas item yang sama malam itu;
    HOLDER tidak punya pane, jadi `heartbeat` selalu menjawab UNKNOWN untuknya dan jalur resume tidak
    akan pernah bisa mengetuknya — aman secara konstruksi, bukan karena ada yang ingat menjaga.

    `mode`/`origin` adalah seluruh beda SEMI dengan FULL AUTO di sisi pengiriman: SEMI hanya mengambil
    baris `origin='operator'`, dan ia menunggu satu engine menyelesaikan perintahnya sebelum memberi
    perintah kedua. Yang terakhir itu jawaban untuk "kalau melihat kamu Working, nunggu sampai
    selesai": mesin yang sedang mengerjakan sesuatu tidak ditumpuki, karena dua perintah dalam satu
    pane bukan dua pekerjaan — yang satu akan tertelan yang lain dan keduanya tercatat jalan.
    ON tidak mendapat aturan ini: tidak ada pengawas yang menunggui giliran, dan memperlambat
    semalam untuk sebuah kesamaan yang tidak diminta adalah penurunan, bukan penguatan.
    """
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    item = next_ready(conn, engine, origin=origin)
    if not item:
        emit("QUEUE_EMPTY", origin=origin or "apa pun")
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
        _release_claim(conn, engine, item["id"])
        emit("NO_TARGET", item=int(item["id"]), why="kolom cli_engine kosong — memilih engine "
             "dari ingatan dilarang, jadi item ini menunggu orang mengisinya")
        return None
    if mode == "SEMI":
        busy = [r for r in open_claims(conn, engine)
                if (r.get("cli_engine") or "").strip() == target
                and int(r["id"]) != int(item["id"])]
        if busy:
            _release_claim(conn, engine, item["id"])
            cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
            emit("ENGINE_BUSY", item=int(item["id"]), target=target,
                 waiting=[int(r["id"]) for r in busy],
                 why="SEMI menunggu engine ini selesai lebih dulu — bekerja berarti menunggu, "
                     "bukan menumpuki pane")
            return None
    if mode == "SEMI":
        prompt = SEMI_PROMPT.format(id=item["id"], title=item["title"],
                                    ws=wo.resolve_workspace(full),
                                    gate=(full.get("check_command") or "tanpa gerbang"))
    else:
        prompt = START_PROMPT.format(id=item["id"], title=item["title"],
                                     gate=(full.get("check_command") or "tanpa gerbang"))
    try:
        reply = (sender or default_sender)(target, prompt)
        emit("WORK_DISPATCHED", item=int(item["id"]), target=target, mode=mode,
             reply=str(reply)[:200])
    except Exception as exc:  # noqa: BLE001 — kegagalan kirim dicatat, tidak dikarang ulang
        emit("SEND_FAILED", item=int(item["id"]), target=target,
             why=f"{type(exc).__name__}: {exc}")
    return {"item": int(item["id"]), "target": target}


def nudge_resting(conn, engine, beats=None, sender=None, emit=None, mode="ON"):
    """CONTINUE untuk klaim yang terbukti istirahat — anggaran dihitung oleh `wo.nudge`.

    Di bawah SEMI hanya klaim atas perintah operator yang boleh disentuh: menaikkan `CONTINUE` ke
    pane yang sedang memegang item roadmap adalah cara memperluas mandat SEMI tanpa ada yang
    mengangkatnya, dan pagar yang bisa dilebarkan sendiri bukan pagar.
    """
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    sender = sender or default_sender
    if beats is None:
        beats = wo.heartbeat(conn, engine)
    for b in beats:
        if mode == "SEMI":
            row = wo.get_item(conn, engine, int(b["id"])) or {}
            if (row.get("origin") or "legacy") != wo.ORIGIN_OPERATOR:
                emit("SKIP_NOT_OPERATOR", item=b["id"], who=b["claimed_by"],
                     origin=row.get("origin") or "legacy",
                     why="SEMI tidak menegur pekerjaan roadmap — naikkan ke ON untuk itu")
                continue
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


ANSWER_PROMPT = (
    "JAWABAN OPERATOR untuk keputusan #{decision}, item #{item}:\n"
    "  {answer}\n"
    "Jawaban ini tercatat di tabel decisions sebagai state=ANSWERED, answered_by={by} — datang dari "
    "manusia di meja, bukan dari resolver. Kerjakan sekarang. Jangan ajukan pertanyaan yang sama "
    "untuk kedua kalinya; kalau pilihannya berubah, itu pertanyaan baru, bukan suntingan sejarah."
)


def deliver_answer(conn, engine, decision_id, sender=None, beats=None, capture=None, busy=None):
    """Antar satu jawaban yang barusan ditulis ke mesin yang menunggunya — atau laporkan mengapa tidak.

    Ini tangan, dan pembedanya dari `nudge_resting` bukan kecil: `nudge_resting` dipanggil timer dan
    mengetik `CONTINUE` atas nama mesin, sementara fungsi ini hanya dipanggil dari satu klik manusia
    (`POST /api/decide`) dan mengetik jawaban manusia atas nama manusia. Sakelar OFF melindungi orang
    yang tidur dari mesin yang mempersenjatai dirinya sendiri — bukan dari jawabannya sendiri.
    `tick()` tidak pernah menyebut fungsi ini, dan selftest menegakkan dua-duanya: token kirim tidak
    ada di jalur timer, dan nama fungsi ini tidak ada di badan `tick`.

    `AT_REST` tetap diminta walau yang menekan tombol adalah operator. Alasannya bukan izin, tapi
    keselamatan pekerjaan: teks yang masuk ke pane yang sedang di tengah kalimat mengacaukan giliran
    mesin itu, dan yang hilang adalah pekerjaannya, bukan cuma lognya. Untuk item tanpa klaim terbuka
    — kasus normal, karena item berhenti di `HUMAN` justru sambil menunggu ini — buktinya satu
    pembacaan pane: tidak sedang menampilkan spinner, dan terbaca.

    Yang tidak dilakukan: memilih engine. Tujuannya `cli_engine` pada baris item, atau tidak ada
    tujuan sama sekali (§12).
    """
    ph = "%s" if engine == "POSTGRESQL" else "?"
    rows = wo.run(conn, engine, f"SELECT * FROM decisions WHERE id={ph}", (decision_id,))
    decision = rows[0] if rows else None
    if not decision:
        return {"ok": False, "sent": False, "why": f"decision {decision_id} tidak ada"}
    if decision.get("state") != "ANSWERED":
        return {"ok": False, "sent": False,
                "why": f"decision {decision_id} berstatus {decision.get('state')}: "
                       "yang diantar hanya jawaban yang sudah tercatat"}
    item_id = decision.get("item_id")
    if not item_id:
        return {"ok": False, "sent": False,
                "why": f"decision {decision_id} tidak menautkan item: jawabannya ada di ledger, "
                       "tapi tidak ada pekerjaan yang bisa dibangunkan"}
    item = wo.get_item(conn, engine, int(item_id)) or {}
    target = (item.get("cli_engine") or "").strip()
    if not target:
        return {"ok": False, "sent": False, "why": f"item {item_id} tidak punya cli_engine; "
                                                  "tujuan tidak dipilih dari kebiasaan"}
    if beats is None:
        beats = wo.heartbeat(conn, engine)
    held = next((b for b in beats if int(b["id"]) == int(item_id)), None)
    if held is not None:
        if held["state"] != "AT_REST":
            return {"ok": False, "sent": False, "target": target, "item": int(item_id),
                    "why": f"klaim masih {held['state']} ({held['why']}): jawaban sudah tercatat, "
                           "pengiriman ditunda — bukan digagalkan"}
        proof = f"heartbeat AT_REST ({held['why']})"
    else:
        import cross_verify as cv
        capture = capture or cv.capture_pane
        busy = busy or cv.pane_is_busy
        text = capture(target)
        if not text:
            return {"ok": False, "sent": False, "target": target, "item": int(item_id),
                    "why": f"pane {target} tidak terbaca: UNKNOWN bukan berarti diam"}
        if busy(text):
            return {"ok": False, "sent": False, "target": target, "item": int(item_id),
                    "why": f"pane {target} sedang bekerja: jawaban tercatat, diantar nanti"}
        proof = f"tidak ada klaim terbuka; pane {target} terbaca tanpa pekerjaan berjalan"
    prompt = ANSWER_PROMPT.format(
        decision=decision_id, item=int(item_id),
        answer=(decision.get("answer") or "").strip()[:1500],
        by=decision.get("answered_by") or decision.get("model_id") or "operator")
    sender = sender or default_sender
    try:
        reply = (sender(target, prompt) or "")
    except Exception as exc:  # noqa: BLE001 — pengiriman gagal bukan berarti jawaban hilang
        return {"ok": True, "sent": False, "target": target, "item": int(item_id),
                "why": f"tersimpan, pengiriman gagal: {type(exc).__name__}: {exc}"}
    return {"ok": True, "sent": True, "target": target, "item": int(item_id),
            "proof": proof, "reply": str(reply)[:300]}


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
    if ap.get("mode") not in ("ON", "SEMI"):
        ev("CLOSED_OFF", why=ap.get("off_reason") or "sakelar OFF")
        return events
    # SEMI membuka pintu yang sama dengan ON dan mengambil keranjang yang lebih kecil. Yang membedakan
    # keduanya di sini bukan seberapa jauh tangan boleh pergi — itu tangga `level`, dan ia tidak punya
    # versi SEMI — melainkan APA yang boleh diambil dari antrean.
    mode = ap.get("mode")
    origin = wo.ORIGIN_OPERATOR if mode == "SEMI" else None
    if not night.get("ok"):
        ev("CLOSED_NIGHT", why=night.get("why"))
        return events
    if lvl["level"] == "observe":
        ev("OBSERVED", claims=len(open_claims(conn, engine)),
           next=(next_ready(conn, engine, origin=origin) or {}).get("id"), rest=lvl.get("why"),
           mode=mode)
        if mode == "SEMI":
            # Di SEMI, `observe` adalah kegagalan yang terlihat: operator sedang di meja, dan
            # diamnya tangan berarti perintah yang dia tulis tidak akan pernah sampai. Dijawab
            # dengan jujur, bukan dengan menaikkan level diam-diam.
            ev("SEMI_HAND_FLAT", why="level masih observe — naikkan dari terminal: arm dispatch",
               operator_queue=(next_ready(conn, engine, origin=origin) or {}).get("id"))
        return events

    if lvl["allows_dispatch"]:
        reconcile(conn, engine, runner=runner, emit=ev)
        hand_new_work(conn, engine, runner=runner, sender=sender, emit=ev,
                      mode=mode, origin=origin)
    rest = [b for b in (beats if beats is not None else wo.heartbeat(conn, engine))
            if b["state"] == "AT_REST"]
    if lvl["allows_resume"]:
        nudge_resting(conn, engine, beats=beats, sender=sender, emit=ev, mode=mode)
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
    code = src.partition("def selftest(")[0]
    # Sumber modul adalah bagian dari klaim. Ini tangan: ia tidak boleh bisa menyimpulkan
    # "istirahat" sendiri (dua definisi = dua laporan yang boleh berbeda) dan tidak boleh punya
    # jalur kirim kedua di luar API daemon.
    #
    # Yang dipindai hanya kode, bukan selftest-nya sendiri: daftar token terlarang di bawah ini
    # menyebut token-token itu secara literal, jadi memindai seluruh berkas akan menemukan daftarnya
    # sendiri dan gagal.
    #
    # Klaimnya juga sudah bukan "modul ini tidak pernah menyentuh pane". Yang benar, dan yang bisa
    # ditegakkan, adalah SIAPA yang boleh mengetik: jalur timer (`tick` dan seluruh yang dipanggilnya)
    # tidak punya tangan, sementara `deliver_answer` adalah tangan manusia yang hanya dipanggil satu
    # klik. Karena itu pemindaian per fungsi, dan badan fungsi manusia dibuang dari pemindaian jalur
    # timer secara terbuka — token-nya tidak diizinkan diam-diam, ia memang bukan bagian timer.
    import ast as _ast
    segments = {n.name: (_ast.get_source_segment(code, n) or "")
                for n in _ast.parse(code).body if isinstance(n, _ast.FunctionDef)}
    hand = segments.get("deliver_answer") or ""
    timer_code = code.replace(hand, "") if hand else code
    check("tangan manusia ada satu fungsi, bukan tersebar", bool(hand) and hand != code,
          str(sorted(segments)))
    for banned in ("capture_pane", "pane_is_busy", "action_log", "ydotool", "send-keys",
                   "set_level(", "subprocess.run", "check_output", "Popen", "tmux"):
        check(f"jalur timer tidak memakai {banned!r}", banned not in timer_code)
    for name in ("tick", "reconcile", "hand_new_work", "nudge_resting", "gate_item", "read_gate"):
        check(f"{name}() tidak pernah memanggil tangan manusia",
              "deliver_answer" not in segments.get(name, ""), name)
    for needed in ("wo.heartbeat", "wo.drain_level", "wo.nudge", "wo.next_item", "cw.run_grouped",
                   "cv.daemon_post"):
        check(f"memakai {needed}, bukan menduakalinya", needed.split(".", 1)[1] in timer_code)

    # Pemicu SEMI (F10.10) adalah berkas TERPISAH di luar modul ini, dan klaim bahwa ia tidak
    # menyentuh pane harus ditegakkan dengan pemindaian yang sama — bukan dengan peringatan di
    # komentar. Satu suntingan yang menambahkan `default_sender(...)` ke hook akan mengubah
    # "menyuntik prompt ke sesi sendiri" menjadi "mengetik ke pane siapa pun", dan itu perbedaan
    # yang tidak bisa dilihat dari hasilnya sampai seseorang kehilangan pekerjaannya.
    hook_file = wo.REPO / "hooks" / "semi_wake.py"
    hsrc = hook_file.read_text() if hook_file.is_file() else ""
    check("pemicu SEMI ada di tempat yang didaftarkan", bool(hsrc.strip()), str(hook_file))
    if hsrc:
        hsegs = {n.name: (_ast.get_source_segment(hsrc, n) or "")
                 for n in _ast.parse(hsrc).body
                 if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        for banned in ("deliver_answer", "default_sender", "daemon_post", "/api/cli/run",
                       "send-keys", "ydotool", "set_level(", "arm(", "tmux", "subprocess"):
            hit = [f"{banned} di {fn}()" for fn, body in hsegs.items() if banned in body]
            check(f"hook SEMI tidak memakai {banned!r}", not hit, "; ".join(hit))
        check("hook SEMI hanya bisa bicara sesudah satu klaim berhasil — antrean adalah pagarnya",
              "wo.claim(" in hsegs.get("main", ""), str(sorted(hsegs)))
        check("hook SEMI membaca mode dari database pada setiap tembakan, bukan dari harapan",
              "wo.autopilot(" in hsegs.get("main", ""))
        check("hook SEMI menyaring asal baris, jadi ia tidak pernah mengambil item roadmap",
              "origin=wo.ORIGIN_OPERATOR" in hsegs.get("main", ""))

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

    def make(title, status="PENDING", gate="true", engine_key=None, holder=None, lease=None,
             origin=None, ws=None):
        wo.run(conn, "SQLITE",
               "INSERT INTO quest_tasks (title, status, check_command, cli_engine, claimed_by, "
               "lease_epoch, origin, ws_path) VALUES (?,?,?,?,?,?,?,?)",
               (title, status, gate, engine_key, holder, lease, origin, ws))
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

        # --- tangan manusia: satu klik yang mengantar jawaban ----------------------
        # Beda dari `nudge_resting` di atas dan bedanya bukan kosakata: yang itu diketik timer atas
        # nama mesin, yang ini diketik karena operator menekan Save. Sakelar OFF melindungi orang yang
        # tidur dari mesin yang mempersenjatai dirinya sendiri — bukan dari jawabannya sendiri.
        tgt = make("selftest-answer-target", status="HUMAN", gate="true", engine_key="qoder")
        lonely = make("selftest-answer-notarget", status="HUMAN", gate="true", engine_key=None)
        q_open = wo.decide(conn, "SQLITE", "A atau B?", ["A", "B"], "heavy", tgt)[0]["id"]
        q_alone = wo.decide(conn, "SQLITE", "pertanyaan tanpa pekerjaan", ["A"], "heavy")[0]["id"]
        wo.answer_decision(conn, "SQLITE", q_alone, "A", "selftest-operator")
        d_nolink = deliver_answer(conn, "SQLITE", q_alone, sender=sender, beats=[])
        check("jawaban yang tidak menautkan item tidak membangunkan apa pun",
              not d_nolink["sent"] and "tidak menautkan" in d_nolink["why"], str(d_nolink))
        check("yang belum dijawab tidak diantar",
              not deliver_answer(conn, "SQLITE", q_open, sender=sender, beats=[])["sent"],
              str(deliver_answer(conn, "SQLITE", q_open, sender=sender, beats=[])))
        wo.answer_decision(conn, "SQLITE", q_open, "A, lebih hemat", "selftest-operator")
        sent.clear()
        d_work = deliver_answer(conn, "SQLITE", q_open, sender=sender,
                                beats=[{"id": tgt, "state": "WORKING", "claimed_by": "qoder",
                                        "why": "spinner"}])
        check("pane yang sedang bekerja tidak diketuki walau jawabannya sudah ada",
              not sent and not d_work["sent"] and "ditunda" in d_work["why"], str(d_work))
        d_rest = deliver_answer(conn, "SQLITE", q_open, sender=sender,
                                beats=[{"id": tgt, "state": "AT_REST", "claimed_by": "qoder",
                                        "why": "dua sinyal setuju"}])
        check("jawaban diantar ke engine yang tertulis di baris itemnya, bukan yang sedang disukai",
              d_rest["sent"] and [t for t, _ in sent] == ["qoder"], str(sent))
        check("isi kiriman membawa jawaban orang apa adanya",
              bool(sent) and "lebih hemat" in sent[0][1] and str(q_open) in sent[0][1], str(sent[:1]))
        # Item yang berhenti justru karena menunggu tidak memegang klaim: di sana buktinya satu
        # pembacaan pane, dan pane yang tidak terbaca tetap UNKNOWN.
        sent.clear()
        d_blind = deliver_answer(conn, "SQLITE", q_open, sender=sender, beats=[],
                                 capture=lambda t: "", busy=lambda t: False)
        check("pane yang tidak terbaca bukan berarti diam",
              not d_blind["sent"] and "tidak terbaca" in d_blind["why"], str(d_blind))
        check("pane yang sedang menampilkan pekerjaan tidak diketuki",
              not deliver_answer(conn, "SQLITE", q_open, sender=sender, beats=[],
                                 capture=lambda t: "esc to cancel",
                                 busy=lambda t: True)["sent"])
        d_clear = deliver_answer(conn, "SQLITE", q_open, sender=sender, beats=[],
                                 capture=lambda t: "prompt siap", busy=lambda t: False)
        check("tanpa klaim terbuka, satu pembacaan pane yang bersih cukup untuk menjawab",
              d_clear["sent"], str(d_clear))
        wo.run(conn, "SQLITE", "UPDATE decisions SET item_id=? WHERE id=?", (lonely, q_open))
        check("tanpa cli_engine pengiriman ditolak, bukan ditebak",
              not deliver_answer(conn, "SQLITE", q_open, sender=sender,
                                 beats=[{"id": lonely, "state": "AT_REST", "claimed_by": "qoder",
                                         "why": "x"}])["sent"])

        # --- F10.10 SEMI: otomatis dengan pengawas -------------------------------------
        # Yang diuji di blok ini bukan "apakah drainer masih hidup", melainkan BEDA yang membuatnya
        # layak jadi mode ketiga: keranjang mana yang diambil, kapan ia menunggu, dan di direktori
        # mana gerbangnya diukur.
        #
        # Baris autopilot fixture DIBIARKAN OFF sementara gerbangnya disuntik sebagai SEMI, dan itu
        # bukan kecurangan yang diam: `claim()` membaca pemutus dari baris database, dan baris itu
        # akan membaca git NYATA — yang kotor justru karena selftest ini sedang berjalan di tengah
        # suntingan. Arah penyampangan ini aman (fixture lebih longgar dari produksi), dan aturan
        # "SEMI juga ditutup pemutus" dibuktikan di modul yang punya kata itu,
        # `work_order selftest: pemutus malam menutup SEMI persis seperti menutup ON`. Menambahkan
        # knob `dirty` ke jalur dispatch akan jauh lebih mudah, dan itu berarti setiap pemanggil
        # masa depan punya tombol untuk melewati §6 — pagar yang bisa dilewati pemakainya bukan pagar.
        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("observe", mode="SEMI"), beats=[],
                   sender=sender, runner=runner)
        acts = [e["action"] for e in out]
        check("SEMI tidak disalahtafsir sebagai OFF — pintu dibuka, tangan belum diangkat",
              "CLOSED_OFF" not in acts and "SEMI_HAND_FLAT" in acts and not sent, str(out))

        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("dispatch", mode="SEMI"), beats=[],
                   sender=sender, runner=runner)
        acts = [e["action"] for e in out]
        check("SEMI dengan antrean tanpa perintah operator tidak mengambil apa pun — itu isi kata 'semi'",
              "QUEUE_EMPTY" in acts and not sent, str(out))

        op = make("selftest-semi-cmd", gate="false", engine_key="qoder",
                  origin="operator", ws="/tmp")
        sent.clear()
        tick(conn, "SQLITE", gate=gate_for("dispatch", mode="SEMI"), beats=[],
             sender=sender, runner=runner)
        check("perintah operator diantar ke mesin yang tertulis di barisnya",
              sent and sent[0][0] == "qoder" and str(op) in sent[0][1], str(sent)[:220])
        check("yang dikirim adalah prompt perintah-orang, bukan tugas roadmap",
              sent and "Perintah operator dari Dashboard" in sent[0][1], str(sent)[:160])
        check("prompt SEMI menyebut direktori baris, jadi engine tidak dikerjakan di tree lain",
              sent and "/tmp" in sent[0][1], str(sent)[:220])

        op2 = make("selftest-semi-cmd-2", gate="false", engine_key="qoder", origin="operator")
        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("dispatch", mode="SEMI"), beats=[],
                   sender=sender, runner=runner)
        acts = [e["action"] for e in out]
        check("engine yang sedang mengerjakan satu perintah tidak ditumpuki perintah kedua — "
              "'kalau Working, tunggu sampai selesai'",
              "ENGINE_BUSY" in acts and not sent, str(out))

        # Beda mode diukur pada ANTREAN YANG SAMA, bukan dengan dua fixture berbeda: satu baris yang
        # bukan perintah operator harus terlihat oleh FULL AUTO dan tidak terlihat oleh SEMI.
        # (Sebelumnya ada cek di sini dengan `... or True` — dan cek yang selalu hijau adalah bug
        # yang kemarin kutangkap di gerbang f109, jadi ia dibuang, tidak diperhalus.)
        wo.run(conn, "SQLITE", "DELETE FROM quest_tasks WHERE id=?", (op2,))
        free = make("selftest-full-auto-row", gate="false", engine_key="qoder")
        wide = next_ready(conn, "SQLITE")
        narrow = next_ready(conn, "SQLITE", origin=wo.ORIGIN_OPERATOR)
        check("perbedaan FULL AUTO dan SEMI adalah isi keranjang — baris bebas terlihat oleh yang "
              "pertama dan tidak terlihat oleh yang kedua, pada antrean yang sama",
              wide and int(wide["id"]) == free and narrow is None,
              str({"wide": wide and wide["id"], "semi": narrow}))

        rm_claim = make("selftest-semi-roadmap-claim", status="WORKING", engine_key="qoder",
                        holder="qoder", lease=wo.now() + 600)
        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("resume", mode="SEMI"),
                   beats=[{"id": rm_claim, "state": "AT_REST", "claimed_by": "qoder",
                           "why": "dua sinyal setuju"}], sender=sender, runner=runner)
        acts = [e["action"] for e in out]
        check("SEMI tidak mengirim CONTINUE ke klaim roadmap — itu perluasan mandat yang tidak "
              "pernah diberikan", "SKIP_NOT_OPERATOR" in acts and not sent, str(out))

        ran.clear()
        probe = make("selftest-gate-cwd", gate="pwd", engine_key="qoder", ws="/tmp")
        gate_item(conn, "SQLITE", wo.get_item(conn, "SQLITE", probe), runner=runner,
                  stage="before", emit=sink([]))
        check("gerbang baris proyek dijalankan di direktori baris itu",
              ran and "cd /tmp && pwd" in ran[-1], str(ran[-1:]))
        ran.clear()
        probe_old = make("selftest-gate-repo", gate="pwd", engine_key="qoder")
        gate_item(conn, "SQLITE", wo.get_item(conn, "SQLITE", probe_old), runner=runner,
                  stage="before", emit=sink([]))
        check("baris tanpa ws_path masih diukur di REPO — tidak ada satu baris lama yang dipaksa "
              "punya proyek", ran and f"cd {REPO} && pwd" in ran[-1], str(ran[-1:]))

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
    p_del = sub.add_parser("deliver", help="antar jawaban yang barusan ditulis operator ke pane-nya")
    p_del.add_argument("--decision", type=int, required=True)
    sub.add_parser("selftest")
    a = p.parse_args(argv)

    if a.cmd == "selftest":
        return selftest()
    conn, engine = wo.connect()
    wo.ensure_schema(conn, engine)
    if a.cmd == "deliver":
        out = deliver_answer(conn, engine, a.decision)
        events = [{"ts": wo.now(), "action": "ANSWER_SENT" if out.get("sent") else
                   ("ANSWER_NOT_SENT" if out.get("ok") else "ANSWER_DELIVER_REFUSED"), **out}]
        path = write_events(events)
        print(json.dumps({"result": out, "log": str(path)}, default=str, indent=2))
        return 0 if out.get("sent") or out.get("ok") else 1
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
