//! Probe cache: jalankan perintah luar, ambil JSON-nya, dan jangan ulangi dalam TTL-nya.
//!
//! GUI mem-poll /api/stats beberapa kali per detik. Tanpa cache, tiap poll spawns
//! `qoder --version`, `agy models`, dan dua script Python — beban CPU yang tidak
//! pernah berhenti walau tidak ada yang berubah. Satu cache untuk semua probe: kunci
//! bebas, TTL dipilih per probe oleh pemanggilnya.
use serde_json::Value;
use std::collections::HashMap;
use std::process::Command;
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant};

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

pub(crate) fn run_capture(program: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(program).args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Run a python one-liner that prints JSON on stdout.
pub(crate) fn py_json(script: &str, args: &[&str]) -> Option<Value> {
    let mut cmd = Command::new("python3");
    cmd.arg("-c").arg(script);
    cmd.args(args);
    let out = cmd.output().ok()?;
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
    stdout
        .find('{')
        .and_then(|i| serde_json::from_str(&stdout[i..]).ok())
}
