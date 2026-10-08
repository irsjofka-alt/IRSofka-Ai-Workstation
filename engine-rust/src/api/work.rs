//! Jalur tulis Dashboard (F10.10): antrean perintah operator, daftar proyek, fokus, dan pedoman.
//!
//! Aturan yang sama dengan `autopilot.rs`, dan untuk alasan yang sama: **Python memegang
//! kosakata, Rust memegang HTTP**. Yang boleh diperiksa di sini hanyalah BENTUK masukan —
//! string dipotong, id bilangan bulat positif, action ada dalam daftar. Keputusan apakah sebuah
//! perintah boleh diantrekan, mesin mana yang boleh memakainya, dan apakah sebuah path boleh
//! disebut proyek semuanya dibuat satu kali di `tools/work_order.py`. Menyalin salah satunya ke
//! file ini berarti workstation punya dua aturan untuk satu kata, dan kontrak §12 melarang itu.
//!
//! Empat action, empat subcommand Python, tanpa satu pun perhitungan di tengah:
//!
//! ```text
//! queue        -> work_order.py queue "<title>" --engine X [--gate G] [--ws W] --by B
//! create       -> work_order.py ws create --name N --path P
//! focus        -> work_order.py ws focus <nama|id|path> --by B
//! guidelines   -> work_order.py ws guides <id> --files a,b,c
//! ```
//!
//! Proses anak selalu lewat `run_tool_async` milik `autopilot.rs` — satu-satunya tempat
//! `work_order.py` diberangkatkan, supaya tidak ada jalur kedua yang boleh lupa memotong grup
//! prosesnya (kontrak §4).
use axum::body::Body;
use axum::extract::Json;
use axum::http::header::CONTENT_TYPE;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};

use crate::api::autopilot::run_tool_async;
use crate::spool::spool_event;

fn envelope(status: StatusCode, body: Value) -> Response {
    (status, Json(body)).into_response()
}

/// Balas dengan byte milik `work_order.py` apa adanya — lihat alasan yang identik di autopilot.rs.
fn raw_json(status: StatusCode, text: String) -> Response {
    (status, [(CONTENT_TYPE, "application/json")], Body::from(text)).into_response()
}

/// String yang dipotong; `None` berarti field itu tidak dikirim, bukan dikirim kosong.
fn clipped(v: Option<&Value>, max: usize) -> Option<String> {
    v.and_then(Value::as_str)
        .map(|s| s.trim().chars().take(max).collect::<String>())
        .filter(|s| !s.is_empty())
}

fn positive_id(v: Option<&Value>) -> Option<u64> {
    match v.and_then(Value::as_u64) {
        Some(n) if n > 0 && n <= 10_000_000 => Some(n),
        _ => None,
    }
}

/// GET /api/work — daftar proyek, fokus yang diturunkan, dan pedoman yang terdeteksi.
///
/// Handler ini tidak menyusun angka apa pun: `ws list` adalah pembacaan `work_order.py` atas
/// database + symlink, dan byte-nya dikirim apa adanya ke browser.
pub(crate) async fn get_work() -> Response {
    match run_tool_async(vec!["ws".to_string(), "list".to_string()]).await {
        Ok(text) => raw_json(StatusCode::OK, text),
        Err(e) => envelope(
            StatusCode::SERVICE_UNAVAILABLE,
            json!({"ok": false, "error": e, "note": "daftar proyek tidak terbaca — Dashboard menampilkan UNKNOWN, bukan kosong"}),
        ),
    }
}

/// POST /api/work — satu tulisan antrean/proyek, lewat pemilik kosakatanya.
pub(crate) async fn post_work(Json(body): Json<Value>) -> Response {
    let action = match body.get("action").and_then(Value::as_str) {
        Some(a) => a.trim().to_ascii_lowercase(),
        None => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "stage": "validate", "error": "action wajib diisi"}),
            )
        }
    };
    let by = clipped(body.get("by"), 80).unwrap_or_else(|| "operator-gui".to_string());

    let args: Vec<String> = match action.as_str() {
        "queue" => {
            let title = match clipped(body.get("title"), 255) {
                Some(t) => t,
                None => {
                    return envelope(
                        StatusCode::BAD_REQUEST,
                        json!({"ok": false, "stage": "validate",
                               "error": "perintah kosong ditolak; tidak ada baris yang ditulis"}),
                    )
                }
            };
            // `--engine` wajib di sini BUKAN karena Rust memilih mesin: Rust hanya memaksa
            // field itu dikirim, lalu Python yang memutuskan apakah nama itu terdaftar.
            let engine = match clipped(body.get("engine"), 60) {
                Some(e) => e,
                None => {
                    return envelope(
                        StatusCode::BAD_REQUEST,
                        json!({"ok": false, "stage": "validate",
                               "error": "mesin tujuan wajib dipilih — memilih dari ingatan dilarang §12"}),
                    )
                }
            };
            let mut a = vec!["queue".to_string(), title, "--engine".to_string(), engine];
            if let Some(g) = clipped(body.get("gate"), 2000) {
                a.extend(["--gate".to_string(), g]);
            }
            if let Some(w) = clipped(body.get("ws"), 600) {
                a.extend(["--ws".to_string(), w]);
            }
            a.extend(["--by".to_string(), by.clone()]);
            a
        }
        "create" => {
            let path = match clipped(body.get("path"), 600) {
                Some(p) => p,
                None => {
                    return envelope(
                        StatusCode::BAD_REQUEST,
                        json!({"ok": false, "stage": "validate",
                               "error": "path proyek wajib diisi — tidak ada yang didaftarkan"}),
                    )
                }
            };
            let mut a = vec!["ws".to_string(), "create".to_string(), "--path".to_string(), path];
            if let Some(n) = clipped(body.get("name"), 120) {
                a.extend(["--name".to_string(), n]);
            }
            a
        }
        "focus" => {
            let needle = match clipped(body.get("workspace"), 600) {
                Some(n) => n,
                None => {
                    return envelope(
                        StatusCode::BAD_REQUEST,
                        json!({"ok": false, "stage": "validate",
                               "error": "proyek target wajib diisi — fokus tidak diubah"}),
                    )
                }
            };
            vec![
                "ws".to_string(),
                "focus".to_string(),
                needle,
                "--by".to_string(),
                by.clone(),
            ]
        }
        "guidelines" => {
            let id = match positive_id(body.get("workspace")) {
                Some(i) => i.to_string(),
                None => {
                    return envelope(
                        StatusCode::BAD_REQUEST,
                        json!({"ok": false, "stage": "validate",
                               "error": "workspace harus bilangan bulat positif — pedoman tidak disimpan"}),
                    )
                }
            };
            let files = body
                .get("files")
                .and_then(Value::as_array)
                .map(|arr| {
                    arr.iter()
                        .filter_map(|x| x.as_str().map(|s| s.trim().chars().take(300).collect::<String>()))
                        .filter(|s| !s.is_empty())
                        .collect::<Vec<String>>()
                        .join(",")
                })
                .unwrap_or_default();
            // String kosong berarti "kosongkan pilihan", dan itu tulisan yang sah — karena itu
            // `--files` dikirim walaupun kosong, dengan satu spasi penahan agar argparse tidak
            // menafsirkannya sebagai flag.
            vec![
                "ws".to_string(),
                "guidelines".to_string(),
                id,
                "--files".to_string(),
                if files.is_empty() { " ".to_string() } else { files },
            ]
        }
        other => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "stage": "validate",
                       "error": format!("action {other:?} tidak dikenal (queue/create/focus/guidelines)")}),
            )
        }
    };

    let shown = args.iter().take(2).cloned().collect::<Vec<_>>().join(" ");
    match run_tool_async(args).await {
        Ok(text) => {
            spool_event(
                "dashboard_write",
                "station",
                format!("{action} oleh {by} lewat Dashboard ({shown}) — hasil ada di byte yang dikembalikan work_order.py"),
                None,
                Some("work_order"),
            );
            raw_json(StatusCode::OK, text)
        }
        Err(e) => {
            spool_event(
                "dashboard_write_failed",
                "station",
                format!("{action} oleh {by} gagal: {}", e.chars().take(200).collect::<String>()),
                None,
                Some("work_order"),
            );
            envelope(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"ok": false, "stage": "dispatch", "action": action, "error": e}),
            )
        }
    }
}
