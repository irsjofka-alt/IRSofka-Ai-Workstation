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
use crate::paths::{fresh_marker_path, profiles_path};
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
    // Nama tab masuk ke nama berkas penanda, jadi hanya bentuk [a-z0-9_-] yang diterima.
    if tab.is_empty()
        || !tab
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
    {
        return Json(json!({"status": "error", "reason": "nama tab tidak sah"}));
    }
    let fresh = body
        .get("fresh")
        .and_then(Value::as_bool)
        .unwrap_or(false);
    let hint = read_profiles().get(&tab).map(|p| engine_hint(&p.engine)).unwrap_or(None);

    // dikenal = pane-nya benar-benar ada di bawah daemon. Menandai lebih dulu untuk tab
    // yang tidak dikenal akan meninggalkan penanda basi yang dibayar pada tab berikutnya.
    let known = state.sessions.contains_key(&tab);
    // Penanda hanya berguna kalau ada CLI yang memang akan disalakan ulang. Untuk tab
    // shell (hint None) penandanya tidak akan pernah dikonsumsi dan tertinggal selamanya.
    let fresh_started = known && fresh && hint.is_some();
    if fresh_started {
        if let Err(e) = fs::write(fresh_marker_path(&tab), b"") {
            return Json(json!({"status": "error", "reason": format!("penanda gagal ditulis: {e}")}));
        }
    }

    // Dijelaskan SEBELUM menembak, supaya catatan di respons menyebut sasaran yang benar
    // dan bukan keadaan setelahnya.
    let note = match state.sessions.get(&tab) {
        None => "tab not known to the daemon".to_string(),
        Some(_) if hint.is_none() => "shell tab: pane cleared, no CLI to terminate".to_string(),
        Some(s) => match tab_cli_target(s.root_pid, hint) {
            Some((pid, comm, true)) => format!("{comm} (pid {pid}) terminated"),
            _ => "no live CLI: the supervisor loop relaunches it with the fresh marker"
                .to_string(),
        },
    };
    let killed = match state.sessions.get(&tab) {
        Some(s) if hint.is_some() => kill_tab_cli_root(s.root_pid, hint),
        _ => false,
    };
    // Bersihkan bek SETELAH bunuh, bukan sebelumnya: SIGTERM membuat CLI mati dalam
    // puluhan milidetik sementara loop baru menyalakan ulang setelah >=1 detik, jadi
    // jendela itu cukup untuk memotong log tanpa memakan byte pertama sesi baru.
    // Tab shell tidak punya CLI untuk dibunuh (hint None) — bagi pane itu "sesi baru"
    // berarti layar bersih, dan itu yang dikerjakan di sini.
    let cleared = match state.sessions.get(&tab) {
        Some(s) => {
            s.clear_backlog();
            true
        }
        None => false,
    };

    let cwd = crate::api::terminal::live_tab_cwd(&state.sessions, &tab);
    spool_event(
        "tab_restart",
        &tab,
        format!(
            "New Session: killed={killed} fresh={fresh_started} backlog_cleared={cleared} — {note}"
        ),
        cwd,
        Some("restart"),
    );
    Json(json!({
        "status": if known { "ok" } else { "unknown_tab" },
        "tab": tab,
        "killed": killed,
        "fresh": fresh_started,
        "backlog_cleared": cleared,
        "detail": note
    }))
}

pub(crate) fn kill_tab_cli_root(root: u32, hint: Option<&str>) -> bool {
    match tab_live_process(root, hint) {
        // `root` adalah bash penunggu loop pane. Ia tidak boleh jadi korban: tidak ada
        // watchdog yang membuat pane yang mati, dan restart daemon akan MENGADOPSI pane
        // mati itu (has-session masih true) alih-alih menggantinya. Jadi kalau tidak ada
        // CLI yang hidup, kembalikan false dan biarkan loop menyalakan ulang sendiri.
        Some(live) if live.pid != root => {
            let _ = Command::new("kill")
                .args(["-TERM", &live.pid.to_string()])
                .spawn();
            true
        }
        _ => false,
    }
}

/// Proses mana yang sebenarnya jadi sasaran, untuk pesan yang jujur ke operator.
pub(crate) fn tab_cli_target(root: u32, hint: Option<&str>) -> Option<(u32, String, bool)> {
    let live = tab_live_process(root, hint)?;
    Some((live.pid, live.comm, live.pid != root))
}
