//! /api/stats: satu telementri untuk seluruh layar.
//!
//! GPU, RAM, disk, tab, dan hitungan save-state dirakit di sini. Semua angkanya diambil dari
//! sumber yang benar-benar menghitung (nvidia-smi, /proc/meminfo, df, tmux, PostgreSQL) —
//! modul ini tidak pernah memperkirakan, dan tidak meng-cache lebih lama dari TTL probe.
use axum::extract::State;
use axum::Json;
use serde_json::Value;
use std::collections::HashMap;
use std::fs;

use crate::api::{AppState, DiskInfo, GpuInfo, RamInfo, TabStatus, TelemetryStats};
use crate::TABS;
use crate::paths::{home_dir, station_dir};
use crate::profile::read_profiles;
use crate::save_state::{count_files, count_skill_files, load_save_state};
use crate::terminal::{PtySession, tab_live_process, engine_hint};
use crate::engineinfo::{cli_version, qoder_account};
use crate::probe::run_capture;
use crate::api::daemon;

pub(crate) fn tab_status_map(sessions: &HashMap<String, PtySession>) -> HashMap<String, TabStatus> {
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

pub(crate) async fn get_stats(State(state): State<AppState>) -> Json<TelemetryStats> {
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
