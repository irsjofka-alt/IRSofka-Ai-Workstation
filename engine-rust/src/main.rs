//! Irsofka AI Workstation daemon — one process, both the native window and the API.
//!
//! Two launch modes share one state: `--headless` / `--daemon` / `--server` serves
//! only http://127.0.0.1:8999; otherwise tao + wry open `src/gui.html` and talk to
//! the same router. Terminal panes are tmux sessions on socket `-L irsofka`, hosted
//! by `irsofka-tabs.service` outside this process's cgroup, so restarting the daemon
//! never kills a running CLI.
//!
//! What is left here is deliberately small: startup, the window, and the router table.
//! Everything else has a home — `paths` (where things live), `profile` (how a tab spawns
//! its CLI), `terminal` (who owns the pane), `spool` (how events reach SQL), `probe` and
//! `engineinfo` (what the CLIs say about themselves), `save_state` (PostgreSQL/SQLite
//! counts), and `api/` (one file per HTTP domain). The full map is generated into
//! `brain/memory/projects/ARCHITECTURE.md` by `bin/arch_map.sh`.
use axum::{
    http::HeaderValue,
    routing::{get, post}, Router,
};

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
use api::AppState;
use profile::{default_profiles, ensure_config, TabProfile};
use terminal::create_session;
use paths::{
    station_port, FALLBACK_HOST,
};
use std::{
    collections::HashMap,
    sync::{
        atomic::AtomicU64,
        Arc, Mutex,
    },
    time::Duration,
};
use tower_http::cors::{AllowOrigin, CorsLayer};

pub(crate) const TABS: [&str; 3] = ["qoder", "antigravity", "shell"];

/// Penghitung permintaan muat-ulang UI; dibaca WebView lewat polling /api/stats.
pub(crate) static UI_RELOAD_TICK: AtomicU64 = AtomicU64::new(0);

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
        .route("/station", get(static_files::serve_station))
        .route("/api/station", get(api::workspace::get_station))
        .route("/api/treasury", get(api::workspace::get_treasury))
        .route("/assets/*path", get(static_files::serve_asset))
        .route("/api/stats", get(api::stats::get_stats))
        .route("/api/workspace", get(api::workspace::get_workspace))
        .route("/api/log", get(api::workspace::get_log))
        .route("/api/recovery", get(api::workspace::get_recovery))
        .route("/api/models", get(api::engine::get_models))
        .route("/api/master", get(api::master::get_master).post(api::master::post_master))
        .route("/api/usage", get(api::engine::get_usage))
        .route("/api/cli/config", get(api::engine::get_cli_config).post(api::engine::patch_cli_config))
        .route("/api/cli/restart", post(api::engine::restart_cli_tab))
        .route("/api/term/read", get(api::terminal::term_read))
        .route("/api/term/screen", get(api::terminal::term_screen))
        .route("/api/term/history", get(api::terminal::term_history))
        .route("/api/term/write", post(api::terminal::term_write))
        .route("/api/term/reset", post(api::terminal::term_reset))
        .route("/api/term/resize", post(api::terminal::term_resize))
        .route("/api/cli/run", post(api::terminal::cli_run))
        .route("/api/chat", post(api::terminal::cli_run))
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
