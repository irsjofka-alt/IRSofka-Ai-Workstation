//! Save-state workstation: PostgreSQL primer, SQLite cadangan.
//!
//! GUI cuma butuh satu hal: berapa banyak memori, quest, skill, insiden, dan turn yang
//! tersimpan. Jalur Postgres dipakai selama bisa; kalau tidak, pertanyaan yang sama
//! dijawab dari berkas SQLite. Keduanya wajib menghasilkan bentuk JSON yang identik
//! supaya /api/stats tidak perlu tahu sedang jatuh di engine mana.
use serde_json::{json, Value};
use std::fs;

use crate::paths::{config_dir, station_dir};
use crate::probe::py_json;

/// Rangkaian koneksi PostgreSQL: bawaan -> config/db_local.json -> variabel lingkungan.
///
/// Password dulu tertanam sebagai konstan waktu-kompilasi di berkas ini. Karena repo
/// workstation kini public, kredensial dipindah ke config/db_local.json yang di-ignore git;
/// hasil clone bersih gagal terhubung secara terang-terangan, bukan ikut membawa password.
pub(crate) fn pg_conn_str() -> String {
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
    // Default-nya nama user OS saat ini, bukan nama orang tertentu: konstan "irsofka"
    // ikut ter-compile ke setiap hasil clone dan membuat mesin lain mencoba login
    // dengan akun yang bukan punyanya.
    let os_user = std::env::var("USER").unwrap_or_default();
    format!(
        "host={} port={} user={} password={} dbname={}",
        pick("host", "STATION_PG_HOST", "localhost"),
        pick("port", "STATION_PG_PORT", "5432"),
        pick("user", "STATION_PG_USER", &os_user),
        pick("password", "STATION_PG_PASSWORD", ""),
        pick("dbname", "STATION_PG_DB", "irsofka_ai_workstation"),
    )
}

#[derive(Default)]
pub(crate) struct SaveState {
    pub(crate) engine: String,
    pub(crate) skills: i64,
    pub(crate) quests: i64,
    pub(crate) memories: i64,
    pub(crate) incidents: i64,
    pub(crate) turns: i64,
    pub(crate) player: Value,
}

pub(crate) async fn load_save_state() -> SaveState {
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

pub(crate) async fn count(client: &tokio_postgres::Client, table: &str) -> i64 {
    // Nama tabel tidak bisa diparameterisasi — ia identifier, bukan nilai. Dibatasi ke
    // pola yang modul ini kenal, supaya pemanggil berikutnya tidak pernah bisa
    // meneruskan input user ke dalam SQL.
    if table.is_empty()
        || !table.chars().all(|c| c.is_ascii_lowercase() || c == '_')
    {
        return 0;
    }
    client
        .query_one(&format!("SELECT COUNT(*) FROM {};", table), &[])
        .await
        .map(|r| r.get(0))
        .unwrap_or(0)
}

pub(crate) async fn player_profile(client: &tokio_postgres::Client) -> Value {
    // Kolom username sengaja tidak dikeluarkan: GUI tidak membacanya (dicari di gui.html,
    // nol hasil), sedangkan tiap engine di tab bisa membaca endpoint ini lewat shell-nya.
    // Nama lengkap pemilik mesin tidak ada alasan melayani diri sendiri di API.
    let sql = "SELECT level, exp_points, active_title FROM player_profile LIMIT 1";
    match client.query_opt(sql, &[]).await {
        Ok(Some(row)) => json!({
            "level": row.get::<_, i32>(0),
            "exp": row.get::<_, i32>(1),
            "title": row.get::<_, String>(2),
        }),
        _ => Value::Null,
    }
}

pub(crate) fn count_files(dir: &std::path::Path, ext: &str) -> i64 {
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

pub(crate) fn count_skill_files() -> i64 {
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
