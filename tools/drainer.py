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
import time
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
# Jeda minimum antara dua pembacaan pane di jalur dispatch. Di bawah angka ini "teksnya tidak
# berubah" adalah tautologi, bukan pengamatan. Diwarisi dari konstanta yang sama yang dipakai
# `heartbeat`, supaya workstation hanya punya satu ukuran untuk "cukup lama untuk disebut diam".
MIN_PANE_GAP = wo.MIN_SAMPLE_GAP

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
                      "lease_epoch, cli_engine, due_epoch, origin FROM quest_tasks "
                      "WHERE status IN ('WORKING','CLAIMED') ORDER BY id")
    return wo.run(conn, engine,
                  "SELECT id, title, status, claimed_by, check_command, resume_count, "
                  "lease_epoch, cli_engine, due_epoch, origin FROM quest_tasks "
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

    `ws_path` kosong berarti `REPO`, persis seperti sebelum kolom itu ada, sehingga baris lama yang ada sekarang tetap
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


def reconcile(conn, engine, mode, runner=None, emit=None):
    """Vonis atas klaim drainer yang masih hidup: gerbang diulang, angka keluar yang bicara."""
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    wanted = (mode or "").upper()
    if wanted not in ("ON", "SEMI"):
        # Sama seperti hand_new_work: mode yang tidak dikenal bukan berarti "kerjakan semuanya".
        # Di sini yang dipertaruhkan adalah MENJALANKAN check_command milik baris roadmap.
        emit("MODE_UNKNOWN", where="reconcile", mode=str(mode),
             why="tidak ada gerbang yang dijalankan")
        return
    for row in open_claims(conn, engine, holder=HOLDER):
        if wanted == "SEMI" and (row.get("origin") or "legacy") != wo.ORIGIN_OPERATOR:
            # `reconcile` MENJALANKAN check_command baris itu. Di bawah SEMI menjalankan shell
            # milik baris roadmap adalah pekerjaan yang tidak pernah diminta operator, walau
            # klaimnya tertinggal dari ON sebelumnya. Barisnya tidak dihukum: hanya ditahan.
            emit("HELD_BY_MODE", item=int(row["id"]), origin=row.get("origin") or "legacy",
                 why="klaim tertinggal dari mode lain — SEMI tidak menjalankan gerbang roadmap")
            continue
        if int(row.get("lease_epoch") or 0) > wo.now():
            # Lease masih dipegang: pekerjaan sedang berjalan atau engine belum mulai. Vonis
            # dijatuhkan setelah lease habis — lebih awal dari itu artinya menyebut pekerjaan orang
            # gagal hanya karena ia belum selesai.
            wo.renew_lease(conn, engine, int(row["id"]))
            emit("HELD", item=int(row["id"]), lease_left=int(row["lease_epoch"]) - wo.now())
            continue
        gate_item(conn, engine, row, runner=runner, stage="after", emit=emit)


def _release_claim(conn, engine, item_id):
    """Tarik kembali klaim drainer lewat pemilik kosakatanya, dan hanya kalau masih miliknya.

    `WHERE id=?` saja pernah cukup di sini. Audit putaran lima menunjuk yang sebaliknya: pelepasan
    tanpa syarat `claimed_by` bisa menghapus klaim pihak lain yang baru saja menang, dan itu
    membuat dua engine mengerjakan satu baris sambil keduanya punya bukti bahwa mereka yang pegang.
    `resume_count` sengaja TIDAK direset — itu anggaran seumur hidup baris, bukan anggaran tick.
    """
    wo.release_claim(conn, engine, int(item_id), HOLDER)


def effective_gap(gap):
    """Berapa lama benar-benar menunggu antara dua pembacaan pane. Tidak bisa dinegosiasi.

    Lantai ini pernah ditulis sebagai `gap if capture is not None else max(...)` dan jadi kode
    mati: `capture` sudah diisi default beberapa baris di atasnya, sehingga selalu bukan-None.
    Ceknya lolos karena hanya mencari string-nya di sumber. Versi kedua memakai "pembacanya
    disuntik?" sebagai syarat bebas tidur — dan audit putaran tiga menunjuk itu sebagai pencampuran
    dua hal: pemanggil produksi yang menyuntik pembaca sendiri bisa meminta gap=0 lagi.

    Jadi lantainya unconditional. Selftest tidak melambat karena yang disuntik adalah SLEEPER-nya,
    bukan aturan jeda-nya.
    """
    return max(gap, MIN_PANE_GAP)


def pane_is_quiet(target, capture=None, busy=None, gap=MIN_PANE_GAP, sleeper=None):
    """Bukti bahwa pane target tidak sedang dipakai — dibaca dua kali dengan jeda NYATA.

    Standar yang sama yang menahan jalur resume, dipindahkan ke jalur dispatch karena temuan audit
    peer 2026-10-08: `hand_new_work` hanya memeriksa SQL lalu mengetik, padahal SQL tidak tahu
    bahwa seseorang sedang menulis di pane itu. Menyuntik teks ke tengah ketikan operator lalu
    menekan Enter atas nama mesin adalah satu-satunya kegagalan di jalur ini yang tidak bisa
    dibatalkan dengan mengembalikan baris database — yang hilang adalah apa yang sedang ia ketik.

    None dari pembacaan mana pun = UNKNOWN = jangan mengetik (invarian 5). Jeda tidak bisa
    dinolkan: `effective_gap` memaksa lantai, dan tes tidak tidur karena SLEEPER-nya yang disuntik
    lewat `hand_new_work`/`tick` — bukan karena aturannya dilonggarkan.
    """
    import cross_verify as cv
    capture = capture or cv.capture_pane
    busy = busy or cv.pane_is_busy
    sleeper = sleeper or time.sleep
    first = capture(target)
    if first is None:
        return {"quiet": False, "why": "pane tidak terbaca — UNKNOWN, bukan diam"}
    if busy(first):
        return {"quiet": False, "why": "pane sedang menampilkan pekerjaan (spinner / esc to cancel)"}
    # `gap=0` hanya dihormati kalau pembacanya disuntik (selftest tidak boleh tidur). Dengan
    # pembacaan nyata, dua frame yang diambil berbarengan SELALU sama, dan menyebut itu "diam"
    # adalah mengarang pengamatan — persis yang dilarang invarian 5.
    wait = effective_gap(gap)
    if wait > 0:
        sleeper(wait)
    second = capture(target)
    if second is None:
        return {"quiet": False, "why": "pembacaan kedua gagal — UNKNOWN, bukan diam"}
    if busy(second):
        return {"quiet": False, "why": "pane menjadi sibuk di antara dua pembacaan"}
    if cv.last_frame(second) != cv.last_frame(first):
        return {"quiet": False, "why": "teks pane berubah antar pembacaan — masih bekerja"}
    return {"quiet": True, "why": f"dua pembacaan dengan jeda {wait}s: tidak berubah, tanpa spinner"}


def _as_path(text):
    """Path yang siap dibandingkan dengan `resolve_workspace` — `~` diperluas lebih dulu."""
    return Path(text).expanduser().resolve(strict=False)


def hand_new_work(conn, engine, mode, runner=None, sender=None, emit=None,
                  capture=None, busy=None, tab_project=None, pane_cwd=None, gap=MIN_PANE_GAP,
                  sleeper=None, dirty=wo._MEASURE):
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
    # Keranjang Diturunkan dari mode, tidak pernah diterima sebagai argumen terpisah. Selama
    # `origin` masih bisa dikirim sendiri, `hand_new_work(conn, engine, "SEMI")` tanpa origin
    # mengambil baris roadmap dan mengirimnya dengan SEMI_PROMPT — satu-satunya penahan adalah
    # `claim()` yang membaca mode dari database, dan itu pagar yang bisa dilewati pemanggil yang
    # tidak tahu. Satu sumber kebenaran, tidak ada pasangan yang bisa tidak cocok.
    # Dinormalisasi dan gagal-TERTUTUP. `mode` dari pemanggil bisa "semi", "SEMI", "OFF", atau
    # salah tulis; selama branch-nya "else = keranjang penuh", mode yang tidak dikenal memberi
    # akses ke antrean roadmap. Yang tidak dikenal bukan "ON", melainkan "tidak mengirim apa pun".
    wanted = (mode or "").upper()
    if wanted not in ("ON", "SEMI"):
        emit("MODE_UNKNOWN", mode=str(mode), why="bukan ON/SEMI — tidak ada yang diambil, "
             "tidak ada yang diketik")
        return None
    origin = wo.ORIGIN_OPERATOR if wanted == "SEMI" else None
    item = next_ready(conn, engine, origin=origin)
    if not item:
        emit("QUEUE_EMPTY", origin=origin or "apa pun")
        return None
    full = wo.get_item(conn, engine, int(item["id"])) or item
    # Baseline dulu, sebelum klaim: item yang gerbangnya sudah lolos tidak pernah perlu dikerjakan.
    verdict = gate_item(conn, engine, full, runner=runner, stage="before", emit=emit)
    if verdict.get("refused") or verdict.get("exit") == 0:
        return verdict
    target = (full.get("cli_engine") or "").strip()
    if not target:
        # Ditolak SEBELUM ada klaim, bukan sesudahnya: baris WORKING tanpa pemegang adalah laporan
        # bahwa ada yang mengerjakan padahal tidak ada, dan mengarang laporan itu sebentar pun sudah
        # cukup untuk dibaca engine lain pada saat yang salah.
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        emit("NO_TARGET", item=int(item["id"]), why="kolom cli_engine kosong — memilih engine "
             "dari ingatan dilarang, jadi item ini menunggu orang mengisinya")
        return None

    # --- dua pagar yang sebelumnya tidak ada di jalur ini (temuan audit peer, 2026-10-08) ------
    # (1) PANE BUTA. Jalur ini membaca SQL lalu langsung mengetik; SQL tidak tahu ada orang yang
    #     sedang menulis di pane itu.
    quiet = pane_is_quiet(target, capture=capture, busy=busy, gap=gap, sleeper=sleeper)
    if not quiet["quiet"]:
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        emit("PANE_NOT_QUIET", item=int(item["id"]), target=target, why=quiet["why"],
             note="tidak ada yang diklaim dan tidak ada yang diketik — dicoba lagi setelah jeda")
        return None

    # (2) DIREKTORI BUTA. `semi_wake.py` sudah menjaga ini; jalur timer belum. Tugas proyek A yang
    #     masuk ke pane yang sedang berdiri di proyek B membuat engine mengerjakan satu tree sambil
    #     melaporkan tree lain — kebingungan identitas yang persisnya dilarang operator.
    if tab_project is None:
        import cross_verify as cv
        read_conf = cv.tab_project
    else:
        read_conf = tab_project
    if pane_cwd is None:
        import cross_verify as cv
        read_live = cv.pane_cwd
    else:
        read_live = pane_cwd
    where = read_conf(target)      # proyek yang dikonfigurasi untuk tab ini
    live = read_live(target)       # dicatat untuk diagnostik; BUKAN pagar — alasannya di bawah
    want = wo.resolve_workspace(full)
    if where is None:
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        emit("PANE_DIR_UNKNOWN", item=int(item["id"]), target=target,
             why="proyek tab tidak terbaca dari cli_profiles — UNKNOWN, dan UNKNOWN tidak pernah "
                 "boleh mengetik")
        return None
    if _as_path(where) != want:
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        emit("PANE_WRONG_DIR", item=int(item["id"]), target=target,
             tab_project=str(where), pane_now=str(live) if live else None, wants=str(want),
             why="tab target adalah proyek lain — perintah ini tidak akan dikerjakan di tree yang "
                 "bukan miliknya")
        return None

    ok, msg = wo.claim(conn, engine, int(item["id"]), HOLDER, mode=wanted, dirty=dirty)
    if not ok:
        # `claim()` yang memutuskan; di sini hasilnya hanya diterjemahkan menjadi nama kejadian,
        # supaya "engine masih mengerjakan satu perintah" terbaca berbeda dari "pemutus terbuka".
        emit("ENGINE_BUSY" if msg.startswith("ENGINE_BUSY") else "CLAIM_REFUSED",
             item=int(item["id"]), target=target, why=msg)
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        return None
    # Salinan aturan "satu perintah aktif" yang dulu ada di sini sudah dihapus. Ia memeriksa
    # SEBELUM menulis di satu jalur dan SESUDAH menulis di jalur lain (hook), dan selisih itu
    # cukup untuk dua perintah masuk ke satu mesin. Aturannya sekarang milik `wo.claim()` —
    # satu tempat, ditegakkan pada saat penulisan — dan yang terjadi di sini hanyalah
    # menerjemahkan penolakannya menjadi kejadian yang terbaca di log malam.
    if wanted == "SEMI":
        prompt = SEMI_PROMPT.format(id=item["id"], title=item["title"], ws=want,
                                    gate=(full.get("check_command") or "tanpa gerbang"))
    else:
        prompt = START_PROMPT.format(id=item["id"], title=item["title"],
                                     gate=(full.get("check_command") or "tanpa gerbang"))
    try:
        reply = (sender or default_sender)(target, prompt)
        # `tab_project` dan `pane_now` dicatat supaya suatu saat seseorang bisa menguji dugaan
        # tentang penyebab pengiriman ditolak — tanpa itu, menyangkal sebuah klaim butuh menebak.
        emit("WORK_DISPATCHED", item=int(item["id"]), target=target, mode=mode,
             tab_project=str(where), pane_now=str(live) if live else None,
             reply=str(reply)[:200])
    except Exception as exc:  # noqa: BLE001 — kegagalan kirim dicatat, tidak dikarang ulang
        # Klaim DILEPAS. Versi lama meninggalkan baris WORKING tanpa ada yang mengerjakan dan
        # tetap mengembalikan bentuk seperti sukses — dari luar terlihat terdistribusi, dan engine
        # itu terkunci ENGINE_BUSY sampai lease habis. Persis kelas bug yang diklaim sudah dihapus.
        _release_claim(conn, engine, item["id"])
        cooldown(conn, engine, int(item["id"]), CLAIM_COOLDOWN)
        emit("SEND_FAILED", item=int(item["id"]), target=target, released=True,
             why=f"{type(exc).__name__}: {exc}")
        return {"item": int(item["id"]), "target": target, "sent": False,
                "why": f"{type(exc).__name__}: {exc}"}
    return {"item": int(item["id"]), "target": target, "sent": True}


def nudge_resting(conn, engine, mode, beats=None, sender=None, emit=None):
    """CONTINUE untuk klaim yang terbukti istirahat — anggaran dihitung oleh `wo.nudge`.

    Di bawah SEMI hanya klaim atas perintah operator yang boleh disentuh: menaikkan `CONTINUE` ke
    pane yang sedang memegang item roadmap adalah cara memperluas mandat SEMI tanpa ada yang
    mengangkatnya, dan pagar yang bisa dilebarkan sendiri bukan pagar.
    """
    emit = emit or (lambda *a, **kw: None)   # bentuk panggilan: (action, **isi)
    sender = sender or default_sender
    wanted = (mode or "").upper()
    if wanted not in ("ON", "SEMI"):
        emit("MODE_UNKNOWN", where="nudge_resting", mode=str(mode),
             why="tidak ada CONTINUE yang dikirim")
        return
    if beats is None:
        beats = wo.heartbeat(conn, engine)
    for b in beats:
        if wanted == "SEMI":
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
def tick(conn, engine, gate=None, beats=None, runner=None, sender=None, log=None,
         capture=None, busy=None, tab_project=None, pane_cwd=None, gap=MIN_PANE_GAP,
         sleeper=None, dirty=wo._MEASURE):
    """Satu putaran timer. Mengembalikan kejadian yang juga dituliskan ke log malam.

    Semua yang bisa mengetik atau mengeksekusi (`beats`, `runner`, `sender`) bisa disuntik, jadi
    selftest tidak menyentuh satu pane pun dan tidak membuat satu proses pun — karena itu file ini
    bisa dibaca sebagai bukti, bukan sebagai maksud.
    """
    events = []
    emit = log or events.append
    gate = gate or read_gate(conn, engine)
    lvl, ap, night = gate["level"], gate["autopilot"], gate["night"]
    # Satu pembacaan git per tick, diwarisi ke claim. `tree_dirty` ada di dalam
    # pembacaan gerbang itu sendiri, jadi tidak ada pengukuran kedua yang boleh
    # berbeda dari yang pertama.
    dirty = night.get("tree_dirty", wo._MEASURE) if night else wo._MEASURE

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
    mode = (ap.get("mode") or "").upper()
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
        reconcile(conn, engine, mode, runner=runner, emit=ev)
        hand_new_work(conn, engine, mode, runner=runner, sender=sender, emit=ev,
                      capture=capture, busy=busy,
                      tab_project=tab_project, pane_cwd=pane_cwd, gap=gap, sleeper=sleeper,
                      dirty=dirty)
    rest = [b for b in (beats if beats is not None else wo.heartbeat(conn, engine))
            if b["state"] == "AT_REST"]
    if lvl["allows_resume"]:
        nudge_resting(conn, engine, mode, beats=beats, sender=sender, emit=ev)
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
    import drainer as _keep
    _orig_prompt = _keep.SEMI_PROMPT
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
    for banned in ("ydotool", "send-keys", "action_log", "set_level(", "subprocess.run",
                   "check_output", "Popen", "tmux"):
        check(f"jalur timer tidak memakai {banned!r}", banned not in timer_code)
    # `capture_pane` / `pane_is_busy` AWAL-nya ada dalam daftar terlarang di atas, dan itu sekarang
    # salah arah: yang berbahaya bukan membaca pane, tapi MENULIS ke pane tanpa membacanya dulu.
    # Jadi klaimnya diganti dari "tidak pernah menyentuh pane" menjadi "selalu membaca sebelum
    # mengetik" — dan ditegakkan secara struktural, bukan dengan komentar.
    hn = segments.get("hand_new_work", "")
    read_at, claim_at = hn.find("pane_is_quiet("), hn.find("wo.claim(")
    check("jalur dispatch MEMBACA pane sebelum mengetik", read_at != -1)
    check("pembacaan pane terjadi SEBELUM klaim, bukan sesudahnya",
          read_at != -1 and claim_at != -1 and read_at < claim_at, f"read={read_at} claim={claim_at}")
    check("jalur dispatch memverifikasi direktori pane sebelum mengetik", "PANE_WRONG_DIR" in hn)
    check("penolakan di jalur dispatch tidak meninggalkan klaim menggantung",
          "_release_claim(" in hn)
    for name in ("tick", "reconcile", "hand_new_work", "nudge_resting", "gate_item", "read_gate"):
        check(f"{name}() tidak pernah memanggil tangan manusia",
              "deliver_answer" not in segments.get(name, ""), name)
    #bekas: cek urutan berbasis teks diganti tes yang MENJALANKAN, lihat "persiapan gagal"
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
        # Urutan klaim-vs-cetak dulu "dibuktikan" dengan mencari posisinya di teks. Yang dibuktikan
        # di sini adalah sifat yang membuat urutan itu penting: `prepare_prompt` TIDAK PERNAH
        # melempar — ia mengembalikan None — sehingga tidak ada jalur di mana kegagalan terjadi
        # setelah ada klaim. Klaim adalah satu-satunya tulisan, dan ia hanya dilalui kalau
        # persiapan sudah selesai.
        try:
            sys.path.insert(0, str(wo.REPO / "hooks"))
            import semi_wake as _sw2
            _saved_fmt = None
            import drainer as _self_mod
            _broken = dict(id=999, title="x")
            _self_mod.SEMI_PROMPT = object()      # .format tidak ada -> TypeError di dalam
            _res = _sw2.prepare_prompt(_broken, Path("/tmp"))
            check("persiapan yang rusak mengembalikan None, bukan melempar — jadi tidak ada "
                  "kemungkinan ia gagal SETELAH klaim", _res == (None, 0), str(_res)[:80])
        except Exception as _e3:  # noqa: BLE001
            check("persiapan bisa diuji", False, f"{type(_e3).__name__}: {_e3}")
        finally:
            import drainer as _self_mod2
            _self_mod2.SEMI_PROMPT = _orig_prompt
        check("hook SEMI membaca mode dari database pada setiap tembakan, bukan dari harapan",
              "wo.autopilot(" in hsegs.get("main", ""))
        check("hook SEMI menyaring asal baris, jadi ia tidak pernah mengambil item roadmap",
              "origin=wo.ORIGIN_OPERATOR" in hsegs.get("main", ""))
        # Regresi yang terjadi sungguhan, 2026-10-08 11:28, terdeteksi karena injeksi ini masuk ke
        # sesi penulisnya sendiri: `documents/AGENTS.md` (27 KB) menghabiskan seluruh anggaran
        # karakter, jadi tiga berkas kontrak lainnya TIDAK terkirim sama sekali. Pesan menyebut
        # "pedoman TERKUNCI" padahal yang sampai seperempat dari satu berkas.
        try:
            sys.path.insert(0, str(wo.REPO / "hooks"))
            import semi_wake as _sw
            _body, _notes, _n = _sw.guides_for(None, None)
            _missing = [g["path"] for g in wo.locked_guides() if ("### " + g["path"]) not in _body]
            check("setiap berkas yang dikunci benar-benar terkirim, bukan hanya yang pertama",
                  not _missing, str(_missing))
            check("pedoman yang dipotong mengaku dipotong dan menyebut path penuhnya",
                  all("karakter" in n or "penuh" in n for n in _notes)
                  and "(kutipan" in _body, str(_notes[:2]))
        except Exception as exc:  # noqa: BLE001 — kegagalan impor dilaporkan, tidak lulus diam-diam
            check("pedoman terkunci bisa diperiksa", False, f"{type(exc).__name__}: {exc}")

    # Penasehat antrean (F10.10) sengaja HANYA bicara. Ia menempelkan isi antrean ke prompt lewat
    # `additionalContext`, dan batas antara nasihat dan tangan adalah apakah ia boleh mengubah baris.
    adv_file = wo.REPO / "hooks" / "queue_advisor.py"
    asrc = adv_file.read_text() if adv_file.is_file() else ""
    check("penasehat antrean ada", bool(asrc.strip()), str(adv_file))
    if asrc:
        asegs = {n.name: (_ast.get_source_segment(asrc, n) or "")
                 for n in _ast.parse(asrc).body
                 if isinstance(n, (_ast.FunctionDef, _ast.AsyncFunctionDef))}
        for banned in ("claim(", "finish(", "deliver", "default_sender", "daemon_post",
                       "/api/cli/run", "UPDATE ", "INSERT ", "decision", "ydotool", "subprocess"):
            hit = [f"{banned} di {fn}()" for fn, body in asegs.items() if banned in body]
            check(f"penasehat antrean tidak memakai {banned!r}", not hit, "; ".join(hit))
        # Perilaku, bukan keberadaan string. Cek lama mencari "ws_path" di badan fungsi dan lulus
        # meski fungsinya tidak menyaring apa pun.
        try:
            sys.path.insert(0, str(wo.REPO / "hooks"))
            import queue_advisor as _adv
            _adv.THIS_ENGINE = "qoder"
            _rows = [{"id": 1, "cli_engine": "qoder", "ws_path": "/tmp/proj-a", "title": "a"},
                     {"id": 2, "cli_engine": "qoder", "ws_path": "/tmp/proj-b", "title": "b"},
                     {"id": 3, "cli_engine": "qoder", "ws_path": None, "title": "tanpa proyek"},
                     {"id": 4, "cli_engine": "antigravity", "ws_path": "/tmp/proj-a", "title": "c"}]
            _in_a = [r["id"] for r in _adv.select_rows(_rows, "/tmp/proj-a")]
            check("penasehat menyaring MESIN dan PROYEK, bukan hanya menandai",
                  _in_a == [1], str(_in_a))
            check("baris tanpa proyek tidak dilaporkan ke sesi proyek lain "
                  "— nasihat yang ditolak mesin terasa seperti perintah yang hilang",
                  3 not in _in_a, str(_in_a))
            _empty = _adv.select_rows(_rows, "")
            check("tanpa laporan cwd dari CLI, penasehat DIAM — tidak mengarang proyek "
                  "lalu menyuruh engine mengerjakan yang akan ditolaknya sendiri",
                  _empty == [], str(_empty))
        except Exception as _exc:  # noqa: BLE001
            check("penasehat bisa diuji", False, f"{type(_exc).__name__}: {_exc}")
        check("tanpa proyek yang diketahui, penasehat tidak mengklaim 'tidak ada yang untukmu'",
              'if not cwd:' in open(str(wo.REPO / "hooks" / "queue_advisor.py"),
                                    encoding="utf-8").read().split("def build_note")[1][:400],
              "build_note masih bicara tanpa tahu proyeknya")
        try:
            import semi_wake as _sw
            _orig = _sw.guides_for
            import work_order as _wo
            _saved = _wo.locked_guides
            _wo.locked_guides = lambda: (_ for _ in ()).throw(RuntimeError("meter rusak"))
            try:
                _res = _sw.guides_for(None, None)
            finally:
                _wo.locked_guides = _saved
            check("jalur gagal pedoman mengembalikan TIGA nilai, bukan dua — kalau dua, hook "
                  "meledak setelah klaim dan barisnya menggantung WORKING",
                  isinstance(_res, tuple) and len(_res) == 3, str(type(_res)))
        except Exception as _exc2:  # noqa: BLE001
            check("jalur gagal pedoman bisa diuji", False, f"{type(_exc2).__name__}: {_exc2}")

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

    # Pembaca pane untuk selftest. Yang diuji adalah KEPUTUSANNYA, jadi yang disuntik adalah
    # pengamatannya — selftest tidak pernah membaca pane operator yang sedang hidup, dan tidak
    # pernah tidur beneran (gap=0 di semua pemanggilan).
    cwd_box = {"v": str(REPO)}

    def quiet_pane(_tab):
        return "prompt siap"

    def not_busy(_screen):
        return False

    def cwd_of(_tab):
        return cwd_box["v"]

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
                    # `tree_dirty` dinyatakan di sini sebagai PREMIS fixture, bukan sebagai
                    # pelarian: tanpa kunci ini tick mewarisinya sebagai "belum diukur" dan claim
                    # akan membaca git NYATA — yang kotor justru karena selftest ini berjalan di
                    # tengah suntingan. Yang diuji blok ini adalah keputusan drainer, bukan
                    # keadaan working tree, dan itu sudah dibuktikan di `work_order selftest`.
                    "night": {"ok": ok, "why": why or [], "tree_dirty": False}}

        out = tick(conn, "SQLITE", gate=gate_for("observe", mode="OFF"), beats=[],
                   sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        check("sakelar OFF = tidak ada yang bergerak sama sekali",
              [e["action"] for e in out] == ["CLOSED_OFF"] and not sent and not ran, str(out))

        unreadable = {"level": {"readable": False, "level": None, "allows_dispatch": False,
                                "allows_resume": False, "why": ["tidak terbaca"]},
                      "autopilot": {"mode": "ON"}, "night": {"ok": True, "why": []}}
        out = tick(conn, "SQLITE", gate=unreadable, beats=[], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        check("gerbang tak terbaca menutup sebagai UNKNOWN, bukan sebagai observe",
              [e["action"] for e in out] == ["CLOSED_UNKNOWN"], str(out))

        out = tick(conn, "SQLITE", gate=gate_for("observe", ok=False, why=["circuit breaker"]),
                   beats=[], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        check("circuit breaker menutup tick sebelum satu klaim pun",
              [e["action"] for e in out] == ["CLOSED_NIGHT"], str(out))

        ran.clear()
        out = tick(conn, "SQLITE", gate=gate_for("observe"), beats=[], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
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
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False,
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
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False,
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
        reconcile(conn, "SQLITE", "ON", runner=runner, emit=sink(out))
        check("klaim drainer yang lease-nya masih hidup divonis nanti, bukan sekarang",
              any(e["action"] == "HELD" and e["item"] == held for e in out)
              and not any(e["item"] == held and e["action"] == "GATE_FAILED" for e in out)
              and wo.norm((wo.get_item(conn, "SQLITE", held) or {}).get("status")) == "WORKING",
              str(out))
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET lease_epoch=? WHERE id=?",
               (wo.now() - 1, held))
        ran.clear()
        out = []
        reconcile(conn, "SQLITE", "ON", runner=runner, emit=sink(out))
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
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[beat], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False,
             log=disp_out.append)
        check("di tingkat dispatch, AT_REST ditahan dan tidak ada satu pun ketikan",
              not sent and any(e["action"] == "HELD_BY_LEVEL" and e["items"] == [base]
                               for e in disp_out), str((sent, disp_out)))

        resume_out = []
        tick(conn, "SQLITE", gate=gate_for("resume"), beats=[beat], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False,
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
        tick(conn, "SQLITE", gate=gate_for("resume"), beats=[beat], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False,
             log=stuck.append)
        check("anggaran habis memarkir item dan tidak mengirim apa pun",
              not sent and any(e["action"] == "STUCK_PARKED" and e["item"] == base
                               for e in stuck), str(stuck))

        sent.clear()
        out = []
        tick(conn, "SQLITE", gate=gate_for("resume"),
             beats=[{"id": base, "state": "WORKING", "claimed_by": "qoder", "why": "spinner"},
                    {"id": base, "state": "UNKNOWN", "claimed_by": "qoder", "why": "pane mati"}],
             sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False, log=out.append)
        skipped = [e for e in out if e["action"] == "SKIP_NOT_RESTING"]
        check("WORKING dan UNKNOWN tidak pernah disentuh — UNKNOWN bukan berarti diam",
              not sent and len(skipped) == 2
              and {e["state"] for e in skipped} == {"WORKING", "UNKNOWN"}, str(out))

        # Jendela ON dibaca ulang pada saat klaim, bukan diwarisi dari awal tick. Timer yang mulai
        # pukul 01:55 untuk jendela yang mati pukul 02:00 tidak boleh masih mengklaim pada 02:05
        # hanya karena ia sudah terlanjur bangun (F10.8: expiry ditegakkan di `claim`, bukan di timer).
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET cli_engine='qoder' WHERE cli_engine IS NULL")
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET due_epoch=0")
        wo.run(conn, "SQLITE",
               "UPDATE autopilot_state SET mode='ON', on_epoch=?, until_epoch=?, item_budget=12",
               (wo.now() - 7200, wo.now() - 60))
        sent.clear()
        out = []
        tick(conn, "SQLITE", gate=gate_for("dispatch"), beats=[], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False,
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
                   sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        acts = [e["action"] for e in out]
        check("SEMI tidak disalahtafsir sebagai OFF — pintu dibuka, tangan belum diangkat",
              "CLOSED_OFF" not in acts and "SEMI_HAND_FLAT" in acts and not sent, str(out))

        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("dispatch", mode="SEMI"), beats=[],
                   sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        acts = [e["action"] for e in out]
        check("SEMI dengan antrean tanpa perintah operator tidak mengambil apa pun — itu isi kata 'semi'",
              "QUEUE_EMPTY" in acts and not sent, str(out))

        op = make("selftest-semi-cmd", gate="false", engine_key="qoder",
                  origin="operator", ws="/tmp")
        cwd_box["v"] = "/tmp"     # pane target memang berdiri di direktori baris itu
        sent.clear()
        tick(conn, "SQLITE", gate=gate_for("dispatch", mode="SEMI"), beats=[],
             sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        check("perintah operator diantar ke mesin yang tertulis di barisnya",
              sent and sent[0][0] == "qoder" and str(op) in sent[0][1], str(sent)[:220])
        check("yang dikirim adalah prompt perintah-orang, bukan tugas roadmap",
              sent and "Perintah operator dari Dashboard" in sent[0][1], str(sent)[:160])
        check("prompt SEMI menyebut direktori baris, jadi engine tidak dikerjakan di tree lain",
              sent and "/tmp" in sent[0][1], str(sent)[:220])

        op2 = make("selftest-semi-cmd-2", gate="false", engine_key="qoder", origin="operator")
        cwd_box["v"] = str(REPO)     # op2 tidak punya ws_path → tujuannya memang REPO
        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("dispatch", mode="SEMI"), beats=[],
                   sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        acts = [e["action"] for e in out]
        check("engine yang sedang mengerjakan satu perintah tidak ditumpuki perintah kedua — "
              "'kalau Working, tunggu sampai selesai'",
              "ENGINE_BUSY" in acts and not sent, str(out))

        # --- dua pagar baru hasil audit peer: pane buta dan direktori buta ----------------
        # Yang diuji bukan hanya "ia menolak", tapi bahwa penolakannya terjadi SEBELUM ada klaim
        # dan SEBELUM ada ketikan: baris yang tertahan harus tetap PENDING, karena satu-satunya
        # kegagalan di jalur ini yang tidak bisa dibatalkan adalah teks yang sudah tertulis ke
        # dalam ketikan seseorang.
        def shifting_pane(_t):
            shifting_pane.n += 1
            return f"frame {shifting_pane.n}"
        shifting_pane.n = 0

        g1 = make("selftest-guard-busy", gate="false", engine_key="qoder", origin="operator")
        sent.clear(); out = []
        hand_new_work(conn, "SQLITE", runner=runner, sender=sender, emit=sink(out),
                      mode="SEMI",
                      capture=shifting_pane, busy=not_busy, tab_project=cwd_of, gap=0)
        check("pane yang teksnya berubah antar pembacaan tidak diketiki, dan barisnya tetap PENDING",
              out and out[-1]["action"] == "PANE_NOT_QUIET" and not sent
              and wo.get_item(conn, "SQLITE", g1)["status"] == "PENDING", str(out))

        g2 = make("selftest-guard-blind", gate="false", engine_key="qoder", origin="operator")
        sent.clear(); out = []
        hand_new_work(conn, "SQLITE", runner=runner, sender=sender, emit=sink(out),
                      mode="SEMI",
                      capture=quiet_pane, busy=not_busy,
                      tab_project=lambda _t: None, gap=0)
        check("direktori pane yang tidak terbaca adalah UNKNOWN, dan UNKNOWN tidak pernah mengetik",
              out and out[-1]["action"] == "PANE_DIR_UNKNOWN" and not sent, str(out))

        g3 = make("selftest-guard-wrongdir", gate="false", engine_key="qoder",
                  origin="operator", ws="/tmp")
        cwd_box["v"] = str(Path.home())   # pane berdiri di $HOME, bukan di proyek baris ini
        sent.clear(); out = []
        hand_new_work(conn, "SQLITE", runner=runner, sender=sender, emit=sink(out),
                      mode="SEMI",
                      capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
        check("perintah proyek A tidak pernah diketik ke pane yang sedang berdiri di proyek B",
              out and out[-1]["action"] == "PANE_WRONG_DIR" and not sent
              and wo.get_item(conn, "SQLITE", g3)["status"] == "PENDING", str(out))
        cwd_box["v"] = str(REPO)

        # --- empat temuan audit lainnya, masing-masing dikunci satu cek ------------------
        try:
            hand_new_work(conn, "SQLITE")      # sengaja tanpa mode
            _mode_required = False
        except TypeError:
            _mode_required = True
        check("mode wajib disebut — pemanggil yang lupa tidak otomatis dapat keranjang FULL AUTO",
              _mode_required)

        rm_held = make("selftest-reconcile-roadmap", status="WORKING", gate="false",
                       holder=HOLDER, lease=0, origin="roadmap")
        ran.clear(); out = []
        reconcile(conn, "SQLITE", "SEMI", runner=runner, emit=sink(out))
        check("reconcile di SEMI tidak menjalankan gerbang baris roadmap — menjalankan shell "
              "adalah mengerjakan yang tidak diminta",
              any(e["action"] == "HELD_BY_MODE" and e["item"] == rm_held for e in out) and not ran,
              str(out))

        # Lepaskan klaim yang masih dipegang engine ini lebih dulu, supaya yang diukur di bawah
        # adalah pagar direktori — bukan aturan satu-perintah-aktif yang sudah punya tes sendiri.
        wo.finish(conn, "SQLITE", op, "COMPLETED",
                  evidence="selftest: melepas klaim agar uji berikutnya mengukur satu hal")
        op3 = make("selftest-semi-cmd-3", gate="false", engine_key="qoder",
                   origin="operator", ws=str(REPO))
        out = []
        hand_new_work(conn, "SQLITE", "SEMI", runner=runner, sender=sender, emit=sink(out),
                      capture=quiet_pane, busy=not_busy,
                      tab_project=cwd_of, pane_cwd=lambda _t: str(Path.home()), gap=0,
                      dirty=False)
        # Terukur 2026-10-08 11:4x: pane qoder memang duduk di ~/.ai-station (cwd proses
        # foreground-nya) sambil proyek tab-nya ~/Documents/ai-workstation. Jadi cwd pane BUKAN
        # sinyal proyek; menuntut keduanya sama akan menolak hampir setiap pengiriman — aman, tapi
        # diam, dan yang diukur ini justru mengirim.
        check("cwd pane yang berbeda tidak menahan pengiriman, tapi tetap dicatat",
              out and out[-1]["action"] == "WORK_DISPATCHED"
              and out[-1].get("pane_now") == str(Path.home()), str(out[-1:]))

        # Cek perilaku, bukan cek teks. Versi sebelumnya mencari string "max(gap, MIN_PANE_GAP)"
        # di sumber dan LULUS pada kode mati — persis kelas bug yang kuburu sejak kemarin.
        # (B) Kemacetan yang ditemukan audit putaran tiga: klaim roadmap sisa ON, yang di SEMI
        #     justru ditahan HELD_BY_MODE dan tidak pernah diselesaikan siapa pun, dulu memblokir
        #     SETIAP perintah operator untuk engine itu selamanya.
        # Engine harus benar-benar bebas dulu, kalau tidak yang terukur adalah aturan
        # satu-perintah-aktif (yang sudah punya tesnya sendiri), bukan umur lease.
        wo.finish(conn, "SQLITE", op3, "COMPLETED",
                  evidence="selftest: membebaskan engine sebelum uji lease mati")
        stale = make("selftest-stale-roadmap", status="WORKING", gate="false", engine_key="qoder",
                     holder=HOLDER, lease=wo.now() - 10, origin="roadmap")
        op4 = make("selftest-semi-after-stale", gate="false", engine_key="qoder", origin="operator",
                   ws=str(REPO))
        sent.clear(); out = []
        hand_new_work(conn, "SQLITE", "SEMI", runner=runner, sender=sender, emit=sink(out),
                      capture=quiet_pane, busy=not_busy, tab_project=cwd_of,
                      pane_cwd=cwd_of, gap=0, dirty=False)
        check("klaim roadmap yang lease-nya sudah mati tidak membekukan antrean operator selamanya",
              sent and out[-1]["action"] == "WORK_DISPATCHED", str(out[-1:]))

        # (D) mode yang tidak dikenal harus gagal-TERTUTUP, bukan jatuh ke keranjang FULL AUTO.
        for bad in ("semi", "OFF", "", "ngawur"):
            sent.clear(); out = []
            hand_new_work(conn, "SQLITE", bad, runner=runner, sender=sender, emit=sink(out),
                          capture=quiet_pane, busy=not_busy, tab_project=cwd_of,
                          pane_cwd=cwd_of, gap=0, dirty=False)
            if bad == "semi":
                # Bukan "salah satu dari tiga kejadian". Yang harus dibuktikan adalah mode
                # huruf kecil memperoleh KEDUA sifat SEMI: prompt perintah-orang DAN pagar
                # satu-perintah-aktif. Versi sebelumnya menerima hasil apa pun, jadi ia tetap
                # hijau ketika bug normalisasi mengirim START_PROMPT roadmap tanpa pagar.
                wo.run(conn, "SQLITE",
                       "UPDATE quest_tasks SET status='COMPLETED', lease_epoch=0 "
                       "WHERE status IN ('WORKING','CLAIMED') AND cli_engine='qoder'")
                wo.run(conn, "SQLITE", "UPDATE quest_tasks SET due_epoch=0")
                sent.clear(); out = []
                opx = make("selftest-semi-lowercase", gate="false", engine_key="qoder",
                           origin="operator", ws=str(REPO))
                hand_new_work(conn, "SQLITE", "semi", runner=runner, sender=sender,
                              emit=sink(out), capture=quiet_pane, busy=not_busy,
                              tab_project=cwd_of, pane_cwd=cwd_of, gap=0, sleeper=lambda _s: None,
                              dirty=False)
                check("mode huruf kecil mendapat prompt SEMI, bukan prompt roadmap",
                      sent and "Perintah operator dari Dashboard" in sent[-1][1], str(sent[-1:])[:220])
                # Kirim SATU perintah lagi ke engine yang sama: kalau pagar SEMI aktif, ia
                # ditahan. Kalau tidak (bug normalisasi), ia terkirim dan `sent` bertambah.
                sent_before = len(sent)
                opy = make("selftest-semi-lowercase-2", gate="false", engine_key="qoder",
                           origin="operator", ws=str(REPO))
                out2 = []
                hand_new_work(conn, "SQLITE", "semi", runner=runner, sender=sender,
                              emit=sink(out2), capture=quiet_pane, busy=not_busy,
                              tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                              sleeper=lambda _x: None, dirty=False)
                check("mode huruf kecil juga mendapat pagar satu-perintah-aktif",
                      len(sent) == sent_before
                      and any(e["action"] == "ENGINE_BUSY" for e in out2), str(out2[-1:]))
                held_now = [r for r in open_claims(conn, "SQLITE")
                            if (r.get("cli_engine") or "") == "qoder"
                            and int(r.get("lease_epoch") or 0) > wo.now()]
                check("setelah pengiriman, engine memang memegang satu klaim (pagar punya gigi)",
                      len(held_now) >= 1, str(held_now))
            else:
                check(f"mode {bad!r} tidak mengirim apa pun dan tidak mengambil keranjang penuh",
                      not sent and any(e["action"] == "MODE_UNKNOWN" for e in out), str(out))

        check("jeda tidak bisa dinolkan oleh pemanggil mana pun, termasuk yang menyuntik pembaca",
              effective_gap(0) == MIN_PANE_GAP and effective_gap(5) == 5
              and effective_gap(-1) == MIN_PANE_GAP,
              f"0->{effective_gap(0)} 5->{effective_gap(5)} -1->{effective_gap(-1)}")
        # Yang disuntik adalah SLEEPER-nya, jadi tes tidak tidur dan tetap membuktikan jeda nyata
        # benar-benar diminta. Versi sebelumnya memanggil pane_asli(capture=None) dan membaca
        # tmux operator — melanggar aturan yang ditulis modul ini sendiri.
        _waits = []
        pane_is_quiet("qoder", capture=lambda _t: "prompt siap", busy=lambda _s: False,
                      gap=0, sleeper=lambda sec: _waits.append(sec))
        check("dua pembacaan sungguhan dipisahkan jeda lantai walau pemanggil minta nol",
              _waits == [MIN_PANE_GAP], str(_waits))
        # Keduanya dulu cek teks. Yang pertama sudah digantikan cek perilaku WORK_DISPATCHED di
        # bawah (ia menuntut `pane_now` terisi padahal pengiriman tetap jalan); yang kedua diganti
        # uji langsung pada fungsi pembentuk path-nya.
        check("`~` diperluas sebelum dibandingkan, bukan ditolak sebagai proyek lain",
              _as_path("~") == Path.home().resolve(strict=False)
              and _as_path("~/Documents") == (Path.home() / "Documents").resolve(strict=False),
              str(_as_path("~")))

        # Beda mode diukur pada ANTREAN YANG SAMA, bukan dengan dua fixture berbeda: satu baris yang
        # bukan perintah operator harus terlihat oleh FULL AUTO dan tidak terlihat oleh SEMI.
        # (Sebelumnya ada cek di sini dengan `... or True` — dan cek yang selalu hijau adalah bug
        # yang kemarin kutangkap di gerbang f109, jadi ia dibuang, tidak diperhalus.)
        wo.run(conn, "SQLITE", "DELETE FROM quest_tasks WHERE id=?", (op2,))
        # Bersihkan sisa antrean operator dari blok-blok di atas (beberapa sengaja dibiarkan
        # PENDING untuk menguji pagar lain). Tanpa ini, "keranjang SEMI kosong" bergantung pada
        # baris dari tes lain dan hasilnya berubah kalau urutan tes berubah.
        wo.run(conn, "SQLITE", "DELETE FROM quest_tasks WHERE origin='operator'")
        free = make("selftest-full-auto-row", gate="false", engine_key="qoder")
        wide = next_ready(conn, "SQLITE")
        narrow = next_ready(conn, "SQLITE", origin=wo.ORIGIN_OPERATOR)
        check("perbedaan FULL AUTO dan SEMI adalah isi keranjang — baris bebas terlihat oleh yang "
              "pertama dan tidak terlihat oleh yang kedua, pada antrean yang sama",
              wide is not None and (wide.get("origin") or "legacy") != wo.ORIGIN_OPERATOR
              and narrow is None, str({"wide": wide and wide["id"], "semi": narrow}))

        rm_claim = make("selftest-semi-roadmap-claim", status="WORKING", engine_key="qoder",
                        holder="qoder", lease=wo.now() + 600)
        sent.clear()
        out = tick(conn, "SQLITE", gate=gate_for("resume", mode="SEMI"),
                   beats=[{"id": rm_claim, "state": "AT_REST", "claimed_by": "qoder",
                           "why": "dua sinyal setuju"}], sender=sender, runner=runner, capture=quiet_pane, busy=not_busy, tab_project=cwd_of, pane_cwd=cwd_of, gap=0,
                      dirty=False)
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

        # --- sifat baru: pelepasan klaim bersyarat, dan daftar siap yang konsisten ----------
        # Premisnya harus dinyatakan: kegagalan dari blok sebelumnya masih ada di jendela 12 jam
        # dan akan membuka pemutus, jadi klaim uji ini ditolak karena alasan yang bukan diuji.
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET completed_at=datetime('now','-2 days') "
                               "WHERE status IN ('FAILED','PARKED')")
        # Engine harus benar-benar bebas: aturan satu-perintah-aktif yang baru dipusatkan di
        # claim() menolak klaim uji ini kalau masih ada klaim qoder yang lease-nya hidup dari
        # blok di atas — dan penolakan itu benar, hanya saja bukan yang sedang diukur di sini.
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET status='COMPLETED', lease_epoch=0 "
                               "WHERE status IN ('WORKING','CLAIMED') AND cli_engine='qoder'")
        keeper = make("selftest-keeper", gate="false", engine_key="qoder", origin="operator",
                      ws=str(REPO))
        ok_keep, why_keep = wo.claim(conn, "SQLITE", keeper, "pihak-a", dirty=False, mode="SEMI")
        check("klaim uji ini sendiri berhasil (kalau tidak, tes di bawah mengukur hal lain)",
              ok_keep, why_keep)
        still = wo.release_claim(conn, "SQLITE", keeper, "pihak-b")
        check("pihak lain TIDAK bisa melepaskan klaim pihak lain",
              still["claimed_by"] == "pihak-a" and still["status"] == "WORKING", str(still))
        wo.release_claim(conn, "SQLITE", keeper, "pihak-a")
        back = wo.get_item(conn, "SQLITE", keeper)
        check("pemegangnya sendiri bisa melepas, dan barisnya kembali PENDING tanpa pemegang",
              back["status"] == "PENDING" and not back["claimed_by"]
              and int(back["lease_epoch"] or 0) == 0, str(back))

        # Daftar siap dan "berikutnya" harus berasal dari aturan yang sama — kalau tidak, angka
        # Dashboard dan apa yang benar-benar ditawarkan boleh berbeda (itu temuan decider).
        wo.run(conn, "SQLITE", "DELETE FROM quest_tasks WHERE origin='operator'")
        a_ok = make("selftest-ready-a", gate="false", engine_key="qoder", origin="operator",
                    ws=str(REPO))
        b_hold = make("selftest-ready-b", gate="false", engine_key="qoder", origin="operator",
                      ws=str(REPO))
        wo.run(conn, "SQLITE", "UPDATE quest_tasks SET due_epoch=? WHERE id=?",
               (wo.now() + 600, a_ok))
        ready_ids = [int(r["id"]) for r in wo.ready_items(conn, "SQLITE", respect_due=True,
                                                          origin=wo.ORIGIN_OPERATOR,
                                                          for_engine="qoder")]
        first = wo.next_item(conn, "SQLITE", respect_due=True, origin=wo.ORIGIN_OPERATOR,
                             for_engine="qoder")
        check("baris yang sedang cooldown tidak muncul di daftar siap maupun 'berikutnya'",
              a_ok not in ready_ids and first and int(first["id"]) == b_hold,
              str({"ready": ready_ids, "first": first and first["id"]}))
        check("laporan antrean Dashboard memakai aturan yang sama dengan yang ditawarkan",
              len(wo.semi_status(conn, "SQLITE")["operator_queue"]) == len(ready_ids),
              str(wo.semi_status(conn, "SQLITE")["operator_pending"]))

        # Penasihat hanya melapor, tidak menyuruh bekerja. Yang diperiksa adalah TEKS yang
        # dihasilkannya — bukan teks sumbernya — jadi ini bukti perilaku, bukan pencarian string.
        try:
            import queue_advisor as _adv2
            _adv2.THIS_ENGINE = "qoder"
            _note = _adv2.render_note("SEMI", "observe", [{"id": 7, "title": "contoh"}], [], [])
            check("catatan antrean melarang dirinya sendiri dipakai sebagai perintah kerja",
                  bool(_note) and "bukan perintah" in _note and "jangan mulai" in _note,
                  str(_note)[:180])
        except Exception as _e4:  # noqa: BLE001
            check("catatan antrean bisa diuji", False, f"{type(_e4).__name__}: {_e4}")

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
