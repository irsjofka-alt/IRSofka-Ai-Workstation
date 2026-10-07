//! Irsofka AI Workstation daemon — one process, both the native window and the API.
//!
//! Two launch modes share one state: `--headless` / `--daemon` / `--server` serves
//! only http://127.0.0.1:8999; otherwise tao + wry open `src/gui.html` and talk to
//! the same router. Terminal panes are tmux sessions on socket `-L irsofka`, hosted
//! by `irsofka-tabs.service` outside this process's cgroup, so restarting the daemon
//! never kills a running CLI.
//!
//! This file still holds startup, config resolution, state types and the router.
//! Reading the process table already lives in `procinfo.rs`; the remaining split is
//! tracked in `brain/memory/projects/ARCHITECTURE.md`.
use axum::{
    extract::{Query, State},
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde_json::{json, Value};

mod api;
mod engineinfo;
mod paths;
mod probe;
mod profile;
mod procinfo;
mod save_state;
mod spool;
mod terminal;
use api::{daemon, desktop, static_files};
use api::{
    AppState, CliRunPayload, ConfigPatch, DiskInfo, GpuInfo, LogQuery, RamInfo, ReadQuery,
    ResizePayload, ResetPayload, TabStatus, TelemetryStats, WritePayload,
};
use procinfo::proc_cwd;
use profile::{default_profiles, ensure_config, read_profiles, TabProfile};
use save_state::{count_files, count_skill_files, load_save_state, pg_conn_str};
use terminal::{
    capture_screen, create_session, engine_hint, pane_context_pct, tab_live_process,
    use_tmux_backend, PtySession,
};
use spool::{ingestor_json, spool_event};
use engineinfo::{
    agy_efforts, agy_models, antigravity_last_model, antigravity_usage, cli_version, qoder_account,
    qoder_efforts, qoder_models, qoder_session_usage,
};
use probe::{cached_json, py_json, run_capture};
use paths::{
    home_dir, profiles_path, spool_path, station_dir, station_port,
    tmux_socket, FALLBACK_HOST,
};
use std::{
    collections::HashMap,
    fs,
    process::Command,
    sync::{
        atomic::AtomicU64,
        Arc, Mutex,
    },
    time::Duration,
};
use tower_http::cors::{AllowOrigin, CorsLayer};

const TABS: [&str; 3] = ["qoder", "antigravity", "shell"];

/// Penghitung permintaan muat-ulang UI; dibaca WebView lewat polling /api/stats.
pub(crate) static UI_RELOAD_TICK: AtomicU64 = AtomicU64::new(0);


// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Pane: tipe state dan handler layar (sisanya hidup di terminal.rs)
// ---------------------------------------------------------------------------

async fn term_screen(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    let tab = query.tab.unwrap_or_else(|| "qoder".to_string());
    if let Some(screen) = capture_screen(&tab) {
        return stream_body(screen.into_bytes());
    }
    // Fallback untuk tab PTY langsung (tanpa tmux): pakai buffer riwayat.
    stream_body(poll_buffer(&state, &Some(tab)))
}

// HTTP payloads
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let is_headless = args
        .iter()
        .any(|a| a == "--headless" || a == "--daemon" || a == "--server");

    let profiles = ensure_config();

    if is_headless {
        println!("🦀 Irsofka AI Workstation — headless daemon");
        let rt = tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
            .expect("Failed to initialize tokio runtime");
        rt.block_on(run_server(profiles));
    } else {
        println!("🦀 Irsofka AI Workstation — native desktop GUI");
        let port = station_port();
        let server_up = std::net::TcpStream::connect(format!("127.0.0.1:{}", port)).is_ok();
        if !server_up {
            println!("🚀 Engine tidak aktif di port {}, start embedded...", port);
            std::thread::spawn(move || {
                let rt = tokio::runtime::Builder::new_multi_thread()
                    .enable_all()
                    .build()
                    .expect("Failed to initialize embedded tokio runtime");
                rt.block_on(run_server(profiles));
            });
            for _ in 0..50 {
                if std::net::TcpStream::connect(format!("127.0.0.1:{}", port)).is_ok() {
                    break;
                }
                std::thread::sleep(Duration::from_millis(100));
            }
        } else {
            println!("⚡ Terhubung ke daemon aktif di 127.0.0.1:{}", port);
        }
        launch_native_window();
    }
}

fn launch_native_window() {
    use tao::{
        event::{Event, WindowEvent},
        event_loop::{ControlFlow, EventLoop},
        platform::unix::WindowExtUnix,
        window::WindowBuilder,
    };
    use wry::{WebViewBuilder, WebViewBuilderExtUnix};

    let port = station_port();
    println!("🖼️ Membuka jendela native (WebKitGTK + Tao)...");
    let event_loop = EventLoop::new();
    let window = WindowBuilder::new()
        .with_title("Irsofka AI Workstation - Native Studio")
        .with_inner_size(tao::dpi::LogicalSize::new(1440.0, 900.0))
        .with_min_inner_size(tao::dpi::LogicalSize::new(900.0, 620.0))
        .build(&event_loop)
        .expect("Failed to create native desktop window");

    let vbox = window.default_vbox().expect("Failed to get window default vbox");
    let _webview = WebViewBuilder::new()
        .with_url(format!("http://127.0.0.1:{}", port))
        .with_devtools(true)
        .build_gtk(vbox)
        .expect("Failed to initialize WebKitGTK webview inside native window");

    println!("✨ Irsofka AI Workstation Native Rust Window is Running!");

    event_loop.run(move |event, _, control_flow| {
        *control_flow = ControlFlow::Wait;
        match event {
            Event::WindowEvent {
                event: WindowEvent::CloseRequested,
                ..
            } => {
                println!("🛑 Jendela ditutup.");
                *control_flow = ControlFlow::Exit;
            }
            _ => (),
        }
    });
}

async fn run_server(profiles: HashMap<String, TabProfile>) {
    println!("🦀 Inisialisasi PTY sessions & Axum API...");

    let mut map = HashMap::new();
    let fallbacks = default_profiles();
    for tab in TABS {
        let profile = profiles
            .get(tab)
            .cloned()
            .or_else(|| fallbacks.get(tab).cloned())
            .unwrap_or_default();
        map.insert(tab.to_string(), create_session(tab, &profile));
    }
    println!(
        "🔌 Session aktif: {}",
        map.iter()
            .map(|(k, v)| format!("{}=pid{}", k, v.root_pid))
            .collect::<Vec<_>>()
            .join(" ")
    );

    let state = AppState {
        sessions: Arc::new(map),
        typed: Arc::new(Mutex::new(HashMap::new())),
    };

    let port = station_port();
    let allowed: Vec<HeaderValue> = vec![
        format!("http://127.0.0.1:{}", port),
        format!("http://localhost:{}", port),
    ]
    .into_iter()
    .filter_map(|s| s.parse().ok())
    .collect();

    let app = Router::new()
        .route("/", get(static_files::serve_gui))
        .route("/assets/*path", get(static_files::serve_asset))
        .route("/api/stats", get(get_stats))
        .route("/api/workspace", get(get_workspace))
        .route("/api/log", get(get_log))
        .route("/api/recovery", get(get_recovery))
        .route("/api/models", get(get_models))
        .route("/api/usage", get(get_usage))
        .route("/api/cli/config", get(get_cli_config).post(patch_cli_config))
        .route("/api/cli/restart", post(restart_cli_tab))
        .route("/api/term/read", get(term_read))
        .route("/api/term/screen", get(term_screen))
        .route("/api/term/history", get(term_history))
        .route("/api/term/write", post(term_write))
        .route("/api/term/reset", post(term_reset))
        .route("/api/term/resize", post(term_resize))
        .route("/api/cli/run", post(cli_run))
        .route("/api/chat", post(cli_run))
        .route("/api/see", post(desktop::handle_see))
        .route("/api/action", post(desktop::handle_action))
        .route("/api/screenshot/latest", get(desktop::serve_screenshot))
        .route("/api/restart-server", post(daemon::restart_server))
        .route("/api/ui/reload", post(daemon::ui_reload))
        .layer(
            CorsLayer::new()
                .allow_origin(AllowOrigin::list(allowed))
                .allow_methods(tower_http::cors::Any)
                .allow_headers(tower_http::cors::Any),
        )
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(format!("{}:{}", FALLBACK_HOST, port))
        .await
        .expect("Failed to bind TCP listener");

    println!("⚡ Irsofka AI Workstation berjalan di http://127.0.0.1:{}", port);
    axum::serve(listener, app).await.expect("Failed to run Axum server");
}

// Terminal handlers
// ---------------------------------------------------------------------------

async fn term_read(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    stream_body(poll_buffer(&state, &query.tab))
}

fn poll_buffer(state: &AppState, tab: &Option<String>) -> Vec<u8> {
    let tab = tab.clone().unwrap_or_else(|| "qoder".to_string());
    state
        .sessions
        .get(&tab)
        .map(|session| session.read_new())
        .unwrap_or_default()
}

fn stream_body(output: Vec<u8>) -> Response {
    Response::builder()
        .header(header::CONTENT_TYPE, "text/plain; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-store")
        .body(axum::body::Body::from(output))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

async fn term_history(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    let tab = query.tab.unwrap_or_else(|| "qoder".to_string());
    let output = state
        .sessions
        .get(&tab)
        .map(|session| session.read_history())
        .unwrap_or_default();
    stream_body(output)
}

fn live_tab_cwd(sessions: &HashMap<String, PtySession>, tab: &str) -> Option<String> {
    let mut profiles = read_profiles();
    let profile = profiles.remove(tab)?;
    sessions
        .get(tab)
        .and_then(|s| tab_live_process(s.root_pid, engine_hint(&profile.engine)))
        .map(|l| l.cwd)
}

async fn term_write(
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

async fn term_reset(
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

async fn term_resize(
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

async fn cli_run(
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

// ---------------------------------------------------------------------------
// CLI config / models / usage
// ---------------------------------------------------------------------------

fn tab_status_map(sessions: &HashMap<String, PtySession>) -> HashMap<String, TabStatus> {
    let profiles = read_profiles();
    let mut out = HashMap::new();
    for tab in TABS {
        let profile = profiles.get(tab).cloned().unwrap_or_default();
        let root_pid = sessions.get(tab).map(|s| s.root_pid).unwrap_or(0);
        let live = sessions
            .get(tab)
            .and_then(|s| tab_live_process(s.root_pid, engine_hint(&profile.engine)));
        out.insert(
            tab.to_string(),
            match live {
                Some(live) => TabStatus {
                    alive: true,
                    pid: live.pid,
                    process: live.comm,
                    cwd: live.cwd,
                    cmdline: live.cmdline,
                    root_pid,
                    profile,
                },
                None => TabStatus {
                    alive: false,
                    pid: 0,
                    process: String::new(),
                    cwd: profile.workspace.clone(),
                    cmdline: String::new(),
                    root_pid,
                    profile,
                },
            },
        );
    }
    out
}

// ---------------------------------------------------------------------------
// Workspace nyata, log aksi, dan pemulihan sesi
// ---------------------------------------------------------------------------

fn flag_from_cmdline(cmdline: &str, flags: &[&str]) -> String {
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
fn qoder_session_pointer(cwd: &str) -> Value {
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

async fn get_workspace(State(state): State<AppState>) -> Response {
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

async fn query_history(mode: &str, q: LogQuery) -> Json<Value> {
    let mode = mode.to_string();
    let limit = q.limit.unwrap_or(40).clamp(1, 400);
    let engine = q.engine.unwrap_or_default();
    let kind = q.kind.unwrap_or_default();
    let result = tokio::task::spawn_blocking(move || ingestor_json(&mode, limit, &engine, &kind))
        .await
        .unwrap_or_else(|_| json!({ "error": "query history terhenti" }));
    Json(result)
}

async fn get_log(Query(q): Query<LogQuery>) -> Json<Value> {
    query_history("api", q).await
}

async fn get_recovery(Query(q): Query<LogQuery>) -> Json<Value> {
    query_history("api", q).await
}

async fn get_models() -> Json<Value> {
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

async fn get_usage() -> Json<Value> {
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

async fn get_cli_config() -> Json<Value> {
    Json(json!({ "profiles": read_profiles() }))
}

async fn patch_cli_config(
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

async fn restart_cli_tab(State(state): State<AppState>, Json(body): Json<Value>) -> Json<Value> {
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
    let cwd = live_tab_cwd(&state.sessions, &tab);
    spool_event(
        "tab_restart",
        &tab,
        format!("CLI tab dihentikan agar spawn ulang dengan profil terkini; killed={}", killed),
        cwd,
        Some("restart"),
    );
    Json(json!({ "status": "ok", "tab": tab, "killed": killed }))
}

fn kill_tab_cli_root(root: u32, hint: Option<&str>) -> bool {
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

// ---------------------------------------------------------------------------
// Stats
// ---------------------------------------------------------------------------

async fn get_stats(State(state): State<AppState>) -> Json<TelemetryStats> {
    let agy_cli = cli_version("agy");
    let qoder_cli = cli_version("qoder");
    let account = qoder_account();

    let mut gpu = GpuInfo {
        name: "NVIDIA RTX 3060".to_string(),
        used_mb: 0,
        total_mb: 12288,
        load: 0,
        temp_c: 0,
    };
    if let Some(out) = run_capture(
        "nvidia-smi",
        &[
            "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits",
        ],
    ) {
        let parts: Vec<&str> = out.split(',').map(|s| s.trim()).collect();
        if parts.len() >= 5 {
            gpu.name = parts[0].to_string();
            gpu.used_mb = parts[1].parse().unwrap_or(0);
            gpu.total_mb = parts[2].parse().unwrap_or(12288);
            gpu.load = parts[3].parse().unwrap_or(0);
            gpu.temp_c = parts[4].parse().unwrap_or(0);
        }
    }

    let mut ram = RamInfo {
        used_gb: 0.0,
        total_gb: 0.0,
    };
    if let Ok(meminfo) = fs::read_to_string("/proc/meminfo") {
        let mut total_kb = 0u64;
        let mut avail_kb = 0u64;
        for line in meminfo.lines() {
            if line.starts_with("MemTotal:") {
                total_kb = line.split_whitespace().nth(1).and_then(|s| s.parse().ok()).unwrap_or(0);
            } else if line.starts_with("MemAvailable:") {
                avail_kb = line
                    .split_whitespace()
                    .nth(1)
                    .and_then(|s| s.parse().ok())
                    .unwrap_or(0);
            }
        }
        if total_kb > 0 {
            ram.total_gb = (total_kb as f32 / (1024.0 * 1024.0) * 10.0).round() / 10.0;
            ram.used_gb = ((total_kb - avail_kb) as f32 / (1024.0 * 1024.0) * 10.0).round() / 10.0;
        }
    }

    let mut disk = DiskInfo {
        free: "N/A".to_string(),
        total: "N/A".to_string(),
    };
    if let Some(out) = run_capture("df", &["-h", &home_dir().display().to_string()]) {
        if let Some(line) = out.lines().nth(1) {
            let p: Vec<&str> = line.split_whitespace().collect();
            if p.len() >= 4 {
                disk.total = p[1].to_string();
                disk.free = p[3].to_string();
            }
        }
    }

    let save = load_save_state().await;
    let rules_dir = station_dir().join("brain").join("rules");
    let tabs = tab_status_map(&state.sessions);

    Json(TelemetryStats {
        user_name: account
            .get("username")
            .and_then(Value::as_str)
            .unwrap_or("Owner")
            .to_string(),
        antigravity_cli: agy_cli,
        qoder_cli: qoder_cli,
        qoder_account: account,
        gpu,
        ram,
        disk,
        skills_count: count_skill_files().max(save.skills),
        rules_count: count_files(&rules_dir, "md"),
        memories_count: save.memories,
        incidents_count: save.incidents,
        total_quests: save.quests,
        total_turns: save.turns,
        player: save.player,
        db_engine: save.engine,
        ui_reload_tick: daemon::reload_tick(),
        tabs,
    })
}
