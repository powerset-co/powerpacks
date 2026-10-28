//! The Powerpacks code the app ships (`resources/powerpacks.tar.gz`, from
//! scripts/bundle-runtime.sh), installed as the checkout every Powerpacks tool expects: code,
//! `.env` and `.powerpacks/` in one folder.
//!
//! A git checkout (only reached through POWERPACKS_REPO_ROOT) belongs to its owner and is used
//! as it is. The app's own folder is refreshed when the app's version changes: the archive's
//! top-level entries are replaced and `.env`, `.powerpacks/` and `.venv/` (never in the
//! archive) stay.

use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::paths;

pub const ARCHIVE_RESOURCE: &str = "powerpacks.tar.gz";
/// The commit the archive was made from (scripts/bundle-runtime.sh); the folder refreshes when
/// it changes, so every build counts, not only a new app version.
pub const VERSION_RESOURCE: &str = "powerpacks.version";
const MARKER: &str = ".powerpacks/desktop/source-version";
const ENV_TEMPLATE: &str = "packs/powerset/templates/env.powerset.example";

pub enum Plan {
    /// The installed code is current, or a git checkout owns it.
    Ready,
    /// Install (or refresh) the bundled code.
    Install,
}

/// What to do with `root` for this app version; an unrelated non-empty folder is an error.
pub fn plan(root: &Path, version: &str) -> Result<Plan, String> {
    if root.join(".git").exists() {
        return Ok(Plan::Ready);
    }
    if let Ok(installed) = fs::read_to_string(root.join(MARKER)) {
        return Ok(if installed.trim() == version {
            Plan::Ready
        } else {
            Plan::Install
        });
    }
    let empty = fs::read_dir(root)
        .map(|mut entries| entries.next().is_none())
        .unwrap_or(true);
    if empty || root.join("bin/onboard").is_file() {
        return Ok(Plan::Install);
    }
    Err(format!(
        "{} already holds other files. Move them, or set POWERPACKS_REPO_ROOT to another folder.",
        root.display()
    ))
}

pub fn install(archive: &Path, root: &Path, version: &str) -> Result<(), String> {
    fs::create_dir_all(root)
        .map_err(|error| format!("Could not create {}: {error}", root.display()))?;
    for entry in top_level_entries(archive)? {
        let path = root.join(&entry);
        let Ok(metadata) = fs::symlink_metadata(&path) else {
            continue;
        };
        let removed = if metadata.is_dir() {
            fs::remove_dir_all(&path)
        } else {
            fs::remove_file(&path)
        };
        removed.map_err(|error| format!("Could not replace {}: {error}", path.display()))?;
    }
    tar(&[
        OsArg::from("-xzf"),
        archive.into(),
        "-C".into(),
        root.into(),
    ])?;
    write_marker(root, version)
}

/// The default command-line install's folder; bootstrap also reuses `~/workspace/powerpacks`.
const CLI_INSTALL: &str = "powerpacks";
const DATA_DIR: &str = ".powerpacks";
/// Deep Context's store, also initialized empty by the Sets page before import.
const NETWORK_STORE: &str = ".powerpacks/deep-context/deep-context-v2.sqlite";

/// The command-line install with data on this machine, if any: the setup page offers to
/// import it instead of starting over.
pub fn cli_install(root: &Path) -> Option<PathBuf> {
    cli_install_in(&dirs::home_dir()?, root)
}

fn cli_install_in(home: &Path, root: &Path) -> Option<PathBuf> {
    [CLI_INSTALL, "workspace/powerpacks"]
        .into_iter()
        .map(|path| home.join(path))
        .filter(|cli| {
            let installer = cli.join("install.sh");
            #[cfg(unix)]
            let executable = {
                use std::os::unix::fs::PermissionsExt;
                installer.metadata().is_ok_and(|metadata| {
                    metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
                })
            };
            #[cfg(not(unix))]
            let executable = installer.is_file();
            cli.as_path() != root
                && executable
                && cli.join("packs").is_dir()
                && cli.join(DATA_DIR).join("install").is_dir()
        })
        .min_by_key(|cli| !cli.join(NETWORK_STORE).is_file())
}

/// Share that install's data folder (a link, so both see the same files: the network, imports,
/// searches and account) and take over its `.env`. The app's own data folder, holding only
/// what this launch recorded, is backed up; one with a network or sets of its own is kept, and
/// setup goes on to Connect as after an import.
pub fn import_cli_data(root: &Path, cli: &Path) -> Result<(), String> {
    if root.join(NETWORK_STORE).is_file() {
        const PROBE: &str = r#"
import sqlite3, sys
from pathlib import Path
with sqlite3.connect(Path(sys.argv[1]).resolve().as_uri() + '?mode=ro', uri=True) as conn:
    tables = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name <> 'meta' AND name NOT LIKE 'sqlite_%'").fetchall()
    sys.exit(2 if any(conn.execute('SELECT 1 FROM "' + name.replace('"', '""') + '" LIMIT 1').fetchone() for (name,) in tables) else 0)
"#;
        let status = Command::new(paths::project_python(root))
            .args(["-c", PROBE])
            .arg(root.join(NETWORK_STORE))
            .status()
            .map_err(|error| format!("Could not inspect this app's data: {error}"))?;
        match status.code() {
            Some(0) => {}
            Some(2) => return Ok(()),
            _ => return Err("Could not inspect this app's data; it was kept.".into()),
        }
    }
    let data = root.join(DATA_DIR);
    let version = fs::read_to_string(root.join(MARKER)).ok();
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|error| error.to_string())?
        .as_nanos();
    if root.join(".env").is_file() && cli.join(".env").is_file() {
        fs::copy(
            root.join(".env"),
            root.join(format!(".env.{timestamp}.bkup")),
        )
        .map_err(|error| format!("Could not keep .env: {error}"))?;
    }
    if data.exists() {
        let backup = root.join(format!("{DATA_DIR}.{timestamp}.bkup"));
        fs::rename(&data, &backup)
            .map_err(|error| format!("Could not keep {}: {error}", data.display()))?;
    }
    link_dir(&cli.join(DATA_DIR), &data)
        .map_err(|error| format!("Could not share {}: {error}", cli.join(DATA_DIR).display()))?;
    if let Some(version) = version {
        write_marker(root, version.trim())?;
    }
    if cli.join(".env").is_file() {
        fs::copy(cli.join(".env"), root.join(".env"))
            .map_err(|error| format!("Could not copy .env: {error}"))?;
    }
    Ok(())
}

/// True once `import_cli_data` linked a command-line install's data folder here: its setup is that
/// install's, finished (it has a network), so the app runs none of its own.
pub fn imported(root: &Path) -> bool {
    fs::symlink_metadata(root.join(DATA_DIR)).is_ok_and(|metadata| metadata.is_symlink())
}

/// Forget everything the app holds (debug menu): its data folder, or the link to the
/// command-line install's (that install keeps its data), and `.env`. The code stays.
pub fn reset_data(root: &Path, version: &str) -> Result<(), String> {
    remove_data(&root.join(DATA_DIR))?;
    match fs::remove_file(root.join(".env")) {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => {
            return Err(format!("Could not remove .env: {error}"))
        }
        _ => {}
    }
    write_marker(root, version)
}

fn remove_data(data: &Path) -> Result<(), String> {
    let Ok(metadata) = fs::symlink_metadata(data) else {
        return Ok(());
    };
    let removed = if metadata.is_symlink() {
        if cfg!(windows) {
            fs::remove_dir(data)
        } else {
            fs::remove_file(data)
        }
    } else {
        fs::remove_dir_all(data)
    };
    removed.map_err(|error| format!("Could not remove {}: {error}", data.display()))
}

fn write_marker(root: &Path, version: &str) -> Result<(), String> {
    let marker = root.join(MARKER);
    fs::create_dir_all(marker.parent().expect("marker has a parent"))
        .map_err(|error| error.to_string())?;
    fs::write(&marker, version).map_err(|error| error.to_string())
}

#[cfg(unix)]
fn link_dir(target: &Path, link: &Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(windows)]
fn link_dir(target: &Path, link: &Path) -> std::io::Result<()> {
    // A junction needs no privilege, unlike a symbolic link.
    let status = Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(link)
        .arg(target)
        .status()?;
    if status.success() {
        Ok(())
    } else {
        Err(std::io::Error::other("mklink failed"))
    }
}

/// The installer's stamp in ~/.agents/skills, which the command-line install writes too.
const SKILLS_STAMP: &str = ".powerpacks-install.json";

/// Codex reads skills from ~/.agents/skills, the one place the command-line install fills too.
/// The app puts a link there for each bundled skill, into the code it just installed, so a
/// skill is always this version's; a copy or an older link with the same name is replaced.
pub fn link_skills(root: &Path, home: &Path, version: &str) -> Result<(), String> {
    let skills = home.join(".agents/skills");
    fs::create_dir_all(&skills)
        .map_err(|error| format!("Could not create {}: {error}", skills.display()))?;
    for skill in bundled_skills(root)? {
        let name = skill.file_name().ok_or("a skill without a name")?;
        let link = skills.join(name);
        if fs::symlink_metadata(&link).is_ok() {
            remove_any(&link)?;
        }
        link_dir(&skill, &link)
            .map_err(|error| format!("Could not link {}: {error}", link.display()))?;
    }
    let stamp = format!(
        "{{\n  \"package\": \"powerpacks\",\n  \"version\": \"{version}\",\n  \"harness\": \"app\",\n  \"repo_root\": \"{}\"\n}}\n",
        root.display()
    );
    fs::write(skills.join(SKILLS_STAMP), stamp).map_err(|error| error.to_string())
}

/// Every `packs/<pack>/skills/<name>` holding a SKILL.md.
fn bundled_skills(root: &Path) -> Result<Vec<PathBuf>, String> {
    let mut found = Vec::new();
    let packs = root.join("packs");
    for pack in fs::read_dir(&packs)
        .map_err(|error| format!("Could not read {}: {error}", packs.display()))?
    {
        let skills = pack
            .map_err(|error| error.to_string())?
            .path()
            .join("skills");
        let Ok(entries) = fs::read_dir(&skills) else {
            continue;
        };
        for entry in entries {
            let path = entry.map_err(|error| error.to_string())?.path();
            if path.join("SKILL.md").is_file() {
                found.push(path);
            }
        }
    }
    found.sort();
    Ok(found)
}

fn remove_any(path: &Path) -> Result<(), String> {
    let metadata = fs::symlink_metadata(path).map_err(|error| error.to_string())?;
    let removed = if metadata.is_dir() {
        fs::remove_dir_all(path)
    } else {
        fs::remove_file(path)
    };
    removed.map_err(|error| format!("Could not remove {}: {error}", path.display()))
}

/// `.env` from the Powerset template, as bootstrap writes it, readable only by the user.
pub fn ensure_env(root: &Path) -> Result<(), String> {
    let env = root.join(".env");
    if env.exists() {
        return Ok(());
    }
    fs::copy(root.join(ENV_TEMPLATE), &env)
        .map_err(|error| format!("Could not write .env: {error}"))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(&env, fs::Permissions::from_mode(0o600))
            .map_err(|error| error.to_string())?;
    }
    Ok(())
}

type OsArg = std::ffi::OsString;

fn tar(args: &[OsArg]) -> Result<String, String> {
    let output = Command::new("tar")
        .args(args)
        .output()
        .map_err(|error| format!("Could not run tar: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "Unpacking Powerpacks failed: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn top_level_entries(archive: &Path) -> Result<Vec<PathBuf>, String> {
    let listing = tar(&["-tzf".into(), archive.into()])?;
    let mut entries: Vec<PathBuf> = listing
        .lines()
        .filter_map(|line| {
            line.split('/')
                .find(|part| !part.is_empty() && *part != ".")
        })
        .map(PathBuf::from)
        .collect();
    entries.sort();
    entries.dedup();
    Ok(entries)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::paths;

    #[test]
    fn the_app_links_its_skills_into_agents_skills_over_any_copy() {
        let tmp = std::env::temp_dir().join(format!("powerpacks-skills-{}", std::process::id()));
        let _ = fs::remove_dir_all(&tmp);
        let root = tmp.join("app");
        let home = tmp.join("home");
        for (pack, name) in [("search", "search"), ("ingestion", "deep-context")] {
            let skill = root.join("packs").join(pack).join("skills").join(name);
            fs::create_dir_all(&skill).unwrap();
            fs::write(skill.join("SKILL.md"), "---\nname: x\n---\n").unwrap();
        }
        fs::create_dir_all(root.join("packs/memory/skills/memory")).unwrap(); // no SKILL.md: not a skill
        let skills = home.join(".agents/skills");
        fs::create_dir_all(skills.join("search")).unwrap();
        fs::write(skills.join("search/SKILL.md"), "stale copy").unwrap();
        fs::create_dir_all(skills.join("ship")).unwrap(); // the user's own, untouched

        link_skills(&root, &home, "3.46.0").unwrap();
        link_skills(&root, &home, "3.46.0").unwrap(); // a second launch replaces the links, nothing else

        assert_eq!(
            fs::read_link(skills.join("search")).unwrap(),
            root.join("packs/search/skills/search")
        );
        assert!(skills.join("deep-context/SKILL.md").is_file());
        assert!(!skills.join("memory").exists());
        assert!(skills.join("ship").is_dir());
        assert!(fs::read_to_string(skills.join(SKILLS_STAMP))
            .unwrap()
            .contains("3.46.0"));
        let _ = fs::remove_dir_all(&tmp);
    }

    #[test]
    fn finds_importable_supported_checkouts_in_order_without_the_runtime_root() {
        use std::os::unix::fs::PermissionsExt;

        let home = std::env::temp_dir().join(format!(
            "powerpacks-cli-discovery-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let runtime = home.join(".powerpacks/app");
        let default = home.join("powerpacks");
        let workspace = home.join("workspace/powerpacks");
        let checkout = |path: &Path, with_data: bool| {
            fs::create_dir_all(path.join("packs")).unwrap();
            fs::write(path.join("install.sh"), "#!/bin/sh\n").unwrap();
            fs::set_permissions(path.join("install.sh"), fs::Permissions::from_mode(0o755))
                .unwrap();
            if with_data {
                fs::create_dir_all(path.join(DATA_DIR).join("install")).unwrap();
            }
        };

        assert_eq!(cli_install_in(&home, &runtime), None);
        checkout(&workspace, true);
        assert_eq!(cli_install_in(&home, &runtime), Some(workspace.clone()));
        checkout(&default, false);
        assert_eq!(cli_install_in(&home, &runtime), Some(workspace.clone()));
        checkout(&default, true);
        assert_eq!(cli_install_in(&home, &runtime), Some(default.clone()));
        assert_eq!(cli_install_in(&home, &default), Some(workspace.clone()));
        fs::create_dir_all(workspace.join(NETWORK_STORE).parent().unwrap()).unwrap();
        fs::write(workspace.join(NETWORK_STORE), "synthetic").unwrap();
        assert_eq!(cli_install_in(&home, &runtime), Some(workspace.clone()));
        fs::set_permissions(
            default.join("install.sh"),
            fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        assert_eq!(cli_install_in(&home, &runtime), Some(workspace.clone()));
        assert_eq!(cli_install_in(&home, &workspace), None);
        checkout(&home.join("workspace/unrelated-worktree"), true);
        assert_eq!(cli_install_in(&home, &workspace), None);
        fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn import_accepts_empty_schema_but_preserves_candidates_and_sets() {
        let repo = Path::new(env!("CARGO_MANIFEST_DIR")).join("../..");
        let python = paths::project_python(&repo).canonicalize().unwrap();
        let temporary = std::env::temp_dir().join(format!(
            "powerpacks-import-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        for fixture in ["empty", "candidates", "sets"] {
            let root = temporary.join(fixture);
            let cli = temporary.join(format!("cli-{fixture}"));
            fs::create_dir_all(root.join(".venv/bin")).unwrap();
            std::os::unix::fs::symlink(&python, paths::project_python(&root)).unwrap();
            fs::create_dir_all(cli.join(DATA_DIR)).unwrap();
            fs::write(cli.join(DATA_DIR).join("synthetic.txt"), "imported").unwrap();
            fs::write(cli.join(".env"), "SYNTHETIC=imported\n").unwrap();
            let held_env =
                "SYNTHETIC=held\nPOWERPACKS_CREDENTIALS_PATH=/synthetic/native credentials.json\n";
            fs::write(root.join(".env"), held_env).unwrap();
            write_marker(&root, "synthetic-version").unwrap();
            fs::create_dir_all(root.join(DATA_DIR).join("inbox")).unwrap();
            let invite = "{\"kind\":\"set_invite\",\"id\":\"synthetic\"}";
            fs::write(root.join(DATA_DIR).join("inbox/synthetic.json"), invite).unwrap();
            let made = Command::new(&python)
                .current_dir(&repo)
                .args(["-c", r#"
import sys
from pathlib import Path
from packs.ingestion.primitives.deep_context_v2.db.store import open_store
with open_store(Path(sys.argv[1])) as conn:
    if sys.argv[2] == 'candidates':
        conn.execute("INSERT INTO candidates VALUES ('synthetic', 'Casey', 0, '{}', '2026-10-12')")
    if sys.argv[2] == 'sets':
        conn.execute("INSERT INTO sets VALUES ('synthetic', 'Recruiting', 'owner', '[]', '2026-10-12')")
"#])
                .arg(root.join(NETWORK_STORE))
                .arg(fixture)
                .status()
                .unwrap();
            assert!(made.success());
            let stored = fs::read(root.join(NETWORK_STORE)).unwrap();
            let result = import_cli_data(&root, &cli);
            if fixture == "empty" {
                result.unwrap();
                assert!(fs::symlink_metadata(root.join(DATA_DIR))
                    .unwrap()
                    .is_symlink());
                assert_eq!(
                    fs::read_to_string(root.join(DATA_DIR).join("synthetic.txt")).unwrap(),
                    "imported"
                );
                assert_eq!(
                    fs::read_to_string(root.join(".env")).unwrap(),
                    "SYNTHETIC=imported\n"
                );
                assert_eq!(
                    fs::read_to_string(root.join(MARKER)).unwrap(),
                    "synthetic-version"
                );
                let backup = fs::read_dir(&root)
                    .unwrap()
                    .map(|entry| entry.unwrap().path())
                    .find(|path| path.is_dir() && path.to_string_lossy().ends_with(".bkup"))
                    .unwrap();
                let suffix = backup
                    .file_name()
                    .unwrap()
                    .to_str()
                    .unwrap()
                    .strip_prefix(DATA_DIR)
                    .unwrap();
                assert_eq!(
                    fs::read_to_string(root.join(format!(".env{suffix}"))).unwrap(),
                    held_env
                );
                assert_eq!(
                    fs::read_to_string(backup.join("inbox/synthetic.json")).unwrap(),
                    invite
                );
                assert_eq!(
                    fs::read(backup.join("deep-context/deep-context-v2.sqlite")).unwrap(),
                    stored
                );
            } else {
                result.unwrap();
                assert_eq!(fs::read(root.join(NETWORK_STORE)).unwrap(), stored);
                assert_eq!(fs::read_to_string(root.join(".env")).unwrap(), held_env);
                assert!(!fs::symlink_metadata(root.join(DATA_DIR))
                    .unwrap()
                    .is_symlink());
                assert_eq!(
                    fs::read_to_string(root.join(DATA_DIR).join("inbox/synthetic.json")).unwrap(),
                    invite
                );
            }
        }
        fs::remove_dir_all(temporary).unwrap();
    }
}
