//! Ruang kerja nyata, log aksi, dan pemulihan sesi.
//!
//! Yang membedakan modul ini dari telementri biasa: isinya menjawab "sedang apa mesin ini
//! sekarang" — cwd tiap pane, proses CLI yang hidup di bawahnya, flag model yang benar-benar
//! dipakai baris perintahnya, dan jejak aksi dari PostgreSQL. Semua dibangun ulang setiap
//! request; tidak ada state sesi yang dipegang di sini.
use axum::extract::{Query, State};
use axum::Json;
use serde_json::{json, Value};

use crate::api::{AppState, LogQuery};
use crate::paths::{spool_path, station_port, tmux_socket};
use crate::procinfo::proc_cwd;
use crate::TABS;
use crate::probe::{cached_json, py_json};
use crate::profile::read_profiles;
use crate::spool::ingestor_json;
use crate::terminal::{engine_hint, tab_live_process, use_tmux_backend};
use axum::response::{IntoResponse, Response};
use std::collections::HashMap;
use std::fs;
use std::sync::Arc;
use std::time::Duration;
pub(crate) fn flag_from_cmdline(cmdline: &str, flags: &[&str]) -> String {
    let parts: Vec<&str> = cmdline.split_whitespace().collect();
    let mut out = String::new();
    for pair in parts.windows(2) {
        if flags.contains(&pair[0]) {
            out = pair[1].to_string();
        }
    }
    out
}

/// Penunjuk log sesi Qoder paling baru untuk sebuah cwd: inilah alamat yang harus
/// dibaca sesi berikutnya bila konteks hilang.
pub(crate) fn qoder_session_pointer(cwd: &str) -> Value {
    let key = format!("pointer_{}", cwd);
    cached_json(&key, Duration::from_secs(10), || {
        let script = r#"
import glob, json, os, sys
enc = (sys.argv[1] or "").replace("/", "-").replace(".", "-")
base = os.path.expanduser("~/.ai-station/engines/qoder")
def newest(pattern):
    files = [p for p in glob.glob(pattern) if os.path.isfile(p)]
    return max(files, key=os.path.getmtime) if files else ""
seg = newest(os.path.join(base, "logs/sessions", enc, "*", "segments", "*.jsonl"))
tr  = newest(os.path.join(base, "projects", enc, "*.jsonl"))
sid = ""
if seg and "segments" in seg.split(os.sep):
    parts = seg.split(os.sep); sid = parts[parts.index("segments") - 1]
elif tr:
    sid = os.path.basename(tr).split(".")[0]
out = {"session_id": sid, "transcript": tr, "segment_log": seg, "workspace_key": enc}
for p in (seg, tr):
    if p and os.path.exists(p):
        out["last_updated"] = str(int(os.path.getmtime(p)))
        out["bytes"] = os.path.getsize(p)
        break
print(json.dumps(out))
"#;
        py_json(script, &[cwd]).unwrap_or_else(|| json!({"error": "pointer tidak terbaca"}))
    })
}

pub(crate) async fn get_workspace(State(state): State<AppState>) -> Response {
    let typed_snapshot: HashMap<String, String> = state
        .typed
        .lock()
        .map(|m| m.clone())
        .unwrap_or_default();
    let sessions = Arc::clone(&state.sessions);
    let payload = tokio::task::spawn_blocking(move || {
        let profiles = read_profiles();
        let mut tabs = serde_json::Map::new();
        for tab in TABS {
            let profile = profiles.get(tab).cloned().unwrap_or_default();
            let live = sessions
                .get(tab)
                .and_then(|s| tab_live_process(s.root_pid, engine_hint(&profile.engine)));
            let (alive, pid, comm, cwd, cmdline) = match &live {
                Some(l) => (true, l.pid, l.comm.clone(), l.cwd.clone(), l.cmdline.clone()),
                None => (
                    false,
                    0,
                    String::new(),
                    profile.workspace.clone(),
                    String::new(),
                ),
            };
            let declared = profile.workspace.clone();
            let backend = sessions.get(tab).map(|s| s.backend().to_string()).unwrap_or_default();
            let diverged = !declared.is_empty()
                && !cwd.is_empty()
                && !(cwd == declared || cwd.starts_with(&declared));
            let session = if profile.engine == "qoder" && !cwd.is_empty() {
                qoder_session_pointer(&cwd)
            } else {
                Value::Null
            };
            tabs.insert(
                tab.to_string(),
                json!({
                    "alive": alive,
                    "backend": backend,
                    "pid": pid,
                    "process": comm,
                    "cmdline": cmdline,
                    "declared_workspace": declared,
                    "live_cwd": cwd,
                    "diverged": diverged,
                    "profile_model": profile.model,
                    "profile_effort": profile.effort,
                    "model_in_command": flag_from_cmdline(&cmdline, &["-m", "--model"]),
                    "effort_in_command": flag_from_cmdline(&cmdline, &[
                        "--reasoning-effort",
                        "--effort",
                    ]),
                    "context_in_command": flag_from_cmdline(&cmdline, &["--context-window"]),
                    "session": session,
                }),
            );
        }
        Value::Object(tabs)
    })
    .await
    .unwrap_or(Value::Null);

    Json(json!({
        "tabs": payload,
        "pending_input": typed_snapshot,
        "daemon": {
            "pid": std::process::id(),
            "cwd": proc_cwd(std::process::id()).unwrap_or_default(),
            "port": station_port(),
            "tmux_persistent": use_tmux_backend(),
            "tmux_socket": tmux_socket(),
        },
        "spool": {
            "path": spool_path().display().to_string(),
            "bytes": fs::metadata(spool_path()).map(|m| m.len()).unwrap_or(0),
        },
    }))
    .into_response()
}

pub(crate) async fn query_history(mode: &str, q: LogQuery) -> Json<Value> {
    let mode = mode.to_string();
    let limit = q.limit.unwrap_or(40).clamp(1, 400);
    let engine = q.engine.unwrap_or_default();
    let kind = q.kind.unwrap_or_default();
    let result = tokio::task::spawn_blocking(move || ingestor_json(&mode, limit, &engine, &kind))
        .await
        .unwrap_or_else(|_| json!({ "error": "query history terhenti" }));
    Json(result)
}

pub(crate) async fn get_log(Query(q): Query<LogQuery>) -> Json<Value> {
    query_history("api", q).await
}

pub(crate) async fn get_recovery(Query(q): Query<LogQuery>) -> Json<Value> {
    query_history("api", q).await
}

/// /api/station — seluruh isi halaman Station dalam satu panggilan.
///
/// Angkanya tidak dihitung di sini dan tidak dihitung oleh AI mana pun: handler ini cuma
/// meneruskan permintaan ke ingestor, yang bertanya langsung ke view `station_posts` dan
/// `station_artifacts` di PostgreSQL.
pub(crate) async fn get_station(Query(q): Query<LogQuery>) -> Json<Value> {
    query_history("station", q).await
}
