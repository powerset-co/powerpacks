//! The processes the app runs for its lifetime (the page server, Codex, setup), stopped when
//! the app exits so nothing of Powerpacks keeps running without its window.

use std::process::Child;
use std::sync::Mutex;

#[derive(Default)]
pub struct Children {
    running: Mutex<Vec<Child>>,
}

impl Children {
    pub fn adopt(&self, child: Child) {
        self.running.lock().expect("children").push(child);
    }

    pub fn stop_all(&self) {
        for mut child in self.running.lock().expect("children").drain(..) {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Stop a process by id (one this app did not spawn, such as a page left from an earlier run).
pub fn stop_pid(pid: u32) {
    if pid == 0 || pid == std::process::id() {
        return;
    }
    #[cfg(unix)]
    let _ = std::process::Command::new("kill")
        .args(["-TERM", &pid.to_string()])
        .status();
    #[cfg(windows)]
    let _ = std::process::Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/F"])
        .status();
}

/// Whether a process with this id exists.
pub fn is_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(unix)]
    {
        std::process::Command::new("kill")
            .args(["-0", &pid.to_string()])
            .status()
            .map(|status| status.success())
            .unwrap_or(false)
    }
    #[cfg(windows)]
    {
        std::process::Command::new("tasklist")
            .args(["/FI", &format!("PID eq {pid}"), "/NH"])
            .output()
            .map(|output| String::from_utf8_lossy(&output.stdout).contains(&pid.to_string()))
            .unwrap_or(false)
    }
}
