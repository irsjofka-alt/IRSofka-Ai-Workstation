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

# Mesin milik sesi yang memanggil hook ini. Bukan tebakan: berkas ini terdaftar di
# engines/qoder/settings.json, jadi setiap tembakan datang dari sesi Qoder. Baris perintah untuk
# mesin lain TIDAK diambil di sini — itu jalur timer, dan memilih engine dari ingatan dilarang §12.
SESSION_ENGINE = "qoder"
NIGHT_LOG = Path.home() / ".ai-station" / "logs" / "semi_wake.jsonl"
GUIDE_MAX_CHARS = 3200          # plafon yang sama dengan yang sudah dipakai auto_restore.py


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


def guides_for(conn, engine, cwd: Path) -> tuple[str, list[str]]:
    """Pedoman proyek yang sedang dibuka, dibaca dari registry lewat path — bukan dari ingatan."""
    try:
        import work_order as wo
        listed = wo.list_workspaces(conn, engine)["workspaces"]
    except Exception as exc:  # noqa: BLE001
        return "", [f"registry tidak terbaca: {type(exc).__name__}"]
    hit = next((w for w in listed
                if Path(w["path"]).resolve(strict=False) == cwd), None)
    if not hit or not (hit.get("guidelines") or "").strip():
        return "", []
    blob, taken, notes = [], [], []
    budget = GUIDE_MAX_CHARS
    # Yang disimpan di registry adalah path ABSOLUT, bukan nama relatif: proyek ini punya
    # AGENTS.md dan workstation juga punya AGENTS.md, dan menyaring keduanya dengan satu nama
    # berarti engine patuh pada berkas yang tidak bisa ditunjuk.
    for raw in [x.strip() for x in (hit.get("guidelines") or "").split(",") if x.strip()]:
        p = Path(raw)
        if not p.is_absolute():
            p = Path(hit["path"]) / raw
        label = raw
        if not p.is_file():
            notes.append(f"{label} hilang dari disk")
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:budget]
        except Exception as exc:  # noqa: BLE001
            notes.append(f"{label} tidak terbaca ({type(exc).__name__})")
            continue
        budget -= len(text)
        taken.append(label)
        blob.append(f"### {label}\n{text}")
        if budget <= 0:
            notes.append(f"daftar dipotong pada {GUIDE_MAX_CHARS} karakter — sisa pedoman tidak disertakan")
            break
    return "\n\n".join(blob), ([f"{r}: {n}" for r, n in zip(taken, notes)] if notes else notes)


def main() -> int:
    data = read_input()
    cwd_raw = data.get("cwd") or ""
    try:
        cwd = Path(cwd_raw).expanduser().resolve(strict=False) if cwd_raw else None
    except OSError:
        cwd = None

    import work_order as wo
    conn, engine = wo.connect()
    ap = wo.autopilot(conn, engine) or {}
    if (ap.get("mode") or "").upper() != "SEMI":
        return 0                      # OFF/ON: jalur ini tidak pernah ikut campur, tanpa jejak

    item = wo.next_item(conn, engine, respect_due=True, origin=wo.ORIGIN_OPERATOR)
    if not item:
        return 0                      # antrean operator kosong: diam adalah jawaban yang benar

    if not cwd:
        log("SEMI_REFUSED", reason="hook dipanggil tanpa cwd sesi — tidak ada proyek yang bisa dicocokkan",
            stop_hook_active=bool(data.get("stop_hook_active")))
        return 0

    where = Path(item.get("ws_path") or "").resolve(strict=False) if item.get("ws_path") else None
    if where is not None and where != cwd:
        # Baris milik proyek lain TIDAK pernah disuntik ke sesi ini. Menyuntik perintah proyek B ke
        # sesi yang sedang berdiri di direktori A adalah persis kebingungan identitas yang
        # dikhawatirkan operator, dan hasilnya pekerjaan yang dilaporkan di tree yang salah.
        log("SEMI_OTHER_PROJECT", item=int(item["id"]), row_ws=str(where), session_cwd=str(cwd),
            reason="perintah itu bukan untuk proyek sesi ini — ia tetap antre, tidak dibatalkan")
        return 0

    ok, msg = wo.claim(conn, engine, int(item["id"]), SESSION_ENGINE)
    if not ok:
        # claim() sudah memuat pemutus malam dan aturan tree kotor, jadi penolakan di sini adalah
        # keputusan yang tercatat, bukan kegagalan jalur ini.
        log("SEMI_CLAIM_REFUSED", item=int(item["id"]), reason=msg,
            stop_hook_active=bool(data.get("stop_hook_active")))
        return 0

    import drainer
    full = wo.get_item(conn, engine, int(item["id"])) or item
    body, notes = guides_for(conn, engine, cwd)
    prompt = drainer.SEMI_PROMPT.format(id=item["id"], title=item["title"], ws=cwd,
                                        gate=(full.get("check_command") or "tanpa gerbang"))
    if body:
        prompt += "\n\nPedoman proyek yang harus dipatuhi item ini:\n" + body
    if notes:
        prompt += "\n\nCatatan pedoman: " + "; ".join(notes)
    log("SEMI_INJECTED", item=int(item["id"]), title=str(item["title"])[:80],
        claim=msg, guides=len(body.splitlines()),
        stop_hook_active=bool(data.get("stop_hook_active")))
    print(json.dumps({"decision": "block", "reason": prompt}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — hook yang meledak tidak boleh menahan orang pulang
        log("SEMI_BROKE", reason=f"{type(exc).__name__}: {exc}")
        sys.exit(0)
