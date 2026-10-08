"""Semi Autopilot — tangan yang bukan tangan.

Satu-satunya tujuan berkas ini: menghilangkan kata DIAM. Ketika Qoder berhenti karena item
`finish`, perintah yang operator tulis sendiri ke PostgreSQL tidak boleh menunggu engine berikutnya
sadar. Hook `Stop` adalah tempat yang tepat untuk itu karena ia dipanggil pada batas giliran itu
juga — ia tidak mengetik ke pane siapa pun, ia hanya menolak sebuah penghentian di sesi yang
sudah berjalan.

Mengapa ini bukan `arm` yang menyamar
-------------------------------------
Tangga `level` menjaga tangan yang menulis ke pane MILIK ORANG LAIN (`default_sender` /
`/api/cli/run`) dan karena itu `arm` hanya bisa dinaikkan dari `/dev/tty`. Jalur ini berbeda secara
struktural: ia hanya hidup di dalam sesi yang memanggilnya, nama mesin tujuan ditulis di berkas ini
dan harus sama dengan barisnya, dan yang menyalakannya adalah satu baris `mode='SEMI'` yang hanya
manusia yang bisa tulis lewat klik di Dashboard. Sakelar matinya tetap empat dan tidak satu pun
meminta kerja sama AI (invarian 6): `off` dari shell mana pun menutup jalur ini pada tembakan
berikutnya, karena yang dibaca setiap tembakan adalah database, bukan harapan.

Berhenti berlarut tidak mungkin secara konstruksi, bukan karena ada yang ingat menjaga: sebuah
blok HANYA boleh terjadi sebagai akibat dari klaim yang berhasil atas satu baris PENDING, dan baris
yang diklaim berhenti menjadi PENDING. Antrean kosong berarti tidak ada satu byte pun yang keluar.
Pemutus malam (`NIGHT_MAX_FAILURES`) dan aturan working-tree kotor (§6) ditegakkan oleh `claim()`
itu sendiri, jadi mode yang naik ke SEMI tidak punya jalur pintas ke keduanya.

Kesalahan apa pun membuat berkas ini diam dan keluar 0. Hook yang rusak tidak boleh mencegah
seseorang menutup pekerjaannya.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

TOOLS = Path.home() / ".ai-station" / "tools"
sys.path.insert(0, str(TOOLS))

# Mesin milik sesi yang memanggil hook ini. Bukan tebakan dan bukan hiasan: hook yang sama
# terdaftar di dua CLI berbeda, dan sebuah baris perintah hanya boleh diambil oleh mesin yang
# namanya tertulis padanya. Dibaca dari argv[1] supaya yang menentukan adalah pendaftarannya.
SESSION_ENGINE = (sys.argv[1] if len(sys.argv) > 1 else "qoder")
# Kata keputusannya berbeda di tiap CLI dan keduanya terdokumentasi di tempatnya sendiri:
# qodercli menolak penghentian dengan `decision:"block"`, Antigravity dengan `"continue"`.
# Salah satu kata ini membuat hook berjalan, mengembalikan sesuatu, dan tidak ada yang lanjut —
# kegagalan yang dari luar terlihat sukses.
BLOCK_DECISION = {"qoder": "block", "antigravity": "continue"}.get(SESSION_ENGINE, "block")
NIGHT_LOG = Path.home() / ".ai-station" / "logs" / "semi_wake.jsonl"
# Plafon ini TIDAK sama dengan milik auto_restore.py (3200) dan tidak lagi mengaku begitu:
# dinaikkan karena anggaran dibagi per berkas, dan dengan 3200 setiap berkas kontrak hanya
# kebagian ~800 karakter.
GUIDE_MAX_CHARS = 9000
GUIDE_MIN_PER_FILE = 900        # walau plafon habis dibagi rata, tiap berkas dapat setidaknya ini


def session_cwd(data: dict):
    """Direktori proyek sesi ini, dari bentuk payload CLI yang memanggil.

    qodercli mengirim `cwd` (satu string); Antigravity mengirim `workspacePaths` (array), dan itu
    justru lebih benar — yang menentukan "proyek siapa yang dikerjakan" adalah workspace yang
    dilaporkan agent sendiri, bukan cwd shell tempat ia diluncurkan.
    """
    raw = data.get("cwd")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    paths = data.get("workspacePaths")
    if isinstance(paths, list) and paths and isinstance(paths[0], str) and paths[0].strip():
        return paths[0].strip()
    return None


def is_really_idle(data: dict) -> bool:
    """`fullyIdle` milik Antigravity: ia MELAPORKAN dirinya, bukan disimpulkan dari teks pane.

    Kalau field itu tidak ada (qodercli), kembali ke aturan lama: hook ini hanya dipanggil pada
    batas giliran, jadi penghentian yang sedang terjadi memang akhir pekerjaannya. Kalau ada dan
    False, masih ada tugas latar berjalan — dan menyuntik perintah di sana berarti melanggar
    "kalau Working, tunggu sampai selesai" yang diminta operator.
    """
    flag = data.get("fullyIdle")
    return True if flag is None else bool(flag)


def log(action: str, **rest) -> None:
    """Jejak tembakan — termasuk yang tidak melakukan apa pun.

    Sebuah pemicu yang diam tanpa catatan tidak bisa dibedakan dari pemicu yang tidak pernah
    dipanggil, dan itu berarti operator menyangka perintahnya hilang padahal mesin menolaknya.

    Parameter pertama bernama `action`, bukan `why`, dan itu bukan selera: panggilan seperti
    `log("X", why=msg)` pernah terbukti melempar TypeError karena namanya menabrak parameter
    posisional — dan tabrakan itu terjadi Justru di cabang penanganan kesalahan, sehingga hook
    keluar non-zero pada peristiwa Stop. Sebuah pemicu yang bisa crash saat melaporkan penolakan
    lebih buruk daripada pemicu yang diam.
    """
    try:
        NIGHT_LOG.parent.mkdir(parents=True, exist_ok=True)
        row = {"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S%z"),
               "action": action, **rest}
        with NIGHT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 — mencatat kegagalan tidak boleh ikut menggagalkan
        pass


def read_input() -> dict:
    raw = (sys.stdin.read() or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}


def guides_for(conn, engine) -> tuple[str, list[str], int]:
    """Pedoman yang TERKUNCI — dibaca dari kontrak, bukan dari pilihan layar.

    Operator mengunci daftar ini 2026-10-08 11:05. Hook yang membaca pilihan dari tabel `workspaces`
    akan patuh pada siapa pun yang bisa menulis ke tabel itu, dan itu membuat kontrak bisa
    dimatikan dari UI yang seharusnya iaikatinya.
    """
    try:
        import work_order as wo
        wanted = wo.locked_guides()
    except Exception as exc:  # noqa: BLE001
        # Tiga nilai, bukan dua. Pemanggilnya `body, notes, nguides = guides_for(...)`, dan
        # cabang gagal ini terjadi SETELAH baris diklaim — mengembalikan dua nilai di sana
        # membuat hook melempar ValueError setelah klaim sukses: baris jadi WORKING dan tidak
        # ada yang menyuntik, kegagalan yang tidak terlihat dari mana pun.
        return "", [f"daftar terkunci tidak terbaca: {type(exc).__name__}"], 0
    blob, taken, notes = [], [], []
    # Anggaran dibelah PER BERKAS, bukan satu kolam yang dihabiskan berkas pertama.
    # Terukur 2026-10-08 11:28: `documents/AGENTS.md` 27 KB menghabiskan seluruh 3200 karakter,
    # sehingga ROADMAP / DESIGN_NOTES / ARCHITECTURE tidak terkirim sama sekali — pesan menyebut
    # "pedoman TERKUNCI" padahal yang sampai cuma awalan satu berkas. Kunci empat berkas yang
    # mengirim satu berarti engine menaati seperempat kontrak sambil mengira sudah menaati semuanya.
    wanted = wo.locked_guides()
    budget = max(GUIDE_MIN_PER_FILE, GUIDE_MAX_CHARS // max(1, len(wanted)))
    for g in wanted:
        p = Path(g["abs"])
        if not g.get("exists") or not p.is_file():
            notes.append(f"{g['path']} hilang dari disk")
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{g['path']} tidak terbaca ({type(exc).__name__})")
            continue
        clipped = len(text) > budget
        taken.append(g["path"])
        head = text[:budget]
        blob.append(f"### {g['path']}  (kutipan {len(head)}/{len(text)} karakter — "
                    f"isi penuh: {g['abs']})\n{head}")
        if clipped:
            notes.append(f"{g['path']} dipotong ke {budget} karakter; baca berkasnya untuk penuh")
    # Jumlah pedoman yang benar-benar terkirim, bukan jumlah baris teks: log melaporkan angka
    # yang dibaca operator, dan `len(body.splitlines())` menghitung baris dari satu berkas.
    return "\n\n".join(blob), notes, len(taken)


def prepare_prompt(item, cwd):
    """Susun prompt SEBELUM ada klaim — dan kembalikan None kalau gagal, bukan melempar.

    Ini alasan fungsinya dipisah: property yang ingin ditegakkan adalah "tidak ada baris yang
    tertinggal WORKING tanpa ada yang menyuntik". Selama pembentuk prompt hidup di dalam `main`
    setelah `claim`, satu exception dari `import drainer`, `get_item`, atau `.format` akan
    meninggalkan klaim itu — dan satu-satunya bukti yang tersisa adalah log SEMI_BROKE dengan
    exit 0. Dibuat fungsi terpisah, property-nya bisa dites: bikin fungsi ini gagal, lalu
    tunjukkan bahwa tidak ada apa pun yang diklaim.
    """
    import work_order as wo
    try:
        import drainer
        conn, engine = wo.connect()
        full = wo.get_item(conn, engine, int(item["id"])) or item
        body, notes, nguides = guides_for(conn, engine)
        prompt = drainer.SEMI_PROMPT.format(id=item["id"], title=item["title"], ws=cwd,
                                            gate=(full.get("check_command") or "tanpa gerbang"))
    except Exception as exc:  # noqa: BLE001 — sebelum ada klaim, gagal berarti diam
        log("SEMI_PREPARE_FAILED", item=int(item["id"]), engine=SESSION_ENGINE,
            reason=f"{type(exc).__name__}: {exc}",
            note="belum ada yang diklaim — barisnya tetap PENDING dan akan dicoba lagi")
        return None, 0
    if body:
        prompt += "\n\nPedoman yang mengikat (TERKUNCI oleh kontrak):\n" + body
    if notes:
        prompt += "\n\nCatatan pedoman: " + "; ".join(notes)
    # SEMI_PROMPT ditulis untuk jalur timer, di mana drainer memegang lease sampai engine mulai.
    # Di jalur hook ini klaimnya atas nama mesin ini sendiri, jadi kalimat itu dikoreksi.
    prompt += (f"\n\nCatatan jalur: hook ini akan mengklaim item {item['id']} atas nama "
               f"{SESSION_ENGINE} (bukan drainer). Perpanjang lease dengan "
               f"`python3 tools/work_order.py lease {item['id']}`.")
    return prompt, nguides


def main() -> int:
    data = read_input()
    cwd_raw = session_cwd(data)
    try:
        cwd = Path(cwd_raw).expanduser().resolve(strict=False) if cwd_raw else None
    except OSError:
        cwd = None

    import work_order as wo
    conn, engine = wo.connect()
    ap = wo.autopilot(conn, engine) or {}
    if (ap.get("mode") or "").upper() != "SEMI":
        return 0                      # OFF/ON: jalur ini tidak pernah ikut campur, tanpa jejak

    if not is_really_idle(data):
        # Yang diminta operator: "kalau melihat kamu Working, nunggu sampai selesai". Antigravity
        # mengirim fullyIdle=false ketika masih ada tugas latar — di sini jawabannya adalah diam,
        # dan batas giliran berikutnya yang akan memanggil kita lagi.
        log("SEMI_NOT_IDLE", engine=SESSION_ENGINE,
            reason="laporan sesi: masih ada pekerjaan latar — penghentian ini bukan akhirnya")
        return 0

    # Satu perintah aktif per mesin. Jalur drainer sudah punya aturan ini (`ENGINE_BUSY`); tanpa
    # padanannya di sini, setiap `Stop` bisa menyuntik perintah BARU sementara mesin masih memegang
    # item yang belum di-complete — persis yang dilarang kalimat "kalau Working, tunggu sampai
    # selesai". Yang menunggu tidak hilang: ia tetap PENDING dan diambil pada batas giliran
    # berikutnya setelah yang ini selesai.
    held = wo.run(conn, engine,
                  "SELECT id, lease_epoch FROM quest_tasks WHERE cli_engine=%s "
                  "AND status IN ('WORKING','CLAIMED') AND lease_epoch > %s ORDER BY id"
                  if engine == "POSTGRESQL" else
                  "SELECT id, lease_epoch FROM quest_tasks WHERE cli_engine=? "
                  "AND status IN ('WORKING','CLAIMED') AND lease_epoch > ? ORDER BY id",
                  (SESSION_ENGINE, wo.now()))
    if held:
        log("SEMI_ENGINE_BUSY", engine=SESSION_ENGINE, waiting=[int(r["id"]) for r in held],
            reason="mesin ini masih memegang item yang lease-nya hidup — antrean menunggu, "
                   "tidak ditumpuk")
        return 0

    item = wo.next_item(conn, engine, respect_due=True, origin=wo.ORIGIN_OPERATOR,
                        for_engine=SESSION_ENGINE)
    if not item:
        return 0                      # antrean untuk mesin ini kosong: diam adalah jawaban yang benar

    if not cwd:
        log("SEMI_REFUSED", engine=SESSION_ENGINE,
            reason="hook dipanggil tanpa cwd/workspacePaths — tidak ada proyek yang bisa dicocokkan",
            stop_hook_active=bool(data.get("stop_hook_active")))
        return 0

    # Satu resolver untuk dua jalur. Audit peer 2026-10-08 menemukan bahwa hook ini memperlakukan
    # `ws_path` KOSONG sebagai "boleh di proyek mana pun", sementara drainer memperlakukan kosong
    # sebagai REPO dan menuntut pane berada di REPO. Dua definisi untuk satu kata, dan yang longgar
    # adalah milik jalur yang menyuntik ke dalam sesi. Sekarang keduanya memanggil
    # `resolve_workspace`, jadi baris tanpa proyek hanya bisa jalan di REPO — tidak di mana saja.
    want = wo.resolve_workspace(item)
    if cwd != want:
        # Baris milik proyek lain TIDAK pernah disuntik ke sesi ini. Menyuntik perintah proyek B ke
        # sesi yang sedang berdiri di direktori A adalah persis kebingungan identitas yang
        # dikhawatirkan operator, dan hasilnya pekerjaan yang dilaporkan di tree yang salah.
        log("SEMI_OTHER_PROJECT", item=int(item["id"]), row_ws=str(want), session_cwd=str(cwd),
            engine=SESSION_ENGINE,
            reason="perintah itu bukan untuk proyek sesi ini — ia tetap antre, tidak dibatalkan")
        return 0

    # Klaim DITAHAN sampai prompt sudah utuh. Urutan ini bukan kerapian: versi pertama mengklaim
    # lebih dulu, lalu `get_item`, `guides_for`, `SEMI_PROMPT.format` dan `import drainer` bisa
    # melempar setelahnya — `__main__` menelannya sebagai SEMI_BROKE dengan exit 0, dan barisnya
    # tinggal WORKING tanpa ada yang menyuntik. Itulah persis kegagalan yang temuan (f) minta
    # dihapus, dan memperbaiki salah satu cabangnya saja tidak menghapus akarnya.
    prompt, nguides = prepare_prompt(item, cwd)
    if prompt is None:
        return 0

    ok, msg = wo.claim(conn, engine, int(item["id"]), SESSION_ENGINE)
    if not ok:
        # claim() sudah memuat pemutus malam dan aturan tree kotor, jadi penolakan di sini adalah
        # keputusan yang tercatat, bukan kegagalan jalur ini.
        log("SEMI_CLAIM_REFUSED", item=int(item["id"]), reason=msg, engine=SESSION_ENGINE,
            stop_hook_active=bool(data.get("stop_hook_active")))
        return 0

    # Satu-satunya hal yang tersisa setelah klaim adalah menulis satu baris ke stdout. Kalau itu
    # pun gagal, klaimnya dilepas — tidak ada jendela lagi tempat baris bisa tertinggal WORKING
    # tanpa ada yang menyuntik.
    try:
        print(json.dumps({"decision": BLOCK_DECISION, "reason": prompt}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        wo.finish(conn, engine, int(item["id"]), "PENDING",
                  reason=f"stdout gagal: {type(exc).__name__}")
        log("SEMI_EMIT_FAILED", item=int(item["id"]), engine=SESSION_ENGINE,
            reason=f"{type(exc).__name__}: {exc}", note="klaim dilepas, baris kembali PENDING")
        return 0
    log("SEMI_INJECTED", item=int(item["id"]), title=str(item["title"])[:80],
        engine=SESSION_ENGINE, decision=BLOCK_DECISION,
        claim=msg, guides=nguides,
        stop_hook_active=bool(data.get("stop_hook_active")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — hook yang meledak tidak boleh menahan orang pulang
        log("SEMI_BROKE", reason=f"{type(exc).__name__}: {exc}")
        sys.exit(0)
