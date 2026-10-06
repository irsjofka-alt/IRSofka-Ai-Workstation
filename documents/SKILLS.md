# Templat katalog skill (`brain/skills/SKILLS.md`)

Salin berkas ini ke `~/.ai-station/brain/skills/SKILLS.md` pada mesin target, lalu isi
baris katalognya. Sama seperti memori: yang dibawa antar mesin adalah aturan, bukan
pengalaman.

## Dua bentuk, jangan dicampur

**1. Skill workstation** — catatan cara kerja, satu topik satu file:

```
skills/<kategori>/<topik>.md
   coding/  sysadmin/  visual/  analysis/  learned/
```

Dibaca lewat indeks `SKILLS.md`, tidak didaftarkan ke CLI mana pun.

**2. Skill yang dimuat engine** — direktori dengan `SKILL.md` ber-frontmatter, dan file
bernomor di dalamnya:

```
skills/<nama-skill>/
  SKILL.md                      frontmatter: name, description, when_to_use + tabel index
  01-<topik>.md                 baca kalau ...
  02-<topik>.md
  references/<bukti>.md         kutipan sumber, jangan dicampur dengan instruksi
  scripts/<alat>.py            alat yang dipakai skill itu
```

Penomoran wajib dua digit dan urut. Mesin pembaca skill hanya memuat `SKILL.md`
(lengkap) dan file bernomor (atas permintaan). Nama file yang tidak bernomor dianggap
referensi, bukan instruksi.

**Jebakan yang sudah pernah terjadi:** `description:` yang tidak diberi tanda kutip tapi
berisi `: ` di tengah teks (mis. "Contains verified hard limits: Landscape cannot ...")
membuat YAML frontmatter tidak terbaca, dan pack-nya **hilang diam-diam** dari daftar
engine — tanpa pesan error. Kalau sebuah pack tidak muncul: kutip nilainya, lalu
`python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]).read().split('---')[1])" SKILL.md`.

## Pintu masuk per engine

Satu pohon skill, dibaca semua engine. Pohonnya adalah daftar symlink:

```
~/.ai-station/engines/agents/skills/<nama-pack>/SKILL.md
   ^ isi aslinya tetap di ~/.ai-station/brain/skills/, tidak pernah disalin
```

| Engine | Ia memuat skill dari | Status |
|---|---|---|
| Qoder CLI | `~/.agents/skills/` (kunci `loadFromAgentsDirectory`, bawaan aktif) | terverifikasi: 11 pack muncul di daftar skill sesi yang sedang berjalan, tanpa respawn |
| Antigravity/Gemini | `~/.gemini/config/plugins/station-rules/skills/` | terverifikasi: 11 pack + 7 skill bawaan = 18, ditanya langsung lewat pane tmux-nya |
| Model lokal | tidak membaca skill; prompt dirakit `tools/cross_verify.py` dari `config/engines.json` | |

`~/.agents` sendiri adalah symlink ke `~/.ai-station/engines/agents`, didaftarkan di
`config/home_shims.json`. Guard `hooks/self_preservation.py` mengizinkan nama terdaftar
**hanya** untuk bentuk `ln -s <dalam workstation atau runtime> ~/.nama`; `mkdir` atau
redirect ke nama yang sama tetap diblokir.

Kalau menambah pack baru: buat direktori berisi `SKILL.md` di `brain/skills/`, lalu
tautkan satu symlink ke `engines/agents/skills/`. Jangan menaruh berkas skill langsung
di dalam engine — itu yang membuat tiap engine punya salinan sendiri yang berbeda isi.

## Aturan belajar otomatis

`tools/incident_recorder.py` menghitung kegagalan per `error_signature`. Pada kelipatan
5x ia:
1. menulis `skills/learned/<signature>.md`,
2. mendaftarkannya di tabel `skills_inventory` (`auto_learned = TRUE`),
3. menandai insidennya `resolved`,
4. menambahkan barisnya ke indeks ini.

Skill otomatis tidak pernah dihapus oleh manusia tanpa jejak; kalau salah, koreksi isinya
dan catat alasannya di dalam file itu.

## Selective loading

1. Jangan baca katalog lalu membuka semua file. Satu tugas = satu skill.
2. Skill lama dari mesin lain boleh dipakai sebagai pengetahuan, tapi path di dalamnya
   jangan dipakai sebagai lokasi — petanya ada di `memory/legacy/LEGACY_MAP.md`.
3. Katalog tanpa baris = skill tidak terdaftar. Daftarkan atau jangan tulis.
4. Isi `skills/` tidak pernah di-push. Repo hanya membawa templat ini.
