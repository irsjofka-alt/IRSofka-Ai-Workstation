use axum::{
    extract::{Path, Query, State},
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use chrono::Local;
use portable_pty::{CommandBuilder, MasterPty, NativePtySystem, PtySize, PtySystem};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::HashMap,
    fs,
    io::{Read, Seek, SeekFrom, Write},
    path::PathBuf,
    process::Command,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, OnceLock,
    },
    time::{Duration, Instant},
};
use tower_http::cors::{AllowOrigin, CorsLayer};

const FALLBACK_HOST: &str = "127.0.0.1";
const DEFAULT_PORT: u16 = 8999;
const TABS: [&str; 3] = ["qoder", "antigravity", "shell"];

/// Penghitung permintaan muat-ulang UI; dibaca WebView lewat polling /api/stats.
static UI_RELOAD_TICK: AtomicU64 = AtomicU64::new(0);

/// Rangkaian koneksi PostgreSQL: bawaan -> config/db_local.json -> variabel lingkungan.
///
/// Password dulu tertanam sebagai konstan waktu-kompilasi di berkas ini. Karena repo
/// workstation kini public, kredensial dipindah ke config/db_local.json yang di-ignore git;
/// hasil clone bersih gagal terhubung secara terang-terangan, bukan ikut membawa password.
fn pg_conn_str() -> String {
    if let Ok(explicit) = std::env::var("STATION_PG_CONN") {
        if !explicit.is_empty() {
            return explicit;
        }
    }
    let local: Value = fs::read_to_string(config_dir().join("db_local.json"))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_else(|| json!({}));
    let pick = |key: &str, env: &str, default: &str| -> String {
        if let Ok(value) = std::env::var(env) {
            if !value.is_empty() {
                return value;
            }
        }
        match local.get(key) {
            Some(v) if v.is_string() => v.as_str().unwrap_or(default).to_string(),
            Some(v) if v.is_number() => v.to_string(),
            _ => default.to_string(),
        }
    };
    format!(
        "host={} port={} user={} password={} dbname={}",
        pick("host", "STATION_PG_HOST", "localhost"),
        pick("port", "STATION_PG_PORT", "5432"),
        pick("user", "STATION_PG_USER", "irsofka"),
        pick("password", "STATION_PG_PASSWORD", ""),
        pick("dbname", "STATION_PG_DB", "irsofka_ai_workstation"),
    )
}

fn station_port() -> u16 {
    std::env::var("STATION_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_PORT)
}

fn home_dir() -> PathBuf {
    // $HOME praktis selalu terisi (systemd user maupun shell mengisinya). Fallback-nya
    // tidak boleh berupa nama orang tertentu — hasil clone tidak boleh membawa home
    // directory milik mesin saya. Urutannya: $HOME, lalu /home/$USER, lalu direktori kerja.
    if let Ok(h) = std::env::var("HOME") {
        if !h.is_empty() {
            return PathBuf::from(h);
        }
    }
    if let Ok(u) = std::env::var("USER") {
        if !u.is_empty() {
            return PathBuf::from(format!("/home/{u}"));
        }
    }
    std::env::current_dir().unwrap_or_else(|_| PathBuf::from("/"))
}

fn station_dir() -> PathBuf {
    home_dir().join(".ai-station")
}

fn config_dir() -> PathBuf {
    station_dir().join("config")
}

fn profiles_path() -> PathBuf {
    config_dir().join("cli_profiles.json")
}

fn runner_path() -> PathBuf {
    config_dir().join("run_tab.sh")
}

fn assets_dir() -> PathBuf {
    station_dir().join("engine-rust").join("assets")
}

fn gui_path() -> PathBuf {
    station_dir().join("engine-rust").join("src").join("gui.html")
}

fn spool_path() -> PathBuf {
    station_dir().join("logs").join("station_events.jsonl")
}

fn ingestor_path() -> PathBuf {
    station_dir().join("tools").join("session_ingestor.py")
}

// ---------------------------------------------------------------------------
// Spool kejadian: daemon menulis baris JSON, service irsofka-action-log.service
// yang memipanya ke PostgreSQL/SQLite. Daemon tidak perlu memegang dialek SQL,
// dan log tetap tercatat walaupun database sedang mati.
// ---------------------------------------------------------------------------

static SPOOL_SEQ: AtomicU64 = AtomicU64::new(1);

fn spool_event(kind: &str, tab: &str, summary: String, cwd: Option<String>, tool: Option<&str>) {
    let record = json!({
        "seq": SPOOL_SEQ.fetch_add(1, Ordering::Relaxed),
        "ts": Local::now().to_rfc3339(),
        "engine": "station",
        "tab": tab,
        "kind": kind,
        "summary": summary.chars().take(400).collect::<String>(),
        "cwd": cwd,
        "tool": tool,
        "pid": std::process::id(),
    });
    if let Ok(mut line) = serde_json::to_string(&record) {
        line.push('\n');
        if let Some(parent) = spool_path().parent() {
            let _ = fs::create_dir_all(parent);
        }
        match fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(spool_path())
        {
            Ok(mut fh) => {
                let _ = fh.write_all(line.as_bytes());
            }
            Err(_) => {}
        }
    }
}

/// Jalan satu-satunya menuju SQL: panggil lapisan dialek milik ingestor.
fn ingestor_json(mode: &str, limit: u32, engine: &str, kind: &str) -> Value {
    let script = ingestor_path().display().to_string();
    let mut cmd = Command::new("python3");
    cmd.arg(&script).arg(mode).arg(limit.to_string());
    if !engine.is_empty() {
        cmd.arg("--engine").arg(engine);
    }
    if !kind.is_empty() {
        cmd.arg("--kind").arg(kind);
    }
    let out = cmd.output().ok();
    out.map(|o| {
        String::from_utf8_lossy(&o.stdout)
            .find('{')
            .and_then(|i| serde_json::from_str::<Value>(&String::from_utf8_lossy(&o.stdout)[i..]).ok())
            .unwrap_or_else(|| json!({"error": "ingestor tidak mengembalikan JSON valid"}))
    })
    .unwrap_or_else(|| json!({"error": format!("gagal menjalankan {}", script)}))
}

// ---------------------------------------------------------------------------
// CLI profiles: single source of truth for how every tab spawns its CLI.
// ---------------------------------------------------------------------------

#[derive(Clone, Serialize, Deserialize, Default)]
pub struct TabProfile {
    #[serde(default)]
    engine: String,
    #[serde(default)]
    model: String,
    #[serde(default)]
    effort: String,
    #[serde(default)]
    context_window: String,
    #[serde(default)]
    permission_mode: String,
    #[serde(default)]
    workspace: String,
    #[serde(default)]
    continue_session: bool,
    #[serde(default)]
    extra_args: Vec<String>,
}

fn default_profiles() -> HashMap<String, TabProfile> {
    let home = home_dir().display().to_string();
    // Profil ini hanya FALLBACK. Kalau config/cli_profiles.json terhapus, tab tidak boleh
    // jatuh ke $HOME atau folder proyek acak — keduanya memutus kontrak bahwa semua AI
    // membaca dokumen dan workspace yang sama.
    let workspace = format!("{}/Documents/ai-workstation", home);
    let mut m = HashMap::new();
    m.insert(
        "qoder".to_string(),
        TabProfile {
            engine: "qoder".to_string(),
            model: "Qwen3.8-Flash".to_string(),
            effort: "xhigh".to_string(),
            context_window: "1000000".to_string(),
            permission_mode: "bypass_permissions".to_string(),
            workspace: workspace.clone(),
            continue_session: true,
            extra_args: vec![],
        },
    );
    m.insert(
        "antigravity".to_string(),
        TabProfile {
            engine: "agy".to_string(),
            model: "gemini-3.8-flash-high".to_string(),
            effort: String::new(),
            context_window: String::new(),
            permission_mode: "skip".to_string(),
            workspace: workspace,
            continue_session: true,
            extra_args: vec![],
        },
    );
    m.insert(
        "shell".to_string(),
        TabProfile {
            engine: "shell".to_string(),
            workspace: home,
            ..Default::default()
        },
    );
    m
}

const RUNNER_SCRIPT: &str = r#"#!/usr/bin/env bash
TAB="${1:-qoder}"
PROF="$HOME/.ai-station/config/cli_profiles.json"

SPEC="$(python3 - "$PROF" "$TAB" <<'PY'
import json, shlex, sys

path, tab = sys.argv[1], sys.argv[2]
try:
    p = json.load(open(path)).get(tab, {})
except Exception as exc:
    sys.stderr.write("run_tab: profil tidak terbaca: %s\n" % exc)
    raise SystemExit(1)

engine = p.get("engine") or "shell"
perm = p.get("permission_mode") or ""
cmd = []

if engine == "qoder":
    cmd = ["qoder"]
    if p.get("model"):
        cmd += ["-m", str(p["model"])]
    if p.get("effort"):
        cmd += ["--reasoning-effort", str(p["effort"])]
    if p.get("context_window"):
        cmd += ["--context-window", str(p["context_window"])]
    cmd += ["--permission-mode", perm or "default"]
    if p.get("continue_session"):
        cmd.append("--continue")
elif engine == "agy":
    cmd = ["agy"]
    if p.get("model"):
        cmd += ["--model", str(p["model"])]
    if p.get("effort"):
        cmd += ["--effort", str(p["effort"])]
    if perm == "skip":
        cmd.append("--dangerously-skip-permissions")
    elif perm in ("accept-edits", "plan"):
        cmd += ["--mode", perm]
    if p.get("continue_session"):
        cmd.append("--continue")
elif engine == "local":
    cmd = ["ollama", "run", p.get("model") or "qwen2.5-coder:7b"]
else:
    cmd = ["bash", "-i"]

for a in p.get("extra_args") or []:
    cmd.append(str(a))

print("WORKSPACE=" + shlex.quote(p.get("workspace") or ""))
print("CMDLINE=(" + " ".join(shlex.quote(c) for c in cmd) + ")")
PY
)" || { echo "run_tab: gagal membaca profil $TAB"; exit 1; }

# CMDLINE adalah array bash: tiap argumen utuh, termasuk yang berisi spasi.
# Bentuk lama (`CMDLINE=bash -i`) membuat eval menetapkan CMDLINE=bash lalu
# mencoba menjalankan "-i" sebagai perintah, sehingga SEMUA flag model/effort/
# context/permission hilang dan CLI spawn tanpa argumen.
eval "$SPEC"
if [ -n "${WORKSPACE:-}" ] && [ -d "$WORKSPACE" ]; then
    cd "$WORKSPACE" || cd "$HOME"
fi
if [ "${#CMDLINE[@]}" -eq 0 ]; then echo "run_tab: perintah kosong untuk $TAB"; exit 1; fi
exec "${CMDLINE[@]}"
"#;

fn ensure_config() -> HashMap<String, TabProfile> {
    let dir = config_dir();
    let _ = fs::create_dir_all(&dir);
    let profiles = match fs::read_to_string(profiles_path()) {
        Ok(raw) => serde_json::from_str::<HashMap<String, TabProfile>>(&raw)
            .unwrap_or_else(|_| default_profiles()),
        Err(_) => default_profiles(),
    };
    let merged = {
        let mut base = default_profiles();
        for (k, v) in profiles {
            base.insert(k, v);
        }
        base
    };
    if let Ok(raw) = serde_json::to_string_pretty(&merged) {
        let _ = fs::write(profiles_path(), raw);
    }
    let _ = fs::write(runner_path(), RUNNER_SCRIPT);
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = fs::set_permissions(runner_path(), fs::Permissions::from_mode(0o755));
    }
    merged
}

fn read_profiles() -> HashMap<String, TabProfile> {
    fs::read_to_string(profiles_path())
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_else(default_profiles)
}

// ---------------------------------------------------------------------------
// PTY sessions
// ---------------------------------------------------------------------------

pub enum SessionKind {
    /// PTY dimiliki langsung oleh daemon. Simpel, tapi prosesnya berada di cgroup
    /// service daemon sehingga `systemctl restart` ikut membunuh CLI-nya.
    Direct {
        master: Arc<Mutex<Box<dyn MasterPty + Send>>>,
        writer: Arc<Mutex<Box<dyn Write + Send>>>,
        buffer: Arc<Mutex<Vec<u8>>>,
        history: Arc<Mutex<Vec<u8>>>,
    },
    /// Pane tmux pada server `irsofka-tabs.service` (cgroup berbeda). Daemon hanya
    /// membaca hasil `pipe-pane` dan mengirim byte mentah, jadi restart daemon,
    /// crash daemon, maupun reload UI tidak menghentikan CLI di dalamnya.
    Tmux {
        session: String,
        log: PathBuf,
        offset: Arc<AtomicU64>,
    },
}

pub struct PtySession {
    kind: SessionKind,
    root_pid: u32,
}

const TMUX_READ_CAP: usize = 512 * 1024;
const TMUX_KEY_CHUNK: usize = 96;

impl PtySession {
    fn read_new(&self) -> Vec<u8> {
        match &self.kind {
            SessionKind::Direct { buffer, .. } => buffer
                .lock()
                .map(|mut b| std::mem::take(&mut *b))
                .unwrap_or_default(),
            SessionKind::Tmux { log, offset, .. } => {
                let mut fh = match fs::File::open(log) {
                    Ok(fh) => fh,
                    Err(_) => return Vec::new(),
                };
                let len = fh.metadata().map(|m| m.len() as usize).unwrap_or(0);
                let mut start = offset.load(Ordering::Relaxed) as usize;
                if start > len {
                    start = 0; // file dirotasi atau dipotong
                }
                if fh.seek(SeekFrom::Start(start as u64)).is_err() {
                    return Vec::new();
                }
                let mut out = Vec::new();
                let _ = fh.take(TMUX_READ_CAP as u64).read_to_end(&mut out);
                offset.store((start + out.len()) as u64, Ordering::Relaxed);
                out
            }
        }
    }

    fn read_history(&self) -> Vec<u8> {
        match &self.kind {
            SessionKind::Direct { history, .. } => history.lock().map(|h| h.clone()).unwrap_or_default(),
            SessionKind::Tmux { log, session, .. } => {
                let bytes = fs::read(log).unwrap_or_default();
                if !bytes.is_empty() {
                    let skip = bytes.len().saturating_sub(TMUX_READ_CAP);
                    return bytes[skip..].to_vec();
                }
                // Tanpa file pipa (sesi diadopsi dari tmux luar): lukis ulang layar saat ini.
                run_tmux(&[
                    "capture-pane", "-p", "-e", "-S", "-2000", "-t", session,
                ])
                .map(|s| s.into_bytes())
                .unwrap_or_default()
            }
        }
    }

    fn send_bytes(&self, data: &[u8]) {
        match &self.kind {
            SessionKind::Direct { writer, .. } => {
                if let Ok(mut w) = writer.lock() {
                    let _ = w.write_all(data);
                }
            }
            SessionKind::Tmux { session, .. } => {
                // Byte DITERUSKAN APA ADANYA lewat send-keys -H: panah, Esc, Ctrl-C,
                // dan sequence lain tidak diterjemahkan ulang oleh tmux.
                let mut i = 0usize;
                while i < data.len() {
                    let end = (i + TMUX_KEY_CHUNK).min(data.len());
                    let hex: Vec<String> = data[i..end]
                        .iter()
                        .map(|b| format!("0x{:02x}", b))
                        .collect();
                    let mut args = vec!["send-keys", "-t", session.as_str(), "-H"];
                    for token in &hex {
                        args.push(token.as_str());
                    }
                    run_tmux(&args);
                    i = end;
                }
            }
        }
    }

    fn resize(&self, cols: u16, rows: u16) {
        match &self.kind {
            SessionKind::Direct { master, .. } => {
                if let Ok(m) = master.lock() {
                    let _ = m.resize(PtySize {
                        rows,
                        cols,
                        pixel_width: 0,
                        pixel_height: 0,
                    });
                }
            }
            SessionKind::Tmux { session, .. } => {
                let c = cols.to_string();
                let r = rows.to_string();
                run_tmux(&["resize-window", "-t", session, "-x", &c, "-y", &r]);
            }
        }
    }

    fn interrupt(&self) {
        match &self.kind {
            SessionKind::Direct { writer, .. } => {
                if let Ok(mut w) = writer.lock() {
                    let _ = w.write_all(b"\x03\n");
                }
            }
            SessionKind::Tmux { .. } => self.send_bytes(&[0x03]),
        }
    }

    fn backend(&self) -> &'static str {
        match &self.kind {
            SessionKind::Direct { .. } => "pty",
            SessionKind::Tmux { .. } => "tmux",
        }
    }
}

#[derive(Clone)]
pub struct AppState {
    sessions: Arc<HashMap<String, PtySession>>,
    /// Penyangga ketikan per tab, supaya satu baris perintah utuh (bukan per-tombol)
    /// yang masuk ke action_log.
    typed: Arc<Mutex<HashMap<String, String>>>,
}

fn pty_loop_command(tab: &str, profile: &TabProfile) -> String {
    let runner = runner_path().display().to_string();
    if profile.engine == "shell" {
        format!("exec {}", shell_words(&[runner.as_str(), tab]))
    } else {
        format!(
            "while true; do {} {}; echo -e '\\n\\033[1;33m[Sesi {} selesai. Memulai ulang dengan profil terkini...]\\033[0m'; sleep 1; done",
            runner, tab, tab
        )
    }
}

fn create_session(tab: &str, profile: &TabProfile) -> PtySession {
    if use_tmux_backend() {
        if let Some(session) = create_tmux_session(tab, profile) {
            return session;
        }
        eprintln!("⚠ tmux tidak siap — tab {} fallback ke PTY milik daemon", tab);
    }
    create_direct_session(tab, profile)
}

fn create_direct_session(tab: &str, profile: &TabProfile) -> PtySession {
    let pty_system = NativePtySystem::default();
    let pair = pty_system
        .openpty(PtySize {
            rows: 32,
            cols: 120,
            pixel_width: 0,
            pixel_height: 0,
        })
        .expect("Failed to open PTY");

    let shell_cmd = pty_loop_command(tab, profile);

    let mut cmd = CommandBuilder::new("bash");
    cmd.arg("-c");
    cmd.arg(&shell_cmd);
    cmd.env("TERM", "xterm-256color");
    cmd.env("COLORTERM", "truecolor");
    let home = home_dir().display().to_string();
    cmd.env("HOME", &home);
    cmd.env(
        "PATH",
        format!(
            "{}/.cargo/bin:{}/.gemini/antigravity/bin:{}/.local/bin:{}/.ai-station/bin:/usr/local/bin:/usr/bin:/bin",
            home, home, home, home
        ),
    );

    let child = pair
        .slave
        .spawn_command(cmd)
        .expect("Failed to spawn process in PTY");
    let root_pid = child.process_id().unwrap_or(0);
    drop(child);

    let writer = pair.master.take_writer().expect("Failed to take PTY writer");
    let mut reader = pair.master.try_clone_reader().expect("Failed to clone PTY reader");

    let buffer = Arc::new(Mutex::new(Vec::new()));
    let buffer_clone = Arc::clone(&buffer);
    let history = Arc::new(Mutex::new(Vec::new()));
    let history_clone = Arc::clone(&history);

    std::thread::spawn(move || {
        let mut temp_buf = [0u8; 8192];
        loop {
            match reader.read(&mut temp_buf) {
                Ok(0) => break,
                Ok(n) => {
                    if let Ok(mut b) = buffer_clone.lock() {
                        b.extend_from_slice(&temp_buf[..n]);
                    }
                    if let Ok(mut h) = history_clone.lock() {
                        h.extend_from_slice(&temp_buf[..n]);
                        if h.len() > 512_000 {
                            let excess = h.len() - 512_000;
                            h.drain(0..excess);
                        }
                    }
                }
                Err(_) => break,
            }
        }
    });

    spool_event(
        "pty_spawn",
        tab,
        format!(
            "root_pid={} engine={} cmdline={}",
            root_pid,
            profile.engine,
            shell_cmd.split_whitespace().take(6).collect::<Vec<_>>().join(" ")
        ),
        proc_cwd(root_pid),
        Some("pty"),
    );

    PtySession {
        kind: SessionKind::Direct {
            master: Arc::new(Mutex::new(pair.master)),
            writer: Arc::new(Mutex::new(writer)),
            buffer,
            history,
        },
        root_pid,
    }
}

// ---------------------------------------------------------------------------
// tmux backend: CLI berumur panjang, daemon hanya penayang
// ---------------------------------------------------------------------------

fn tmux_socket() -> String {
    std::env::var("STATION_TMUX_SOCKET").unwrap_or_else(|_| "irsofka".to_string())
}

fn tabs_log_dir() -> PathBuf {
    station_dir().join("logs").join("tabs")
}

fn tmux_session_name(tab: &str) -> String {
    format!("station-{}", tab)
}

/// Ambil TAMPILAN pane saat ini, lengkap dengan sekuens escape-nya.
///
/// Ini yang dibutuhkan xterm.js untuk menggambar ulang sebuah TUI dengan benar. Memutar
/// ulang byte historis tidak memadai: TUI menggambar dengan gerak kursor absolut
/// (ESC[2A dan sejenisnya) yang mengasumsikan lebar tertentu, jadi replay di lebar yang
/// berbeda menghasilkan teks saling menimpa. capture-pane memberi hasil render final.
fn capture_screen(tab: &str) -> Option<String> {
    run_tmux(&[
        "capture-pane", "-e", "-p", "-S", "-2000", "-t", tmux_session_name(tab).as_str(),
    ])
}

async fn term_screen(Query(query): Query<ReadQuery>, State(state): State<AppState>) -> Response {
    let tab = query.tab.unwrap_or_else(|| "qoder".to_string());
    if let Some(screen) = capture_screen(&tab) {
        return stream_body(screen.into_bytes());
    }
    // Fallback untuk tab PTY langsung (tanpa tmux): pakai buffer riwayat.
    stream_body(poll_buffer(&state, &Some(tab)))
}

fn run_tmux(args: &[&str]) -> Option<String> {
    let socket = tmux_socket();
    let mut full: Vec<&str> = vec!["-L", socket.as_str(), "-f", "/dev/null"];
    full.extend_from_slice(args);
    let out = Command::new("tmux").args(&full).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).to_string())
}

/// Server tmux HARUS dinyalakan oleh `irsofka-tabs.service`, tidak pernah oleh daemon:
/// anak yang di-fork daemon akan masuk ke cgroup service daemon dan ikut mati saat
/// restart — justru masalah yang ingin kita hilangkan.
fn use_tmux_backend() -> bool {
    match std::env::var("STATION_BACKEND")
        .unwrap_or_else(|_| "auto".to_string())
        .to_lowercase()
        .as_str()
    {
        "tmux" => true,
        "pty" => false,
        _ => cached_json("tmux_ready", Duration::from_secs(15), || {
            json!(run_tmux(&["list-sessions"]).is_some())
        })
        .as_bool()
        .unwrap_or(false),
    }
}

fn create_tmux_session(tab: &str, profile: &TabProfile) -> Option<PtySession> {
    let name = tmux_session_name(tab);
    let existed = run_tmux(&["has-session", "-t", &name]).is_some();
    let loop_cmd = pty_loop_command(tab, profile);

    if !existed {
        run_tmux(&[
            "new-session", "-d", "-s", &name, "-x", "120", "-y", "32", loop_cmd.as_str(),
        ])?;
    }

    let pane_pid = run_tmux(&["display-message", "-p", "-t", &name, "#{pane_pid}"])
        .and_then(|s| s.trim().parse::<u32>().ok())
        .unwrap_or(0);
    let _ = run_tmux(&["set-option", "-t", &name, "window-size", "latest"]);
    let _ = run_tmux(&["set-option", "-t", &name, "remain-on-exit", "on"]);

    let log = tabs_log_dir().join(format!("{}.log", tab));
    if !existed {
        let _ = fs::create_dir_all(tabs_log_dir());
        let pipe = format!("cat >> {}", log.display());
        run_tmux(&["pipe-pane", "-O", "-t", &name, pipe.as_str()]);
        if fs::File::create(&log).is_err() {
            return None;
        }
    }
    let start = fs::metadata(&log).map(|m| m.len()).unwrap_or(0);
    spool_event(
        if existed { "tmux_adopt" } else { "tmux_spawn" },
        tab,
        format!(
            "session={} pane_pid={} backend=tmux log={} engine={}",
            name,
            pane_pid,
            log.display(),
            profile.engine
        ),
        proc_cwd(pane_pid),
        Some("tmux"),
    );
    Some(PtySession {
        kind: SessionKind::Tmux {
            session: name,
            log,
            offset: Arc::new(AtomicU64::new(start)),
        },
        root_pid: pane_pid,
    })
}

fn shell_words(parts: &[&str]) -> String {
    parts
        .iter()
        .map(|p| format!("'{}'", p.replace('\'', r"'\''")))
        .collect::<Vec<_>>()
        .join(" ")
}

// ---------------------------------------------------------------------------
// /proc introspection: real pid + cwd per tab
// ---------------------------------------------------------------------------

fn proc_ppid(pid: u32) -> Option<u32> {
    let stat = fs::read_to_string(format!("/proc/{}/stat", pid)).ok()?;
    let rest = stat.rsplit_once(") ")?.1;
    rest.split_whitespace().nth(1)?.parse().ok()
}

fn proc_comm(pid: u32) -> String {
    fs::read_to_string(format!("/proc/{}/comm", pid))
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// State proses dari /proc/<pid>/stat (huruf setelah ')' karena comm boleh berisi spasi).
fn proc_state(pid: u32) -> Option<char> {
    let stat = fs::read_to_string(format!("/proc/{}/stat", pid)).ok()?;
    stat.rsplit_once(") ")?.1.split_whitespace().next()?.chars().next()
}

/// Proses hantu (zombie) masih punya direktori /proc tetapi sudah tidak punya cwd
/// maupun cmdline. Melaporkannya sebagai "alive" adalah sumber workspace ngaco.
fn proc_alive(pid: u32) -> bool {
    matches!(proc_state(pid), Some(c) if c != 'Z')
}

fn proc_cmdline(pid: u32) -> String {
    fs::read(format!("/proc/{}/cmdline", pid))
        .map(|raw| {
            raw.split(|b| *b == 0)
                .filter(|s| !s.is_empty())
                .map(|s| String::from_utf8_lossy(s).to_string())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .unwrap_or_default()
}

fn proc_cwd(pid: u32) -> Option<String> {
    fs::read_link(format!("/proc/{}/cwd", pid))
        .ok()
        .map(|p| p.to_string_lossy().to_string())
}

fn all_pids() -> Vec<u32> {
    fs::read_dir("/proc")
        .map(|rd| {
            rd.filter_map(|e| e.ok().and_then(|e| e.file_name().to_string_lossy().parse::<u32>().ok()))
                .collect()
        })
        .unwrap_or_default()
}

fn descendants(root: u32) -> Vec<u32> {
    let pids = all_pids();
    let mut children: HashMap<u32, Vec<u32>> = HashMap::new();
    for pid in &pids {
        if let Some(pp) = proc_ppid(*pid) {
            children.entry(pp).or_default().push(*pid);
        }
    }
    let mut out = Vec::new();
    let mut stack = vec![root];
    while let Some(p) = stack.pop() {
        if let Some(kids) = children.get(&p) {
            for k in kids {
                out.push(*k);
                stack.push(*k);
            }
        }
    }
    out
}

#[derive(Clone)]
struct LiveProc {
    pid: u32,
    comm: String,
    cwd: String,
    cmdline: String,
}

/// The live process behind a tab. `hint` is the engine's expected binary name
/// (qoder/agy); falls back to the deepest non-helper descendant, then to the
/// PTY root itself so that a plain shell tab still reports its real cwd.
fn tab_live_process(root: u32, hint: Option<&str>) -> Option<LiveProc> {
    let kids: Vec<u32> = descendants(root)
        .into_iter()
        .filter(|pid| proc_alive(*pid))
        .collect();
    let mut pick: Option<u32> = None;

    if let Some(h) = hint {
        for pid in &kids {
            if proc_comm(*pid) == h {
                pick = Some(*pid);
            }
        }
    }
    if pick.is_none() {
        for pid in &kids {
            let c = proc_comm(*pid);
            if matches!(c.as_str(), "bash" | "sh" | "python3" | "node" | "run_tab.sh" | "") {
                continue;
            }
            pick = Some(*pid);
        }
    }

    let pid = pick.unwrap_or(root);
    if !proc_alive(pid) {
        return None;
    }
    Some(LiveProc {
        pid,
        comm: proc_comm(pid),
        cwd: proc_cwd(pid).unwrap_or_default(),
        cmdline: proc_cmdline(pid),
    })
}

fn engine_hint(engine: &str) -> Option<&'static str> {
    match engine {
        "qoder" => Some("qoder"),
        "agy" => Some("agy"),
        _ => None,
    }
}

// ---------------------------------------------------------------------------
// Cached subprocess helpers
// ---------------------------------------------------------------------------

fn cache() -> &'static Mutex<HashMap<String, (Instant, Value)>> {
    static CACHE: OnceLock<Mutex<HashMap<String, (Instant, Value)>>> = OnceLock::new();
    CACHE.get_or_init(Default::default)
}

fn cached_json<F>(key: &str, ttl: Duration, producer: F) -> Value
where
    F: FnOnce() -> Value,
{
    if let Ok(c) = cache().lock() {
        if let Some((at, val)) = c.get(key) {
            if at.elapsed() < ttl {
                return val.clone();
            }
        }
    }
    let val = producer();
    if let Ok(mut c) = cache().lock() {
        c.insert(key.to_string(), (Instant::now(), val.clone()));
    }
    val
}

fn run_capture(program: &str, args: &[&str]) -> Option<String> {
    let out = Command::new(program).args(args).output().ok()?;
    if !out.status.success() {
        return None;
    }
    Some(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

/// Run a python one-liner that prints JSON on stdout.
fn py_json(script: &str, args: &[&str]) -> Option<Value> {
    let mut cmd = Command::new("python3");
    cmd.arg("-c").arg(script);
    cmd.args(args);
    let out = cmd.output().ok()?;
    let stdout = String::from_utf8_lossy(&out.stdout).trim().to_string();
    stdout
        .find('{')
        .and_then(|i| serde_json::from_str(&stdout[i..]).ok())
}

// ---------------------------------------------------------------------------
// Models catalog
// ---------------------------------------------------------------------------

fn qoder_models() -> Vec<Value> {
    run_capture("qoder", &["--list-models"])
        .map(|raw| {
            raw.lines()
                .map(|l| l.trim().to_string())
                .filter(|l| !l.is_empty() && !l.eq_ignore_ascii_case("MODEL"))
                .map(|name| json!({ "id": name, "label": name }))
                .collect()
        })
        .unwrap_or_default()
}

fn agy_models() -> Vec<Value> {
    run_capture("agy", &["models"])
        .map(|raw| {
            raw.lines()
                .filter_map(|l| {
                    let l = l.trim();
                    if l.is_empty() || l.starts_with("Fetching") {
                        return None;
                    }
                    let mut it = l.split('\t');
                    let id = it.next()?.trim();
                    if id.is_empty() {
                        return None;
                    }
                    let label = it.next().unwrap_or(id).trim();
                    Some(json!({ "id": id, "label": label }))
                })
                .collect()
        })
        .unwrap_or_default()
}

fn qoder_efforts() -> Vec<&'static str> {
    vec!["xhigh", "high", "medium", "low", "auto"]
}

fn agy_efforts() -> Vec<&'static str> {
    vec!["max", "xhigh", "high", "medium", "low"]
}

// ---------------------------------------------------------------------------
// Usage / account telemetry
// ---------------------------------------------------------------------------

fn qoder_account() -> Value {
    cached_json("qoder_account", Duration::from_secs(60), || {
        run_capture("qoder", &["status", "-o", "json"])
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .unwrap_or_else(|| json!({ "logged_in": false }))
    })
}

fn cli_version(bin: &str) -> Value {
    cached_json(&format!("ver_{}", bin), Duration::from_secs(300), || {
        match run_capture(bin, &["--version"]) {
            Some(raw) => {
                let ver = raw
                    .lines()
                    .next()
                    .and_then(|l| l.split_whitespace().last())
                    .unwrap_or("N/A")
                    .trim_matches(|c: char| c == 'v' || c == ':')
                    .to_string();
                json!({ "connected": true, "version": ver })
            }
            None => json!({ "connected": false, "version": "N/A" }),
        }
    })
}

/// Aggregate today's activity from the newest Qoder session log of the tab's cwd.
fn qoder_session_usage(workspace: &str) -> Value {
    let script = r#"
import glob, json, os, sys, time
enc = sys.argv[1].replace("/", "-").replace(".", "-")
base = os.path.expanduser("~/.ai-station/engines/qoder/logs/sessions")
cand = []
for seg in glob.glob(os.path.join(base, enc, "*", "segments", "*.jsonl")):
    try: cand.append((os.path.getmtime(seg), seg))
    except OSError: pass
out = {"turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0,
       "duration_ms": 0, "models": [], "session_id": "", "log_file": "", "tool_calls": 0}
if cand:
    newest = max(cand)[1]
    out["log_file"] = newest
    out["session_id"] = newest.split(os.sep)[-3]
    today = time.strftime("%Y-%m-%d")
    models = {}
    for line in open(newest, errors="ignore"):
        try: ev = json.loads(line)
        except Exception: continue
        if not str(ev.get("ts", "")).startswith(today): continue
        d = ev.get("data") or {}
        t = ev.get("type")
        if t == "turn.finished":
            out["turns"] += int(d.get("num_turns") or 0) or 1
            out["duration_ms"] += int(d.get("duration_ms") or 0)
            out["input_tokens"] += int(d.get("input_tokens") or 0)
            out["output_tokens"] += int(d.get("output_tokens") or 0)
            out["cache_read_tokens"] += int(d.get("cache_read_input_tokens") or 0)
        elif t == "tool.execution.finished":
            out["tool_calls"] += 1
        elif t == "session.config.loaded" and d.get("model"):
            models[d["model"]] = models.get(d["model"], 0) + 1
    out["models"] = list(models.keys())
print(json.dumps(out))
"#;
    py_json(script, &[workspace]).unwrap_or(Value::Null)
}

fn antigravity_usage() -> Value {
    let script = r#"
import json, os, sqlite3, sys
db = os.path.expanduser("~/.gemini/antigravity/conversation_summaries.db")
out = {"conversation":"","steps":0,"status":"","workspace":"","project_id":"","last_modified":""}
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    c.row_factory = sqlite3.Row
    row = c.execute("select * from conversation_summaries order by last_modified_time desc limit 1").fetchone()
    if row:
        d = dict(row)
        uris = d.get("workspace_uris") or "[]"
        try:
            import urllib.parse as up
            ws = ", ".join(up.urlparse(u).path for u in json.loads(uris) if u)
        except Exception:
            ws = uris
        out = {"conversation": d.get("title",""), "steps": int(d.get("step_count") or 0),
               "status": (d.get("status") or "").replace("CASCADE_RUN_STATUS_","").lower(),
               "workspace": ws, "project_id": d.get("project_id",""),
               "last_modified": str(d.get("last_modified_time",""))[:19]}
except Exception as exc:
    out["error"] = str(exc)
print(json.dumps(out))
"#;
    py_json(script, &[]).unwrap_or(Value::Null)
}

fn antigravity_last_model() -> String {
    fs::read_to_string(home_dir().join(".gemini/antigravity/antigravity_state.pbtxt"))
        .ok()
        .and_then(|raw| {
            raw.lines()
                .find(|l| l.trim_start().starts_with("last_selected_agent_model:"))
                .map(|l| l.split(':').nth(1).unwrap_or("").trim().to_string())
        })
        .unwrap_or_default()
}

// ---------------------------------------------------------------------------
// Save-state (PostgreSQL primary, SQLite fallback)
// ---------------------------------------------------------------------------

#[derive(Default)]
struct SaveState {
    engine: String,
    skills: i64,
    quests: i64,
    memories: i64,
    incidents: i64,
    turns: i64,
    player: Value,
}

async fn load_save_state() -> SaveState {
    let mut st = SaveState::default();

    if let Ok((client, conn)) = tokio_postgres::connect(&pg_conn_str(), tokio_postgres::NoTls).await {
        tokio::spawn(async move {
            let _ = conn.await;
        });
        st.engine = "POSTGRESQL".to_string();
        st.skills = count(&client, "skills_inventory").await;
        st.quests = count(&client, "quest_tasks").await;
        st.memories = count(&client, "world_memory").await;
        st.incidents = count(&client, "incident_log").await;
        st.turns = count(&client, "session_turns").await;
        st.player = player_profile(&client).await;
        return st;
    }

    st.engine = "SQLITE".to_string();
    let script = r#"
import json, os, sqlite3
db = os.path.expanduser("~/.ai-station/brain/workstation.db")
out = {"engine":"SQLITE"}
try:
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True); c.row_factory = sqlite3.Row
    for t in ("skills_inventory","quest_tasks","world_memory","incident_log","session_turns"):
        try: out[t] = c.execute("select count(*) n from %s" % t).fetchone()["n"]
        except Exception: out[t] = 0
    try:
        r = c.execute("select * from player_profile limit 1").fetchone()
        out["player"] = dict(r) if r else {}
    except Exception: out["player"] = {}
except Exception as exc:
    out["error"] = str(exc)
print(json.dumps(out))
"#;
    if let Some(v) = py_json(script, &[]) {
        st.skills = v.get("skills_inventory").and_then(Value::as_i64).unwrap_or(0);
        st.quests = v.get("quest_tasks").and_then(Value::as_i64).unwrap_or(0);
        st.memories = v.get("world_memory").and_then(Value::as_i64).unwrap_or(0);
        st.incidents = v.get("incident_log").and_then(Value::as_i64).unwrap_or(0);
        st.turns = v.get("session_turns").and_then(Value::as_i64).unwrap_or(0);
        st.player = v.get("player").cloned().unwrap_or(Value::Null);
    }
    st
}

async fn count(client: &tokio_postgres::Client, table: &str) -> i64 {
    client
        .query_one(&format!("SELECT COUNT(*) FROM {};", table), &[])
        .await
        .map(|r| r.get(0))
        .unwrap_or(0)
}

async fn player_profile(client: &tokio_postgres::Client) -> Value {
    let sql = "SELECT level, exp_points, active_title, username FROM player_profile LIMIT 1";
    match client.query_opt(sql, &[]).await {
        Ok(Some(row)) => json!({
            "level": row.get::<_, i32>(0),
            "exp": row.get::<_, i32>(1),
            "title": row.get::<_, String>(2),
            "username": row.get::<_, String>(3),
        }),
        _ => Value::Null,
    }
}

fn count_files(dir: &std::path::Path, ext: &str) -> i64 {
    fs::read_dir(dir)
        .map(|rd| {
            rd.filter_map(|e| e.ok())
                .filter(|e| {
                    e.path()
                        .extension()
                        .map(|x| x == ext)
                        .unwrap_or(false)
                })
                .count() as i64
        })
        .unwrap_or(0)
}

fn count_skill_files() -> i64 {
    let root = station_dir().join("brain").join("skills");
    let mut n = 0i64;
    if let Ok(rd) = fs::read_dir(&root) {
        for entry in rd.filter_map(|e| e.ok()) {
            if entry.path().is_dir() {
                n += count_files(&entry.path(), "md");
            }
        }
    }
    n
}

// ---------------------------------------------------------------------------
// HTTP payloads
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct GpuInfo {
    name: String,
    used_mb: u64,
    total_mb: u64,
    load: u32,
    temp_c: u32,
}

#[derive(Serialize)]
struct RamInfo {
    used_gb: f32,
    total_gb: f32,
}

#[derive(Serialize)]
struct DiskInfo {
    free: String,
    total: String,
}

#[derive(Serialize)]
struct TabStatus {
    alive: bool,
    pid: u32,
    process: String,
    cwd: String,
    cmdline: String,
    root_pid: u32,
    profile: TabProfile,
}

#[derive(Serialize)]
struct TelemetryStats {
    user_name: String,
    antigravity_cli: Value,
    qoder_cli: Value,
    qoder_account: Value,
    gpu: GpuInfo,
    ram: RamInfo,
    disk: DiskInfo,
    skills_count: i64,
    rules_count: i64,
    memories_count: i64,
    incidents_count: i64,
    total_quests: i64,
    total_turns: i64,
    player: Value,
    db_engine: String,
    /// Naik tiap ada permintaan muat-ulang UI. WebView membandingkannya saat polling
    /// /api/stats, sehingga `refreshUI` bisa dipicu dari luar (MCP/perintah), bukan
    /// hanya dari tombol di dalam halaman itu sendiri.
    ui_reload_tick: u64,
    tabs: HashMap<String, TabStatus>,
}

#[derive(Deserialize)]
struct ReadQuery {
    tab: Option<String>,
}

#[derive(Deserialize)]
struct WritePayload {
    tab: Option<String>,
    data: Option<String>,
}

#[derive(Deserialize)]
struct ResetPayload {
    tab: Option<String>,
}

#[derive(Deserialize)]
struct ResizePayload {
    tab: Option<String>,
    cols: u16,
    rows: u16,
}

#[derive(Deserialize)]
struct CliRunPayload {
    prompt: Option<String>,
    target_cli: Option<String>,
}

#[derive(Deserialize)]
struct ActionPayload {
    action: Option<String>,
}

#[derive(Deserialize)]
struct ConfigPatch {
    tab: String,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    effort: Option<String>,
    #[serde(default)]
    context_window: Option<String>,
    #[serde(default)]
    permission_mode: Option<String>,
    #[serde(default)]
    workspace: Option<String>,
    #[serde(default)]
    continue_session: Option<bool>,
    #[serde(default)]
    restart: bool,
}

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
        .route("/", get(serve_gui))
        .route("/assets/*path", get(serve_asset))
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
        .route("/api/see", post(handle_see))
        .route("/api/action", post(handle_action))
        .route("/api/screenshot/latest", get(serve_screenshot))
        .route("/api/restart-server", post(restart_server))
        .route("/api/ui/reload", post(ui_reload))
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

// ---------------------------------------------------------------------------
// Static handlers
// ---------------------------------------------------------------------------

async fn serve_gui() -> Response {
    let html = fs::read_to_string(gui_path())
        .or_else(|_| Ok::<String, std::io::Error>(include_str!("gui.html").to_string()))
        .unwrap_or_else(|_| "<h1>gui.html tidak ditemukan</h1>".to_string());
    // Tanpa header ini WebKit boleh menyajikan HTML dari cache, dan tombol "Refresh UI"
    // akan terlihat berhasil padahal yang termuat masih JavaScript lama. Semua perubahan
    // gui.html jadi tidak pernah sampai ke jendela yang sedang terbuka.
    Response::builder()
        .header(header::CONTENT_TYPE, "text/html; charset=utf-8")
        .header(header::CACHE_CONTROL, "no-store, must-revalidate")
        .body(axum::body::Body::from(html))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

async fn serve_asset(Path(rel): Path<String>) -> Response {
    if rel.contains("..") {
        return StatusCode::BAD_REQUEST.into_response();
    }
    let full = assets_dir().join(&rel);
    match fs::read(&full) {
        Ok(bytes) => {
            let ctype = match full.extension().and_then(|e| e.to_str()).unwrap_or("") {
                "js" => "text/javascript; charset=utf-8",
                "css" => "text/css; charset=utf-8",
                "woff2" => "font/woff2",
                "png" => "image/png",
                "svg" => "image/svg+xml",
                _ => "application/octet-stream",
            };
            Response::builder()
                .header(header::CONTENT_TYPE, ctype)
                .header(header::CACHE_CONTROL, "public, max-age=3600")
                .body(axum::body::Body::from(bytes))
                .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
        }
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

// ---------------------------------------------------------------------------
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
        session.send_bytes(format!("{}\r", prompt).as_bytes());
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

#[derive(Deserialize)]
struct LogQuery {
    #[serde(default)]
    limit: Option<u32>,
    #[serde(default)]
    engine: Option<String>,
    #[serde(default)]
    kind: Option<String>,
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
            "workspace": qws
        },
        "antigravity": {
            "conversation": agy,
            "last_model": antigravity_last_model()
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
        ui_reload_tick: UI_RELOAD_TICK.load(Ordering::Relaxed),
        tabs,
    })
}

// ---------------------------------------------------------------------------
// Desktop actions
// ---------------------------------------------------------------------------

async fn handle_see() -> Json<Value> {
    let script = station_dir().join("tools").join("wayland_actor.py");
    let script_arg = script.display().to_string();
    let res = Command::new("python3")
        .args([&script_arg, "screenshot"])
        .output();
    match res {
        Ok(out) if out.status.success() => {
            Json(json!({ "status": "ok", "message": "Screenshot berhasil" }))
        }
        Ok(out) => Json(json!({
            "status": "error",
            "message": String::from_utf8_lossy(&out.stderr)
        })),
        Err(e) => Json(json!({ "status": "error", "message": e.to_string() })),
    }
}

async fn serve_screenshot() -> Response {
    let p = station_dir()
        .join("logs")
        .join("current_screen.png");
    match fs::read(&p) {
        Ok(bytes) => Response::builder()
            .header(header::CONTENT_TYPE, "image/png")
            .header(header::CACHE_CONTROL, "no-store")
            .body(axum::body::Body::from(bytes))
            .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response()),
        Err(_) => StatusCode::NOT_FOUND.into_response(),
    }
}

async fn handle_action(Json(payload): Json<ActionPayload>) -> Json<Value> {
    match payload.action.as_deref() {
        Some("vol_up") => {
            let _ = Command::new("pactl")
                .args(["set-sink-volume", "@DEFAULT_SINK@", "+10%"])
                .spawn();
        }
        Some("vol_down") => {
            let _ = Command::new("pactl")
                .args(["set-sink-volume", "@DEFAULT_SINK@", "-10%"])
                .spawn();
        }
        Some("vol_mute") => {
            let _ = Command::new("pactl")
                .args(["set-sink-mute", "@DEFAULT_SINK@", "toggle"])
                .spawn();
        }
        Some("notify_test") => {
            let _ = Command::new("notify-send")
                .args([
                    "-a",
                    "Irsofka AI Workstation",
                    "Halo Bro Ichsan!",
                    "AI Workstation Native Rust Core Aktif!",
                ])
                .spawn();
        }
        _ => {}
    }
    Json(json!({ "status": "ok" }))
}

/// Permintaan muat ulang tampilan dari luar (MCP, `curl`, atau tombol GUI).
/// Aman dipanggil otomatis: hanya menaikkan penghitung yang dibaca WebView, tidak menyentuh
/// PTY maupun tmux, jadi sesi CLI di dalam tab tidak ikut berhenti.
async fn ui_reload() -> Json<Value> {
    let tick = UI_RELOAD_TICK.fetch_add(1, Ordering::Relaxed) + 1;
    spool_event(
        "ui_reload_requested",
        "-",
        format!("Permintaan muat ulang UI (tick={tick})"),
        proc_cwd(std::process::id()),
        Some("daemon"),
    );
    Json(json!({ "status": "ok", "ui_reload_tick": tick }))
}

async fn restart_server() -> Json<Value> {
    spool_event(
        "daemon_restart_requested",
        "-",
        "Permintaan restart daemon via UI: systemctl --user restart irsofka-ai-workstation.service"
            .to_string(),
        proc_cwd(std::process::id()),
        Some("daemon"),
    );
    tokio::spawn(async {
        tokio::time::sleep(Duration::from_millis(500)).await;
        let _ = Command::new("systemctl")
            .args(["--user", "restart", "irsofka-ai-workstation.service"])
            .spawn();
    });
    Json(json!({
        "status": "restarting",
        "message": "Rust server service sedang di-restart..."
    }))
}
