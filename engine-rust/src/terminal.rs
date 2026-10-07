//! Pemilikan pane terminal: satu sesi CLI, dua backend — PTY milik daemon atau tmux.
//!
//! Pilihan backend adalah keputusan arsitektur, bukan detail: PTY langsung menempatkan
//! proses CLI di dalam cgroup service daemon, sehingga `systemctl restart` ikut membunuh
//! AI yang sedang bekerja. Karena itu pane normal dititipkan ke `irsofka-tabs.service`
//! lewat socket tmux sendiri; daemon hanya membaca hasil `pipe-pane` dan mengirim byte
//! mentah. Direct tetap ada sebagai fallback ketika tmux tidak siap.
//!
//! Semua angka pemakaian konteks di GUI DIPINJAM dari baris status CLI-nya sendiri — modul
//! ini tidak pernah menghitung konteksnya sendiri, dan tidak menebak kalau barisnya hilang.
use portable_pty::{CommandBuilder, MasterPty, NativePtySystem, PtySize, PtySystem};
use serde_json::json;
use std::fs;
use std::io::{Read, Seek, SeekFrom, Write};
use std::path::PathBuf;
use std::process::Command;
use std::sync::{
    atomic::{AtomicU64, Ordering},
    Arc, Mutex,
};
use std::time::Duration;

use crate::paths::{home_dir, runner_path, tabs_log_dir, tmux_socket, tmux_session_name};
use crate::probe::cached_json;
use crate::procinfo::{descendants, proc_alive, proc_cmdline, proc_comm, proc_cwd, LiveProc};
use crate::profile::TabProfile;
use crate::spool::spool_event;

pub(crate) enum SessionKind {
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

pub(crate) struct PtySession {
    kind: SessionKind,
    pub(crate) root_pid: u32,
}

const TMUX_READ_CAP: usize = 512 * 1024;

const TMUX_KEY_CHUNK: usize = 96;

impl PtySession {
    pub(crate) fn read_new(&self) -> Vec<u8> {
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

    pub(crate) fn read_history(&self) -> Vec<u8> {
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

    pub(crate) fn send_bytes(&self, data: &[u8]) {
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

    pub(crate) fn resize(&self, cols: u16, rows: u16) {
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

    pub(crate) fn interrupt(&self) {
        match &self.kind {
            SessionKind::Direct { writer, .. } => {
                if let Ok(mut w) = writer.lock() {
                    let _ = w.write_all(b"\x03\n");
                }
            }
            SessionKind::Tmux { .. } => self.send_bytes(&[0x03]),
        }
    }

    pub(crate) fn backend(&self) -> &'static str {
        match &self.kind {
            SessionKind::Direct { .. } => "pty",
            SessionKind::Tmux { .. } => "tmux",
        }
    }
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

pub(crate) fn create_session(tab: &str, profile: &TabProfile) -> PtySession {
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

/// Ambil TAMPILAN pane saat ini, lengkap dengan sekuens escape-nya.
///
/// Ini yang dibutuhkan xterm.js untuk menggambar ulang sebuah TUI dengan benar. Memutar
/// ulang byte historis tidak memadai: TUI menggambar dengan gerak kursor absolut
/// (ESC[2A dan sejenisnya) yang mengasumsikan lebar tertentu, jadi replay di lebar yang
/// berbeda menghasilkan teks saling menimpa. capture-pane memberi hasil render final.
pub(crate) fn capture_screen(tab: &str) -> Option<String> {
    run_tmux(&[
        "capture-pane", "-e", "-p", "-S", "-2000", "-t", tmux_session_name(tab).as_str(),
    ])
}

/// Baris status CLI memuat pemakaian konteks, contoh:
///   "Qwen3.8-Flash Model · Extra High · 1M context · ctx ▓▒░ 53% · ~ +6904 -834"
/// Angka ini DIPINJAM dari yang CLI sendiri tampilkan — bukan hitungan kami, dan kami
/// tidak pernah menggantinya dengan perkiraan kalau barisnya tidak ketemu.
pub(crate) fn pane_context_pct(tab: &str) -> Option<u8> {
    let raw = capture_screen(tab)?;
    let plain: String = raw.chars().filter(|c| !c.is_control() || *c == '\n').collect();
    for line in plain.lines().rev().take(90) {
        let low = line.to_lowercase();
        // Qoder menulis "ctx ... 57%" (PEMAKAIAN). Antigravity menulis panel /context yang
        // justru menyebut "Free space: 938.4k (89.5%)" (SISA) — dan baris itu tidak memuat
        // kata ctx sama sekali. Keduanya harus dibaca berbeda, tidak bisa satu pola.
        let is_free = low.contains("free space");
        let is_total = low.contains("total") && low.contains('%');
        if !is_free && !is_total && !low.contains("ctx") && !low.contains("context") { continue; }
        let bytes = line.as_bytes();
        let mut i = 0usize;
        while i < bytes.len() {
            if bytes[i].is_ascii_digit() {
                // Sertakan desimal: `/context` Antigravity menulis "10.5%", dan angka
                // bulat-saja akan membaca "5" dari "10.5%" — salah tanpa kelihatan.
                let mut j = i;
                while j < bytes.len() && (bytes[j].is_ascii_digit() || bytes[j] == b'.') { j += 1; }
                let token = line[i..j].trim_end_matches('.').to_string();
                let mut k = j;
                while k < bytes.len() && bytes[k] == b' ' { k += 1; }
                if k < bytes.len() && bytes[k] == b'%' {
                    if let Ok(v) = token.parse::<f32>() {
                        let used = if is_free { 100.0 - v } else { v };
                        if (0.0..=100.0).contains(&used) {
                            return Some(used.round().clamp(0.0, 100.0) as u8);
                        }
                    }
                }
                i = j.max(i + 1);
            } else { i += 1; }
        }
    }
    None
}

pub(crate) fn run_tmux(args: &[&str]) -> Option<String> {
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
pub(crate) fn use_tmux_backend() -> bool {
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

pub(crate) fn shell_words(parts: &[&str]) -> String {
    parts
        .iter()
        .map(|p| format!("'{}'", p.replace('\'', r"'\''")))
        .collect::<Vec<_>>()
        .join(" ")
}

// /proc introspection: real pid + cwd per tab

/// The live process behind a tab. `hint` is the engine's expected binary name
/// (qoder/agy); falls back to the deepest non-helper descendant, then to the
/// PTY root itself so that a plain shell tab still reports its real cwd.
pub(crate) fn tab_live_process(root: u32, hint: Option<&str>) -> Option<LiveProc> {
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

pub(crate) fn engine_hint(engine: &str) -> Option<&'static str> {
    match engine {
        "qoder" => Some("qoder"),
        "agy" => Some("agy"),
        _ => None,
    }
}
