//! Scratch probe for PTY window resizing — `cargo run --example resize_probe`.
//!
//! Not part of the daemon. Kept because resize is the one portable-pty call that
//! tmux hides: when the GUI pane stops forwarding SIGWINCH the CLI renders at the
//! old width, and this proves whether the pty layer itself is innocent.
use portable_pty::{MasterPty, NativePtySystem, PtySize, PtySystem};
use std::sync::{Arc, Mutex};

fn main() {
    let pty_system = NativePtySystem::default();
    let pair = pty_system.openpty(PtySize::default()).unwrap();
    let master: Arc<Mutex<Box<dyn MasterPty + Send>>> = Arc::new(Mutex::new(pair.master));
    master
        .lock()
        .unwrap()
        .resize(PtySize { rows: 44, cols: 132, pixel_width: 0, pixel_height: 0 })
        .unwrap();
    println!("pty resized to 44x132 without error");
}
