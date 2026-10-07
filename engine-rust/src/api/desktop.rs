//! Mata dan tangan AI di desktop: screenshot Wayland dan aksi sistem sederhana.
//!
//! Aksi di sini sengaja daftar tertutup — volume dan notifikasi. Handler tidak pernah
//! meneruskan string dari body request ke `Command`, jadi tidak ada jalur dari JSON
//! masuk ke shell.
use axum::extract::Json;
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};
use std::fs;
use std::process::Command;

use crate::paths::{station_dir, station_port};

pub(crate) async fn handle_see() -> Json<Value> {
    let script = station_dir().join("tools").join("wayland_actor.py");
    let script_arg = script.display().to_string();
    let res = Command::new("python3")
        .args([&script_arg, "screenshot"])
        .output();
    match res {
        Ok(out) if out.status.success() => {
            Json(json!({ "status": "ok", "message": "Screenshot berhasil" }))
        }
        Ok(out) => Json(json!({
            "status": "error",
            "message": String::from_utf8_lossy(&out.stderr)
        })),
        Err(e) => Json(json!({ "status": "error", "message": e.to_string() })),
    }
}

pub(crate) async fn serve_screenshot() -> Response {
    let p = station_dir()
        .join("logs")
        .join("current_screen.png");
    match fs::read(&p) {
        Ok(bytes) => Response::builder()
            .header(header::CONTENT_TYPE, "image/png")
            .header(header::CACHE_CONTROL, "no-store")
            .body(axum::body::Body::from(bytes))
            .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

#[derive(serde::Deserialize)]
pub(crate) struct ActionPayload {
    pub(crate) action: Option<String>,
}

pub(crate) async fn handle_action(Json(payload): Json<ActionPayload>) -> Json<Value> {
    match payload.action.as_deref() {
        Some("vol_up") => {
            let _ = Command::new("pactl")
                .args(["set-sink-volume", "@DEFAULT_SINK@", "+10%"])
                .spawn();
        }
        Some("vol_down") => {
            let _ = Command::new("pactl")
                .args(["set-sink-volume", "@DEFAULT_SINK@", "-10%"])
                .spawn();
        }
        Some("vol_mute") => {
            let _ = Command::new("pactl")
                .args(["set-sink-mute", "@DEFAULT_SINK@", "toggle"])
                .spawn();
        }
        Some("open_station") => {
            // Alamatnya dibangun dari port yang sedang dipakai — tombol ini tetap benar
            // setelah STATION_PORT diubah, dan tidak ada string dari body yang masuk ke shell.
            let url = format!("http://127.0.0.1:{}/station", station_port());
            let _ = Command::new("xdg-open").arg(url).spawn();
        }
        Some("notify_test") => {
            let _ = Command::new("notify-send")
                .args([
                    "-a",
                    "Irsofka AI Workstation",
                    "Halo Bro Ichsan!",
                    "AI Workstation Native Rust Core Aktif!",
                ])
                .spawn();
        }
        _ => {}
    }
    Json(json!({ "status": "ok" }))
}
