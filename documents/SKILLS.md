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

## Pintu masuk per engine

| Engine | Ia memuat skill dari | Catatan |
|---|---|---|
| Qoder CLI | `~/.qoder/plugins/` (mekanisme utama) dan `< workspace >/.agents/skills` (kompatibilitas lama, aktif secara bawaan) | butuh restart tab setelah menambah direktori |
| Antigravity/Gemini | plugin di `~/.gemini/config/plugins/<nama>/` | plugin `station-rules` berisi kontrak bersama |
| Model lokal | tidak membaca skill; prompt dirakit `tools/cross_verify.py` dari `config/engines.json` | |

Direktori skill bersama (`~/.ai-station/brain/skills/`) adalah satu-satunya tempat
penyimpanan. Yang ada di folder engine adalah symlink ke sana, bukan salinan — supaya
koreksi cukup ditulis sekali.

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
