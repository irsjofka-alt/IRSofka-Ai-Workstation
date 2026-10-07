//! Endpoint yang menyentuh pane: baca layar, kirim byte, resize, reset, dan jalankan CLI
//! sekali jalan.
//!
//! Handler di sini tidak tahu-menahu soal tmux atau PTY — semuanya lewat PtySession di
//! `crate::terminal`. Yang ditambah modul ini cuma bentuk HTTP-nya: nama tab dari query,
//! byte dari body, dan aturan berapa banyak yang boleh dikirim sekali baca.
use axum::extract::{Json, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};
use std::collections::HashMap;

use crate::api::{AppState, CliRunPayload, ReadQuery, ResetPayload, ResizePayload, WritePayload};
use crate::profile::read_profiles;
use crate::save_state::pg_conn_str;
use crate::spool::spool_event;
use crate::terminal::{
    capture_screen, engine_hint, tab_live_process,
    PtySession,
};

pub(crate) async fn term_screen(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    let tab = query.tab.unwrap_or_else(|| "qoder".to_string());
    if let Some(screen) = capture_screen(&tab) {
        return stream_body(screen.into_bytes());
    }
    // Fallback untuk tab PTY langsung (tanpa tmux): pakai buffer riwayat.
    stream_body(poll_buffer(&state, &Some(tab)))
}

pub(crate) async fn term_read(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    stream_body(poll_buffer(&state, &query.tab))
}

pub(crate) fn poll_buffer(state: &AppState, tab: &Option<String>) -> Vec<u8> {
    let tab = tab.clone().unwrap_or_else(|| "qoder".to_string());
    state
        .sessions
        .get(&tab)
        .map(|session| session.read_new())
        .unwrap_or_default()
}

pub(crate) fn stream_body(output: Vec<u8>) -> Response {
    Response::builder()
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-store")
        .body(axum::body::Body::from(output))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

pub(crate) async fn term_history(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    let tab = query.tab.unwrap_or_else(|| "qoder".to_string());
    let output = state
        .sessions
        .get(&tab)
        .map(|session| session.read_history())
        .unwrap_or_default();
    stream_body(output)
}

pub(crate) fn live_tab_cwd(sessions: &HashMap<String, PtySession>, tab: &str) -> Option<String> {
    let mut profiles = read_profiles();
    let profile = profiles.remove(tab)?;
    sessions
        .get(tab)
        .and_then(|s| tab_live_process(s.root_pid, engine_hint(&profile.engine)))
        .map(|l| l.cwd)
}

pub(crate) async fn term_write(
    State(state): State<AppState>,
    Json(payload): Json<WritePayload>,
) -> Json<Value> {
    let tab = payload.tab.unwrap_or_else(|| "qoder".to_string());
    if let Some(session) = state.sessions.get(&tab) {
        if let Some(data) = payload.data {
            session.send_bytes(data.as_bytes());
            // xterm.js mengirim ketikan sepotong-sepotong; kumpulkan sampai Enter agar
            // yang tercatat di log adalah perintah utuh, bukan-butir huruf.
            let mut flushed: Vec<String> = Vec::new();
            if let Ok(mut typed) = state.typed.lock() {
                let buf = typed.entry(tab.clone()).or_default();
                for ch in data.chars() {
                    match ch {
                        '\r' | '\n' => {
                            let line = buf.trim().to_string();
                            buf.clear();
                            if !line.is_empty() {
                                flushed.push(line);
                            }
                        }
                        '\u{7f}' => {
                            buf.pop();
                        }
                        c if (c as u32) >= 0x20 => {
                            buf.push(c);
                            if buf.len() > 4000 {
                                buf.clear();
                            }
                        }
                        _ => {}
                    }
                }
            }
            for line in flushed {
                let cwd = live_tab_cwd(&state.sessions, &tab);
                spool_event("terminal_command", &tab, line, cwd, Some("terminal"));
            }
        }
    }
    Json(json!({ "status": "ok" }))
}

pub(crate) async fn term_reset(
    State(state): State<AppState>,
    Json(payload): Json<ResetPayload>,
) -> Json<Value> {
    let tab = payload.tab.unwrap_or_else(|| "qoder".to_string());
    if let Some(session) = state.sessions.get(&tab) {
        session.interrupt();
    }
    if let Ok(mut typed) = state.typed.lock() {
        typed.insert(tab.clone(), String::new());
    }
    let cwd = live_tab_cwd(&state.sessions, &tab);
    spool_event("interrupt", &tab, "Sinyal Ctrl+C dikirim ke tab".to_string(), cwd, Some("terminal"));
    Json(json!({ "status": "reset" }))
}

pub(crate) async fn term_resize(
    State(state): State<AppState>,
    Json(payload): Json<ResizePayload>,
) -> Json<Value> {
    // Sengguh hanya tab yang diminta. Pernah diubah menjadi "terapkan ke semua sesi" dan
    // itu REGRESI: tab tersembunyi ikut dipaksa ke geometri tab aktif, TUI di dalamnya
    // menata ulang dirinya sendiri, dan tampilannya jadi kacau sampai jendela di-resize.
    // Geometri tab yang menyimpang memang ada (pernah terlihat 251x49), tapi itu harus
    // dibereskan lewat jalur yang tidak menyentuh render, bukan di sini.
    let tab = payload.tab.unwrap_or_else(|| "qoder".to_string());
    if let Some(session) = state.sessions.get(&tab) {
        session.resize(payload.cols, payload.rows);
    }
    Json(json!({ "status": "ok" }))
}

pub(crate) async fn cli_run(
    State(state): State<AppState>,
    Json(payload): Json<CliRunPayload>,
) -> Json<Value> {
    let prompt = payload.prompt.unwrap_or_default().trim().to_string();
    if prompt.is_empty() {
        return Json(json!({ "status": "empty" }));
    }
    let target_cli = payload.target_cli.unwrap_or_else(|| "qoder".to_string());

    if let Some(session) = state.sessions.get(&target_cli) {
        // Enter di terminal adalah \\r (CR), BUKAN \\n. TUI seperti Antigravity/Qoder membaca
        // stdin dalam raw mode dan hanya mengenali \\r sebagai tombol Enter; dengan \\n teksnya
        // masuk ke kotak input tapi tidak pernah disubmit — kegagalan dispatch yang tampak
        // seperti "CLI tidak menjawab" padahal promptnya cuma menggantung tanpa dikirim.
        // TUI membaca keyboard, bukan stream teks: `\n` di tengah prompt sering DITELAN
        // sehingga semua baris menyambung, atau malah dianggap Enter. Bracketed paste
        // adalah cara standar menyampaikan teks multi-baris utuh ke input TUI.
        let body = if prompt.contains('\n') {
            format!("\x1b[200~{}\x1b[201~\r", prompt)
        } else {
            format!("{}\r", prompt)
        };
        session.send_bytes(body.as_bytes());
    }
    let cwd = live_tab_cwd(&state.sessions, &target_cli);
    spool_event(
        "chat_dispatch",
        &target_cli,
        prompt.clone(),
        cwd,
        Some("dispatch"),
    );

    let prof = read_profiles().get(&target_cli).cloned().unwrap_or_default();

    let m = prof.model.clone();
    let eff = prof.effort.clone();
    let ctx = prof.context_window.clone();
    let prompt_for_db = prompt.clone();
    let cli_for_db = target_cli.clone();

    tokio::spawn(async move {
        if let Ok((client, connection)) =
            tokio_postgres::connect(&pg_conn_str(), tokio_postgres::NoTls).await
        {
            tokio::spawn(async move {
                let _ = connection.await;
            });
            let ctx_val: i32 = ctx.parse().unwrap_or(1_000_000);
            let _ = client
                .execute(
                    "INSERT INTO session_turns (cli_engine, model, reasoning_effort, context_window, prompt) VALUES ($1, $2, $3, $4, $5);",
                    &[&cli_for_db, &m, &eff, &ctx_val, &prompt_for_db],
                )
                .await;
        }
    });

    Json(json!({ "status": "dispatched", "target_cli": target_cli }))
}
