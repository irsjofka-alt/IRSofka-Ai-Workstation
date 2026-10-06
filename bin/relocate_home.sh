#!/usr/bin/env bash
# Rapikan $HOME: pindahkan direktori data ke ~/runtime, bersebelahan dengan
# Documents / Downloads / Music / Pictures / Videos.
#
#   ./relocate_home.sh                 -> lihat rencana (TIDAK mengubah apa pun)
#   ./relocate_home.sh --apply         -> pindahkan semuanya
#   ./relocate_home.sh --apply --only .cache,.local,.bun
#                                      -> hanya item tertentu (cargo/rustup lebih berisiko)
#   ./relocate_home.sh --unpin --apply -> buang symlink kompatibilitas (SETELAH REBOOT)
#
# Kenapa symlink kompatibilitas ditinggalkan sementara:
#   Proses yang sedang berjalan (Brave memegang ribuan handle di ~/.cache dan ~/.local,
#   WebKit GUI workstation menyimpan storage-nya di ~/.local/share) mengingat PATH lama,
#   bukan inode. Kalau path lama lenyap, ia membuat direktori baru yang kosong di sana
#   dan datanya terbelah dua tanpa pesan error apa pun. Symlink membuat path lama dan
#   baru menunjuk ke tempat yang sama. Setelah reboot tidak ada lagi proses dengan path
#   basi, jadi symlink boleh dibuang.
set -uo pipefail

RUNTIME="$HOME/runtime"
APPLY=0; UNPIN=0; ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1 ;;
    --unpin) UNPIN=1 ;;
    --only)  ONLY="${2:-}"; shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "argumen tak dikenal: $1"; exit 2 ;;
  esac
  shift
done

MAP=(".cache|cache" ".local|local" ".rustup|rustup" ".cargo|cargo" ".bun|bun")
have() { [ -e "$1" ] || [ -L "$1" ]; }

selected() {
  [ -z "$ONLY" ] && return 0
  case ",$ONLY," in *",$1,"*) return 0 ;; *) return 1 ;; esac
}

if [ "$UNPIN" = "1" ]; then
  echo "=== membuang symlink kompatibilitas di \$HOME ==="
  for pair in "${MAP[@]}"; do
    old="${pair%%|*}"
    if [ -L "$HOME/$old" ]; then
      echo "  unlink ~/$old -> $(readlink "$HOME/$old")"
      [ "$APPLY" = "1" ] && rm "$HOME/$old"
    elif have "$HOME/$old"; then
      echo "  ~/$old masih direktori asli, dibiarkan"
    fi
  done
  [ "$APPLY" = "1" ] || echo "  (dry-run; pakai --unpin --apply)"
  exit 0
fi

mkdir -p "$RUNTIME" 2>/dev/null || { echo "❌ tidak bisa membuat $RUNTIME"; exit 1; }

echo "=== RENCANA: $HOME  ->  $RUNTIME ==="
n=0
for pair in "${MAP[@]}"; do
  old="${pair%%|*}"; dst="$RUNTIME/${pair##*|}"
  selected "$old" || { echo "  $old tidak dipilih (--only), lewati"; continue; }
  [ -L "$HOME/$old" ] && { echo "  ~/$old sudah symlink, lewati"; continue; }
  have "$HOME/$old" || { echo "  ~/$old tidak ada, lewati"; continue; }
  have "$dst" && { echo "  ⚠ $dst sudah ada — tidak menimpa, perlu keputusan manual"; continue; }
  [ "$(stat -c %d "$HOME")" = "$(stat -c %d "$(dirname "$HOME/$old")")" ] || { echo "  ⚠ ~/$old beda filesystem, dilewati"; continue; }
  echo "  ~/$old ($(du -sh "$HOME/$old" 2>/dev/null | cut -f1)) -> $dst   + symlink ~/$old"
  n=$((n+1))
done
echo "  item siap dipindah: $n"

echo
echo "=== konfigurasi yang ditulis saat --apply ==="
cat <<EOF
  $HOME/.config/environment.d/irsofka.conf   -> dibaca systemd-user, berlaku untuk sesi GUI
    XDG_CACHE_HOME=$RUNTIME/cache
    XDG_DATA_HOME=$RUNTIME/local/share
    XDG_STATE_HOME=$RUNTIME/local/state
    RUSTUP_HOME=$RUNTIME/rustup  CARGO_HOME=$RUNTIME/cargo  BUN_INSTALL=$RUNTIME/bun
    HISTFILE=$RUNTIME/bash_history
  $HOME/.bashrc.d/irsofka-runtime.sh          -> untuk shell interaktif (di-source .bashrc)
  drop-in systemd: irsofka-ai-workstation.service.d/runtime-path.conf
    (unit lama menulis PATH=.../.cargo/bin dan .../.local/bin, jadi harus ikut dipindah)
EOF

[ "$APPLY" = "1" ] || { echo; echo "(dry-run — tidak ada yang diubah. Jalankan: $0 --apply)"; exit 0; }

echo
echo "=== MENERAPKAN ==="
for pair in "${MAP[@]}"; do
  old="${pair%%|*}"; dst="$RUNTIME/${pair##*|}"
  selected "$old" || continue
  [ -L "$HOME/$old" ] && continue
  have "$HOME/$old" || continue
  have "$dst" && { echo "  lewati ~/$old (tujuan sudah ada)"; continue; }
  if mv "$HOME/$old" "$dst" && ln -s "$dst" "$HOME/$old"; then
    echo "  ✓ ~/$old -> $dst"
  else
    echo "  ✗ GAGAL ~/$old — hentikan dan periksa sebelum lanjut"
  fi
done

mkdir -p "$HOME/.config/environment.d" "$HOME/.bashrc.d"
cat > "$HOME/.config/environment.d/irsofka.conf" <<EOF
# Dibuat relocate_home.sh $(date '+%Y-%m-%d %H:%M'). Hapus berkas ini untuk kembali ke bawaan.
XDG_CACHE_HOME=$RUNTIME/cache
XDG_DATA_HOME=$RUNTIME/local/share
XDG_STATE_HOME=$RUNTIME/local/state
RUSTUP_HOME=$RUNTIME/rustup
CARGO_HOME=$RUNTIME/cargo
BUN_INSTALL=$RUNTIME/bun
HISTFILE=$RUNTIME/bash_history
EOF

cat > "$HOME/.bashrc.d/irsofka-runtime.sh" <<EOF
# Dibuat relocate_home.sh — pasangan environment.d untuk shell.
export XDG_CACHE_HOME="\$HOME/runtime/cache"
export XDG_DATA_HOME="\$HOME/runtime/local/share"
export XDG_STATE_HOME="\$HOME/runtime/local/state"
export RUSTUP_HOME="\$HOME/runtime/rustup"
export CARGO_HOME="\$HOME/runtime/cargo"
export BUN_INSTALL="\$HOME/runtime/bun"
export HISTFILE="\$HOME/runtime/bash_history"
for _d in "\$CARGO_HOME/bin" "\$XDG_DATA_HOME/../bin" "\$HOME/runtime/local/bin" "\$BUN_INSTALL/bin"; do
  case ":\$PATH:" in *":\$_d:"*) ;; *) [ -d "\$_d" ] && PATH="\$_d:\$PATH" ;; esac
done
unset _d
export PATH
EOF

grep -q 'bashrc.d/irsofka-runtime' "$HOME/.bashrc" 2>/dev/null || cat >> "$HOME/.bashrc" <<'EOF'

# Runtime di luar titik-titik tersembunyi: ~/runtime (lihat relocate_home.sh)
if [ -d "$HOME/.bashrc.d" ]; then
    for _rc in "$HOME"/.bashrc.d/*.sh; do
        [ -f "$_rc" ] && . "$_rc"
    done
    unset _rc
fi
EOF

mkdir -p "$HOME/.config/systemd/user/irsofka-ai-workstation.service.d"
cat > "$HOME/.config/systemd/user/irsofka-ai-workstation.service.d/runtime-path.conf" <<EOF
# Dibuat relocate_home.sh — daemon harus menemukan agy/qoder/cargo di path baru.
[Service]
Environment=PATH=$RUNTIME/cargo/bin:$HOME/.gemini/antigravity/bin:$RUNTIME/local/bin:/usr/local/bin:/usr/bin:/bin
Environment=XDG_CACHE_HOME=$RUNTIME/cache
Environment=XDG_DATA_HOME=$RUNTIME/local/share
Environment=XDG_STATE_HOME=$RUNTIME/local/state
Environment=RUSTUP_HOME=$RUNTIME/rustup
Environment=CARGO_HOME=$RUNTIME/cargo
EOF
systemctl --user daemon-reload 2>/dev/null || true

echo
echo "=== LANGKAH ANDA ==="
echo " 1. Uji sekarang:  export \$(grep -h '^export' ~/.bashrc.d/irsofka-runtime.sh) && cargo --version"
echo " 2. Tutup Brave & app, lalu REBOOT (wajib — sesi GUI lama masih pakai path lama)."
echo " 3. Setelah reboot:  ls ~/runtime && cargo --version && agy --version"
echo " 4. Kalau semua baik, buang symlink lama di home:"
echo "      $0 --unpin --apply"
echo
echo "BATALKAN: pindahkan kembali tiap isi ~/runtime ke ~ , hapus environment.d/irsofka.conf,"
echo "          .bashrc.d/irsofka-runtime.sh + blok di .bashrc, dan drop-in systemd di atas."
