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
//!    menyebut jalur lain yang tetap jalan tanpa bantuan AI — `OFF_PATHS` di bawah, dan setiap
//!    barisnya dibuktikan ada lebih dulu sebelum ditulis ke sana (kontrak §6).
use axum::body::Body;
use axum::extract::Json;
use axum::http::header::CONTENT_TYPE;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

use crate::paths::{drainer_path, python_bin, work_order_path};
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
///
/// Dipakai dua skrip (`work_order.py` dan `drainer.py`) lewat satu fungsi, bukan dua salinan:
/// dua tempat berangkat berarti dua tempat yang boleh lupa memotong grup prosesnya.
fn run_script(script: &std::path::Path, args: &[String]) -> Result<String, String> {
    if !script.exists() {
        return Err(format!("{} tidak ada", script.display()));
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
            "{} tidak selesai dalam {}s dan seluruh grup prosesnya sudah dipotong",
            script.file_name().unwrap_or_default().to_string_lossy(),
            PROBE_TIMEOUT.as_secs()
        ));
    }
    match probe.stdout.find('{') {
        Some(i) => Ok(probe.stdout[i..].to_string()),
        None => Err(format!(
            "{} tidak mengembalikan JSON: rc={:?}; ok={}; stdout={}; stderr={}",
            script.file_name().unwrap_or_default().to_string_lossy(),
            probe.code,
            probe.ok,
            probe.stdout.chars().take(300).collect::<String>(),
            probe.stderr.chars().take(300).collect::<String>()
        )),
    }
}

fn run_tool(args: &[String]) -> Result<String, String> {
    run_script(&work_order_path(), args)
}

/// Tangan drainer, dipanggil dari satu klik manusia. Kosakata jawaban tetap milik
/// `work_order.py`; yang lewat jalur ini hanya pengantarannya.
fn run_drainer(args: &[String]) -> Result<String, String> {
    run_script(&drainer_path(), args)
}

/// `run_tool` menunggu database — bisa mencapai detik. Di dalam handler async itu berarti satu
/// worker tokio tidak melayani apa pun, jadi prosesnya dijalankan di luar runtime.
async fn run_tool_async(args: Vec<String>) -> Result<String, String> {
    tokio::task::spawn_blocking(move || run_tool(&args))
        .await
        .map_err(|e| format!("task work_order berhenti: {e}"))?
}

async fn run_drainer_async(args: Vec<String>) -> Result<String, String> {
    tokio::task::spawn_blocking(move || run_drainer(&args))
        .await
        .map_err(|e| format!("task drainer berhenti: {e}"))?
}

/// Jalan keluar ketika memanggil sakelar justru yang gagal.
///
/// Daftarnya harus benar hari ini. Sebuah respons yang menyebut `systemctl --user stop timer
/// drainer` sebelum timer itu ada (F10.3 belum dibangun) mengajari operator perintah yang
/// gagal tepat di saat paling buruk: ia sudah kehilangan kendali atas mesinnya. Menambah
/// baris di sini berarti lebih dulu membuktikan bahwa baris itu memang bekerja.
///
/// Dua baris terakhir ditambahkan setelah dibuktikan pada 2026-10-08 02:55 di mesin ini:
/// `systemctl --user stop irsofka-autopilot.timer` keluar 0 dan unitnya terbaca `inactive`, dan
/// `work_order.py disarm` menulis `observe` dari proses tanpa tty. Keduanya adalah jalur mati
/// yang tidak meminta kerja sama AI — invarian 6, dan alasan asimetri tty di F10.3 ada.
const OFF_PATHS: &[&str] = &[
    "tombol Autopilot di kokpit (kalau daemon masih hidup, ini yang paling cepat)",
    "ai-station autopilot off — dari shell mana pun, tanpa daemon",
    "python3 ~/.ai-station/tools/work_order.py autopilot off — menulis langsung ke database",
    "systemctl --user stop irsofka-autopilot.timer — menghentikan detak tanpa daemon",
    "python3 ~/.ai-station/tools/work_order.py disarm — menutup tangan (observe), tidak butuh tty",
];

/// GET /api/autopilot — keadaan sakelar, pemutus malam, antrean manusia, kandidat resume.
pub(crate) async fn get_state() -> Response {
    match run_tool_async(vec!["status".to_string()]).await {
        Ok(text) => raw_json(StatusCode::OK, text),
        Err(e) => envelope(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"ok": false, "error": e, "off_paths": OFF_PATHS}),
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
                       "off_paths": OFF_PATHS,
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

/// POST /api/decide — satu jawaban manusia untuk satu pertanyaan yang tercatat.
///
/// Urutan kerjanya bagian yang tidak boleh tertukar: **tulis, baru antar**. Kalau pane target
/// sedang bekerja, keputusan operator tetap utuh di database dan yang gagal hanya pengirimannya.
/// Kebalikannya — antar lalu tulis — membuat satu pane yang sedang sibuk menghapus jawaban
/// manusia, dan itu kegagalan yang tidak terlihat dari muka halaman.
///
/// Rust tidak memutuskan apa pun di sini. Yang diperiksa cuma bentuk masukan (id bilangan bulat
/// positif, jawaban tidak kosong, string dipotong), lalu `work_order.py answer` yang menulis dan
/// `drainer.py deliver` yang mengetik. Menyalin aturan itu ke file ini berarti workstation punya
/// dua aturan untuk satu kata (§12).
///
/// Handler ini adalah satu-satunya tempat jawaban manusia berubah menjadi ketikan, dan ia hanya
/// bisa dicapai dari klik di halaman. `irsofka-autopilot.timer` tidak pernah memanggilnya —
/// sakelar OFF tetap melindungi orang yang tidur, dari mesin, bukan dari jawabannya sendiri.
pub(crate) async fn post_decide(Json(body): Json<Value>) -> Response {
    let id = match body.get("id").and_then(Value::as_u64) {
        Some(n) if n > 0 => n,
        _ => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "stage": "validate",
                       "error": "id wajib bilangan bulat positif; tidak ada yang ditulis"}),
            )
        }
    };
    let answer = match body.get("answer").and_then(Value::as_str).map(str::trim) {
        Some(a) if !a.is_empty() => a.chars().take(4000).collect::<String>(),
        _ => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "stage": "validate",
                       "error": "jawaban kosong ditolak; pertanyaannya tetap menunggu"}),
            )
        }
    };
    let by = body
        .get("by")
        .and_then(Value::as_str)
        .map(|s| s.chars().take(60).collect::<String>())
        .filter(|s| !s.trim().is_empty())
        .unwrap_or_else(|| "operator-gui".to_string());

    let mut args: Vec<String> = vec![
        "answer".to_string(),
        id.to_string(),
        "--choice".to_string(),
        answer,
        "--by".to_string(),
        by.clone(),
    ];
    if let Some(note) = body
        .get("note")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
    {
        args.extend(["--note".to_string(), note.chars().take(2000).collect::<String>()]);
    }

    let recorded = match run_tool_async(args).await {
        Ok(out) => out,
        Err(e) => {
            spool_event(
                "decision_answer_failed",
                "station",
                format!(
                    "pencatatan jawaban decision {id} gagal: {}",
                    e.chars().take(200).collect::<String>()
                ),
                None,
                Some("work_order"),
            );
            return envelope(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"ok": false, "stage": "record", "error": e,
                       "note": "pertanyaannya masih OPEN; tidak ada yang diantar"}),
            );
        }
    };
    let parsed: Value = serde_json::from_str(&recorded).unwrap_or(Value::Null);
    if parsed.get("ok").and_then(Value::as_bool) != Some(true) {
        // Ditolak oleh pemilik kosakata: barisnya sudah dijawab, atau tidak pernah ada. Bukan
        // kegagalan HTTP yang perlu dicoba lagi — menimpanya berarti menghapus bukti siapa yang
        // memutuskan, jadi bentuk balasannya penolakan, bukan suntingan.
        let msg = parsed
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("ditolak work_order.py")
            .to_string();
        return envelope(
            StatusCode::CONFLICT,
            json!({"ok": false, "stage": "record", "error": msg, "raw": recorded}),
        );
    }

    let delivery = match run_drainer_async(vec![
        "deliver".to_string(),
        "--decision".to_string(),
        id.to_string(),
    ])
    .await
    {
        Ok(out) => serde_json::from_str::<Value>(&out).unwrap_or(json!({"raw": out})),
        Err(e) => json!({"result": {"sent": false, "why": e}}),
    };
    let sent = delivery
        .pointer("/result/sent")
        .and_then(Value::as_bool)
        .map(|b| if b { "dikirim" } else { "tidak dikirim" })
        .unwrap_or("tidak diketahui");
    spool_event(
        "decision_answered",
        "station",
        format!("decision {id} dijawab oleh {by}; ke pane: {sent}"),
        None,
        Some("work_order"),
    );
    envelope(
        StatusCode::OK,
        json!({"ok": true, "recorded": parsed, "delivery": delivery}),
    )
}
