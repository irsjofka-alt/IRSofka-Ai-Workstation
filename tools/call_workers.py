#!/usr/bin/env python3
"""Call-worker discipline: a process an action spawns dies with that action.

The Active Workspace is the only persistent set on this workstation — the tmux panes the
operator works in (Qoder tab, Antigravity tab, shell tab). Everything else that speaks to a
model is a CALL WORKER: it exists for one dispatch, and it must be cut as soon as its answer
has been read, together with every child it left behind. The list of what is persistent is
resolved from the tmux socket and the engine registry at the moment of use, never from a
hard-coded PID and never from an engine's memory (contract §12).

Modes:
  --list              what is persistent right now, and which binaries count as engines
  --audit             live call workers that are still alive, with the reason each is safe
                      or unsafe to cut
  --cut               terminate the process group of every audited stray (explicit only)
  --guard             deploy gate: no raw engine spawner in tools/, no call left behind
  --run CMD...        run one command in its own process group with a hard timeout
  --selftest          prove the group kill reaches grandchildren, not just the direct child
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "config" / "engines.json"
PROFILES = ROOT / "config" / "cli_profiles.json"
TMUX_SOCKET = os.environ.get("STATION_TMUX_SOCKET", "irsofka")

# A worker younger than this is treated as in-flight, never as a leak. Measured dispatch cost:
# a headless `agy -p` answer lands in 5-10s, so 20s is well clear of a healthy run.
GRACE_SECONDS = 20

# Cutting these would take the workstation down. Contract §6 says the same thing about pkill.
NEVER_CUT_BINARIES = {
    "irsofka-station-core", "systemd", "init", "tmux", "python3", "bash", "sh", "node",
}

# `ollama serve` is the local model server, not a call. It matches the registry's binary name,
# so without this the auditor would report a long-running daemon as its own biggest leak.
DAEMON_SUBCOMMANDS = ("serve", "daemon")

PROC_FIELDS = ("pid", "comm", "state", "ppid", "pgrp", "session")


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def read_processes() -> dict[int, dict]:
    """Every live process on this machine, read straight from /proc (no ps parsing, no deps)."""
    table: dict[int, dict] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            # comm is parenthesised and may itself contain spaces: split around the brackets.
            head, rest = stat.split("(", 1)
            comm, tail = rest.split(") ", 1)
            fields = tail.split()
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace").strip()
            status = Path(f"/proc/{pid}/status").read_text()
            rss_kb = 0
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    rss_kb = _int(line.split()[1])
                    break
            table[pid] = {
                "pid": pid,
                "comm": comm,
                "state": fields[0],
                "ppid": _int(fields[1]),
                "pgrp": _int(fields[2]),
                "session": _int(fields[3]),
                "rss_mb": round(rss_kb / 1024, 1),
                "cmdline": cmdline,
            }
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            continue
    return table


def pane_roots() -> dict[str, int]:
    """The persistent set: the PID behind each tmux session on the workstation socket.

    Keyed by session name, not window name: every window here is called 'bash', so keying by
    window collapses four panes into one and leaves three tabs unprotected. Measured 2026-10-07.
    """
    try:
        out = subprocess.run(
            ["tmux", "-L", TMUX_SOCKET, "list-panes", "-a",
             "-F", "#{session_name}\t#{pane_pid}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if out.returncode != 0:
        return {}
    roots: dict[str, int] = {}
    for line in out.stdout.splitlines():
        if "\t" not in line:
            continue
        name, pid = line.rsplit("\t", 1)
        if pid.isdigit():
            roots[name] = int(pid)
    return roots


def persistent_pids(table: dict[int, dict], roots: dict[str, int]) -> set[int]:
    """Whole descendant tree of every pane, plus the panes themselves. Nothing here is a leak.

    Breadth-first from the roots through a children map. Walking upward per-pid instead looked
    equivalent but is not: a child whose parent had already been marked persistent would break
    out of the walk early and escape protection.
    """
    children: dict[int, list[int]] = {}
    for pid, proc in table.items():
        children.setdefault(proc["ppid"], []).append(pid)

    keep: set[int] = set(roots.values())
    frontier = list(roots.values())
    while frontier:
        pid = frontier.pop()
        for child in children.get(pid, []):
            if child not in keep:
                keep.add(child)
                frontier.append(child)
    return keep


def _argv_head(probe) -> str:
    """First executable of a registry probe: either a list, or a string split the way a shell would."""
    if isinstance(probe, list) and probe:
        return os.path.basename(str(probe[0]))
    if isinstance(probe, str) and probe.strip():
        try:
            parts = shlex.split(probe)
        except ValueError:
            return ""
        return os.path.basename(parts[0]) if parts else ""
    return ""


def engine_binaries() -> dict[str, list[str]]:
    """Which binaries count as engine calls, resolved from the registry and tab profiles.

    Nothing here is typed by hand: `binary`, the first word of each `template`, and the first
    word of every `usage` meter probe are the same commands the dispatchers and the Treasury
    actually execute. A new engine registered in the Station is audited the next second without
    touching this file. Names that are not installed are dropped, so the profile's placeholder
    'shell' does not become a fake engine.
    """
    found: dict[str, list[str]] = {}

    def add(binary: str, source: str) -> None:
        if binary and source not in found.setdefault(binary, []):
            found[binary].append(source)

    try:
        doc = json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        doc = {}
    for key, spec in (doc.get("engines") or {}).items():
        if not isinstance(spec, dict):
            continue
        add(str(spec.get("binary") or ""), f"registry:{key}.binary")
        add(_argv_head(spec.get("template")), f"registry:{key}.template")
        usage = spec.get("usage") or {}
        if isinstance(usage, dict):
            add(_argv_head(usage.get("probe")), f"registry:{key}.usage.probe")
            for extra in usage.get("extra") or []:
                if isinstance(extra, dict):
                    add(_argv_head(extra.get("probe")), f"registry:{key}.usage.extra")
    for key, spec in (doc.get("local_tiers") or {}).items():
        if isinstance(spec, dict):
            add(str(spec.get("binary") or ""), f"local_tier:{key}.binary")

    try:
        profiles = json.loads(PROFILES.read_text())
    except (OSError, ValueError):
        profiles = {}
    for tab, spec in profiles.items():
        if isinstance(spec, dict):
            add(str(spec.get("engine") or ""), f"profile:{tab}")

    return {name: sources for name, sources in found.items()
            if name and name not in NEVER_CUT_BINARIES and shutil.which(name)}


def _exe_of(pid: int) -> str:
    """Resolved executable path of a process, with the ' (deleted)' marker stripped.

    A CLI that self-updates keeps running from a file that no longer exists: measured on this
    workstation, `agy` (pid 498148) reports '/…/runtime/local/bin/agy.1791344384795052416.old
    (deleted)'. Comparing to realpath alone would make it invisible to the audit.
    """
    try:
        raw = os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return ""
    return raw[:-len(" (deleted)")] if raw.endswith(" (deleted)") else raw


def _prefix_of(basename: str) -> str:
    """Strip a trailing version so 'qodercli-1.1.65' also recognises its own future releases."""
    match = re.match(r"^(.*?)[-_]\d[\d.]*$", basename)
    return match.group(1) if match else basename


def engine_identities() -> list[tuple[str, str, str]]:
    """(directory, name-prefix, binary) triples that identify a real engine call on this machine.

    Identity comes from where the executable actually lives, not from what argv[0] says. This is
    the difference between a leak and a disaster: the Qoder desktop app is also named `qoder`, and
    an audit keyed on the bare name reported its 15 Electron processes as 2.1 GB of strays.
    """
    triples = []
    for name in engine_binaries():
        resolved = shutil.which(name)
        if not resolved:
            continue
        real = os.path.realpath(resolved)
        triples.append((os.path.dirname(real), _prefix_of(os.path.basename(real)), name))
    return triples


def matches_identity(exe: str, identities) -> str:
    """Which engine a resolved executable path belongs to, or '' when it belongs to none."""
    if not exe or "/Apps/" in exe or exe.endswith(".AppImage"):
        return ""
    dirname, basename = os.path.dirname(exe), os.path.basename(exe)
    for path, prefix, name in identities:
        if dirname == path and basename.startswith(prefix):
            return name
    return ""


def _identity_of(proc: dict, identities) -> str:
    return matches_identity(_exe_of(proc["pid"]), identities)


def _starts_engine(proc: dict, identities) -> str:
    return _identity_of(proc, identities)


def _is_daemon(proc: dict) -> bool:
    """`agy`/`ollama` invoked as a service is infrastructure, not a call that should be cut."""
    argv = proc["cmdline"].split()[1:]
    return any(word in DAEMON_SUBCOMMANDS for word in argv[:2])


def process_age(pid: int) -> float:
    """Seconds since the process started, from the kernel's own clock.

    /proc/<pid>/stat field 22 is starttime in clock ticks since boot; st_atime on the same file
    says 'just now' because every read refreshes it, which would silently make every worker look
    young enough to spare.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ticks = float(stat.split(") ", 1)[1].split()[19])
        boot = 0.0
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                boot = float(line.split()[1])
                break
        if not boot:
            return 0.0
        return max(0.0, time.time() - (boot + ticks / os.sysconf("SC_CLK_TCK")))
    except (OSError, IndexError, ValueError):
        return 0.0


def audit(cut_now: bool = False) -> dict:
    table = read_processes()
    roots = pane_roots()
    keep = persistent_pids(table, roots)
    identities = engine_identities()
    me = os.getpid()

    strays, protected, refused = [], [], []
    for pid, proc in sorted(table.items()):
        source = _starts_engine(proc, identities)
        if not source:
            continue
        if proc["comm"] in NEVER_CUT_BINARIES:
            continue
        record = {
            "pid": pid, "binary": proc["comm"], "source": source,
            "exe": _exe_of(pid), "rss_mb": proc["rss_mb"], "pgrp": proc["pgrp"],
            "age_s": round(process_age(pid), 1),
            "cmdline": proc["cmdline"][:160],
        }
        if pid in keep or pid == me:
            record["why"] = "inside an Active Workspace pane tree"
            protected.append(record)
        elif _is_daemon(proc):
            record["why"] = "dijalankan sebagai service, bukan sebagai panggilan"
            protected.append(record)
        elif proc["session"] != proc["pgrp"] and proc["ppid"] not in (0, 1) \
                and proc["ppid"] in table and _starts_engine(table[proc["ppid"]], identities):
            # A child of a live engine is part of that engine's group, not its own leak.
            record["why"] = "child of a live engine call — cut its group instead"
            protected.append(record)
        elif record["age_s"] < GRACE_SECONDS:
            record["why"] = f"younger than the {GRACE_SECONDS}s grace window — may be in flight"
            refused.append(record)
        else:
            record["why"] = "not in any pane tree and older than the grace window"
            strays.append(record)

    result = {
        "ok": True,
        "persistent": [{"pane": name, "pid": pid} for name, pid in sorted(roots.items())],
        "engine_binaries": engine_binaries(),
        "protected": protected,
        "in_flight": refused,
        "strays": strays,
        "stray_mb": round(sum(s["rss_mb"] for s in strays), 1),
    }
    if cut_now:
        result["cut"] = cut_strays(strays)
    return result


def cut_strays(strays: list[dict]) -> list[dict]:
    """Terminate whole process groups, by explicit PID — never by pattern (§6 forbids pattern kills)."""
    done = []
    groups = {s["pgrp"] for s in strays if s["pgrp"] > 1}
    for pgid in sorted(groups):
        try:
            os.killpg(pgid, signal.SIGTERM)
            outcome = "SIGTERM"
        except ProcessLookupError:
            done.append({"pgrp": pgid, "outcome": "already gone"})
            continue
        except PermissionError:
            done.append({"pgrp": pgid, "outcome": "refused: not our process"})
            continue
        for _ in range(20):
            time.sleep(0.1)
            if not _group_alive(pgid):
                break
        else:
            try:
                os.killpg(pgid, signal.SIGKILL)
                outcome = "SIGKILL"
            except (ProcessLookupError, PermissionError):
                pass
        done.append({"pgrp": pgid, "outcome": outcome})
    return done


def _group_alive(pgid: int) -> bool:
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
            fields = stat.split(") ", 1)[1].split()
            if _int(fields[2]) == pgid:
                return True
        except (OSError, IndexError):
            continue
    return False


def run_grouped(cmd: list[str], timeout: float, *, stdin: bytes | None = None,
                env: dict | None = None, text: bool = False,
                merge_stderr: bool = False) -> subprocess.CompletedProcess:
    """Run one command in its own process group and guarantee the group dies with the call.

    subprocess.run(timeout=...) only kills the direct child, so a CLI that forked a worker
    leaves it behind holding its RSS — proven by this module's selftest, which measures exactly
    one grandchild surviving that path. Here the command leads a new session, and on timeout the
    whole group is signalled: that is what makes the contract's 'cut when finished' true rather
    than intended.

    `merge_stderr` reproduces `stderr=subprocess.STDOUT`, because dispatchers parse the answer
    out of one interleaved stream; changing that would be a second, unrelated behaviour change.
    """
    stderr_mode = subprocess.STDOUT if merge_stderr else subprocess.PIPE
    child = subprocess.Popen(
        cmd, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=stderr_mode, start_new_session=True, env=env,
    )
    try:
        out, err = child.communicate(input=stdin, timeout=timeout)
        if text:
            out = (out or b"").decode("utf-8", "replace")
            err = (err or b"").decode("utf-8", "replace") if err is not None else None
        return subprocess.CompletedProcess(cmd, child.returncode, out, err)
    except subprocess.TimeoutExpired as exc:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(child.pid, sig)
            except (ProcessLookupError, PermissionError):
                break
            if sig == signal.SIGTERM:
                time.sleep(0.5)
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        partial = exc.stdout or b""
        if text and isinstance(partial, bytes):
            partial = partial.decode("utf-8", "replace")
        raise subprocess.TimeoutExpired(cmd, timeout, output=partial,
                                        stderr=exc.stderr or b"") from None
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        raise


PROBE_TOKEN = "31337"


def _probe_cmd(body: str) -> list[str]:
    return ["sh", "-c", f"sleep {PROBE_TOKEN} & {body}"]


def _probe_survivors() -> list[int]:
    return sorted(p["pid"] for p in read_processes().values() if PROBE_TOKEN in p["cmdline"])


def selftest() -> int:
    """Prove the group kill reaches a grandchild, which is the only part that actually leaks."""
    print("selftest call_workers:")

    # Control first: the plain subprocess.run(timeout=) path this workstation used everywhere.
    try:
        subprocess.run(_probe_cmd("wait"), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=2)
    except subprocess.TimeoutExpired:
        pass
    leaked = _probe_survivors()
    if leaked:
        print(f"  ✓ kontrol terbukti: subprocess.run(timeout=) meninggalkan {len(leaked)} cucu "
              f"hidup (pid {leaked}) — inilah kebocoran yang dilarang kontrak")
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(0.3)
    else:
        print("  · kontrol: tidak ada cucu yang tertinggal — tidak bisa membuktikan apa pun")

    started = time.time()
    try:
        run_grouped(_probe_cmd("wait"), timeout=2)
        print("  ✗ perintah tidak ternyata menggantung")
        return 1
    except subprocess.TimeoutExpired:
        pass
    wall = time.time() - started
    survivors = _probe_survivors()
    if survivors:
        print(f"  ✗ {len(survivors)} anak lolos dari pemotongan grup: {survivors}")
        return 1
    print(f"  ✓ cucu yang menggantung ikut mati dalam {wall:.1f}s (timeout 2s, grup dipotong)")

    ok = run_grouped(["printf", "hello"], timeout=10)
    if (ok.stdout or b"").decode().strip() != "hello" or ok.returncode != 0:
        print("  ✗ jalur sukses berubah bentuk")
        return 1
    print("  ✓ perintah yang selesai normal tetap mengembalikan output apa adanya")

    report = audit()
    if not report["engine_binaries"]:
        print("  ✗ registry engine tidak terbaca — audit tidak akan tahu apa yang dihitung")
        return 1
    if len(report["persistent"]) < 3:
        print(f"  ✗ hanya {len(report['persistent'])} pane terdeteksi — pane lain tidak terlindungi")
        return 1
    print(f"  ✓ persistent = {[p['pane'] for p in report['persistent']]}, "
          f"{len(report['engine_binaries'])} binary engine dikenali dari registry")

    probed = [b for b, src in report["engine_binaries"].items()
              if any(".usage." in s for s in src)]
    if not probed:
        print("  ✗ meter Treasury tidak ikut dihitung sebagai panggilan engine")
        return 1
    print(f"  ✓ meter usage ikut menurunkan binary ({', '.join(probed)}) — tidak ada nama yang diketik tangan")

    ghosts = [b for b in report["engine_binaries"] if not shutil.which(b)]
    if ghosts:
        print(f"  ✗ binary tidak terpasang ikut diawasi: {ghosts}")
        return 1
    print(f"  ✓ hanya binary yang benar-benar terpasang yang diawasi ({', '.join(sorted(report['engine_binaries']))})")

    identities = engine_identities()
    traps = [f"{Path.home()}/Apps/Qoder-x64/opt/Qoder/qoder",
             f"{Path.home()}/Apps/Stuff/qoder"]
    caught = [p for p in traps if matches_identity(p, identities)]
    if caught:
        print(f"  ✗ aplikasi desktop dikenali sebagai panggilan: {caught} — 2 GB akan ikut terpotong")
        return 1
    print(f"  ✓ {len(traps)} path aplikasi desktop ditolak sebagai panggilan engine")

    for path, prefix, name in identities:
        if matches_identity(f"{path}/{prefix}.1791344384795052416.old", identities) != name:
            print(f"  ✗ {name} yang berjalan dari file lama tidak dikenali")
            return 1
        if matches_identity(f"{path}/totally-other-tool", identities):
            print(f"  ✗ tetangga satu direktori dengan {name} ikut dianggap engine")
            return 1
    print(f"  ✓ {len(identities)} identitas: binary lama yang masih jalan dikenali, tetangganya tidak")

    both = {p["pid"] for p in report["protected"]} & {p["pid"] for p in report["strays"]}
    if both:
        print(f"  ✗ pid {sorted(both)} diklasifikasikan terlindungi DAN bocor sekaligus")
        return 1
    print("  ✓ tidak ada pid yang berdiri di dua kelas sekaligus")

    root = report["persistent"][0]
    keep = persistent_pids(read_processes(), {root["pane"]: root["pid"]})
    if root["pid"] not in keep:
        print("  ✗ akar pane tidak terlindungi oleh tree-nya sendiri")
        return 1
    print(f"  ✓ pane {root['pane']} (pid {root['pid']}) dan seluruh keturunannya tidak pernah dipotong")

    clash = NEVER_CUT_BINARIES & set(report["engine_binaries"])
    if clash:
        print(f"  ✗ {sorted(clash)} terdaftar sebagai engine sekaligus sebagai yang tak boleh mati")
        return 1
    print(f"  ✓ {len(NEVER_CUT_BINARIES)} nama terlindungi, tidak bersinggungan dengan registry")
    return 0


RAW_SPAWN = re.compile(r"subprocess\.(run|check_output|call|Popen)\b")


def unbounded_engine_calls() -> list[str]:
    """Berkas yang masih memanggil biner engine lewat subprocess langsung.

    Ini pemeriksaan bentuk, bukan ingatan: daftarnya dihitung dari `engine_binaries()`, jadi
    engine baru yang didaftarkan di Station otomatis ikut dijaga. `run_grouped` di sekitarnya
    dianggap sah — pemanggilnya sudah memegang grup prosesnya.
    """
    binaries = set(engine_binaries())
    offenders: list[str] = []
    for path in sorted((ROOT / "tools").glob("*.py")):
        if path.name == os.path.basename(__file__):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        for match in RAW_SPAWN.finditer(text):
            window = text[max(0, match.start() - 150):match.start() + 300]
            if "run_grouped" in window:
                continue
            if not re.search(r"[\"'\[]\s*(%s)" % "|".join(map(re.escape, binaries)), window):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{text[:match.start()].count(chr(10)) + 1}")
    return offenders


def guard() -> int:
    """Pintu deploy: tidak ada spawner liar, tidak ada panggilan yang tertinggal."""
    print("guard call-worker:")
    rc = 0
    offenders = unbounded_engine_calls()
    if offenders:
        rc = 1
        print(f"  ✗ {len(offenders)} pemanggilan engine masih lewat subprocess langsung "
              f"(anak prosesnya tidak ikut mati):")
        for item in offenders:
            print(f"      {item}")
    else:
        print(f"  ✓ {len(engine_binaries())} biner engine hanya dipanggil lewat run_grouped")

    report = audit()
    if report["strays"]:
        rc = 1
        print(f"  ✗ {len(report['strays'])} panggilan engine tertinggal "
              f"({report['stray_mb']} MB): {[s['pid'] for s in report['strays']]}")
        print("     jalankan: python3 tools/call_workers.py --cut")
    else:
        print(f"  ✓ 0 panggilan tertinggal; {len(report['protected'])} proses pane terlindungi")
    return rc


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    mode = argv[0]
    if mode == "--selftest":
        return selftest()
    if mode == "--guard":
        return guard()
    if mode == "--list":
        report = audit()
        print("=== Active Workspace (persisten) ===")
        for pane in report["persistent"]:
            print(f"  {pane['pane']:<22} pid {pane['pid']}")
        if not report["persistent"]:
            print("  (socket tmux tidak terdeteksi — audit tidak akan melindungi apa pun)")
        print("=== binary yang dihitung sebagai panggilan engine ===")
        for binary, sources in sorted(report["engine_binaries"].items()):
            print(f"  {binary:<12} {', '.join(sources)}")
        return 0
    if mode in ("--audit", "--cut"):
        report = audit(cut_now=(mode == "--cut"))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report["strays"] and mode == "--audit":
            print("\n0 panggilan yang tertinggal — tidak ada yang perlu di-CUT")
        return 0
    if mode == "--run":
        try:
            res = run_grouped(argv[1:], timeout=60)
        except subprocess.TimeoutExpired:
            print("dipotong: melewati batas 60s, seluruh grup ikut mati", file=sys.stderr)
            return 124
        sys.stdout.write((res.stdout or b"").decode("utf-8", "replace"))
        sys.stderr.write((res.stderr or b"").decode("utf-8", "replace"))
        return res.returncode or 0
    print(f"mode tidak dikenal: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
