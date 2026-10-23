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

/// Stop every process whose command line matches `pattern`: what an earlier run of this app
/// started and never got to stop (a crash, a force quit, an update mid-request). A Codex
/// app-server left this way keeps the chats' writer locks, and the next run cannot open them.
pub fn stop_matching(pattern: &str) {
    #[cfg(unix)]
    {
        let Ok(found) = std::process::Command::new("pgrep").args(["-f", pattern]).output() else {
            return;
        };
        let me = std::process::id();
        let pids: Vec<u32> = String::from_utf8_lossy(&found.stdout)
            .lines()
            .filter_map(|line| line.trim().parse().ok())
            .filter(|pid| *pid != me)
            .collect();
        for pid in &pids {
            stop_pid(*pid);
        }
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(2);
        while std::time::Instant::now() < deadline && pids.iter().any(|pid| is_alive(*pid)) {
            std::thread::sleep(std::time::Duration::from_millis(100));
        }
        for pid in pids.iter().filter(|pid| is_alive(**pid)) {
            let _ = std::process::Command::new("kill")
                .args(["-KILL", &pid.to_string()])
                .status();
        }
    }
    #[cfg(windows)]
    let _ = pattern;
}
