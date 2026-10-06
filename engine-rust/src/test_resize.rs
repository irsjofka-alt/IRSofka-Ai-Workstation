
use portable_pty::{NativePtySystem, PtySystem, PtySize, CommandBuilder, MasterPty};
use std::sync::{Arc, Mutex};

fn test_resize() {
    let pty_system = NativePtySystem::default();
    let pair = pty_system.openpty(PtySize::default()).unwrap();
    let master: Arc<Mutex<Box<dyn MasterPty + Send>>> = Arc::new(Mutex::new(pair.master));
    master.lock().unwrap().resize(PtySize { rows: 40, cols: 140, pixel_width: 0, pixel_height: 0 }).unwrap();
}
fn main() { test_resize(); }
