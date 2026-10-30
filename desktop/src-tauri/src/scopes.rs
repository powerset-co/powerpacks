//! Each chat's search scope, by thread id: the set or local index its Search-in picker named when
//! the chat started. One JSON file under the data folder; a chat not in it has no scope.

use std::fs;
use std::path::{Path, PathBuf};

use serde_json::{json, Value};

const FILE: &str = ".powerpacks/desktop/chat-scopes.json";

fn path(root: &Path) -> PathBuf {
    root.join(FILE)
}

fn all(root: &Path) -> Value {
    fs::read_to_string(path(root))
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .filter(Value::is_object)
        .unwrap_or_else(|| json!({}))
}

/// The chat's scope, or null for a chat started before scopes.
pub fn read(root: &Path, thread_id: &str) -> Value {
    all(root).get(thread_id).cloned().unwrap_or(Value::Null)
}

pub fn write(root: &Path, thread_id: &str, scope: &Value) -> Result<(), String> {
    let mut scopes = all(root);
    scopes[thread_id] = scope.clone();
    let file = path(root);
    if let Some(parent) = file.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("Could not create {}: {error}", parent.display()))?;
    }
    fs::write(
        &file,
        serde_json::to_string_pretty(&scopes).unwrap_or_default(),
    )
    .map_err(|error| format!("Could not keep the chat's scope: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn keeps_each_chat_scope_and_answers_null_for_an_unknown_chat() {
        let root = std::env::temp_dir().join(format!("scopes-{}", std::process::id()));
        write(&root, "t1", &json!({ "local_db": "/idx.duckdb" })).unwrap();
        write(&root, "t2", &json!({ "set_id": "s-1" })).unwrap();
        assert_eq!(read(&root, "t1"), json!({ "local_db": "/idx.duckdb" }));
        assert_eq!(read(&root, "t2"), json!({ "set_id": "s-1" }));
        assert_eq!(read(&root, "t3"), Value::Null);
        fs::remove_dir_all(root).unwrap();
    }
}
