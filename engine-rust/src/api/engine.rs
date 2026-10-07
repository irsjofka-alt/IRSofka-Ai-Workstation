//! Katalog model, pemakaian, dan profil CLI per tab.
//!
//! Daftar model tidak pernah ditulis tangan di workstation ini: `/api/models` memanggil
//! `qoder --list-models` dan `agy models` apa adanya. Yang bisa diubah lewat API cuma profil
//! (model/effort/context/workspace per tab), dan perubahan itu baru benar-benar berlaku
//! setelah pane di-restart — endpoint di sinilah yang menjembatani keduanya.
use axum::extract::{Json, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{json, Value};
use std::fs;
use std::process::Command;
use std::time::Duration;

use crate::api::{AppState, ConfigPatch};
use crate::engineinfo::{
    agy_efforts, agy_models, antigravity_last_model, antigravity_usage,
    qoder_account, qoder_efforts, qoder_models, qoder_session_usage,
};
use crate::paths::profiles_path;
use crate::probe::cached_json;
use crate::profile::{read_profiles, TabProfile};
use crate::spool::spool_event;
use crate::terminal::{engine_hint, pane_context_pct, tab_live_process};
pub(crate) async fn get_models() -> Json<Value> {
    let q = cached_json("models_qoder", Duration::from_secs(120), || {
        json!(qoder_models())
    });
    let a = cached_json("models_agy", Duration::from_secs(120), || {
        json!(agy_models())
    });
    Json(json!({
        "qoder": q,
        "agy": a,
        "qoder_efforts": qoder_efforts(),
        "agy_efforts": agy_efforts(),
        "qoder_permission_modes": [
            {"id":"bypass_permissions","label":"Auto-Approve (bypass)"},
            {"id":"accept_edits","label":"Accept Edits"},
            {"id":"auto","label":"Auto"},
            {"id":"dont_ask","label":"Don't Ask"},
            {"id":"default","label":"Tanya Setiap Kali"}
        ],
        "agy_permission_modes": [
            {"id":"skip","label":"Auto-Approve (dangerously skip)"},
            {"id":"accept-edits","label":"Accept Edits"},
            {"id":"plan","label":"Plan Mode"},
            {"id":"default","label":"Tanya Setiap Kali"}
        ]
    }))
}

pub(crate) async fn get_usage() -> Json<Value> {
    let profiles = read_profiles();
    let qws = profiles
        .get("qoder")
        .map(|p| p.workspace.clone())
        .unwrap_or_default();
    let account = qoder_account();
    let sess = cached_json(
        &format!("qoder_usage_{}", qws),
        Duration::from_secs(20),
        || qoder_session_usage(&qws),
    );
    let agy = cached_json("agy_usage", Duration::from_secs(20), antigravity_usage);
    Json(json!({
        "qoder": {
            "account": account,
            "session": sess,
            "workspace": qws,
            "context_pct": pane_context_pct("qoder")
        },
        "antigravity": {
            "conversation": agy,
            "last_model": antigravity_last_model(),
            "context_pct": pane_context_pct("antigravity")
        }
    }))
}

pub(crate) async fn get_cli_config() -> Json<Value> {
    Json(json!({ "profiles": read_profiles() }))
}

pub(crate) async fn patch_cli_config(
    State(state): State<AppState>,
    Json(patch): Json<ConfigPatch>,
) -> Response {
    let mut profiles = read_profiles();
    let tab = patch.tab.clone();
    let entry = profiles.entry(tab.clone()).or_insert_with(TabProfile::default);
    if let Some(v) = patch.model {
        entry.model = v;
    }
    if let Some(v) = patch.effort {
        entry.effort = v;
    }
    if let Some(v) = patch.context_window {
        entry.context_window = v;
    }
    if let Some(v) = patch.permission_mode {
        entry.permission_mode = v;
    }
    if let Some(v) = patch.workspace {
        entry.workspace = v;
    }
    if let Some(v) = patch.continue_session {
        entry.continue_session = v;
    }

    let engine = entry.engine.clone();

    let body = match serde_json::to_string_pretty(&profiles) {
        Ok(b) => b,
        Err(e) => {
            return (
                StatusCode::INTERNAL_SERVER_ERROR,
                format!("serialisasi gagal: {}", e),
            )
                .into_response()
        }
    };
    if let Err(e) = fs::write(profiles_path(), body) {
        return (StatusCode::INTERNAL_SERVER_ERROR, format!("gagal simpan: {}", e)).into_response();
    }

    let saved = profiles.get(&tab).cloned().unwrap_or_default();
    spool_event(
        "cli_config_change",
        &tab,
        format!(
            "model={} effort={} context={} permission={} workspace={} continue={}",
            saved.model, saved.effort, saved.context_window, saved.permission_mode,
            saved.workspace, saved.continue_session
        ),
        if saved.workspace.is_empty() { None } else { Some(saved.workspace.clone()) },
        Some("config"),
    );

    let restarted = if patch.restart {
        state
            .sessions
            .get(&tab)
            .map(|s| kill_tab_cli_root(s.root_pid, engine_hint(&engine)))
            .unwrap_or(false)
    } else {
        false
    };

    Json(json!({
        "status": "saved",
        "tab": tab,
        "restarted": restarted,
        "profiles": profiles
    }))
    .into_response()
}

pub(crate) async fn restart_cli_tab(State(state): State<AppState>, Json(body): Json<Value>) -> Json<Value> {
    let tab = body
        .get("tab")
        .and_then(Value::as_str)
        .unwrap_or("qoder")
        .to_string();
    let hint = read_profiles().get(&tab).map(|p| engine_hint(&p.engine)).unwrap_or(None);
    let killed = match state.sessions.get(&tab) {
        Some(s) if hint.is_some() => kill_tab_cli_root(s.root_pid, hint),
        _ => false,
    };
    let cwd = crate::api::terminal::live_tab_cwd(&state.sessions, &tab);
    spool_event(
        "tab_restart",
        &tab,
        format!("CLI tab dihentikan agar spawn ulang dengan profil terkini; killed={}", killed),
        cwd,
        Some("restart"),
    );
    Json(json!({ "status": "ok", "tab": tab, "killed": killed }))
}

pub(crate) fn kill_tab_cli_root(root: u32, hint: Option<&str>) -> bool {
    match tab_live_process(root, hint) {
        Some(live) => {
            let _ = Command::new("kill")
                .args(["-TERM", &live.pid.to_string()])
                .spawn();
            true
        }
        None => false,
    }
}
