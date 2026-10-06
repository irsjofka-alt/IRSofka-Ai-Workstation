#!/usr/bin/env bash
# Bangun ulang jembatan skill: satu pohon, dibaca semua engine.
#
#   wire_skills.sh            buat/perbaiki tautan
#   wire_skills.sh --check    laporkan saja, jangan menulis
#
# Kenapa perlu: brain/skills adalah satu-satunya tempat penyimpanan, tapi Qoder mencari
# ~/.agents/skills dan Antigravity mencari plugins/station-rules/skills. Kalau tautan ini
# dibuat manual sekali lalu lupa, engine lagi-lagi bikin salinan sendiri — persis kegagalan
# yang bikin tiap AI punya cerita berbeda. Pohonnya symlink, jadi isinya tidak pernah dua.
set -uo pipefail

STATION="$HOME/.ai-station"
B="$STATION/brain/skills"
TREE="$STATION/engines/agents/skills"
GEMINI="$STATION/engines/gemini_config/plugins/station-rules"
CHECK=0
[ "${1:-}" = "--check" ] && CHECK=1

# nama-pack:arah. Lima kategori workstation diberi awalan station- supaya tidak bertabrakan
# dengan skill builtin; pack legacy memakai namanya sendiri (sudah spesifik).
PACKS="
station-visual:$B/visual
station-coding:$B/coding
station-sysadmin:$B/sysadmin
station-analysis:$B/analysis
station-learned:$B/learned
"
for p in "$B"/legacy/*/; do
  [ -f "$p/SKILL.md" ] || continue
  PACKS+="$(basename "$p"):$p
"
done

say() { printf "%s\n" "$*"; }
problem=0

link_one() { # target-pack <link> — wajib punya SKILL.md di dalamnya
  local src="$1" dst="$2"
  if [ ! -d "$src" ]; then say "  LEWAT  $dst (sumber tidak ada: $src)"; return; fi
  if [ ! -f "$src/SKILL.md" ]; then
    say "  SALAH  $src tidak punya SKILL.md — engine tidak akan menganggapnya skill"
    problem=$((problem + 1)); return
  fi
  link_tree "$src" "$dst"
}

link_tree() { # direktori-kontainer <link> — tidak menuntut SKILL.md
  local src="$1" dst="$2"
  if [ "$CHECK" = "1" ]; then
    if [ "$(readlink -f "$dst" 2>/dev/null)" = "$(readlink -f "$src")" ]; then
      say "  ok     ${dst/#$HOME/\~}"
    else
      say "  PUTUS   ${dst/#$HOME/\~} -> $(readlink "$dst" 2>/dev/null || echo '(tidak ada)')"
      problem=$((problem + 1))
    fi
  else
    mkdir -p "$(dirname "$dst")"
    ln -sfn "$src" "$dst"
    say "  taut    ${dst/#$HOME/\~}"
  fi
}

say "== pohon skill bersama =="
while IFS= read -r entry; do
  [ -n "$entry" ] || continue
  link_one "${entry#*:}" "$TREE/${entry%%:*}"
done <<< "$PACKS"

# ~/.agents harus berupa symlink, bukan direktori data. Guard self_preservation hanya
# mengizinkan bentuk ini (nama terdaftar di config/home_shims.json + sumber di workstation).
link_tree "$STATION/engines/agents" "$HOME/.agents"

link_tree "$TREE" "$GEMINI/skills"

count=$(ls -1 "$TREE" 2>/dev/null | wc -l)
say ""
say "pack tersedia: $count"
if [ "$CHECK" = "1" ]; then
  [ "$problem" -eq 0 ] && say "semua tautan sehat" || say "ADA $problem masalah"
  exit $([ "$problem" -eq 0 ] && echo 0 || echo 1)
fi
say "Qoder memuat ini tanpa restart; untuk engine lain butuh respawn tab."
