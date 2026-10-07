//! Sakelar Autopilot dan antrean Human Decide — HTTP di atas `tools/work_order.py`.
//!
//! Pembagian kerja ditiru dari F8.3 dan bukan kebetulan: **Python memegang kosakata tugas**
//! (klaim, lease, bukti, pemutus, antrean manusia), **Rust memegang HTTP dan batas waktu
//! prosesnya**. Angka `NIGHT_MAX_FAILURES` sengaja tidak disalin ke sini — menyalinnya berarti
//! workstation punya dua angka yang boleh berbeda, dan kontrak §12 melarang dua definisi untuk
//! satu kata.
//!
//! Tiga hal yang dijaga di file ini:
//!
//! 1. Bentuk masukan, bukan kebijakan. `mode` harus `on`/`off`; `minutes`/`budget` harus bilangan
//!    bulat positif; string dipotong. Apakah sebuah jendela boleh diterima atau tidak diputuskan
//!    `work_order.py`, satu kali, di satu tempat.
//! 2. Tidak ada satu byte pun yang pernah masuk ke shell. Argumen disusun sebagai array dan
//!    diberangkatkan lewat `run_bounded`, yang memberi grup proses sendiri dan memotong seluruh
//!    grup itu saat batas lewat (kontrak §4) — panggilan yang menggantung tidak boleh meninggalkan
//!    worker yang memegang RAM.
//! 3. Mode OFF adalah jalur yang paling tidak boleh gagal. Kalau proses anak mati, responsnya
//!    menyebut dua jalur mati lain yang tetap jalan tanpa bantuan AI (`ai-station autopilot off`
//!    dari shell mana pun, dan menghentikan timer drainer), supaya operator tidak pernah berdiri
//!    di depan satu-satunya tombol yang rusak.
use axum::body::Body;
use axum::extract::Json;
use axum::http::header::CONTENT_TYPE;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

use crate::paths::{python_bin, work_order_path};
use crate::probe::PROBE_TIMEOUT;
use crate::spool::spool_event;

fn envelope(status: StatusCode, body: Value) -> Response {
    (status, Json(body)).into_response()
}

/// Balas dengan byte milik `work_order.py` apa adanya.
///
/// Angka pemutus dan status antrean adalah laporan: menulisnya ulang di sini (atau mengurainya
/// lalu menyerialisasikan kembali) membuat muka GUI bukan lagi turunan dari baris database.
fn raw_json(status: StatusCode, text: String) -> Response {
    (status, [(CONTENT_TYPE, "application/json")], Body::from(text)).into_response()
}

/// Satu-satunya tempat proses anak diberangkatkan: batas waktu keras + grup proses sendiri.
fn run_tool(args: &[String]) -> Result<String, String> {
    let script = work_order_path();
    if !script.exists() {
        return Err(format!("work_order.py tidak ada di {}", script.display()));
    }
    let script_arg = script.display().to_string();
    let python = python_bin().display().to_string();
    let mut argv: Vec<&str> = Vec::with_capacity(args.len() + 1);
    argv.push(script_arg.as_str());
    for one in args {
        argv.push(one.as_str());
    }
    let probe = crate::probe::run_bounded(&python, &argv, None, PROBE_TIMEOUT);
    if probe.timed_out {
        return Err(format!(
            "work_order.py tidak selesai dalam {}s dan seluruh grup prosesnya sudah dipotong",
            PROBE_TIMEOUT.as_secs()
        ));
    }
    match probe.stdout.find('{') {
        Some(i) => Ok(probe.stdout[i..].to_string()),
        None => Err(format!(
            "work_order.py tidak mengembalikan JSON: rc={:?}; ok={}; stdout={}; stderr={}",
            probe.code,
            probe.ok,
            probe.stdout.chars().take(300).collect::<String>(),
            probe.stderr.chars().take(300).collect::<String>()
        )),
    }
}

/// `run_tool` menunggu database — bisa mencapai detik. Di dalam handler async itu berarti satu
/// worker tokio tidak melayani apa pun, jadi prosesnya dijalankan di luar runtime.
async fn run_tool_async(args: Vec<String>) -> Result<String, String> {
    tokio::task::spawn_blocking(move || run_tool(&args))
        .await
        .map_err(|e| format!("task work_order berhenti: {e}"))?
}

/// GET /api/autopilot — keadaan sakelar, pemutus malam, antrean manusia, kandidat resume.
pub(crate) async fn get_state() -> Response {
    match run_tool_async(vec!["status".to_string()]).await {
        Ok(text) => raw_json(StatusCode::OK, text),
        Err(e) => envelope(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"ok": false, "error": e,
                   "off_paths": ["tombol Autopilot di kokpit",
                                  "ai-station autopilot off dari shell mana pun",
                                  "systemctl --user stop timer drainer"]}),
        ),
    }
}

/// Ambil satu bilangan bulat positif tanpa menilai kebijakannya.
fn positive_int(v: Option<&Value>) -> Option<u64> {
    match v.and_then(Value::as_u64) {
        Some(n) if n > 0 && n <= 100_000 => Some(n),
        _ => None,
    }
}

/// POST /api/autopilot — hanya `on` dan `off` yang dikenal, selain itu ditolak sebelum berangkat.
///
/// Badan yang sah: `{"mode":"on","minutes":240,"budget":8,"reason":"...","by":"operator-gui"}`.
pub(crate) async fn post_state(Json(body): Json<Value>) -> Response {
    let mode = match body.get("mode").and_then(Value::as_str) {
        Some(m) if m.eq_ignore_ascii_case("on") => "on",
        Some(m) if m.eq_ignore_ascii_case("off") => "off",
        Some(other) => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "error": format!("mode {other:?} bukan on/off — status tidak disentuh")}),
            )
        }
        None => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "error": "mode wajib diisi"}),
            )
        }
    };

    let mut args: Vec<String> = vec!["autopilot".to_string(), mode.to_string()];
    if mode == "on" {
        if let Some(m) = positive_int(body.get("minutes")) {
            args.extend(["--minutes".to_string(), m.to_string()]);
        } else if body.get("minutes").is_some() {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "error": "minutes harus bilangan bulat positif; jendela ON tidak dipasang"}),
            );
        }
        if let Some(b) = positive_int(body.get("budget")) {
            args.extend(["--budget".to_string(), b.to_string()]);
        } else if body.get("budget").is_some() {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "error": "budget harus bilangan bulat positif; jendela ON tidak dipasang"}),
            );
        }
    }
    let by = body
        .get("by")
        .and_then(Value::as_str)
        .map(|s| s.chars().take(80).collect::<String>())
        .unwrap_or_else(|| "gui".to_string());
    args.extend(["--by".to_string(), by.clone()]);
    if let Some(reason) = body.get("reason").and_then(Value::as_str) {
        let clean = reason.chars().take(300).collect::<String>();
        if !clean.is_empty() {
            args.extend(["--reason".to_string(), clean]);
        }
    }

    let (status, text) = match run_tool_async(args).await {
        Ok(out) => (StatusCode::OK, out),
        Err(e) => {
            // Kegagalan dibaca apa adanya: ON/OFF adalah status yang tercatat, bukan harapan.
            spool_event(
                "autopilot_switch_failed",
                "station",
                format!("autopilot {mode} gagal lewat GUI (by {by}): {}", e.chars().take(200).collect::<String>()),
                None,
                Some("work_order"),
            );
            return envelope(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"ok": false, "error": e, "mode_requested": mode,
                       "off_paths": ["ai-station autopilot off dari shell mana pun",
                                      "systemctl --user stop timer drainer"],
                       "note": "kalau yang gagal adalah jalur ON, mesin tidak memegang apa pun; kalau yang gagal jalur OFF, pakai salah satu off_paths di atas"}),
            );
        }
    };
    spool_event(
        "autopilot_switch",
        "station",
        format!(
            "autopilot {mode} oleh {by} — keadaan lengkap (jendela, budget, alasan) ada di tabel autopilot_state"
        ),
        None,
        Some("work_order"),
    );
    raw_json(status, text)
}
