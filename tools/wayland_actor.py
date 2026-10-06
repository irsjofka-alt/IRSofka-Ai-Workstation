#!/usr/bin/env python3
"""
Wayland Visual Actor (Mata & Tangan)
Mengimplementasikan Computer-Use protocol untuk Pop!_OS COSMIC (Wayland).
Menggunakan cosmic-screenshot / grim untuk Screen Capture,
dan ydotool / uinput untuk pergerakan mouse & pengetikan keyboard.
"""

import os
import sys
import json
import shutil
import subprocess
from pathlib import Path

AI_STATION = Path.home() / ".ai-station"
LOGS_DIR = AI_STATION / "logs"
SCREENSHOT_PATH = LOGS_DIR / "current_screen.png"


class WaylandActor:
    def __init__(self):
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        self.has_cosmic_ss = shutil.which("cosmic-screenshot") is not None
        self.has_grim = shutil.which("grim") is not None
        self.has_ydotool = shutil.which("ydotool") is not None

    def capture_screen(self, output_path=SCREENSHOT_PATH):
        """Mengambil screenshot layar di Pop!_OS COSMIC Wayland"""
        if self.has_grim:
            subprocess.run(["grim", str(output_path)], check=True)
            self._prune_shots(output_path.parent)
            return str(output_path)
        elif self.has_cosmic_ss:
            # cosmic-screenshot save
            subprocess.run([
                "cosmic-screenshot",
                "--interactive=false",
                "--modal=false",
                "--notify=false",
                "-s", str(output_path.parent)
            ], check=True)
            # Ambil file terbaru di folder
            files = sorted(output_path.parent.glob("Screenshot*.png"), key=os.path.getmtime)
            if files:
                latest = files[-1]
                shutil.copy(latest, output_path)
                self._prune_shots(output_path.parent)
                return str(output_path)
        raise RuntimeError("Tidak ditemukan tool screenshot Wayland (butuh grim atau cosmic-screenshot).")

    @staticmethod
    def _prune_shots(folder, keep=1):
        """Buang tangkapan lama; hanya `keep` terbaru yang disimpan.

        Tanpa ini logs/ menumpuk puluhan MB setiap kali AI melihat layar, dan file itu
        tidak dibaca siapa-siapa — current_screen.png sudah jadi salinan tetapnya.
        """
        try:
            shots = sorted(folder.glob("Screenshot*.png"), key=os.path.getmtime, reverse=True)
            for stale in shots[keep:]:
                try:
                    stale.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def mouse_move(self, x, y):
        """Memindahkan kursor ke koordinat absolut (x, y)"""
        if self.has_ydotool:
            subprocess.run(["ydotool", "mousemove", "--absolute", str(x), str(y)], check=True)
            print(f"[Actuator] Mouse dipindahkan ke: ({x}, {y})")
        else:
            print(f"[Simulation] Mouse move ke ({x}, {y}) [ydotool belum terinstall di sistem]")

    def left_click(self, x=None, y=None):
        """Melakukan klik kiri mouse"""
        if x is not None and y is not None:
            self.mouse_move(x, y)
        if self.has_ydotool:
            subprocess.run(["ydotool", "click", "0xC0"], check=True)
            print("[Actuator] Klik Kiri dieksekusi")
        else:
            print("[Simulation] Left click [ydotool belum terinstall di sistem]")

    def right_click(self, x=None, y=None):
        """Melakukan klik kanan mouse"""
        if x is not None and y is not None:
            self.mouse_move(x, y)
        if self.has_ydotool:
            subprocess.run(["ydotool", "click", "0xC1"], check=True)
            print("[Actuator] Klik Kanan dieksekusi")
        else:
            print("[Simulation] Right click [ydotool belum terinstall di sistem]")

    def type_text(self, text):
        """Mengetik string teks"""
        if self.has_ydotool:
            subprocess.run(["ydotool", "type", text], check=True)
            print(f"[Actuator] Teks diketik: {text}")
        else:
            print(f"[Simulation] Ketik teks: {text} [ydotool belum terinstall di sistem]")


if __name__ == "__main__":
    actor = WaylandActor()
    if len(sys.argv) > 1:
        cmd = sys.argv[1]
        if cmd == "screenshot":
            path = actor.capture_screen()
            print(f"Screenshot tersimpan di: {path}")
        elif cmd == "click" and len(sys.argv) >= 4:
            x, y = int(sys.argv[2]), int(sys.argv[3])
            actor.left_click(x, y)
        elif cmd == "type" and len(sys.argv) >= 3:
            actor.type_text(" ".join(sys.argv[2:]))
        else:
            print("Usage: wayland_actor.py [screenshot | click <x> <y> | type <text>]")
    else:
        print("Wayland Actor Engine Ready.")
        print(f"Status Tool: cosmic-screenshot={actor.has_cosmic_ss}, grim={actor.has_grim}, ydotool={actor.has_ydotool}")
