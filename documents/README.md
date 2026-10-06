# `documents/` — peta masuk, bukan tempat penyimpanan

Isi folder ini adalah **templat dan aturan** untuk mengarahkan workspace AI CLI ke dalam
workstation. Folder ini ikut ter-push ke repo publik; `brain/` tidak.

Garis pemisahnya keras dan disengaja:

| | Repo publik (ini) | Mesin (`~/.ai-station/brain/`) |
|---|---|---|
| Berisi | aturan, templat kosong, kontrak bersama | memori, skill, insiden, handoff, catatan akun |
| Boleh dibaca AI lain | ya | ya, lewat filesystem mesin |
| Berisi data pribadi/kredensial | **tidak pernah** | ya, karena itu di-gitignore |
| Dianggap state | tidak | ya |

Kalau kamu mengkloning repo ini ke mesin baru, folder ini **bukan** memori. Ia hanya
memberi tahu CLI ke mana ia harus menulis. Salin templat ke mesin target, lalu arahkan
CLI ke sana. Kalau suatu templat mulai berisi pengalaman, ia salah tempat — pindahkan
ke `brain/`.

## Berkas di sini

| Berkas | Untuk siapa | Fungsi |
|---|---|---|
| `AGENTS.md` | semua engine | kontrak bersama; menang kalau bertabrakan dengan berkas lain |
| `QODER.md` | Qoder CLI | pointer; isinya cuma menuju `AGENTS.md` |
| `GEMINI.md` | Antigravity/Gemini CLI | pointer yang sama |
| `MEMORY.md` | penulis templat | templat indeks memori + aturan selective loading |
| `SKILLS.md` | penulis templat | templat katalog skill + aturan penomoran file |

## Letak sebenarnya di mesin ini

```
workspace bersama : ~/Documents/ai-workstation/     (AGENTS.md live ada di sini)
memori             : ~/.ai-station/brain/memory/     (indeks: MEMORY.md)
skill              : ~/.ai-station/brain/skills/     (indeks: SKILLS.md)
state faktual      : PostgreSQL irsofka_ai_workstation
```

Berkas konfigurasi tool tidak boleh menyimpan pengetahuan. `~/.qoder`, `~/.qodersec`,
`~/.qmind`, dan `~/.gemini` di mesin ini hanya symlink/shim ke
`~/.ai-station/engines/` — supaya tidak ada dua salinan yang bisa saling
bertentangan.
