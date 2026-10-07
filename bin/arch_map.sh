#!/usr/bin/env bash
# arch_map.sh — bangkitkan peta struktur workstation: folder -> berkas -> untuk apa.
#
#   arch_map.sh            tulis ulang brain/memory/projects/ARCHITECTURE.md
#   arch_map.sh --check    laporkan berkas TANPA keterangan, jangan menulis (exit 1 bila ada)
#
# Kenapa dibangkitkan, bukan ditulis tangan: peta yang dikarang manusia selalu tertinggal
# dari kodenya, dan peta yang salah lebih berbahaya daripada tidak punya peta — mesin akan
# grep ke tempat yang salah dengan keyakinan penuh. Keterangan tiap berkas diambil dari
# docstring/komentar pertamanya: satu sumber kebenaran, tidak ada salinan kedua.
set -uo pipefail

STATION="${STATION_DIR:-$HOME/.ai-station}"
exec python3 - "$STATION" "$@" <<'PY'
import io
import os
import re
import sys
import time

station = sys.argv[1]
check = "--check" in sys.argv[2:]
out_path = os.path.join(station, "brain", "memory", "projects", "ARCHITECTURE.md")

# Yang dianggap bagian arsitektur. brain/ (isi kepala), engines/ (data sesi CLI),
# logs/ dan target/ (hasil jalan) sengaja tidak dipetakan.
DIRS = [
    ("engine-rust/src", "Rust daemon + GUI: one process serving both the native window and http://127.0.0.1:8999"),
    ("engine-rust/examples", "Standalone probes run with `cargo run --example` — never part of the daemon"),
    ("bin", "Shell entry points: deploy, backup/restore, skill wiring, the `ai-station` command itself"),
    ("tools", "Python workers the hooks and timers call: ingestors, verifiers, MCP bridges"),
    ("hooks", "Qoder CLI lifecycle guards: memory handoff, compaction restore, self-preservation"),
    ("systemd", "User units that keep panes and jobs alive outside the daemon's cgroup"),
    ("documents", "Pushable templates that teach an outside AI CLI how to enter this workstation"),
    ("config", "Declarative state: engine registry, CLI profiles, DB coords, $HOME shim policy"),
]
LOOSE = [
    ("engine-rust/Cargo.toml", None),
    (".gitignore", None),
    ("README.md", None),
    ("INSTALL.md", None),
    ("ROADMAP.md", None),
    ("LICENSE", None),
    ("THIRD_PARTY_NOTICES.md", None),
]

PRUNE_DIRS = {"target", "__pycache__", "venv", "node_modules", "archive", ".git", "static", ".qoder"}
CODE_EXT = {".rs", ".py", ".sh", ".html", ".json", ".toml", ".yaml", ".yml",
            ".md", ".service", ".timer", ".txt"}
NAMED = {".gitignore", "LICENSE", "NOTICE"}
MAX_BYTES = 400_000

# Hanya untuk berkas yang secara struktural tidak bisa menaruh komentar sendiri:
# cli_profiles.json dibaca sebagai HashMap<String, TabProfile> — key asing apa pun
# membuat seluruh file gagal parse dan daemon jatuh ke profil bawaan.
FALLBACK = {
    "config/cli_profiles.json": "Per-model context/effort caps and CLI flags, one entry per tab",
    "config/db_local.json": "PostgreSQL connection string — git-ignored, never committed",
}


def is_source(path):
    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    if ext not in CODE_EXT and name not in NAMED:
        # No extension only counts for a script — the compiled daemon in bin/ must not
        # show up in the map as if it were something to read.
        try:
            with open(path, "rb") as fh:
                if fh.read(2) != b"#!":
                    return False
        except OSError:
            return False
    try:
        if os.path.getsize(path) > MAX_BYTES:
            return False
        with open(path, "rb") as fh:
            head = fh.read(512)
    except OSError:
        return False
    if b"\x00" in head:
        return False
    return True


def desc_of(path):
    """First file-level comment — the file's own statement of purpose, never a copy."""
    try:
        lines = io.open(path, encoding="utf-8", errors="ignore").read().splitlines()
    except OSError:
        return ""
    ext = os.path.splitext(path)[1].lower()

    def cut(s):
        return re.sub(r"\s+", " ", s).strip()[:150]

    if ext == ".rs":
        for l in lines[:60]:
            if l.startswith("//!"):
                return cut(l[3:])
        return ""
    if ext == ".py":
        txt = "\n".join(lines[:60])
        m = re.search(r'^\s*(?:"""|\'\'\')(.*?)(?:"""|\'\'\')', txt, re.S | re.M)
        if m:
            first = [x.strip() for x in m.group(1).splitlines() if x.strip()]
            return cut(first[0]) if first else ""
        return ""
    if ext == ".html":
        txt = "\n".join(lines[:40])
        m = re.search(r"<!--(.*?)-->", txt, re.S)
        if m:
            first = [x.strip(" \t*-=#") for x in m.group(1).splitlines() if x.strip(" \t*-=#")]
            return cut(first[0]) if first else ""
        return ""
    if ext in {".service", ".timer"}:
        for l in lines:
            if l.startswith("Description="):
                return cut(l.split("=", 1)[1])
        return ""
    if ext in {".sh", ".toml", ".yaml", ".yml", ".txt"} or not ext:
        start = 1 if lines and lines[0].startswith("#!") else 0
        for l in lines[start:start + 25]:
            s = l.strip()
            if s.startswith("#") and len(s) > 4 and not set(s) <= set("#-*=! "):
                return cut(s.lstrip("# "))
            if ext in {".toml", ".yaml", ".yml"} and s and not s.startswith("#"):
                break
        return ""
    if ext == ".json":
        for l in lines[:15]:
            s = l.strip()
            m = re.match(r'^"_(?:comment|catatan[^"]*|note|notes|doc|docs)"\s*:\s*"(.+?)"\s*,?$', s)
            if m:
                return cut(m.group(1))
            if s.startswith("//") and len(s) > 5:
                return cut(s.lstrip("/ "))
        return ""
    if ext == ".md":
        for l in lines[:20]:
            if l.startswith("# "):
                return cut(l[2:])
        return ""
    return ""


def walk(rel):
    base = os.path.join(station, rel)
    found = []
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in PRUNE_DIRS and not d.endswith(".bak"))
        for f in sorted(files):
            if ".bak-" in f:
                continue
            full = os.path.join(root, f)
            if not is_source(full):
                continue
            found.append(os.path.relpath(full, base))
    return found


rows = []
for rel, note in DIRS:
    for f in walk(rel):
        full = os.path.join(station, rel, f)
        rows.append((rel + "/", f, full))
for rel, _ in LOOSE:
    full = os.path.join(station, rel)
    if os.path.isfile(full):
        d = os.path.dirname(rel)
        rows.append(((d + "/") if d else "repo root", os.path.basename(rel), full))

def purpose(sect, f, full):
    d = desc_of(full) or FALLBACK.get(sect + f, "")
    if d:
        # "gui.html — the cockpit" under a bullet that already says `gui.html` is the
        # same words twice; drop the filename the header comment repeats.
        m = re.match(r"^\s*%s\b[^—:]{0,40}[—:]\s*" % re.escape(os.path.splitext(f)[0]), d)
        if m:
            d = d[m.end():]
    return d.strip()


undocumented = [(s, f) for s, f, full in rows if not purpose(s, f, full)]

if check:
    for s, f in undocumented:
        print("  %s%s" % (s, f))
    print("undocumented files: %d" % len(undocumented))
    sys.exit(1 if undocumented else 0)

body = []
body.append("# Irsofka AI Workstation — file map")
body.append("")
body.append("Generated by `bin/arch_map.sh` at %s. **Do not edit by hand** — change the file"
            % time.strftime("%Y-%m-%d %H:%M"))
body.append("or its first comment, then re-run the script.")
body.append("")
body.append("Every description is read from the file itself (module doc, docstring, or first")
body.append("header comment). A file marked `[UNDOCUMENTED]` owes one line of its own — add it")
body.append("at the top of the file, not here.")
body.append("")
body.append("**How to use this map:** pick the section that owns the behaviour you want, then")
body.append("grep inside that directory only. Folder boundaries are the design.")
body.append("")

current = None
for sect, f, full in rows:
    if sect != current:
        current = sect
        note = dict(DIRS).get(sect.rstrip("/"), "")
        body.append("")
        body.append("## `%s`" % sect)
        body.append("")
        if note:
            body.append("> %s" % note)
            body.append("")
    try:
        n = sum(1 for _ in io.open(full, encoding="utf-8", errors="ignore"))
    except OSError:
        n = 0
    d = purpose(sect, f, full) or "[UNDOCUMENTED]"
    body.append("- `%s` (%d lines) — %s" % (f, n, d))

body.append("")
body.append("---")
body.append("")
body.append("_%d files mapped, %d undocumented._" % (len(rows), len(undocumented)))
body.append("")

text = "\n".join(body)
tmp = out_path + ".tmp"
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with io.open(tmp, "w", encoding="utf-8") as fh:
    fh.write(text)
os.replace(tmp, out_path)

print("map written: " + out_path.replace(station, "~/.ai-station").replace(os.path.expanduser("~"), "~"))
print("files mapped:   %d" % len(rows))
print("undocumented:   %d" % len(undocumented))
for s, f in undocumented:
    print("  - %s%s" % (s, f))
PY
