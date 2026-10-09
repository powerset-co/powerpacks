// The app's commands get permissions (`allow-<command>`), so capabilities/default.json can
// grant them to the local page, which Tauri treats as a remote origin.
const COMMANDS: &[&str] = &[
    "boot_state",
    "boot_retry",
    "codex_status",
    "codex_login",
    "codex_start_thread",
    "codex_open_thread",
    "codex_threads",
    "codex_call",
    "codex_respond",
    "signin_open",
    "signin_place",
    "signin_close",
    "linkedin_read",
    "app_focus",
    "open_external",
    "update_check",
    "onboard_continue",
    "permission_messages",
    "debug_state",
    "debug_skip_setup",
    "debug_reset_data",
    "setup_import_source",
    "setup_import",
];

fn main() {
    tauri_build::try_build(
        tauri_build::Attributes::new()
            .app_manifest(tauri_build::AppManifest::new().commands(COMMANDS)),
    )
    .expect("tauri build");
}
