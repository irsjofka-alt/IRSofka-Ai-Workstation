//! Bentuk berkas HTTP workstation: state yang dibagikan dan kontrak request/response.
//!
//! Struktur di file ini adalah kontrak yang dibaca gui.html. Ia hidup di satu tempat supaya
//! menambah satu field ke /api/stats tidak memerlukan gading lima berkas handler.
//! AppState hanya menampung yang benar-benar harus bertahan antar-request; semua yang lain
//! dibaca ulang dari disk, tmux, atau PostgreSQL agar tidak ada cache yang bisa basi.
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use crate::profile::TabProfile;
use crate::terminal::PtySession;

pub(crate) mod daemon;
pub(crate) mod desktop;
pub(crate) mod engine;
pub(crate) mod master;
pub(crate) mod stats;
pub(crate) mod static_files;
pub(crate) mod terminal;
pub(crate) mod workspace;

#[derive(Clone)]
pub(crate) struct AppState {
    pub(crate) sessions: Arc<HashMap<String, PtySession>>,
    /// Penyangga ketikan per tab, supaya satu baris perintah utuh (bukan per-tombol)
    /// yang masuk ke action_log.
    pub(crate) typed: Arc<Mutex<HashMap<String, String>>>,
}

#[derive(Serialize)]
pub(crate) struct GpuInfo {
    pub(crate) name: String,
    pub(crate) used_mb: u64,
    pub(crate) total_mb: u64,
    pub(crate) load: u32,
    pub(crate) temp_c: u32,
}

#[derive(Serialize)]
pub(crate) struct RamInfo {
    pub(crate) used_gb: f32,
    pub(crate) total_gb: f32,
}

#[derive(Serialize)]
pub(crate) struct DiskInfo {
    pub(crate) free: String,
    pub(crate) total: String,
}

#[derive(Serialize)]
pub(crate) struct TabStatus {
    pub(crate) alive: bool,
    pub(crate) pid: u32,
    pub(crate) process: String,
    pub(crate) cwd: String,
    pub(crate) cmdline: String,
    pub(crate) root_pid: u32,
    pub(crate) profile: TabProfile,
}

#[derive(Serialize)]
pub(crate) struct TelemetryStats {
    pub(crate) user_name: String,
    pub(crate) antigravity_cli: Value,
    pub(crate) qoder_cli: Value,
    pub(crate) qoder_account: Value,
    pub(crate) gpu: GpuInfo,
    pub(crate) ram: RamInfo,
    pub(crate) disk: DiskInfo,
    pub(crate) skills_count: i64,
    pub(crate) rules_count: i64,
    pub(crate) memories_count: i64,
    pub(crate) incidents_count: i64,
    pub(crate) total_quests: i64,
    pub(crate) total_turns: i64,
    pub(crate) player: Value,
    pub(crate) db_engine: String,
    /// Naik tiap ada permintaan muat-ulang UI. WebView membandingkannya saat polling
    /// /api/stats, sehingga `refreshUI` bisa dipicu dari luar (MCP/perintah), bukan
    /// hanya dari tombol di dalam halaman itu sendiri.
    pub(crate) ui_reload_tick: u64,
    pub(crate) tabs: HashMap<String, TabStatus>,
}

#[derive(Deserialize)]
pub(crate) struct ReadQuery {
    pub(crate) tab: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct WritePayload {
    pub(crate) tab: Option<String>,
    pub(crate) data: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct ResetPayload {
    pub(crate) tab: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct ResizePayload {
    pub(crate) tab: Option<String>,
    pub(crate) cols: u16,
    pub(crate) rows: u16,
}

#[derive(Deserialize)]
pub(crate) struct CliRunPayload {
    pub(crate) prompt: Option<String>,
    pub(crate) target_cli: Option<String>,
}

#[derive(Deserialize)]
pub(crate) struct ConfigPatch {
    pub(crate) tab: String,
    #[serde(default)]
    pub(crate) model: Option<String>,
    #[serde(default)]
    pub(crate) effort: Option<String>,
    #[serde(default)]
    pub(crate) context_window: Option<String>,
    #[serde(default)]
    pub(crate) permission_mode: Option<String>,
    #[serde(default)]
    pub(crate) workspace: Option<String>,
    #[serde(default)]
    pub(crate) continue_session: Option<bool>,
    #[serde(default)]
    pub(crate) restart: bool,
}

#[derive(Deserialize)]
pub(crate) struct LogQuery {
    #[serde(default)]
    pub(crate) limit: Option<u32>,
    #[serde(default)]
    pub(crate) engine: Option<String>,
    #[serde(default)]
    pub(crate) kind: Option<String>,
}
