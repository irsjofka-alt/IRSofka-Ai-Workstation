#!/usr/bin/env python3
"""Uji self_preservation.py: apa yang harus diblokir, apa yang harus lolos."""
import json
import subprocess
import sys

GUARD = "/home/irsofka/.ai-station/hooks/self_preservation.py"

BLOCK = [
    'tmux -L irsofka kill-server',
    'tmux -L irsofka kill-session -t station-qoder',
    'pkill -f "irsofka-station-core --headless"',
    'pkill -9 -f station-core',
    'rm -rf /home/irsofka/.ai-station/brain',
    'rm -rf ~/.ai-station/config',
    'kill -9 377055',
    'psql -c "DROP TABLE world_memory"',
    'psql -c "TRUNCATE TABLE action_log"',
    'psql -c "delete from action_log"',
    'echo x > ~/.ai-station/brain/PROJECT_SUMMARY_IRSOFKA_AI_WORKSTATION.md',
    'echo x >> /dev/null; echo y > /home/irsofka/.ai-station/logs/handoff_deploy_1125.md',
    'git checkout .',
    # berbahaya SESUNGGUHNYA tetap tertangkap walau ada heredoc di depannya
    'echo x <<PY\naman\nPY\ntmux -L irsofka kill-server',
]

ALLOW = [
    'ls -la /home/irsofka/.ai-station',
    'git status',
    'kill 999999',
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
    'cat > /home/irsofka/.ai-station/config/berkas_baru_sekali_pakai.json <<JSON\n{"a": 1}\nJSON',
    'echo x > ~/.ai-station/brain/berkas_memori_yang_belum_ada.md',
    'echo x > ~/.ai-station/hooks/berkas_baru.py',
]

WARN = [
    'systemctl --user restart irsofka-ai-workstation.service',
    'git reset --hard HEAD~1',
]


def run(cmd):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
    res = subprocess.run([sys.executable, GUARD], input=payload,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    warned = "additionalContext" in res.stdout
    return res.returncode, warned


def main():
    bad = 0
    for label, cases, want in (("BLOKIR", BLOCK, 2), ("LOLOS", ALLOW, 0), ("PERINGATAN", WARN, 0)):
        print(f"\n--- harus {label} ---")
        for cmd in cases:
            rc, warned = run(cmd)
            ok = (rc == want) and (warned if label == "PERINGATAN" else True)
            if label == "LOLOS":
                ok = rc == 0 and not warned
            print(f"  {'ok ' if ok else 'SALAH'} rc={rc} warn={int(warned)} :: {cmd[:64].replace(chr(10), ' | ')}")
            bad += 0 if ok else 1
    print(f"\n{bad} hasil tidak sesuai harapan")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
