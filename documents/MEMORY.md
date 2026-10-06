# Templat indeks memori (`brain/memory/MEMORY.md`)

Salin berkas ini ke `~/.ai-station/brain/memory/MEMORY.md` pada mesin target, lalu isi
hanya baris indeksnya. Jangan salin isi memori dari mesin lain ke sini — yang boleh
dibawa antar mesin hanyalah aturan di bawah.

## Kontrak

Lokasi tulis memori jangka panjang: `~/.ai-station/brain/memory/<kategori>/`.
Satu topik = satu berkas `.md`. Tidak ada batas jumlah berkas; yang dibatasi adalah
jumlah yang **dibaca sekaligus**.

Kategori yang dipakai workstation ini:

```
memory/profile/     siapa operatornya, gaya kerjanya, aturan yang dia tetapkan
memory/hardware/    spesifikasi mesin, batas VRAM/RAM, partisi
memory/projects/    state proyek + berkas handoff_* hasil auto_handoff.py
memory/incidents/   error_tracker.json dan jejak self-healing
memory/reflections/ cara AI di mesin ini menyikapi kegagalan
memory/legacy/      memori dari mesin lama (path-nya sudah mati — wajib baca LEGACY_MAP.md dulu)
```

## Cara menulis satu entri indeks

```
### 1. Profil & Kebiasaan User (`memory/profile/`)
- [user_preferences.md](user_preferences.md) — satu kalimat: apa yang didapat pembaca dari file ini.
```

Baris indeks harus cukup untuk memutuskan *buka atau tidak*, tanpa perlu membuka file.
Kalau barisnya perlu dua kalimat untuk menjelaskan, file itu terlalu besar dan harus
dipecah.

## Aturan selective loading (tidak bisa ditawar)

1. **Jangan baca seluruh direktori memori ke dalam konteks.** Baca indeks, pilih satu baris,
   buka satu file.
2. Memori adalah pengetahuan, bukan bukti keadaan hari ini. Untuk state faktual
   (aksi terakhir, quest aktif, siapa bicara ke siapa) baca PostgreSQL
   `irsofka_ai_workstation` atau `ai-station recovery 40`.
3. Menulis memori tanpa mendaftar barisnya di indeks = memori itu tidak akan pernah
   terbaca lagi. Indeks adalah satu-satunya pintu masuk.
4. Memori yang bertentangan dengan database kalah. Perbaiki memorinya, jangan sebaliknya.
5. Isi `memory/` tidak pernah di-push. Repo hanya membawa templat ini.
