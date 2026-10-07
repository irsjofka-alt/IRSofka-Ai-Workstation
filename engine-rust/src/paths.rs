//! Setiap jalur berkas yang dibaca atau ditulis workstation, dihitung ulang dari `$HOME`.
//!
//! Tidak ada satu pun yang di-cache di `static`: daemon harus tetap benar ketika direktori
//! workstation dipindah atau dijalankan dengan akun lain. Satu jalur absolut yang ikut
//! ter-compile akan membawa home directory pemilik mesin ke dalam setiap clone repo publik.
use std::path::PathBuf;

pub(crate) const FALLBACK_HOST: &str = "127.0.0.1";
pub(crate) const DEFAULT_PORT: u16 = 8999;

pub(crate) fn station_port() -> u16 {
    std::env::var("STATION_PORT")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DEFAULT_PORT)
}

pub(crate) fn home_dir() -> PathBuf {
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

pub(crate) fn station_dir() -> PathBuf {
    home_dir().join(".ai-station")
}

pub(crate) fn config_dir() -> PathBuf {
    station_dir().join("config")
}

pub(crate) fn profiles_path() -> PathBuf {
    config_dir().join("cli_profiles.json")
}

pub(crate) fn runner_path() -> PathBuf {
    config_dir().join("run_tab.sh")
}

pub(crate) fn assets_dir() -> PathBuf {
    station_dir().join("engine-rust").join("assets")
}

pub(crate) fn gui_path() -> PathBuf {
    station_dir().join("engine-rust").join("src").join("gui.html")
}

pub(crate) fn spool_path() -> PathBuf {
    station_dir().join("logs").join("station_events.jsonl")
}

pub(crate) fn ingestor_path() -> PathBuf {
    station_dir().join("tools").join("session_ingestor.py")
}

/// Halaman Station: muka dari semua catatan, dibaca manusia — bukan tempat mengetik.
pub(crate) fn station_path() -> PathBuf {
    station_dir().join("engine-rust").join("src").join("station.html")
}

pub(crate) fn tabs_log_dir() -> PathBuf {
    station_dir().join("logs").join("tabs")
}

/// Socket tmux milik workstation. Terpisah dari socket default supaya pane AI tidak
/// pernah berbagi server dengan sesi tmux manual pemilik mesin.
pub(crate) fn tmux_socket() -> String {
    std::env::var("STATION_TMUX_SOCKET").unwrap_or_else(|_| "irsofka".to_string())
}

pub(crate) fn tmux_session_name(tab: &str) -> String {
    format!("station-{}", tab)
}
