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


def pane_roots() -> dict[str, int] | None:
    """The persistent set: the PID behind each tmux session on the workstation socket.

    Keyed by session name, not window name: every window here is called 'bash', so keying by
    window collapses four panes into one and leaves three tabs unprotected. Measured 2026-10-07.

    None means the pane list could not be read, and that is a different fact from an empty list.
    An empty persistent set makes every pane process — the operator's own editor included — look
    like a stray, so a caller that treats unknown as nothing has built a way to kill the workspace
    it is guarding. Unknown is refused, never flattened. Found by the independent audit of this
    file on 2026-10-07; the tmux probe itself goes through run_grouped so it cannot leak either.
    """
    try:
        out = run_grouped(
            ["tmux", "-L", TMUX_SOCKET, "list-panes", "-a",
             "-F", "#{session_name}\t#{pane_pid}"],
            timeout=10, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    roots: dict[str, int] = {}
    for line in (out.stdout or "").splitlines():
        if "\t" not in line:
            continue
        name, pid = line.rsplit("\t", 1)
        if pid.isdigit():
            roots[name] = int(pid)
    return roots or None


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
    unknown = roots is None
    keep = set() if unknown else persistent_pids(table, roots)
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
        if unknown:
            record["why"] = "daftar pane tidak terbaca — tidak ada yang boleh dipotong"
            refused.append(record)
        elif pid in keep or pid == me:
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
        "ok": not unknown,
        "persistent": None if unknown else [{"pane": name, "pid": pid}
                                            for name, pid in sorted(roots.items())],
        "engine_binaries": engine_binaries(),
        "protected": protected,
        "in_flight": refused,
        "strays": strays,
        "stray_mb": round(sum(s["rss_mb"] for s in strays), 1),
    }
    if unknown:
        result["root_error"] = ("tmux -L %s list-panes tidak mengembalikan satu pun pane. "
                                "Persistent set tidak diketahui, jadi audit ini tidak bisa "
                                "membedakan yatim dari pekerjaan operator." % TMUX_SOCKET)
    if cut_now:
        result["cut"] = cut_strays(strays, keep, table)
    return result


def cut_strays(strays: list[dict], keep=frozenset(), table: dict | None = None) -> list[dict]:
    """Terminate whole process groups, by explicit PID — never by pattern (§6 forbids pattern kills).

    A group is only killed whole when nothing persistent stands in it. A stray that escaped its
    pane by re-parenting still carries the pane's PGID, so an unconditional killpg on that PGID
    reaches the operator's CLI as well — the independent audit of 2026-10-07 named this as the
    second way this file could destroy the thing it exists to protect. When the group is shared
    with a protected PID, the cut narrows to the individual stray PIDs and says so.
    """
    done = []
    by_pid = table or {}
    kept = set(keep)
    for pgid in sorted({s["pgrp"] for s in strays}):
        guarded_group = [s for s in strays
                         if s["pgrp"] == pgid and s["pid"] in kept]
        for s in guarded_group:
            done.append({"pid": s["pid"], "pgrp": pgid,
                         "outcome": "refused: pid ada di persistent set"})
        targets = [s for s in strays if s["pgrp"] == pgid and s["pid"] not in kept]
        if not targets:
            continue
        members = {p for p, proc in by_pid.items() if proc.get("pgrp") == pgid}
        guarded = sorted(members & kept) if pgid > 1 else []
        if pgid <= 1 or guarded:
            outcome = ("pid %d tidak punya grup yang bisa dipakai (pgrp=%d)" % (targets[0]["pid"], pgid)
                       if pgid <= 1 else
                       "grup %d dihuni pid persisten %s — dipotong per PID, bukan per grup"
                       % (pgid, guarded))
            for s in targets:
                done.append({"pid": s["pid"], "pgrp": pgid,
                             "outcome": _kill_one(s["pid"]) or outcome})
            done.append({"pgrp": pgid, "outcome": outcome})
            continue
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


def _kill_one(pid: int) -> str | None:
    """SIGTERM, then SIGKILL if it is still there. Returns an outcome string, or None if gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return None
    except PermissionError:
        return f"pid {pid} refused: not our process"
    for _ in range(10):
        time.sleep(0.1)
        if not _pid_alive(pid):
            return f"pid {pid} SIGTERM"
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    return f"pid {pid} SIGKILL"


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _unused_pgid() -> int | None:
    """Sebuah pgid yang pasti kosong, supaya uji keputusan grup tidak pernah melukai proses nyata.

    Diambil dari ujung atas rentang PID kernel: nomor di sana belum pernah dialokasikan sejak boot,
    jadi sinyal yang salah alamat mati dengan ProcessLookupError alih-alih membunuh orang lain.
    """
    try:
        pid_max = int(Path("/proc/sys/kernel/pid_max").read_text().strip())
    except (OSError, ValueError):
        return None
    for candidate in range(pid_max - 2, max(2, pid_max - 64), -1):
        if not _group_alive(candidate) and not _pid_alive(candidate):
            return candidate
    return None


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
        # The success path sweeps too. A CLI that exits 0 after daemonising a worker leaves that
        # worker in this very group, and the group outlives its leader: the contract is "cut when
        # the answer has been read", not "cut when the answer timed out". Named as finding 4 of the
        # independent audit of 2026-10-07.
        _sweep_group(child.pid)
        if text:
            out = (out or b"").decode("utf-8", "replace")
            err = (err or b"").decode("utf-8", "replace") if err is not None else None
        return subprocess.CompletedProcess(cmd, child.returncode, out, err)
    except subprocess.TimeoutExpired as exc:
        _sweep_group(child.pid)
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
        _sweep_group(child.pid, immediate=True)
        raise


def _sweep_group(pgid: int, immediate: bool = False) -> None:
    """SIGTERM then SIGKILL to a process group, swallowing the honest answers.

    ProcessLookupError means the group is already empty, which is the normal outcome and not a
    failure worth raising into a caller that just wanted a probe. PermissionError means it belongs
    to someone else; the stray sweep reports that instead of retrying.

    Membership is checked before signalling, not `/proc/<pgid>`: after the leader we started is
    reaped, the directory is gone while the abandoned worker is still in that group, and a leader
    directory test would skip exactly the case this exists for. It also keeps a reused PID from
    being signalled by accident in the common case where nothing is left.
    """
    if pgid <= 1 or not _group_alive(pgid):
        return
    signals = (signal.SIGKILL,) if immediate else (signal.SIGTERM, signal.SIGKILL)
    for sig in signals:
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return
        if sig == signal.SIGTERM and len(signals) > 1:
            for _ in range(6):
                time.sleep(0.1)
                if not _group_alive(pgid):
                    return


PROBE_TOKEN = "31337"


def probe_token() -> str:
    """Token unik per berjalan, bukan konstanta yang tertulis di berkas ini.

    `_probe_survivors` mencocokkan cmdline, dan konstanta '31337' juga hidup di teks sumber ini —
    shell mana pun yang sedang membaca berkasnya dihitung sebagai cucu yang bocor. Uji Rust
    kehilangan tesnya karena sebab yang sama pada 2026-10-07.
    """
    return f"{os.getpid()}{PROBE_TOKEN}"


def _probe_cmd(body: str) -> list[str]:
    return ["sh", "-c", f"sleep {probe_token()} & {body}"]


def _probe_survivors() -> list[int]:
    want = probe_token()
    return sorted(p["pid"] for p in read_processes().values() if want in p["cmdline"])


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

    # Jalur SUKSES harus menyapu grup juga. Temuan 4 audit independen 2026-10-07: pemotongan hanya
    # terjadi di blok timeout, padahal CLI yang exit 0 setelah me-daemon-kan worker meninggalkan
    # grup tanpa pemimpin — dan grup itulah yang memegang RSS sampai sweep berikutnya sempat jalan.
    run_grouped(["sh", "-c", f"sleep {probe_token()} >/dev/null 2>&1 & exit 0"], timeout=10)
    time.sleep(0.2)
    left = _probe_survivors()
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    if left:
        print(f"  ✗ {len(left)} cucu lolos dari jalur sukses run_grouped: {left}")
        return 1
    print("  ✓ worker yang ditinggalkan perintah sukses ikut dipotong (bukan hanya yang timeout)")

    # Temuan 1: daftar pane yang tidak terbaca tidak boleh diterjemahkan menjadi "tidak ada yang
    # persisten", karena itu mengubah seluruh isi workspace operator menjadi yatim yang layak CUT.
    saved_socket = TMUX_SOCKET
    globals()["TMUX_SOCKET"] = f"tidak-ada-{probe_token()}"
    try:
        blind = audit(cut_now=True)
    finally:
        globals()["TMUX_SOCKET"] = saved_socket
    if blind["ok"] or blind["persistent"] is not None:
        print("  ✗ tmux yang gagal terbaca dilaporkan sebagai set persisten yang sah")
        return 1
    if blind["strays"] or blind["cut"]:
        print(f"  ✗ tanpa daftar pane ada yang dipotong: {len(blind['strays'])} yatim, "
              f"{len(blind['cut'])} tindakan")
        return 1
    if not blind["in_flight"]:
        print("  · audit buta tidak menemukan proses engine sama sekali — tidak ada yang dibuktikan")
        return 1
    print(f"  ✓ daftar pane tak terbaca → ok=false, {len(blind['in_flight'])} proses ditahan, "
          "0 dipotong (unknown bukan kosong)")

    # Kontrol untuk temuan 1: bahayanya nyata, bukan khayalan penjaga. roots={} diperiksa TANPA
    # cut_now, jadi tidak ada sinyal yang dikirim — cukup untuk membuktikan bahwa satu-satunya hal
    # yang menahan pane adalah keputusan None di atas.
    real_roots = pane_roots() or {}
    if len(real_roots) >= 3:
        empty_keep = persistent_pids(read_processes(), {})
        victims = [pid for pid in real_roots.values() if pid not in empty_keep]
        if not victims:
            print("  ✗ kontrol tumpul: pane akar tetap terlindungi walau daftar pane kosong")
            return 1
        print(f"  ✓ kontrol terbukti: dengan roots={{}} pane {victims} akan jadi yatim — "
              "inilah yang dicegah oleh None")

    # Temuan 2: killpg menjangkau SEMUA anggota grup, termasuk pid yang seharusnya persisten. Yatim
    # yang lolos dari pane lewat re-parent masih membawa PGID pane-nya, jadi grup itu tidak boleh
    # dipotong utuh; pemotongan menyempit ke PID yatimnya saja. Dua bentuk diuji: keputusan per grup
    # dengan pgid yang dipastikan bebas (tidak ada proses nyata yang tersentuh), dan satu proses
    # nyata yang diklaim persisten — kalau penjaga salah, proses itu mati dan ujiannya bilang begitu.
    free = _unused_pgid()
    if free is None:
        print("  · tidak menemukan pgid bebas — keputusan grup berjaga tidak bisa dibuktikan")
        return 1
    verdicts = cut_strays([{"pid": free, "pgrp": free, "binary": "sleep", "source": "selftest",
                            "exe": "", "rss_mb": 0.0, "age_s": 999.0, "cmdline": "sleep"}],
                          {free}, {free: {"pgrp": free}})
    outcome = " ".join(str(v.get("outcome", "")) for v in verdicts)
    if "persistent set" not in outcome:
        print(f"  ✗ pid persisten ikut masuk jalur pemotongan: {outcome}")
        return 1
    if _group_alive(free) or _pid_alive(free):
        print(f"  ✗ grup bebas {free} malah tercipta oleh uji ini")
        return 1
    share = cut_strays([{"pid": free, "pgrp": free, "binary": "sleep", "source": "selftest",
                         "exe": "", "rss_mb": 0.0, "age_s": 999.0, "cmdline": "sleep"}],
                      {free + 1}, {free: {"pgrp": free}, free + 1: {"pgrp": free}})
    outcome2 = " ".join(str(v.get("outcome", "")) for v in share)
    if "per PID" not in outcome2:
        print(f"  ✗ grup yang berbagi dengan pid persisten dipotong utuh: {outcome2}")
        return 1
    print("  ✓ pid persisten ditolak sama sekali; grup yang berbagi diturunkan ke potong per PID")

    report = audit()
    if report["persistent"] is None:
        print("  ✗ audit normal tidak bisa membaca daftar pane — sisanya tidak bisa diuji")
        return 1
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
    if not report["ok"]:
        rc = 1
        print("  ✗ daftar pane tidak terbaca — audit buta, tidak ada yang boleh dianggap bocor")
        print(f"     {report.get('root_error')}")
        return rc
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
        for pane in (report["persistent"] or []):
            print(f"  {pane['pane']:<22} pid {pane['pid']}")
        if not report["persistent"]:
            print("  (socket tmux tidak terdeteksi — audit tidak akan melindungi apa pun, "
                  "dan karena itu tidak boleh memotong apa pun)")
        print("=== binary yang dihitung sebagai panggilan engine ===")
        for binary, sources in sorted(report["engine_binaries"].items()):
            print(f"  {binary:<12} {', '.join(sources)}")
        return 0 if report["ok"] else 1
    if mode in ("--audit", "--cut"):
        report = audit(cut_now=(mode == "--cut"))
        print(json.dumps(report, indent=2, ensure_ascii=False))
        if not report["ok"]:
            print(f"\nAUDIT BUTA: {report['root_error']}", file=sys.stderr)
            return 1
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
