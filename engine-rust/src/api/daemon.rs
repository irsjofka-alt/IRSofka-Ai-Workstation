//! Endpoint yang mengontrol daemon itu sendiri: muat ulang UI dan restart layanan.
//!
//! Keduanya harus aman dipanggil otomatis. Muat ulang hanya menaikkan penghitung yang
//! dibaca WebView — tidak menyentuh PTY maupun tmux, jadi sesi CLI di dalam tab tidak ikut
//! berhenti. Restart mengirim sinyal ke systemd, bukan membunuh proses sendiri, supaya
//! cgroup daemon yang mati tidak ikut mematikan apa pun di luar dirinya.
use axum::Json;
use serde_json::{json, Value};
use std::process::Command;
use std::sync::atomic::Ordering;
use std::time::Duration;

use crate::procinfo::proc_cwd;
use crate::spool::spool_event;
use crate::UI_RELOAD_TICK;

pub(crate) async fn ui_reload() -> Json<Value> {
    let tick = UI_RELOAD_TICK.fetch_add(1, Ordering::Relaxed) + 1;
    spool_event(
        "ui_reload_requested",
        "-",
        format!("Permintaan muat ulang UI (tick={tick})"),
        proc_cwd(std::process::id()),
        Some("daemon"),
    );
    Json(json!({ "status": "ok", "ui_reload_tick": tick }))
}

pub(crate) fn reload_tick() -> u64 {
    UI_RELOAD_TICK.load(Ordering::Relaxed)
}

pub(crate) async fn restart_server() -> Json<Value> {
    spool_event(
        "daemon_restart_requested",
        "-",
        "Permintaan restart daemon via UI: systemctl --user restart irsofka-ai-workstation.service"
            .to_string(),
        proc_cwd(std::process::id()),
        Some("daemon"),
    );
    tokio::spawn(async {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let _ = Command::new("systemctl")
            .args(["--user", "restart", "irsofka-ai-workstation.service"])
            .spawn();
    });
    Json(json!({
        "status": "restarting",
        "message": "Rust server service sedang di-restart..."
    }))
}
