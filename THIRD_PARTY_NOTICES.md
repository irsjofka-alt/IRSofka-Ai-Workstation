# Pemberitahuan Pihak Ketiga

Kode tulisan proyek ini dilindungi **PolyForm Noncommercial License 1.0.0** (lihat
[`LICENSE`](LICENSE)). Sebagian aset yang ikut dibagikan **bukan** karya proyek ini dan
tetap berada di bawah lisensinya masing-masing. Lisensi-lisensi itu lebih permisif, dan
sengaja **tidak** ikut dibatasi non-komersial oleh `LICENSE`.

## xterm.js — MIT License

| | |
|---|---|
| Berkas | `engine-rust/assets/vendor/xterm.js`, `engine-rust/assets/vendor/xterm.css` |
| Hak cipta | © 2014 The xterm.js authors. Bagian awal © 2012–2013 Christopher Jeffrey |
| Lisensi | MIT |
| Sumber | https://github.com/xtermjs/xterm.js |

Header hak cipta sudah tertanam di dalam berkas `xterm.css` dan `xterm.js` dan harus tetap
ada. Teks lengkap MIT:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this
> software and associated documentation files (the "Software"), to deal in the Software
> without restriction, including without limitation the rights to use, copy, modify, merge,
> publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons
> to whom the Software is furnished to do so, subject to the following conditions...
>
> (Teks utuh ada pada tautan sumber di atas.)

## @xterm/addon-fit — MIT License

| | |
|---|---|
| Berkas | `engine-rust/assets/vendor/addon-fit.js` |
| Hak cipta | © The xterm.js authors |
| Lisensi | MIT |
| Sumber | https://github.com/xtermjs/xterm.js/tree/master/addons/fit |

## Font Inter & JetBrains Mono — SIL Open Font License 1.1

| | |
|---|---|
| Berkas | `engine-rust/assets/fonts/inter-*.woff2`, `engine-rust/assets/fonts/jb-*.woff2` |
| Lisensi | SIL Open Font License 1.1 |
| Teks lisensi | [`engine-rust/assets/fonts/OFL.txt`](engine-rust/assets/fonts/OFL.txt) |
| Sumber | https://rsms.me/inter/ · https://www.jetbrains.com/mono/ |

OFL mewajibkan teks lisensi disertakan setiap kali font diedarkan — karena itu `OFL.txt`
ikut di-commit. Font tidak boleh dijual berdiri sendiri, dan nama "Inter"/"JetBrains Mono"
tidak boleh dipakai untuk versi hasil modifikasi.

## Dependensi Rust & Python

`engine-rust/Cargo.lock` dan modul Python hanya **mereferensikan** pustaka pihak ketiga,
tidak menyalin sumbernya. Lisensi masing-masing pustaka mengikuti crate/package yang
terpasang saat build dan berada di luar cakupan `LICENSE` proyek ini.

## Ringkasan untuk pengguna ulang

Yang boleh Anda lakukan tanpa izin tambahan:

- **Kode proyek ini** — pakai, modifikasi, sebarkan, selama **non-komersial**.
- **Aset vendor di atas** — pakai, modifikasi, sebarkan, bahkan untuk komersial,
  mengikuti MIT/OFL-nya masing-masing, dengan header dan teks lisensi tetap disertakan.
