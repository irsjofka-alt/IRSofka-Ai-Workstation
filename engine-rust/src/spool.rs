//! Spool kejadian: daemon menulis baris JSON, ingestor yang memipanya ke SQL.
//!
//! Daemon sengaja TIDAK memegang dialek SQL. Ia cuma append satu baris JSON ke
//! `logs/station_events.jsonl`; service `irsofka-action-log.service` yang menerjemahkan
//! baris itu ke PostgreSQL (atau SQLite saat PostgreSQL tumbang) lewat satu lapisan
//! dialek di `tools/session_ingestor.py`. Akibatnya log tetap tertulis walaupun database
//! mati, dan biner daemon tidak ikut membawa kredensial ke setiap hasil clone.
use chrono::Local;
use serde_json::{json, Value};
use std::fs;
use std::io::Write;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::paths::{ingestor_path, spool_path};

static SPOOL_SEQ: AtomicU64 = AtomicU64::new(1);

pub(crate) fn spool_event(kind: &str, tab: &str, summary: String, cwd: Option<String>, tool: Option<&str>) {
    let record = json!({
        "seq": SPOOL_SEQ.fetch_add(1, Ordering::Relaxed),
        "ts": Local::now().to_rfc3339(),
        "engine": "station",
        "tab": tab,
        "kind": kind,
        "summary": summary.chars().take(400).collect::<String>(),
        "cwd": cwd,
        "tool": tool,
        "pid": std::process::id(),
    });
    if let Ok(mut line) = serde_json::to_string(&record) {
        line.push('\n');
        if let Some(parent) = spool_path().parent() {
            let _ = fs::create_dir_all(parent);
        }
        if let Ok(mut fh) = fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(spool_path())
        {
            let _ = fh.write_all(line.as_bytes());
        }
    }
}

/// Jalan satu-satunya menuju SQL: panggil lapisan dialek milik ingestor.
pub(crate) fn ingestor_json(mode: &str, limit: u32, engine: &str, kind: &str) -> Value {
    let script = ingestor_path().display().to_string();
    let mut cmd = Command::new("python3");
    cmd.arg(&script).arg(mode).arg(limit.to_string());
    if !engine.is_empty() {
        cmd.arg("--engine").arg(engine);
    }
    if !kind.is_empty() {
        cmd.arg("--kind").arg(kind);
    }
    let out = cmd.output().ok();
    out.map(|o| {
        String::from_utf8_lossy(&o.stdout)
            .find('{')
            .and_then(|i| serde_json::from_str::<Value>(&String::from_utf8_lossy(&o.stdout)[i..]).ok())
            .unwrap_or_else(|| json!({"error": "ingestor tidak mengembalikan JSON valid"}))
    })
    .unwrap_or_else(|| json!({"error": format!("gagal menjalankan {}", script)}))
}
