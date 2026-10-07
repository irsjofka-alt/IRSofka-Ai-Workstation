//! Introspeksi `/proc`: siapa proses ini, anak siapa, masih hidup atau zombie.
//!
//! Semua fungsi di sini murni membaca filesystem `/proc` dan tidak mengenal state
//! aplikasi — justru karena itu ia layak berdiri sendiri: bagian tersulit dari
//! `main.rs` adalah fungsi yang menggabungkan keduanya.
//!
//! Catatan yang berulang kali menyelamatkan workstation ini: proses zombie masih
//! punya `/proc/<pid>` tetapi tidak punya `cwd` maupun `cmdline`. Melaporkannya
//! sebagai "hidup" adalah sumber pelaporan workspace yang ngaco.

use std::collections::HashMap;
use std::fs;

pub(crate) fn proc_ppid(pid: u32) -> Option<u32> {
    let stat = fs::read_to_string(format!("/proc/{}/stat", pid)).ok()?;
    let rest = stat.rsplit_once(") ")?.1;
    rest.split_whitespace().nth(1)?.parse().ok()
}

pub(crate) fn proc_comm(pid: u32) -> String {
    fs::read_to_string(format!("/proc/{}/comm", pid))
        .map(|s| s.trim().to_string())
        .unwrap_or_default()
}

/// State proses dari /proc/<pid>/stat (huruf setelah ')' karena comm boleh berisi spasi).
pub(crate) fn proc_state(pid: u32) -> Option<char> {
    let stat = fs::read_to_string(format!("/proc/{}/stat", pid)).ok()?;
    stat.rsplit_once(") ")?.1.split_whitespace().next()?.chars().next()
}

pub(crate) fn proc_alive(pid: u32) -> bool {
    matches!(proc_state(pid), Some(c) if c != 'Z')
}

pub(crate) fn proc_cmdline(pid: u32) -> String {
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

pub(crate) fn proc_cwd(pid: u32) -> Option<String> {
    fs::read_link(format!("/proc/{}/cwd", pid))
        .ok()
        .map(|p| p.to_string_lossy().to_string())
}

pub(crate) fn all_pids() -> Vec<u32> {
    fs::read_dir("/proc")
        .map(|rd| {
            rd.filter_map(|e| e.ok().and_then(|e| e.file_name().to_string_lossy().parse::<u32>().ok()))
                .collect()
        })
        .unwrap_or_default()
}

pub(crate) fn descendants(root: u32) -> Vec<u32> {
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
pub(crate) struct LiveProc {
    pub pid: u32,
    pub comm: String,
    pub cwd: String,
    pub cmdline: String,
}
