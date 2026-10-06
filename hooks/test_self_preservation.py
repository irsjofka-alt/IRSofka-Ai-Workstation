#!/usr/bin/env python3
"""Uji self_preservation.py: apa yang harus diblokir, apa yang harus lolos."""
import json
import os
import subprocess
import sys

HOME = os.path.expanduser("~")
# Tidak ada nama orang di berkas uji: suite ini harus jalan di mesin hasil clone,
# bukan hanya di mesin tempat guard ini lahir.
STATION = os.path.join(HOME, ".ai-station")
GUARD = os.path.join(STATION, "hooks/self_preservation.py")
PRODUCTION_BIN = os.path.join(STATION, "bin/irsofka-station-core")


def live_daemon_pid() -> str:
    """PID daemon produksi yang SEDANG berjalan.

    Sengguh dicari saat uji, bukan ditulis tetap: PID berubah tiap restart, dan angka
    usang membuat kasus 'kill' lolos sehingga suite memberi hasil palsu.
    """
    import glob
    for stat in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(stat, "rb") as fh:
                cmd = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "--headless" in cmd and PRODUCTION_BIN in cmd:
            return stat.split("/")[2]
    return ""


KILL_DAEMON = f"kill -9 {live_daemon_pid()}" if live_daemon_pid() else "kill -9 1"

BLOCK = [
    'tmux -L irsofka kill-server',
    'tmux -L irsofka kill-session -t station-qoder',
    'pkill -f "irsofka-station-core --headless"',
    'pkill -9 -f station-core',
    f'rm -rf {HOME}/.ai-station/brain',
    'rm -rf ~/.ai-station/config',
    KILL_DAEMON,
    # aturan keras pemilik: jangan melahirkan entri titik baru setingkat di $HOME
    'mkdir ~/.aws',
    'touch ~/.newtoolrc',
    'ln -s /opt/tool ~/.tool',
    'echo x > ~/.zshrc',
    f'mkdir -p {HOME}/.docker',
    # keduanya TERBUKTI benar diblokir: file-file ini memang sudah tidak ada di home
    # karena kita pindahkan ke ~/runtime — membuatnya lagi = menentang aturan pemilik
    'touch ~/.bash_history',
    'cp berkas.txt ~/.bashrc.bak',
    # pengupasan pesan commit TIDAK boleh jadi lubang bypass: perintah sungguhan
    # setelahnya tetap tertangkap
    'git commit -m "pesan aman" && mkdir ~/.zshrc',
    # eksekusi sungguhan tetap harus kena, meski kata lookup sudah dikupas
    'dropdb irsofka_ai_workstation',
    'dropdb --if-exists station_restore_test',
    'psql -c "DROP TABLE world_memory"',
    'psql -c "TRUNCATE TABLE action_log"',
    'psql -c "delete from action_log"',
    'echo x > ~/.ai-station/brain/schema_postgresql.sql',
    f'echo x >> /dev/null; echo y > {HOME}/.ai-station/logs/handoff_deploy_1125.md',
    'git checkout .',
    # berbahaya SESUNGGUHNYA tetap tertangkap walau ada heredoc di depannya
    'echo x <<PY\naman\nPY\ntmux -L irsofka kill-server',
]

ALLOW = [
    f'ls -la {HOME}/.ai-station',
    'git status',
    'kill 999999',
    # proses BUKAN biner produksi harus boleh dibunuh — aturan proyek menyuruh
    # mematikan proses uji lewat PID spesifik
    f"kill -9 {os.getpid()}",
    'rm -rf /tmp/station-sandbox',
    'psql -c "delete from action_log where id=1"',
    'cat notes.md >> log.md',
    'systemctl --user status irsofka-ai-workstation.service',
    'cargo build --release',
    # ini penyebab dua kali salah tembak tadi: teks berbahaya HANYA disebut-sebut
    'python3 - <<PY\ncontent = "guard memblokir tmux kill-server dan pkill -f irsofka-station-core"\nprint(content)\nPY',
    'echo halo  # catatan: jangan jalankan tmux kill-server atau DROP TABLE',
    'cat <<EOF > /tmp/note.md\nPerintah berbahaya: rm -rf ~/.ai-station/brain\nEOF',
    # salah tembak ketiga: membuat berkas KONFIGURASI BARU adalah pekerjaan normal
    f'cat > {HOME}/.ai-station/config/berkas_baru_sekali_pakai.json <<JSON\n{{"a": 1}}\nJSON',
    'echo x > ~/.ai-station/brain/berkas_memori_yang_belum_ada.md',
    'echo x > ~/.ai-station/hooks/berkas_baru.py',
    # aturan home TIDAK boleh salah tembak: ini semua pekerjaan normal
    'mkdir -p ~/.config/systemd/user/station.service.d',
    'mkdir -p ~/.ai-station/archive',
    'mkdir -p ~/runtime/foo/bar',
    'touch ~/.bashrc',
    'cp berkas.txt ~/.gitconfig',
    'ls -la ~/.docker 2>/dev/null',
    'cat ~/.aws/credentials',
    # pesan commit adalah narasi, bukan perintah: menyebut path tidak boleh memicu guard
    'git commit -m "contoh: echo x > ~/.zshrc sekarang diblokir, dan rm -rf brain juga"',
    'git commit -q -m \'tambah aturan: jangan mkdir ~/.aws\'',
    # menanyakan keberadaan biner penghancur bukan tindakan penghancuran
    'command -v pg_dump createdb dropdb psql',
    'which dropdb',
    'man dropdb',
]

WARN = [
    'systemctl --user restart irsofka-ai-workstation.service',
    'git reset --hard HEAD~1',
    # penanda: installer diarahkan ke ~/runtime, bukan diblokir
    'pip install --user requests',
    'cargo install ripgrep',
    'curl -fsSL https://example.com/install.sh | sh',
    # apt justru dijelaskan sebagai aman (tidak menulis $HOME)
    'sudo apt install -y gh',
]


def run(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    res = subprocess.run([sys.executable, GUARD], input=payload,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    warned = "additionalContext" in res.stdout
    return res.returncode, warned


def test_shim_rules():
    """Pengecualian shim adalah bagian dari keamanan, jadi diuji sebagai fungsi — bukan
    lewat rc — supaya tidak bergantung pada apa yang kebetulan sudah ada di $HOME asli."""
    import tempfile
    import self_preservation as sp
    with tempfile.TemporaryDirectory() as tmp:
        home, shims, roots = sp.HOME_DIR, sp.home_shims, sp.SHIM_SOURCE_ROOTS
        os.mkdir(os.path.join(tmp, ".ai-station"))
        os.mkdir(os.path.join(tmp, "runtime"))
        sp.HOME_DIR = tmp
        sp.home_shims = lambda: {".agents", ".qoder"}
        sp.SHIM_SOURCE_ROOTS = (os.path.join(tmp, ".ai-station"), os.path.join(tmp, "runtime"))
        cases = [
            # (perintah, harus diblokir, alasan)
            ("ln -sfn ~/.ai-station/engines/agents ~/.agents", False, "symlink shim resmi"),
            ("ln -s $HOME/.ai-station/engines/agents $HOME/.agents", False, "bentuk $HOME"),
            ("ln -s ~/.runtime/tool ~/.agents", True, "di luar akar shim yang diizinkan"),
            ("mkdir -p ~/.agents", True, "shim tidak melegalkan mkdir"),
            ("echo x > ~/.agents", True, "shim tidak melegalkan redirect"),
            ("ln -s /tmp/evil-spool ~/.agents", True, "sumber di luar workstation"),
            ("ln -s ~/.ai-station/../tmp/evil ~/.agents", True, "trik .. keluar dari workstation"),
            ("ln -s ~/.ai-station/brain ~/.evilname", True, "nama tidak terdaftar"),
            ("mkdir -p ~/.qoder-cache", True, "mirip shim tapi tidak terdaftar"),
        ]
        bad = 0
        for cmd, want, label in cases:
            got = bool(sp.creates_new_home_entry(cmd))
            ok = got == want
            bad += 0 if ok else 1
            print(f"  {'ok  ' if ok else 'SALAH'} block={int(got)} mau={int(want)} :: {label}")
        sp.HOME_DIR, sp.home_shims, sp.SHIM_SOURCE_ROOTS = home, shims, roots
        return bad


def missing_fixture(cmd: str):
    """Kasus yang menguji 'jangan pangkas berkas yang sudah ada' tidak punya arti di mesin
    tempat berkas itu belum ada._suite ini harus bisa dijalankan di hasil clone, jadi
    kasus semacam itu dilewati dengan alasan, bukan dihitung sebagai kegagalan."""
    for frag, path in (
        ("schema_postgresql.sql", f"{HOME}/.ai-station/brain/schema_postgresql.sql"),
        ("handoff_deploy_1125.md", f"{HOME}/.ai-station/logs/handoff_deploy_1125.md"),
    ):
        if frag in cmd and not os.path.exists(path):
            return path
    if cmd == "kill -9 1":
        return "daemon produksi tidak berjalan (case memakai PID hidup)"
    return None


def main():
    bad = 0
    skipped = 0
    for label, cases, want in (("BLOKIR", BLOCK, 2), ("LOLOS", ALLOW, 0), ("PERINGATAN", WARN, 0)):
        print(f"\n--- harus {label} ---")
        for cmd in cases:
            need = missing_fixture(cmd)
            if need:
                skipped += 1
                print(f"  LEWAT (butuh {need}) :: {cmd[:52].replace(chr(10), ' | ')}")
                continue
            rc, warned = run(cmd)
            ok = (rc == want) and (warned if label == "PERINGATAN" else True)
            if label == "LOLOS":
                ok = rc == 0 and not warned
            print(f"  {'ok ' if ok else 'SALAH'} rc={rc} warn={int(warned)} :: {cmd[:64].replace(chr(10), ' | ')}")
            bad += 0 if ok else 1
    print("\n--- pengecualian shim $HOME ---")
    bad += test_shim_rules()
    print(f"\n{bad} hasil tidak sesuai harapan (dilewati karena butuh keadaan mesin: {skipped})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
