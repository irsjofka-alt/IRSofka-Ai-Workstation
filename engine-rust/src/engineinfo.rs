//! Apa yang engine CLI tahu tentang dirinya: daftar model, effort, versi, akun, pemakaian.
//!
//! Semuanya dipungut dari CLI itu sendiri (`qoder --list-models`, `agy models`,
//! `qoder status`, state Antigravity) — tidak ada nama model yang ditulis tangan di
//! workstation ini. Yang hardcode hanya daftar effort, dan itu memang tabel milik CLI
//! yang tidak disediakan lewat perintah apa pun.
use serde_json::{json, Value};
use std::fs;
use std::time::Duration;

use crate::paths::home_dir;
use crate::probe::{cached_json, py_json, run_capture};

pub(crate) fn qoder_models() -> Vec<Value> {
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

pub(crate) fn agy_models() -> Vec<Value> {
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

pub(crate) fn qoder_efforts() -> Vec<&'static str> {
    vec!["xhigh", "high", "medium", "low", "auto"]
}

pub(crate) fn agy_efforts() -> Vec<&'static str> {
    vec!["max", "xhigh", "high", "medium", "low"]
}

/// `qoder status -o json` membawa identitas pemilik akun. Daemon ini hanya listen di
/// 127.0.0.1, tapi yang membaca API-nya bukan cuma manusia — tiap engine di tab ini punya
/// shell dan bisa `curl localhost:8999`. Email dan nama lengkap yang masuk ke konteks model
/// cloud tidak bisa ditarik kembali, jadi dipangkas di sumbernya, bukan di tiap pemakai.
pub(crate) fn redact_account(value: Value) -> Value {
    const SENSITIVE: [&str; 5] = ["email", "username", "avatar_url", "user_id", "name"];
    let mut v = value;
    if let Some(map) = v.as_object_mut() {
        for key in SENSITIVE {
            map.remove(key);
        }
        // Kunci apa pun yang nilainya tampak seperti alamat email, walau namanya tidak
        // ada di daftar — API pihak ketiga suka mengganti bentuknya.
        let looks_like_mail: Vec<String> = map
            .iter()
            .filter(|(_, val)| {
                val.as_str()
                    .is_some_and(|s| s.contains('@') && s.contains('.'))
            })
            .map(|(k, _)| k.clone())
            .collect();
        for key in looks_like_mail {
            map.remove(&key);
        }
        map.insert("identity_redacted".to_string(), json!(true));
    }
    v
}

pub(crate) fn qoder_account() -> Value {
    cached_json("qoder_account", Duration::from_secs(60), || {
        run_capture("qoder", &["status", "-o", "json"])
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .map(redact_account)
            .unwrap_or_else(|| json!({ "logged_in": false }))
    })
}

pub(crate) fn cli_version(bin: &str) -> Value {
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
pub(crate) fn qoder_session_usage(workspace: &str) -> Value {
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

pub(crate) fn antigravity_usage() -> Value {
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

pub(crate) fn antigravity_last_model() -> String {
    fs::read_to_string(home_dir().join(".gemini/antigravity/antigravity_state.pbtxt"))
        .ok()
        .and_then(|raw| {
            raw.lines()
                .find(|l| l.trim_start().starts_with("last_selected_agent_model:"))
                .map(|l| l.split(':').nth(1).unwrap_or("").trim().to_string())
        })
        .unwrap_or_default()
}
