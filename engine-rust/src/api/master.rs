//! Data induk workstation: tim, role, mesin yang boleh diberangkatkan — dan jalan tulisnya.
//!
//! Pembagian kerja F8.3 disengaja dan tidak boleh ditukar: **Python yang memvalidasi**,
//! **Rust yang memiliki berkasnya**. Validator tinggal di `tools/master_data.py` karena di
//! situlah kosakata meter dan parser berada (`session_ingestor`) dan cara mesin resolve dari
//! config berada (`cross_verify`). Kalau aturan itu diduplikasi dalam Rust, kita punya dua
//! laporan yang saling bertentangan dan tidak ada cara tahu mana yang benar — persis yang
//! dilarang kontrak §12.
//!
//! Yang dijaga di sini:
//! 1. hanya berkas data induk yang terdaftar yang boleh ditulis — nama dari proses anak
//!    tidak pernah dipercaya begitu saja,
//! 2. setiap tulisan diawali cadangan bertanda waktu dan ditulis atomik (tmp + rename),
//! 3. sesudah ditulis, dokumen di disk dibaca ulang dan divalidasi ulang; kalau hasilnya
//!    tidak sah, cadangan dikembalikan dan operator mendapat kesalahan yang sebenarnya,
//! 4. setiap perubahan masuk ke `action_log` lewat spool, supaya "siapa mengganti verifier"
//!    bisa dijawab dari database, bukan dari ingatan,
//! 5. dokumen master data tidak pernah diserialisasi ulang di sini. `serde_json::Value`
//!    menyimpan objek sebagai map terurut nama kunci, jadi satu kali parse lalu `Json(v)`
//!    sudah cukup untuk mengacak ulang seluruh `engines.json` — 104 baris berubah tanpa ada
//!    satu nilai pun yang berubah. Byte dari validator dikirim apa adanya ke browser, dan
//!    byte dari browser dikirim apa adanya ke validator; yang boleh disentuh Rust hanyalah
//!    string dan boolean yang dibutuhkan untuk memutuskan (ok, nama berkas, pesan galat).
//! 6. validator diberangkatkan dengan batas waktu keras dan dibunuh beserta grup prosesnya
//!    (kontrak §4): dia memanggil CLI, dan CLI yang tertinggal adalah RAM yang tidak pulang.
use axum::body::{Body, Bytes};
use axum::extract::Json;
use axum::http::header::CONTENT_TYPE;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use chrono::Local;
use serde_json::{json, Value};
use std::fs;
use std::path::PathBuf;

use crate::paths::{engines_registry_path, master_backups_dir, master_data_path, slots_path};
use crate::spool::spool_event;

/// Peta nama berkas yang boleh dituliskan endpoint ini. `plan` mengembalikan nama relatif;
/// nama di luar peta ini ditolak, apa pun yang dikembalikan proses anak.
fn allowed_target(rel: &str) -> Option<PathBuf> {
    match rel {
        "config/engines.json" => Some(engines_registry_path()),
        "config/slots.yaml" => Some(slots_path()),
        _ => None,
    }
}

fn python() -> PathBuf {
    // Daemon systemd tidak mewarisi PATH selebar shell; cari dulu interpreter yang nyata.
    for candidate in ["/usr/bin/python3", "/usr/local/bin/python3"] {
        if PathBuf::from(candidate).exists() {
            return PathBuf::from(candidate);
        }
    }
    PathBuf::from("python3")
}

/// Balas dengan byte keluaran validator, tanpa menyentuh urutan kuncinya.
fn raw_json(status: StatusCode, text: String) -> Response {
    (status, [(CONTENT_TYPE, "application/json")], Body::from(text)).into_response()
}

/// Balas dengan amplop yang memang kita karang sendiri. Tidak pernah dipakai untuk dokumen
/// master data — hanya untuk pesan galat kecil milik endpoint ini.
fn envelope(status: StatusCode, body: Value) -> Response {
    (status, Json(body)).into_response()
}

/// Keluaran validator: teks JSON apa adanya, plus bentuk terurainya untuk memutuskan.
///
/// Keduanya disimpan bersama karena keduanya dibutuhkan: `text` adalah satu-satunya bentuk
/// yang boleh dikirim ke browser, `value` hanya dipakai untuk membaca field tertentu.
struct ModeOutput {
    text: String,
    value: Value,
}

/// Kirim kandidat ke satu mode master_data.py, baca JSON yang ia cetak.
///
/// Kandidat dikirim lewat stdin sebagai byte asli dari browser, bukan hasil serialisasi
/// ulang: dokumen registry berisi template prompt dan bisa ratusan KB, dan argv punya batas;
/// dan hanya byte yang tidak pernah diurai yang membuat urutan kunci `engines.json` utuh.
fn run_mode(mode: &str, candidate: Option<&[u8]>) -> Result<ModeOutput, String> {
    let script = master_data_path();
    if !script.exists() {
        return Err(format!("validator tidak ada di {}", script.display()));
    }
    // Batas keras: validator memanggil `agy models` / `qoder --list-models`, dan satu
    // pembacaan yang menggantung tidak boleh membawa serta thread maupun anak prosesnya.
    let script_arg = script.display().to_string();
    let python_bin = python().display().to_string();
    let probe = crate::probe::run_bounded(
        &python_bin,
        &[script_arg.as_str(), mode],
        candidate,
        crate::probe::PROBE_TIMEOUT,
    );
    if probe.timed_out {
        return Err(format!(
            "validator tidak selesai dalam {}s dan seluruh grup prosesnya sudah dipotong",
            crate::probe::PROBE_TIMEOUT.as_secs()
        ));
    }
    let stdout = probe.stdout;
    let parsed = stdout
        .find('{')
        .and_then(|i| serde_json::from_str::<Value>(&stdout[i..]).ok().map(|v| (i, v)));
    match parsed {
        Some((i, value)) => Ok(ModeOutput { text: stdout[i..].to_string(), value }),
        None => Err(format!(
            "validator tidak mengembalikan JSON: rc={:?}; ok={}; stdout={}; stderr={}",
            probe.code,
            probe.ok,
            stdout.chars().take(400).collect::<String>(),
            probe.stderr.chars().take(400).collect::<String>()
        )),
    }
}

/// GET /api/master — dokumen hidup, laporan validasi, dan apa yang akan benar-benar dipakai.
pub(crate) async fn get_master() -> Response {
    match run_mode_async("show", None).await {
        Ok(out) => raw_json(StatusCode::OK, out.text),
        Err(e) => envelope(StatusCode::SERVICE_UNAVAILABLE, json!({"ok": false, "error": e})),
    }
}

/// Jalankan validator di LUAR worker tokio.
///
/// `run_mode` menunggu proses anak selama beberapa detik — `qoder --list-models` dan
/// `agy models` bicara ke jaringan, dan malam ini keduanya pernah terlihat saling tunggu
/// sampai 60 detik. Di dalam handler async itu berarti satu worker tidak melayani apa pun
/// selama proses berjalan, dan seluruh endpoint lain ikut melambat diam-diam.
async fn run_mode_async(mode: &str, candidate: Option<Vec<u8>>) -> Result<ModeOutput, String> {
    let mode = mode.to_string();
    tokio::task::spawn_blocking(move || run_mode(&mode, candidate.as_deref()))
        .await
        .map_err(|e| format!("validator task berhenti: {e}"))?
}

/// POST /api/master — validasi dulu, baru tulis. Tidak pernah menulis dokumen yang ditolak.
///
/// Badan request adalah dokumen hasil GET dengan bagian `engines` yang sudah disunting.
/// Yang dikembalikan: laporan, berkas yang ditulis, dan nama cadangan untuk membatalkan.
pub(crate) async fn post_master(candidate: Bytes) -> Response {
    // Sekadar memeriksa bentuk — byte aslinya yang dikirim ke validator, bukan nilai ini.
    let parsed: Value = match serde_json::from_slice(&candidate) {
        Ok(v) => v,
        Err(e) => {
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "errors": [{"path": "(body)",
                    "message": format!("badan request bukan JSON yang sah: {e}")}]}),
            );
        }
    };
    if !parsed.is_object() {
        return envelope(
            StatusCode::BAD_REQUEST,
            json!({"ok": false, "errors": [{"path": "(body)",
                "message": "badan request harus objek dokumen master data"}]}),
        );
    }

    let plan = match run_mode_async("plan", Some(candidate.to_vec())).await {
        Ok(out) => out,
        Err(e) => {
            return envelope(
                StatusCode::SERVICE_UNAVAILABLE,
                json!({"ok": false, "errors": [{"path": "validator", "message": e}]}),
            );
        }
    };
    if !plan.value.get("ok").and_then(Value::as_bool).unwrap_or(false) {
        // Tidak ada satu byte pun yang ditulis di jalur ini. Laporan validator dikirim apa
        // adanya supaya yang dibaca operator persis seperti yang validator putuskan.
        return raw_json(StatusCode::UNPROCESSABLE_ENTITY, plan.text);
    }

    let files: Vec<(String, String)> = match plan
        .value
        .get("files")
        .and_then(Value::as_object)
        .map(|f| {
            f.iter()
                .filter_map(|(k, v)| v.as_str().map(|t| (k.clone(), t.to_string())))
                .collect::<Vec<(String, String)>>()
        }) {
        Some(f) if !f.is_empty() => f,
        _ => {
            return envelope(
                StatusCode::INTERNAL_SERVER_ERROR,
                json!({"ok": false, "errors": [{"path": "plan.files",
                    "message": "validator menyatakan sah tapi tidak menghasilkan berkas apa \
                                pun untuk ditulis"}]}),
            );
        }
    };

    let stamp = Local::now().format("%Y%m%d-%H%M%S").to_string();
    let mut written: Vec<String> = Vec::new();
    let mut backups: Vec<String> = Vec::new();
    let mut applied: Vec<(PathBuf, PathBuf)> = Vec::new();

    for (rel, text) in &files {
        let Some(target) = allowed_target(rel) else {
            rollback(&mut applied);
            return envelope(
                StatusCode::BAD_REQUEST,
                json!({"ok": false, "errors": [{"path": rel,
                    "message": format!("validator meminta menulis {rel}, yang bukan berkas \
                                        data induk yang diizinkan endpoint ini")}]}),
            );
        };
        match write_with_backup(&target, text, &stamp) {
            Ok(backup) => {
                written.push(rel.clone());
                backups.push(backup.display().to_string());
                applied.push((target, backup));
            }
            Err(e) => {
                let undone = applied.len();
                rollback(&mut applied);
                return envelope(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({"ok": false, "error": e, "rolled_back": undone > 0}),
                );
            }
        }
    }

    // Ditulis belum berarti sah: berkas bisa terpotong di tengah, atau hasil tulis tidak
    // sama dengan yang divalidasi. Baca ulang dari DISK (bukan dari kandidat) dan periksa.
    match run_mode_async("show", None).await {
        Ok(after) => {
            let report_ok = after
                .value
                .pointer("/report/ok")
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if report_ok {
                spool_event(
                    "master_data_change",
                    "station",
                    format!(
                        "saved {} (backups: {})",
                        written.join(", "),
                        backups.join(", ")
                    ),
                    None,
                    Some("config"),
                );
                envelope(
                    StatusCode::OK,
                    json!({"ok": true, "written": written, "backups": backups}),
                )
            } else {
                let undone = applied.len();
                // Pesan pertama dari laporan-lah yang menjelaskan apa yang sebenarnya salah
                // di disk; laporan lengkapnya tetap bisa dibaca lewat GET /api/master.
                let first = after
                    .value
                    .pointer("/report/errors/0/message")
                    .and_then(Value::as_str)
                    .unwrap_or("tanpa rincian")
                    .to_string();
                rollback(&mut applied);
                envelope(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    json!({"ok": false,
                        "errors": [{"path": "(after write)",
                            "message": format!("berkas yang baru ditulis ternyata tidak sah \
                                                saat dibaca ulang dari disk — cadangan \
                                                dikembalikan: {first}")}],
                        "rolled_back": undone > 0}),
                )
            }
        }
        Err(e) => envelope(
            StatusCode::INTERNAL_SERVER_ERROR,
            json!({"ok": false, "written": written, "backups": backups,
                "error": format!("berkas ditulis, tapi verifikasi pasca-tulis tidak bisa \
                                  dijalankan: {e}")}),
        ),
    }
}

/// Cadangan bertanda waktu lalu tulis-atomik. Mengembalikan path cadangan.
fn write_with_backup(target: &PathBuf, text: &str, stamp: &str) -> Result<PathBuf, String> {
    let backups = master_backups_dir();
    fs::create_dir_all(&backups)
        .map_err(|e| format!("gagal membuat {}: {e}", backups.display()))?;
    let name = target
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "master-data".to_string());
    let backup = backups.join(format!("{name}.{stamp}.bak"));
    if target.exists() {
        fs::copy(target, &backup).map_err(|e| format!("gagal mencadangkan {name}: {e}"))?;
        prune_backups(&backups, &name);
    }
    let tmp = target.with_file_name(format!("{name}.tmp.{stamp}"));
    fs::write(&tmp, text).map_err(|e| format!("gagal menulis {name}.tmp: {e}"))?;
    // rename() atomik dalam satu filesystem: tidak ada pembaca yang pernah melihat
    // engines.json separuh ditulis, termasuk engine yang sedang resolve modelnya sekarang.
    fs::rename(&tmp, target).map_err(|e| {
        let _ = fs::remove_file(&tmp);
        format!("gagal mengganti {name}: {e}")
    })?;
    Ok(backup)
}

/// Kembalikan berkas dari cadangan yang barusan dibuat. Hanya dipakai saat tulisan terbukti buruk.
fn rollback(applied: &mut Vec<(PathBuf, PathBuf)>) {
    while let Some((target, backup)) = applied.pop() {
        let _ = fs::copy(&backup, &target);
    }
    applied.clear();
}

/// Berapa banyak cadangan per berkas yang disimpan. Setiap save menambah satu file, dan
/// tidak ada yang me-rollback 50 langkah ke belakang; yang lama bukan sejarah, cuma disk.
const BACKUP_KEEP: usize = 20;

/// Buang cadangan paling tua dari satu berkas, sisakan `BACKUP_KEEP` terbaru.
///
/// Nama cadangan adalah `{berkas}.{YYYYMMDD-HHMMSS}.bak`, jadi urutan leksikografis sama
/// dengan urutan kronologis — tidak perlu membaca isi atau timestamp filesystem, yang bisa
/// bohong setelah sebuah restore.
fn prune_backups(dir: &std::path::Path, name: &str) {
    let prefix = format!("{name}.");
    let mut files: Vec<String> = match fs::read_dir(dir) {
        Ok(entries) => entries
            .filter_map(|e| e.ok())
            .filter_map(|e| e.file_name().to_str().map(str::to_string))
            .filter(|n| n.starts_with(&prefix) && n.ends_with(".bak"))
            .collect(),
        Err(_) => return,
    };
    files.sort();
    if files.len() <= BACKUP_KEEP {
        return;
    }
    for old in &files[..files.len() - BACKUP_KEEP] {
        let _ = fs::remove_file(dir.join(old));
    }
}
