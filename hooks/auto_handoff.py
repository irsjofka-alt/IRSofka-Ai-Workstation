#!/usr/bin/env python3
"""
auto_handoff.py — SessionEnd hook: writes a mechanical handoff before the session dies.

Menulis handoff mekanis (berkas .md + baris world_memory) tepat sebelum sesi mati,
lalu memastikan baris log terakhir sudah masuk SQL.

Kenapa otomatis: disiplin "tulis handoff sebelum berhenti" hanya bekerja selama AI
masih ingat dan masih punya konteks. Kegagalan workstation ini berulang kali justru
terjadi saat konteks terputus — persis ketika handoff paling dibutuhkan. Hook ini
berada di luar memori AI, jadi ia tetap jalan meski AI-nya sudah lupa apa pun.
"""

import json
import os
import sys

sys.path.insert(0, os.path.expanduser("~/.ai-station/tools"))


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001
        payload = {}

    sid = payload.get("session_id") or os.environ.get("QODER_SESSION_ID") or ""
    if not sid:
        # SessionEnd dari luar Qoder tidak membawa session_id. Nama berkas transcript
        # adalah id sesi itu sendiri (Qoder menulis <uuid>.jsonl per sesi), jadi masih
        # bisa dipulihkan — tanpa ini handoff tercatat sebagai "sesi tidak diketahui"
        # dan kehilangan taut kembali ke jejak aslinya.
        transcript = payload.get("transcript_path") or ""
        if str(transcript).endswith(".jsonl"):
            sid = os.path.basename(transcript)[:-len(".jsonl")]

    from session_ingestor import Store, qoder_runtime_profile, run_once, snapshot

    store = Store()
    # Flush dulu: baris transcript terakhir biasanya belum sempat di-tail oleh watcher.
    run_once(store, qoder_runtime_profile())
    where = snapshot(store, sid, 25)
    sys.stderr.write(f"auto_handoff: {where}\n")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # noqa: BLE001
        # Sesi yang sedang mati tidak boleh ditahan atau digagalkan oleh hook pencatat.
        sys.stderr.write(f"auto_handoff gagal (catat manual dengan `ai-station snapshot`): {exc}\n")
        code = 0
    sys.exit(code)
