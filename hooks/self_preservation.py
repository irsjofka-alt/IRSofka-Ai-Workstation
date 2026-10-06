#!/usr/bin/env python3
"""
self_preservation.py — PreToolUse guard untuk Irsofka AI Workstation.

Alasan keberadaan: pada 2026-10-06 10:01 sesi AI mati bukan karena kesalahan pengguna,
melainkan karena AI itu sendiri menjalankan `pkill -f "irsofka-station-core --headless"`
yang ikut membunuh daemon produksi -> systemd auto-restart -> PTY dihancurkan -> sesi mati.
Aturan itu hanya tertulis sebagai teks di QODER.md, dan teks hilang saat konteks reset.
Skrip ini menegakkannya secara deterministik di luar memori AI.

Kebijakan:
  BLOKIR (exit 2)  = perintah yang menghapus sesi/ingatan dan tidak bisa dibatalkan.
  PERINGATKAN      = perintah yang boleh jalan, tapi AI harus tahu konsekuensinya.
  SELALU exit 0 untuk perintah yang tidak relevan -> tidak mengganggu pekerjaan normal.
"""

import json
import os
import re
import sys

STATION = os.path.expanduser("~/.ai-station")

# Proses yang kalau mati akan menghapus sesi AI yang sedang berjalan.
# "station-core" saja (bukan nama biner penuh) karena pkill/killall memakai pola
# substring: `pkill -f station-core` juga menghanguskan daemon produksi.
CRITICAL_PROC_MARKERS = (
    "station-core",
    "station-qoder",
    "station-antigravity",
)

# Direktori/file yang merupakan ingatan jangka panjang. Menghapusnya permanen.
MEMORY_ROOTS = (
    f"{STATION}/brain",
    f"{STATION}/config",
    f"{STATION}/logs",
    f"{STATION}/engine-rust",
    f"{STATION}/tools",
)

HARD_RULES = [
    (
        r"\brm\s+(?:-\w*[rR]\w*f|-\w*f\w*[rR]|[--]recursive\b[^|]*[--]force|[--]force\b[^|]*[--]recursive)\b.*"
        r"\.ai-station/(brain|config|logs|engine-rust|tools)",
        "rm -rf pada direktori ingatan workstation",
    ),
    (
        r"\b(tmux\b[^|;]*\bkill-server\b)",
        "tmux kill-server akan MEMUSNAHKAN semua tab CLI hidup (qoder/antigravity/shell) "
        "beserta sesi di dalamnya. Tab adalah satu-satunya pembawa sesi AI saat ini.",
    ),
    (
        r"\b(tmux\b[^|;]*\bkill-session\b[^|;]*\bstation-)",
        "tmux kill-session pada tab station-* akan membunuh sesi CLI yang sedang berjalan",
    ),
    (
        r"\b(tmux\b[^|;]*\b(kill-window|delete-buffer)\b)",
        "tmux kill-window/delete-buffer pada host tab workstation menghapus sesi/riwayat tab",
    ),
    (
        r"(?i)\bdrop\s+(table|database|schema)\b",
        "DROP TABLE/DATABASE menghapus save-state (world_memory, action_log, session_turns)",
    ),
    (
        r"(?i)\btruncate\s+table\b",
        "TRUNCATE TABLE mengosongkan memori jangka panjang secara permanen",
    ),
    (
        r"(?i)\bdelete\s+from\s+(world_memory|action_log|session_turns|quest_tasks|skills_inventory|incident_log)\b(?!\s+where\b)",
        "DELETE FROM tabel save-state tanpa WHERE = menghapus memori massal",
    ),
    (
        r"(?i)\b(dropdb|pg_dumpall\s+>\s*/dev/null|psql\b[^|;]*\b-c\s*[\"']drop)\b",
        "perintah penghancuran database PostgreSQL workstation",
    ),
    (
        r"\bgit\b[^|;]*\bcheckout\s+(--\s*)?\.",
        "git checkout . membuang semua suntingan yang belum di-commit di engine-rust",
    ),
]

# Berkas yang sudah ADA di jalur memori tidak boleh ditimpa dengan `>` (truncation).
# Berkas BARU di config/ atau hooks/ tetap boleh dibuat — di situlah guard ini sempat
# salah tembak tiga kali, dan guard yang salah tembak akan dimatikan orang.
MEMORY_DIRS = ("brain", "logs")

SOFT_RULES = [
    (
        r"\bsystemctl\s+--user\s+(restart|stop|kill|try-restart|reload-or-restart)\b[^|;]*\birsofka-",
        "Catatan: restart/stop daemon workstation. Sejak tab berjalan di tmux (-L irsofka) "
        "sesi CLI TIDAK ikut mati, tapi GUI window perlu dimuat ulang. Pastikan user sudah "
        "menyetujui langkah ini sebelum lanjut.",
    ),
    (
        r"\bgit\b[^|;]*\b(reset\s+--hard|clean\s+-[a-zA-Z]*f)\b",
        "Catatan: git reset --hard / clean -f menghapus pekerjaan yang belum di-commit. "
        "Cek `git status` dan `git stash` lebih dulu bila ada suntingan berharga.",
    ),
    (
        r"\bkill\b[^|;]*\b9?\s*\$?\(?pgrep",
        "Catatan: kill berbasis pgrep berisiko melebar ke proses daemon produksi.",
    ),
]


def strip_heredocs(command: str) -> str:
    """Buang ISI heredoc, pertahankan baris pembukanya.

    Tanpa ini guard salah tembak: dokumen yang MENYEBUT `tmux kill-server` di dalam
    heredoc (mis. catatan handoff atau skrip yang sedang ditulis) akan terbaca sebagai
    perintah nyata lalu diblokir. Guard yang memblokir pekerjaan normal akan segera
    dimatikan orang — dan proteksi yang dimatikan sama dengan tidak ada proteksi.
    """
    lines = command.split("\n")
    out, skip, delim = [], None, None
    for line in lines:
        if skip:
            if line.strip() == delim:
                skip = None
            continue
        match = re.search(r"<<-?\s*[\"']?(\w+)[\"']?", line)
        if match:
            delim = match.group(1)
            skip = True
            out.append(line[:match.start()] + " <<HEREDOC")
        else:
            out.append(line)
    return "\n".join(out)


def strip_comments(command: str) -> str:
    """Buang komentar `#` di luar tanda kutip (baris utuh maupun ekor perintah)."""
    cleaned = []
    for line in command.split("\n"):
        quote = None
        for idx, ch in enumerate(line):
            if quote:
                if ch == quote and line[idx - 1] != "\\":
                    quote = None
            elif ch in "\"'":
                quote = ch
            elif ch == "#" and (idx == 0 or line[idx - 1] in " \t"):
                line = line[:idx]
                break
        cleaned.append(line)
    return "\n".join(cleaned)


def scrub(command: str) -> str:
    return strip_comments(strip_heredocs(command))


def truncates_memory_file(command: str):
    """Deteksi `>` yang MENGOSONGKAN berkas memori yang sudah ada.

    Hanya brain/ dan logs/ yang berisi data tak-tergantikan. config/ dan hooks/ sengaja
    tidak ikut dibatasi — membuat berkas konfigurasi baru adalah pekerjaan normal, dan
    guard yang menghalangi pekerjaan normal akan dimatikan orang.
    """
    for match in re.finditer(r"(?<![>])>(?![>])\s*[\"']?([^\s\"';&|<>\n]+)", command):
        raw = match.group(1)
        if raw.startswith("/dev/") or raw.startswith("&"):
            continue
        path = os.path.normpath(os.path.abspath(os.path.expanduser(raw)))
        parts = path.split(os.sep)
        if ".ai-station" not in parts:
            continue
        idx = parts.index(".ai-station")
        if len(parts) > idx + 1 and parts[idx + 1] in MEMORY_DIRS and os.path.isfile(path):
            return path
    return None


def proc_cmdline(pid: str):
    try:
        with open(f"/proc/{int(pid)}/cmdline", "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except Exception:
        return ""


PRODUCTION_BIN = os.path.join(STATION, "bin", "irsofka-station-core")


def proc_exe(pid: str) -> str:
    try:
        return os.path.realpath(os.readlink(f"/proc/{int(pid)}/exe"))
    except Exception:  # noqa: BLE001
        return ""


def kill_targets_lifespan(command: str):
    """Blokir `kill <pid>` hanya bila PID itu benar-benar biner produksi workstation.

    Sengguh dibatasi ke jalur bin produksi, BUKAN ke semua proses bernama station-core:
    aturan proyek menyuruh AI mematikan proses UJIAN lewat PID spesifik, dan guard yang
    melarang itu akan membuat orang mematikan guard-nya.
    """
    if not re.search(r"(?<![\w-])kill(?![\w-])", command):
        return None
    for match in re.finditer(r"(?<![\w-])kill\s+(?:-\w+\s+)*([\d]+)", command):
        pid = match.group(1)
        exe = proc_exe(pid)
        if exe and exe == PRODUCTION_BIN:
            return pid, exe
        if not exe and any(m in proc_cmdline(pid) for m in CRITICAL_PROC_MARKERS):
            return pid, proc_cmdline(pid).strip()[:120]
    return None


def pkill_pattern(command: str):
    match = re.search(r"\b(pkill|killall)\b(.*)", command)
    if not match:
        return None
    rest = match.group(2)
    return rest if any(marker in rest for marker in CRITICAL_PROC_MARKERS) else None


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        # Jangan pernah menghentikan pekerjaan karena hook gagal membaca input.
        return 0

    if payload.get("tool_name") not in ("Bash", "mcp__local-workstation__execute_command"):
        return 0

    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command") or tool_input.get("cmd") or ""
    if not command.strip():
        return 0

    # Pola hanya dicocokkan terhadap kerangka shell-nya, bukan isi heredoc/komentar.
    scanned = scrub(command)

    for pattern, reason in HARD_RULES:
        if re.search(pattern, scanned):
            sys.stderr.write(
                f"DIBLOKIR oleh guard self-preservation workstation: {reason}\n"
                f"Perintah: {command[:200]}\n"
                "Jika memang perlu, minta PENGUNA menjalankan sendiri perintah ini "
                "(mis. lewat `! <perintah>`), atau gunakan jalur yang aman: "
                "append alih-alih redirect, `kill <pid>` spesifik hasil inspeksi, "
                "dan backup dulu.\n"
            )
            return 2

    victim = truncates_memory_file(scanned)
    if victim:
        sys.stderr.write(
            f"DIBLOKIR: redirect '>' akan menghapus ISI berkas memori {victim}.\n"
            "Gunakan alat Edit/Write, atau append (>>), atau tulis ke berkas baru.\n")
        return 2

    target = kill_targets_lifespan(scanned)
    if target:
        pid, cmdline = target
        sys.stderr.write(
            f"DIBLOKIR: kill {pid} menargetkan proses workstation yang menjaga sesi AI.\n"
            f"cmdline: {cmdline}\n"
            "Untuk menghentikan proses uji, pakai PID proses sandbox yang spesifik dan "
            "verifikasi dulu lewat /proc/<pid>/cmdline bahwa itu bukan daemon produksi.\n"
        )
        return 2

    pattern = pkill_pattern(scanned)
    if pattern:
        sys.stderr.write(
            "DIBLOKIR: pola pkill/killall ini pernah membunuh daemon produksi dan sesi AI "
            "yang berjalan di dalamnya (insiden 2026-10-06 10:01).\n"
            "QODER.md melarangnya secara eksplisit. Gunakan PID spesifik setelah "
            "memverifikasi /proc/<pid>/cmdline.\n"
        )
        return 2

    for rule, note in SOFT_RULES:
        if re.search(rule, scanned):
            print(json.dumps({
                "decision": "allow",
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": note,
                },
            }))
            return 0

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # hook tidak boleh menjatuhkan sesi
        sys.stderr.write(f"self_preservation: melewati diri sendiri karena {exc}\n")
        sys.exit(0)
