//! Probe cache: jalankan perintah luar, ambil JSON-nya, dan jangan ulangi dalam TTL-nya.
//!
//! GUI mem-poll /api/stats beberapa kali per detik. Tanpa cache, tiap poll spawns
//! `qoder --version`, `agy models`, dan dua script Python — beban CPU yang tidak
//! pernah berhenti walau tidak ada yang berubah. Satu cache untuk semua probe: kunci
//! bebas, TTL dipilih per probe oleh pemanggilnya.
use serde_json::Value;
use std::collections::HashMap;
use std::io::Write;
use std::os::unix::process::CommandExt;
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

/// Batas satu probe. `Command::output()` tanpa batas pernah terbukti menahan satu permintaan
/// selama 62.8s karena dua pemanggil berebut `qoder --list-models`; tanpa batas sama sekali,
/// probe yang menggantung menahan thread-nya selamanya dan meninggalkan anak proses berisi RSS.
pub(crate) const PROBE_TIMEOUT: Duration = Duration::from_secs(120);

/// Hentikan satu grup proses, bukan hanya anak langsung.
///
/// `child.kill()` membunuh satu PID saja. CLI yang kita panggil punya worker sendiri
/// (node/qodercli), dan worker itu yang tetap hidup memegang RAM setelah pemanggilnya pergi —
/// terukur sebagai yatim 259 MB di kontrak §4. Child sudah dipimpin `process_group(0)`, jadi
/// pgid-nya sama dengan pid-nya, dan pgid dikirim sebagai PID negatif.
///
/// `--` bukan hiasan: `/bin/kill -KILL -883803` keluar dengan rc=0 dan **tidak membunuh apa pun**
/// (procps membaca angka itu sebagai spesifikasi sinyal). Terukur 2026-10-07: tanpa `--`, dua
/// proses tetap hidup; dengan `--`, nol. Uji di modul ini menangkap diam-diamnya kegagalan itu.
pub(crate) fn kill_process_group(pid: u32) {
    let _ = Command::new("kill")
        .arg("-KILL")
        .arg("--")
        .arg(format!("-{pid}"))
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status();
}

/// Masih ada yang menghuni grup ini? Dipakai sebelum mengirim sinyal di jalur sukses: pemimpin
/// grup sudah mati saat itu, jadi `/proc/<pgid>` tidak lagi ada dan satu-satunya bukti yang sah
/// adalah keanggotaan. Ini juga menjaga dari memakai ulang PID yang kebetulan sama.
pub(crate) fn group_alive(pgid: u32) -> bool {
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return false;
    };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(text) = name.to_str() else { continue };
        if text.is_empty() || !text.chars().all(|c| c.is_ascii_digit()) {
            continue;
        }
        let Ok(raw) = std::fs::read_to_string(entry.path().join("stat")) else {
            continue;
        };
        // Nama proses bisa mengandung spasi dan kurung, jadi hitung dari penutup kurung terakhir:
        // setelahnya field ke-3 (0-based) adalah pgrp.
        if let Some((_, rest)) = raw.rsplit_once(") ") {
            if rest.split_whitespace().nth(2).and_then(|v| v.parse::<u32>().ok()) == Some(pgid) {
                return true;
            }
        }
    }
    false
}

fn cache() -> &'static Mutex<HashMap<String, (Instant, Value)>> {
    static CACHE: OnceLock<Mutex<HashMap<String, (Instant, Value)>>> = OnceLock::new();
    CACHE.get_or_init(Default::default)
}

pub(crate) fn cached_json<F>(key: &str, ttl: Duration, producer: F) -> Value
where
    F: FnOnce() -> Value,
{
    if let Ok(c) = cache().lock() {
        if let Some((at, val)) = c.get(key) {
            if at.elapsed() < ttl {
                return val.clone();
            }
        }
    }
    let val = producer();
    if let Ok(mut c) = cache().lock() {
        c.insert(key.to_string(), (Instant::now(), val.clone()));
    }
    val
}

/// Hasil satu probe yang dijalankan dengan batas waktu.
pub(crate) struct Probe {
    pub(crate) ok: bool,
    pub(crate) code: Option<i32>,
    pub(crate) stdout: String,
    pub(crate) stderr: String,
    pub(crate) timed_out: bool,
}

/// Jalankan satu perintah luar dengan batas waktu keras.
///
/// Pembacaan pipe dilakukan thread terpisah: tanpa itu, proses yang mencetak lebih dari kapasitas
/// buffer akan menggantung di `write` selamanya sementara pemanggilnya menunggu. Saat batas
/// terlampaui seluruh grup dibunuh, jadi tidak ada worker yang tertinggal memegang RAM (§4).
pub(crate) fn run_bounded(program: &str, args: &[&str], stdin: Option<&[u8]>,
                         limit: Duration) -> Probe {
    let empty = |timed_out: bool| Probe {
        ok: false,
        code: None,
        stdout: String::new(),
        stderr: String::new(),
        timed_out,
    };

    let mut cmd = Command::new(program);
    cmd.args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    cmd.process_group(0);
    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(_) => return empty(false),
    };
    let pid = child.id();

    match stdin {
        Some(bytes) => {
            if let Some(mut sink) = child.stdin.take() {
                // Dimiliki thread: &[u8] pinjaman tidak boleh hidup lebih lama closures 'static.
                let owned = bytes.to_vec();
                std::thread::spawn(move || {
                    let _ = sink.write_all(&owned);
                    let _ = sink.flush();
                });
            }
        }
        // Diambil lalu dibuang: pipe tertutup, jadi CLI membaca EOF dan tidak menunggu kita.
        None => drop(child.stdin.take()),
    }

    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let _ = tx.send(child.wait_with_output());
    });

    let out = match rx.recv_timeout(limit) {
        // Jalur sukses pun disapu. CLI yang keluar 0 setelah me-daemon-kan worker meninggalkan
        // grup tanpa pemimpin, dan grup itulah yang terus memegang RSS sampai sweep berikutnya
        // sempat jalan — padahal kontrak §4 berbunyi "dipotong begitu jawabannya dibaca".
        // Temuan 4 audit independen 2026-10-07, untuk Python dan Rust sekaligus.
        Ok(Ok(out)) => {
            if group_alive(pid) {
                kill_process_group(pid);
            }
            out
        }
        Ok(Err(_)) => {
            if group_alive(pid) {
                kill_process_group(pid);
            }
            return empty(false);
        }
        Err(_) => {
            kill_process_group(pid);
            return empty(true);
        }
    };

    Probe {
        ok: out.status.success(),
        code: out.status.code(),
        stdout: String::from_utf8_lossy(&out.stdout).trim().to_string(),
        stderr: String::from_utf8_lossy(&out.stderr).trim().to_string(),
        timed_out: false,
    }
}

pub(crate) fn run_capture(program: &str, args: &[&str]) -> Option<String> {
    let probe = run_bounded(program, args, None, PROBE_TIMEOUT);
    if probe.ok && !probe.timed_out {
        Some(probe.stdout)
    } else {
        None
    }
}

/// Run a python one-liner that prints JSON on stdout.
pub(crate) fn py_json(script: &str, args: &[&str]) -> Option<Value> {
    let mut full: Vec<&str> = vec!["-c", script];
    full.extend_from_slice(args);
    let probe = run_bounded("python3", &full, None, PROBE_TIMEOUT);
    if !probe.ok {
        return None;
    }
    probe
        .stdout
        .find('{')
        .and_then(|i| serde_json::from_str(&probe.stdout[i..]).ok())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    const TOKEN: &str = "21337";

    /// Token dibangun dari PID proses uji, jadi tidak ada shell lain — termasuk shell yang
    /// memuat teks eksperimen ini — bisa dihitung sebagai cucu yang bocor.
    fn token() -> String {
        format!("{}{}", std::process::id(), TOKEN)
    }

    fn survivors(want: &str) -> Vec<u32> {
        let mut found = Vec::new();
        if let Ok(entries) = std::fs::read_dir("/proc") {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.is_empty() || !name.chars().all(|c| c.is_ascii_digit()) {
                    continue;
                }
                if let Ok(raw) = std::fs::read(entry.path().join("cmdline")) {
                    if String::from_utf8_lossy(&raw).contains(want) {
                        found.push(name.parse().unwrap());
                    }
                }
            }
        }
        found
    }

    #[test]
    fn probe_yang_menggantung_dipotong_beserta_anaknya() {
        let want = token();
        let script = format!("sleep {want} & wait");
        let started = Instant::now();
        let probe = run_bounded("sh", &["-c", &script], None, Duration::from_secs(1));
        let wall = started.elapsed();
        assert!(probe.timed_out, "probe harus dilaporkan sebagai lewat batas");
        assert!(wall.as_secs() < 8, "pemotongan tidak boleh menunggu anak selesai: {wall:?}");
        // Inilah bagian yang dibayar operator dengan RAM-nya kalau kita hanya membunuh PID langsung.
        let mut left = survivors(&want);
        for _ in 0..20 {
            if left.is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
            left = survivors(&want);
        }
        assert_eq!(left, Vec::<u32>::new(), "cucu masih hidup setelah grup dipotong");
    }

    #[test]
    fn probe_selesai_normal_tidak_berubah_bentuk() {
        let probe = run_bounded("printf", &["hello"], None, Duration::from_secs(10));
        assert!(probe.ok);
        assert!(!probe.timed_out);
        assert_eq!(probe.stdout, "hello");
        assert_eq!(run_capture("printf", &["x"]), Some("x".to_string()));
    }

    #[test]
    fn worker_yang_ditinggalkan_probe_sukses_ikut_mati() {
        // Token kedua, supaya dua uji yang berjalan paralel tidak saling melihat cucunya.
        let want = format!("{}21338", std::process::id());
        let script = format!("sleep {want} >/dev/null 2>&1 & exit 0");
        let probe = run_bounded("sh", &["-c", &script], None, Duration::from_secs(10));
        assert!(probe.ok, "shell keluar 0; probe harus dilaporkan sukses, bukan timeout");
        assert!(!probe.timed_out);
        let mut left = survivors(&want);
        for _ in 0..20 {
            if left.is_empty() {
                break;
            }
            std::thread::sleep(Duration::from_millis(100));
            left = survivors(&want);
        }
        assert_eq!(left, Vec::<u32>::new(), "grup tidak disapu pada jalur sukses");
    }
}
