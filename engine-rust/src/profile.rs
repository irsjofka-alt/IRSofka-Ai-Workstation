//! Profil CLI: satu sumber kebenaran untuk bagaimana tiap tab menyalakan mesinnya.
//!
//! `config/cli_profiles.json` adalah berkasnya, `default_profiles` jaring pengamanannya,
//! dan `RUNNER_SCRIPT` supervisor yang ditulis ulang setiap daemon start. Yang tidak boleh
//! terjadi: sebuah tab jatuh ke `$HOME` atau folder proyek acak saat konfigurasi hilang —
//! keduanya memutus kontrak bahwa semua AI membaca workspace yang sama.
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::fs;

use crate::paths::{config_dir, home_dir, profiles_path, runner_path};

// CLI profiles: single source of truth for how every tab spawns its CLI.
// ---------------------------------------------------------------------------

#[derive(Clone, Serialize, Deserialize, Default)]
pub struct TabProfile {
    #[serde(default)]
    pub(crate) engine: String,
    #[serde(default)]
    pub(crate) model: String,
    #[serde(default)]
    pub(crate) effort: String,
    #[serde(default)]
    pub(crate) context_window: String,
    #[serde(default)]
    pub(crate) permission_mode: String,
    #[serde(default)]
    pub(crate) workspace: String,
    #[serde(default)]
    pub(crate) continue_session: bool,
    #[serde(default)]
    pub(crate) extra_args: Vec<String>,
}

pub(crate) fn default_profiles() -> HashMap<String, TabProfile> {
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
# run_tab.sh <tab> — resolve a tab's CLI profile, launch it, relaunch it when it exits.
# This is the supervisor loop inside one tmux pane. Written by the daemon on every
# start (this constant is the only source); edit it in profile.rs, not in config/.
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

HAS_CONTINUE=0
for arg in "${CMDLINE[@]}"; do
    if [ "$arg" = "--continue" ] || [ "$arg" = "-c" ]; then
        HAS_CONTINUE=1
        break
    fi
done

// Exit 42 dari qoder berarti "no conversation found to continue": tidak ada sesi di
// direktori ini, biasanya karena workspace profil baru saja dipindah. Diturunkan
// percobaan demi percobaan, BUKAN ditulis ke cli_profiles.json — kalau dipersist, tab
// kehilangan resume selamanya walau sesi baru sudah ada beberapa detik kemudian.
if [ "$HAS_CONTINUE" -eq 1 ]; then
    "${CMDLINE[@]}"
    EXIT_CODE=$?
    if [ "$EXIT_CODE" -eq 42 ]; then
        NEW_CMD=()
        for arg in "${CMDLINE[@]}"; do
            [ "$arg" != "--continue" ] && [ "$arg" != "-c" ] && NEW_CMD+=("$arg")
        done
        printf '\n\033[1;33m[run_tab] No session to continue in this directory — starting a new one.\033[0m\n\n'
        exec "${NEW_CMD[@]}"
    fi
    exit "$EXIT_CODE"
else
    exec "${CMDLINE[@]}"
fi
"#;

pub(crate) fn ensure_config() -> HashMap<String, TabProfile> {
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

pub(crate) fn read_profiles() -> HashMap<String, TabProfile> {
    fs::read_to_string(profiles_path())
        .ok()
        .and_then(|raw| serde_json::from_str(&raw).ok())
        .unwrap_or_else(default_profiles)
}

// ---------------------------------------------------------------------------
