#!/usr/bin/env python3
"""master_data.py — penjaga satu-satunya untuk data induk workstation: slot tim & registry mesin.

Alasan keberadaan (ROADMAP F8.3): `config/engines.json` dan `config/slots.yaml` menentukan
mesin mana yang boleh diberangkatkan dan dengan model apa. Sebelum modul ini, satu-satunya
jalan menulisnya adalah menyunting teks mentah — dan kesalahan di sana tidak bersuara:
registry yang membuat mesin tidak bisa start tetap tersimpan, lalu baru terungkap sebagai
`UNAVAILABLE` di saat terburuk.

Satu kosakata (kontrak §12): modul ini TIDAK mendefinisikan ulang apa pun. PARSERS, bentuk
probe, dan pembaca meter diambil dari `session_ingestor`; kunci registry dibaca dari
`cross_verify.load_registry`. Kalau kosakata itu berubah, modul ini ikut atau gagal — bukan
membuat kebenaran kedua.

Mode:
  ./master_data.py show                 # dokumen hidup + laporan validasi (stdout JSON)
  ./master_data.py validate             # baca kandidat JSON dari stdin, keluarkan laporan
  ./master_data.py selftest             # tabel kandidat rusak, pastikan semuanya ditolak

Laporan selalu berbentuk {"ok", "errors", "warnings", "resolved", "evidence"}. `errors`
memblokir penyimpanan; `warnings` tidak, tapi selalu ikut ditampilkan — supaya "tersimpan"
tidak pernah berarti "dipastikan".
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import session_ingestor as ingestor  # noqa: E402  (kosakata meter & parser tinggal di sini)
import cross_verify  # noqa: E402  (load_registry: cara mesin resolve dari config)

AI_STATION = Path.home() / ".ai-station"
ENGINES_PATH = AI_STATION / "config" / "engines.json"
SLOTS_PATH = AI_STATION / "config" / "slots.yaml"

KINDS = ("pane", "cli", "ollama")
# Tab yang benar-benar dipegang daemon (main.rs TABS). kind=pane menunjuk ke salah satunya.
PANE_TABS = ("qoder", "antigravity", "shell")
# Mesin yang kuotanya bukan milik kita sendiri: kalau binary-nya agy, satu turn yang salah
# pilih menghabiskan kolam Anthropic/GPT. Gate wajib, bukan opsional.
GATED_BINARIES = ("agy",)


def _err(errors, path, message):
    errors.append({"path": path, "message": message})


def _warn(warnings, path, message):
    warnings.append({"path": path, "message": message})


def read_live_document() -> dict:
    """Kedua berkas master data apa adanya, sebagai satu dokumen yang bisa diedit."""
    engines = json.loads(ENGINES_PATH.read_text()) if ENGINES_PATH.exists() else {}
    slots = {}
    if SLOTS_PATH.exists():
        import yaml
        slots = yaml.safe_load(SLOTS_PATH.read_text()) or {}
    return {"engines": engines, "slots": slots}


def meter_state() -> dict:
    """Pembacaan meter terakhir per (engine, scope, jendela), atau alasannya tidak ada.

    Ini sumber kebenaran untuk pertanyaan 'apakah jendela yang ditulis di quota_gate benar-
    benar dilaporkan meter'. Malam ini dua kesalahan terbukti lolos tanpa ini: scope ditulis
    'Anthropic' (kolom meter sebenarnya 'Claude and GPT models') dan windows [] — keduanya
    membuat setiap keberangkatan tertutup tanpa ada yang bilang kenapa.
    """
    try:
        store = ingestor.Store()
        rows = ingestor.latest_quota(store)
    except Exception as exc:  # noqa: BLE001
        return {"available": False, "reason": f"database meter tidak terbaca: {exc}", "rows": []}
    return {"available": True, "reason": "", "rows": rows}


def cli_model_lists(warnings) -> dict:
    """Daftar model yang benar-benar dilaporkan CLI, untuk mengecek id yang ditulis config.

    Tiga keadaan dibedakan dan tidak boleh dicampur: daftar terbaca (id salah = error),
    daftar tidak terbaca (warning), dan CLI memang tidak punya perintah daftar (mis. ollama
    — di sini yang dicek adalah model yang sudah di-pull).
    """
    lists = {}
    home_bin = [str(Path.home() / d) for d in (".local/bin", "runtime/local/bin",
                                               ".gemini/antigravity/bin")]
    env = {**os.environ,
           "PATH": os.pathsep.join([os.environ.get("PATH", "")] + home_bin)}
    for key, argv, parser in (("agy", ["agy", "models"], _parse_agy_models),
                              ("qoder", ["qoder", "--list-models"], _parse_lines)):
        binary = shutil.which(argv[0], path=env["PATH"])
        if not binary:
            _warn(warnings, f"evidence.{key}",
                  f"`{argv[0]}` tidak ditemukan di PATH: id model tidak bisa dicocokkan")
            continue
        try:
            res = subprocess.run([*argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, timeout=45, env=env)
        except (subprocess.TimeoutExpired, OSError) as exc:
            _warn(warnings, f"evidence.{key}", f"`{argv[0]} {argv[1]}` gagal: {exc}")
            continue
        if res.returncode != 0:
            _warn(warnings, f"evidence.{key}",
                  f"`{argv[0]} {argv[1]}` keluar dengan rc={res.returncode}: "
                  f"{(res.stdout or '')[:160]}")
            continue
        lists[parser.__name__] = parser(res.stdout or "")
    return lists


def _parse_agy_models(text: str) -> set:
    out = set()
    for line in text.splitlines():
        parts = line.split("\t")
        if parts and parts[0].strip() and not line.startswith("Fetching"):
            out.add(parts[0].strip())
    return out


def _parse_lines(text: str) -> set:
    out = set()
    for line in text.splitlines():
        value = line.strip()
        if value and value.upper() != "MODEL":
            out.add(value)
    return out


def ollama_models(warnings) -> set | None:
    """Model yang sudah di-pull, atau None kalau server tidak hidup."""
    if not shutil.which("ollama"):
        return None
    try:
        res = subprocess.run(["ollama", "list"], stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    names = set()
    for line in res.stdout.splitlines()[1:]:
        if line.strip():
            names.add(line.split()[0])
    return names


def validate(document: dict, with_evidence: bool = True) -> dict:
    """Periksa satu kandidat dokumen master data. Tidak menulis apa pun."""
    errors: list = []
    warnings: list = []
    if not isinstance(document, dict):
        return {"ok": False, "errors": [{"path": "(root)",
                                         "message": "dokumen master data bukan objek"}],
                "warnings": [], "resolved": {}, "evidence": {}}

    engines_doc = document.get("engines") or {}
    registry = engines_doc.get("engines") if isinstance(engines_doc, dict) else None
    if not isinstance(registry, dict) or not registry:
        _err(errors, "engines.engines",
             "tidak ada satu mesin pun di registry: tidak ada yang bisa diminta memverifikasi")
        registry = {}

    meter = meter_state() if with_evidence else {"available": False, "reason": "skip",
                                                 "rows": []}
    lists = cli_model_lists(warnings) if with_evidence else {}
    pulled = ollama_models(warnings) if with_evidence else None

    _validate_registry(registry, engines_doc, errors, warnings, meter, lists, pulled)
    resolved = _validate_slots(document.get("slots") or {}, registry, errors, warnings,
                               lists, pulled)

    ok = not errors
    return {
        "ok": ok,
        "errors": errors,
        "warnings": warnings,
        "resolved": resolved,
        "evidence": {
            "meter_available": meter["available"],
            "meter_reason": meter.get("reason", ""),
            "model_lists": {k: len(v) for k, v in lists.items()},
            "ollama_pulled": None if pulled is None else len(pulled),
        },
    }


def _validate_registry(registry, engines_doc, errors, warnings, meter, lists, pulled) -> None:
    agy_ids = lists.get("_parse_agy_models") or set()
    qoder_ids = lists.get("_parse_lines") or set()
    tiers = (engines_doc.get("local_tiers") or {}) if isinstance(engines_doc, dict) else {}

    for name, spec in registry.items():
        where = f"engines.engines.{name}"
        if not isinstance(spec, dict):
            _err(errors, where, "entry mesin bukan objek: tidak ada kind/model yang bisa dibaca")
            continue
        kind = str(spec.get("kind") or "")
        if kind not in KINDS:
            _err(errors, f"{where}.kind",
                 f"kind {kind!r} tidak dikenal (harus salah satu dari {', '.join(KINDS)}): "
                 "cross_verify tidak tahu cara memberangkatkannya")
        if spec.get("role") and str(spec.get("role")) in ("builder",) and name != "qoder":
            _warn(warnings, f"{where}.role",
                  "role builder dipakai mesin yang menulis kode; mesin ini tidak sah menjadi "
                  "pemeriksa tunggal pekerjaannya sendiri (policy.self_verification)")

        if kind == "pane":
            tab = str(spec.get("tab") or "")
            if tab not in PANE_TABS:
                _err(errors, f"{where}.tab",
                     f"pane menunjuk tab {tab!r} yang tidak dikelola daemon "
                     f"({', '.join(PANE_TABS)}) — tidak ada layar untuk mengetik prompt")
        elif kind == "cli":
            binary = str(spec.get("binary") or "")
            if not binary:
                _err(errors, f"{where}.binary", "kind=cli tanpa binary: tidak ada proses yang "
                     "bisa dijalankan")
            template = spec.get("template")
            if not isinstance(template, list) or not template:
                _err(errors, f"{where}.template",
                     "kind=cli tanpa 'template' (list argv): prompt tidak punya tempat")
            else:
                joined = " ".join(str(t) for t in template)
                for placeholder in ("{model}", "{prompt}"):
                    if placeholder not in joined:
                        _err(errors, f"{where}.template",
                             f"template tidak memuat {placeholder}: nilai "
                             + ("model tidak akan pernah dipakai" if placeholder == "{model}"
                                else "prompt tidak akan pernah terkirim"))
            if binary in GATED_BINARIES and not spec.get("quota_gate"):
                _err(errors, f"{where}.quota_gate",
                     f"mesin {binary} tanpa quota_gate: penulisannya boleh, tapi tidak ada "
                     "bukti jendela 5h/weekly kolam itu masih ada sebelum satu turn dibakar. "
                     "Izin pemilik mesin bersyarat — tanpa gate syaratnya hilang diam-diam.")
        elif kind == "ollama":
            if not spec.get("model"):
                _err(errors, f"{where}.model", "kind=ollama tanpa model")
            mib = spec.get("min_free_mib")
            if not isinstance(mib, (int, float)) or isinstance(mib, bool) or mib <= 0:
                _err(errors, f"{where}.min_free_mib",
                     f"min_free_mib {mib!r} bukan angka positif: gerbang VRAM tidak punya "
                     "ambang, jadi ia tidak melindungi 12 GB yang dibagi dengan desktop")
            tier = str(spec.get("tier") or "")
            if tier and tiers and tier not in tiers:
                _err(errors, f"{where}.tier",
                     f"tier {tier!r} tidak ada di local_tiers ({', '.join(sorted(tiers))})")
            model = str(spec.get("model") or "")
            if pulled is not None and model and ":" in model and model not in pulled:
                _warn(warnings, f"{where}.model",
                      f"model {model!r} belum di-pull (ada: {', '.join(sorted(pulled))}) — "
                      "mesin akan menolak memberangkatkannya sampai `ollama pull` dijalankan")

        _validate_usage(name, spec, errors, warnings)
        _validate_gate(name, spec, errors, warnings, meter)
        _validate_model_id(name, spec, errors, warnings, agy_ids, qoder_ids)


def _validate_usage(name, spec, errors, warnings) -> None:
    usage = spec.get("usage")
    if usage is None:
        return
    where = f"engines.engines.{name}.usage"
    if not isinstance(usage, dict):
        _err(errors, where, "blok usage bukan objek")
        return
    parser = str(usage.get("parser") or "")
    if not parser:
        _err(errors, f"{where}.parser",
             "usage tanpa parser: hasil meter tidak punya pembaca, jadi angkanya hilang")
    elif parser != "none" and parser not in ingestor.PARSERS:
        _err(errors, f"{where}.parser",
             f"parser {parser!r} tidak dikenal ingestor "
             f"(ada: {', '.join(sorted(ingestor.PARSERS))}, atau 'none')")
        return
    probe = usage.get("probe")
    if parser == "none":
        return
    argv = ingestor._probe_argv(probe) if probe not in (None, "", []) else []
    if not argv:
        _err(errors, f"{where}.probe",
             "parser diharapkan membaca sesuatu, tapi probe kosong: meter apa yang dijalankan?")
    else:
        try:
            ingestor._resolve_argv(argv)
        except Exception as exc:  # noqa: BLE001
            _warn(warnings, f"{where}.probe", f"probe tidak bisa dinormalisasi: {exc}")


def _validate_gate(name, spec, errors, warnings, meter) -> None:
    gate = spec.get("quota_gate")
    if gate is None:
        return
    where = f"engines.engines.{name}.quota_gate"
    if not isinstance(gate, dict):
        _err(errors, where, "quota_gate bukan objek: tidak ada yang bisa diperiksa")
        return
    engine = str(gate.get("engine") or "")
    scope = str(gate.get("scope") or "")
    windows = gate.get("windows")
    if not engine or not scope:
        _err(errors, where, "quota_gate tanpa 'engine' atau 'scope': baris meter mana yang "
             "harus dipercaya?")
    if not isinstance(windows, list) or not windows:
        _err(errors, f"{where}.windows",
             "quota_gate tanpa daftar 'windows' yang terisi: tidak ada satu jendela pun yang "
             "bisa dibuktikan, dan gerbang menolak untuk menebak")
        windows = []
    floor = gate.get("min_fraction", 0.0)
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0.0 <= floor <= 1.0:
        _err(errors, f"{where}.min_fraction",
             f"min_fraction {floor!r} bukan pecahan 0..1")
    if not meter["available"]:
        _warn(warnings, where,
              f"jendela gate tidak bisa dicocokkan ke meter: {meter['reason']}")
        return
    seen = {}
    for row in meter["rows"]:
        seen.setdefault((str(row.get("engine")), str(row.get("scope"))), set()).add(
            str(row.get("limit_window")))
    pairs = seen.get((engine, scope))
    if engine and scope and pairs is None:
        _err(errors, f"{where}.scope",
             f"meter tidak pernah melaporkan (engine={engine!r}, scope={scope!r}). Yang ada: "
             + "; ".join(sorted(f"{e}/{s}" for e, s in seen))
             + " — setiap keberangkatan mesin ini akan ditolak gerbang, dan itu terbaca "
               "sebagai 'kuota habis' padahal configurasi yang salah.")
        return
    for window in windows:
        if pairs and str(window) not in pairs:
            _err(errors, f"{where}.windows",
                 f"jendela {window!r} tidak dilaporkan meter untuk {engine}/{scope} "
                 f"(yang ada: {', '.join(sorted(pairs))})")


def _validate_model_id(name, spec, errors, warnings, agy_ids, qoder_ids) -> None:
    model = str(spec.get("model") or "")
    if not model:
        _warn(warnings, f"engines.engines.{name}.model",
              "mesin tanpa model: yang dipakai adalah bawaan CLI, dan laporan tidak "
              "mencatat apa yang sebenarnya bekerja")
        return
    binary = str(spec.get("binary") or "")
    tab = str(spec.get("tab") or "")
    if binary == "agy" or (spec.get("kind") == "pane" and tab == "antigravity"):
        if agy_ids and model not in agy_ids:
            _err(errors, f"engines.engines.{name}.model",
                 f"id model {model!r} tidak ada dalam `agy models` ({len(agy_ids)} id). "
                 "CLI akan menolaknya, atau diam-diam memakai bawaan — keduanya berarti "
                 "laporan workstation menyebut model yang tidak bekerja.")
    elif (binary == "qoder" or tab == "qoder") and qoder_ids and model not in qoder_ids:
        _warn(warnings, f"engines.engines.{name}.model",
              f"id model {model!r} tidak cocok dengan `qoder --list-models`; TUI memakai "
              "nama tampilan, jadi ini sering benar — tapi belum terverifikasi")


def _validate_slots(slots, registry, errors, warnings, lists=None, pulled=None) -> dict:
    resolved: dict = {}
    teams = slots.get("teams")
    if not isinstance(teams, list) or not teams:
        _err(errors, "slots.teams",
             "tidak ada tim di slots.yaml: role tidak bisa di-resolve saat dipakai")
        return resolved
    seen_ids = set()
    for team in teams:
        if not isinstance(team, dict):
            _err(errors, "slots.teams", "entri tim bukan objek")
            continue
        tid = str(team.get("id") or "")
        if not tid:
            _err(errors, "slots.teams", "tim tanpa id")
            continue
        if tid in seen_ids:
            _err(errors, f"slots.teams.{tid}", "id tim dobel: active_team tidak jelas merujuk ke mana")
        seen_ids.add(tid)
        roles = team.get("roles")
        if not isinstance(roles, list) or not roles:
            _err(errors, f"slots.teams.{tid}.roles", "tim tanpa role")
            continue
        role_ids = set()
        for role in roles:
            if not isinstance(role, dict):
                _err(errors, f"slots.teams.{tid}.roles", "entri role bukan objek")
                continue
            rid = str(role.get("role_id") or "")
            where = f"slots.teams.{tid}.roles.{rid or '(tanpa role_id)'}"
            if not rid:
                _err(errors, where, "role tanpa role_id")
            elif rid in role_ids:
                _err(errors, where, f"role_id {rid!r} dobel dalam satu tim")
            role_ids.add(rid)

            rkey = str(role.get("registry_key") or "")
            spec = registry.get(rkey) if rkey else None
            if rkey and not isinstance(spec, dict):
                _err(errors, f"{where}.registry_key",
                     f"registry_key {rkey!r} tidak ada di engines.json "
                     f"({', '.join(sorted(registry))}): role ini tidak bisa diisi")
                continue
            if spec:
                role_model = str(role.get("model") or "")
                reg_model = str(spec.get("model") or "")
                if role_model and reg_model and role_model != reg_model:
                    _err(errors, f"{where}.model",
                         f"dua definisi berbeda untuk satu role: slots mengatakan "
                         f"{role_model!r}, registry ({rkey}) mengatakan {reg_model!r}. "
                         "Kontrak §12: satu kosakata — salinan kedua adalah laporan yang "
                         "saling bertentangan tanpa ada cara tahu mana yang benar.")
                gate = spec.get("quota_gate") or {}
                pool = str(role.get("quota_pool") or "")
                if pool and gate and str(gate.get("scope") or "") != pool:
                    _err(errors, f"{where}.quota_pool",
                         f"quota_pool {pool!r} berbeda dari scope gate "
                         f"{str(gate.get('scope'))!r} pada {rkey}")
                wins = role.get("windows")
                if wins and gate.get("windows") and list(wins) != list(gate.get("windows")):
                    _err(errors, f"{where}.windows",
                         f"jendela role {wins} berbeda dari jendela gate {gate.get('windows')}")
                resolved[rid] = {"registry_key": rkey, "model": reg_model or role_model,
                                 "kind": spec.get("kind"), "binary": spec.get("binary"),
                                 "quota_gate": bool(gate)}
            else:
                loose_model = str(role.get("model") or "")
                resolved[rid] = {"model": loose_model, "registry_key": ""}
                if loose_model:
                    known = set().union(*(set(v) for v in (lists or {}).values()),
                                        set(pulled or ()))
                    hint = ""
                    if known and loose_model not in known:
                        hint = (" — dan tidak ada CLI yang melaporkannya "
                                "(`agy models`/`qoder --list-models`/`ollama list`)")
                    _warn(warnings, f"{where}.registry_key",
                          f"role {rid!r} menulis model {loose_model!r} tanpa 'registry_key': "
                          f"tidak ada yang resolve saat dipakai (kontrak §12){hint}. Ini "
                          "catatan tim, bukan konfigurasi yang mengubah keberangkatan mesin.")

    active = str(slots.get("active_team") or "")
    if active and active not in seen_ids:
        _err(errors, "slots.active_team",
             f"active_team {active!r} tidak ada di daftar tim — workstation sedang memakai "
             "tim lain daripada yang tertulis")
    return resolved


def document_from_files() -> dict:
    return read_live_document()


def cmd_show(args) -> int:
    try:
        document = document_from_files()
    except FileNotFoundError as exc:
        print(json.dumps({"ok": False, "errors": [{"path": str(exc.filename),
                                                   "message": "berkas master data tidak ada"}],
                          "warnings": [], "resolved": {}, "evidence": {}}, indent=2))
        return 2
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "errors": [{"path": str(ENGINES_PATH),
                                                   "message": f"bukan JSON yang sah: {exc}"}],
                          "warnings": [], "resolved": {}, "evidence": {}}, indent=2))
        return 2
    report = validate(document, with_evidence=not args.no_evidence)
    print(json.dumps({"document": document, "report": report,
                      "dispatch": dispatch_reality()}, indent=2))
    return 0 if report["ok"] else 1


def dispatch_reality() -> dict:
    """Apa yang sebenarnya dipakai cross_verify kalau mesin diberangkatkan SEKARANG.

    Bukan salinan kedua dari registry: fungsi ini memanggil `load_registry` dan
    `engine_present` milik cross_verify, jadi yang ditampilkan adalah kode jalan yang sama
    dengan yang memutuskan keberangkatan. Ujian penerimaan F8.3 ada di sini — ubah model di
    config, lalu angka ini berubah; kalau tidak, yang dibaca masih ingatan.
    """
    out = {}
    try:
        registry = cross_verify.load_registry()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"registry tidak bisa dimuat: {exc}"}
    for name, spec in registry.items():
        try:
            ok, why = cross_verify.engine_present(spec)
        except Exception as exc:  # noqa: BLE001
            ok, why = False, f"probe gagal: {exc}"
        out[name] = {"role": spec.get("role", ""), "model": spec.get("model", ""),
                     "can_dispatch": bool(ok), "reason": why or ""}
    return out


def cmd_validate(args) -> int:
    raw = sys.stdin.read()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "errors": [{"path": "(stdin)",
                                                   "message": f"bukan JSON yang sah: {exc}"}],
                          "warnings": [], "resolved": {}, "evidence": {}}, indent=2))
        return 1
    report = validate(document, with_evidence=not args.no_evidence)
    print(json.dumps(report, indent=2, sort_keys=False))
    return 0 if report["ok"] else 1


BROKEN_CASES = [
    ("kind tidak dikenal", lambda d: d["engines"]["engines"]["qoder"].__setitem__("kind", "tty"), True),
    ("pane menunjuk tab yang tidak ada",
     lambda d: d["engines"]["engines"]["gemini"].__setitem__("tab", "antigravityy"), True),
    ("cli tanpa template",
     lambda d: d["engines"]["engines"]["claude-opus"].pop("template"), True),
    ("template tanpa {prompt}",
     lambda d: d["engines"]["engines"]["claude-opus"].__setitem__(
         "template", ["agy", "--model", "{model}", "--output-format", "json"]), True),
    ("agy cli tanpa quota_gate (gerbang hilang diam-diam)",
     lambda d: d["engines"]["engines"]["claude-opus"].pop("quota_gate"), True),
    ("quota_gate scope salah ketik 'Anthropic'",
     lambda d: d["engines"]["engines"]["claude-opus"]["quota_gate"].__setitem__(
         "scope", "Anthropic"), True),
    ("quota_gate windows kosong",
     lambda d: d["engines"]["engines"]["claude-opus"]["quota_gate"].__setitem__(
         "windows", []), True),
    ("quota_gate jendela tak dikenal",
     lambda d: d["engines"]["engines"]["claude-opus"]["quota_gate"].__setitem__(
         "windows", ["daily"]), True),
    ("min_fraction di luar 0..1",
     lambda d: d["engines"]["engines"]["claude-opus"]["quota_gate"].__setitem__(
         "min_fraction", 2), True),
    ("parser meter tidak dikenal",
     lambda d: d["engines"]["engines"]["gemini"]["usage"].__setitem__(
         "parser", "agy_usage_jsonn"), True),
    ("usage tanpa parser",
     lambda d: d["engines"]["engines"]["qoder"]["usage"].pop("parser"), True),
    ("probe string tidak bisa dinormalisasi",
     lambda d: d["engines"]["engines"]["gemini"]["usage"].__setitem__("probe", ""), True),
    ("model id tidak ada di `agy models`",
     lambda d: d["engines"]["engines"]["claude-opus"].__setitem__(
         "model", "claude-opus-5-5-ultra"), True),
    ("ollama min_free_mib bukan angka",
     lambda d: d["engines"]["engines"]["local"].__setitem__("min_free_mib", "6500"), True),
    ("ollama tier tidak ada di local_tiers",
     lambda d: d["engines"]["engines"]["local"].__setitem__("tier", "medium"), True),
    ("registry kosong", lambda d: d["engines"].__setitem__("engines", {}), True),
    ("slots registry_key menunjuk mesin yang dihapus",
     lambda d: d["slots"]["teams"][0]["roles"][-1].__setitem__("registry_key", "claude-gpt"),
     True),
    ("model role bertentangan dengan registry",
     lambda d: d["slots"]["teams"][0]["roles"][-1].__setitem__("model", "gemini-3.1-pro"), True),
    ("quota_pool role bertentangan dengan scope",
     lambda d: d["slots"]["teams"][0]["roles"][-1].__setitem__("quota_pool", "Gemini Models"),
     True),
    ("role_id dobel dalam satu tim",
     lambda d: d["slots"]["teams"][0]["roles"].append(
         {"role_id": "decider", "registry_key": "claude-opus"}), True),
    ("active_team tidak ada",
     lambda d: d["slots"].__setitem__("active_team", "ghost_team"), True),
    ("tim tanpa role",
     lambda d: d["slots"]["teams"][0].__setitem__("roles", []), True),
]


def cmd_selftest(args) -> int:
    """Jalankan tabel kandidat rusak: semuanya HARUS ditolak, dan dokumen hidup harus lolos."""
    failures = 0
    baseline = validate(read_live_document(), with_evidence=not args.no_evidence)
    if not baseline["ok"]:
        print("GAGAL: konfigurasi hidup saat ini sudah tidak sah — selftest tidak bisa jadi "
              "dasar apa pun")
        for e in baseline["errors"]:
            print(f"  {e['path']}: {e['message']}")
        return 1
    print(f"[ok    ] konfigurasi hidup lolos validasi "
          f"({len(baseline['warnings'])} peringatan)")
    for label, mutate, must_refuse in BROKEN_CASES:
        document = read_live_document()
        try:
            mutate(document)
        except (KeyError, IndexError, TypeError) as exc:
            print(f"[GAGAL ] {label}: kasus tidak bisa dibangun ({exc})")
            failures += 1
            continue
        report = validate(document, with_evidence=not args.no_evidence)
        refused = not report["ok"]
        if refused != must_refuse:
            failures += 1
        hit = "; ".join(f"{e['path']}" for e in report["errors"][:2])
        print(f"[{'ok    ' if refused == must_refuse else 'GAGAL '}] "
              f"{'ditolak' if must_refuse else 'diterima'}: {label}"
              + (f"  -> {hit}" if hit else ""))
    print(f"\n=== master-data selftest: {len(BROKEN_CASES) + 1 - failures}/"
          f"{len(BROKEN_CASES) + 1} sesuai ===")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Penjaga data induk workstation")
    ap.add_argument("mode", nargs="?", default="show", choices=("show", "validate", "selftest"))
    ap.add_argument("--no-evidence", action="store_true",
                    help="jangan panggil CLI/manusia-meter (agy models, ollama list, database) — "
                         "hanya periksa bentuk struktur")
    args = ap.parse_args()
    if args.mode == "validate":
        return cmd_validate(args)
    if args.mode == "selftest":
        return cmd_selftest(args)
    return cmd_show(args)


if __name__ == "__main__":
    raise SystemExit(main())
