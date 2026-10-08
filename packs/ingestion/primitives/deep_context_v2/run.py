"""The deep-context pipeline end to end, in one command, with nothing to approve.

`run` goes load -> collect -> synthesize -> dedupe -> worth -> enrich, prints each paid stage's
estimate and runs it (the install's spend rule always says yes, so nothing stops to ask). Then
realize writes people.csv from everything decided so far and the index is built on Modal in the
background (the install's build; people.csv goes to the operator's folder on the team volume), so the
user can search what was found without reviewing first;
the review server starts and its URL is printed: the review page is the one thing the user sees.
`finish` is for after the review: realize and the index again (the index caches, so the second
build costs about nothing). The page stays up until `stop`; `review` starts it again.

Every stage keys its work, so a rerun after a failure or a code fix continues from what is
stored and spends nothing on what is done. A stage that fails ends the run with its error on
stdout and exit code 1; the agent fixes what it can and runs the same command again.

A v1 store beside the v2 one is archived first: deep-context.sqlite and v1's folders go into
deep-context-v1-<utc>.tar.gz under the data root and are removed, so the folder holds one
pipeline's state. owner.json, the profile cache and the JEV caches stay: v2 reads them.

Created: 2026-10-07
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable

from packs.ingestion.primitives.common.paths import DEFAULT_MSGVAULT_DB
from packs.ingestion.primitives.deep_context_v2.collect.collect import DEFAULT_LIMIT as COLLECT_LIMIT
from packs.ingestion.primitives.deep_context_v2.collect.collect import Collect
from packs.ingestion.primitives.deep_context_v2.db.store import now_iso, open_store, store_path
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import DEFAULT_LIMIT as DEDUPE_LIMIT
from packs.ingestion.primitives.deep_context_v2.dedupe.dedupe import Dedupe
from packs.ingestion.primitives.deep_context_v2.enrich.enrich import Enrich
from packs.ingestion.primitives.deep_context_v2.import_load.load import ImportLoad
from packs.ingestion.primitives.deep_context_v2.node import Manifest, Node
from packs.ingestion.primitives.deep_context_v2.openai import load_env
from packs.ingestion.primitives.deep_context_v2.realize.realize import Realize
from packs.ingestion.primitives.share.share_list import Share
from packs.shared.web.server import DEFAULT_PORT, start_server as start_page, stop_page
from packs.ingestion.primitives.deep_context_v2.synthesize.synthesize import DEFAULT_LIMIT as SYNTHESIZE_LIMIT
from packs.ingestion.primitives.deep_context_v2.synthesize.synthesize import Synthesize
from packs.ingestion.primitives.deep_context_v2.worth.worth import DEFAULT_LIMIT as WORTH_LIMIT
from packs.ingestion.primitives.deep_context_v2.worth.worth import Worth
from packs.ingestion.primitives.discover.messages.extract_imessage import DEFAULT_CHAT_DB

ROOT = Path(__file__).resolve().parents[4]
REVIEW_PID_FILE = Path("deep-context") / "review-server.pid"
INDEX_PID_FILE = Path("deep-context") / "index.pid"
INDEX_LOG_FILE = Path("deep-context") / "index.log"
MODAL_PIPELINE = Path("packs/indexing/modal/linkedin_modal_pipeline.py")
INDEX_MAX_USD = "499.99"  # the install's automatic spend rule: Modal refuses only above this
# v1's state under <data root>/deep-context, archived and removed before a v2 run. owner.json,
# identity/ (the JEV caches) and the v2 store are not in this list because v2 reads them.
V1_STATE = ("deep-context.sqlite", "deep-context.sqlite-wal", "deep-context.sqlite-shm", "deep_context.sqlite",
            "raw", "facts", "dossiers", "heal", "parents", "reconcile", "review", "profile-prefetch",
            "index.json", "index.md", "merge-candidates.csv", "merge-candidates.md", "merge-verdicts.csv",
            "owner_profile_lookup.json")
V1_STATE_GLOBS = ("deep-context.sqlite.*", "review-*.log", "*.message-linkedin.bkup")  # v1's .bkup copies and logs


def _print(stage: str, manifest: Manifest) -> None:
    counts: str = " ".join(f"{key}={value}" for key, value in manifest.counts.items())
    print(f"{stage}: {manifest.status} {counts} {manifest.error or ''}".rstrip(), flush=True)


def _stage(stage: str, node: Node) -> Manifest:
    """Estimate (when the node prices its work), run, print; a failed stage ends the run."""
    estimate = getattr(node, "estimate", None)
    if estimate is not None:
        print(f"{stage} estimate: {json.dumps(estimate())}", flush=True)
    manifest: Manifest = node.run()
    _print(stage, manifest)
    if manifest.status != "completed":
        raise SystemExit(1)
    return manifest


def archive_v1(data_root: Path) -> Path | None:
    """v1's store and folders into deep-context-v1-<utc>.tar.gz under the data root, then removed.
    Returns the archive, or None when there was no v1 store."""
    folder: Path = data_root / "deep-context"
    if not (folder / "deep-context.sqlite").exists():
        return None
    present: list[Path] = []
    for name in V1_STATE:
        if (folder / name).exists():
            present.append(folder / name)
    for pattern in V1_STATE_GLOBS:
        present.extend(folder.glob(pattern))
    stamp: str = now_iso().replace(":", "").replace("-", "")[:15]
    archive: Path = data_root / f"deep-context-v1-{stamp}.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for path in present:
            tar.add(path, arcname=str(path.relative_to(data_root)))
    with tarfile.open(archive, "r:gz") as tar:
        names: set[str] = set(tar.getnames())
    for path in present:
        if str(path.relative_to(data_root)) not in names:
            raise SystemExit(f"archive {archive} is missing {path}; nothing removed")
    for path in present:
        if path.is_dir():
            subprocess.run(["rm", "-rf", str(path)], check=True)
        else:
            path.unlink()
    print(f"archived v1 state: {archive} ({len(present)} paths)", flush=True)
    return archive


def stages(conn: sqlite3.Connection, data_root: Path, msgvault_db: Path = DEFAULT_MSGVAULT_DB,
           chat_db: Path = DEFAULT_CHAT_DB) -> list[tuple[str, Callable[[], Node]]]:
    """The stages before realize, in order, each built when it is about to run: a stage reads what the
    one before it stored (synthesize reads the owner load writes). `run` and the install
    (install/pipeline.py) both walk this list."""
    return [("load", lambda: ImportLoad(conn, data_root, msgvault_db=msgvault_db)),
            ("collect", lambda: Collect(conn, data_root, COLLECT_LIMIT, chat_db, msgvault_db=msgvault_db)),
            ("synthesize", lambda: Synthesize(conn, data_root, limit=SYNTHESIZE_LIMIT)),
            ("dedupe", lambda: Dedupe(conn, data_root, limit=DEDUPE_LIMIT)),
            ("worth", lambda: Worth(conn, data_root, limit=WORTH_LIMIT)),
            ("enrich", lambda: Enrich(conn, data_root, limit=None))]


# ---- the review server


def start_review(data_root: Path, port: int) -> str:
    """The one local page server on `port`, started fresh so it runs this checkout's code (a page left up
    across an update would keep serving the old review); returns the review URL. The server is the
    install's too: packs/shared/web/server.py."""
    stop_page("127.0.0.1", port)
    started: dict[str, object] = start_page(data_root.parent, port=port)
    (data_root / REVIEW_PID_FILE).write_text(str(started["pid"]))
    return str(started["url"])


def stop_review(data_root: Path) -> bool:
    """Stop the page server `run` or `review` started; True when one was running."""
    pid_file: Path = data_root / REVIEW_PID_FILE
    if not pid_file.exists():
        return False
    pid = int(pid_file.read_text().strip() or "0")
    pid_file.unlink()
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    return True


def pending_reviews(port: int) -> int:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/review/page", timeout=5) as response:
        return int(json.load(response)["progress"]["linkedin_pending"])


# ---- realize and the index


def realize(data_root: Path) -> Path:
    """people.csv from everything decided so far; returns its path."""
    conn: sqlite3.Connection = open_store(store_path(data_root))
    node = Realize(conn, data_root)
    _stage("realize", node)
    _stage("share", Share(conn, data_root))  # the share list over everything confirmed so far: free, seconds
    conn.close()
    print(f"people.csv: {node.people_csv()}", flush=True)
    return node.people_csv()


def index_command(data_root: Path, people_csv: Path) -> list[str]:
    """The index build on Modal, the same one the install runs: people.csv goes to the operator's folder
    on the team volume, the sandbox classifies and builds the DuckDB against the shared caches, and the
    result lands in search-index/. The operator id comes from POWERPACKS_OPERATOR_ID (.env, or
    --operator-id)."""
    return [sys.executable, str(MODAL_PIPELINE), "index-people", "--people-csv", str(people_csv),
            "--dest", str(data_root / "search-index"), "--max-usd", INDEX_MAX_USD]


def resolve_operator_id(flag: str) -> str:
    """The account's operator id, checked before anything runs: --operator-id, else POWERPACKS_OPERATOR_ID
    from .env (written by $powerset login). The Modal index files everything under it; without one the
    build would land under the placeholder operator, so the run stops here instead."""
    load_env()
    operator_id: str = flag or os.environ.get("POWERPACKS_OPERATOR_ID", "")
    if not operator_id or operator_id.startswith("00000000-"):
        raise SystemExit("deep-context: no operator id. Run `$powerset login` (it writes POWERPACKS_OPERATOR_ID "
                         "to .env) or pass --operator-id <id>.")
    return operator_id


def index_env(operator_id: str) -> dict[str, str]:
    """The index build's environment: the resolved operator id."""
    env: dict[str, str] = dict(os.environ)
    env["POWERPACKS_OPERATOR_ID"] = operator_id
    return env


def index_in_background(data_root: Path, people_csv: Path, operator_id: str) -> Path:
    """Start the index build detached, its output in deep-context/index.log; returns the log."""
    wait_for_index(data_root)
    log: Path = data_root / INDEX_LOG_FILE
    with open(log, "ab") as out:
        process = subprocess.Popen(index_command(data_root, people_csv), cwd=ROOT, env=index_env(operator_id),
                                   stdout=out, stderr=out, start_new_session=True)
    (data_root / INDEX_PID_FILE).write_text(str(process.pid))
    return log


def wait_for_index(data_root: Path) -> None:
    """A background build still running finishes before another starts (one index directory)."""
    pid_file: Path = data_root / INDEX_PID_FILE
    if not pid_file.exists():
        return
    pid = int(pid_file.read_text().strip() or "0")
    while pid:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(2)
    pid_file.unlink()


# ---- the commands


def run(data_root: Path, port: int, msgvault_db: Path, chat_db: Path, operator_id: str) -> int:
    operator_id = resolve_operator_id(operator_id)
    archive_v1(data_root)
    conn: sqlite3.Connection = open_store(store_path(data_root))
    for name, build in stages(conn, data_root, msgvault_db, chat_db):
        _stage(name, build())
    conn.close()
    people_csv: Path = realize(data_root)
    log: Path = index_in_background(data_root, people_csv, operator_id)
    print(f"index: building in the background from what was decided so far (log {log})", flush=True)
    url: str = start_review(data_root, port)
    pending: int = pending_reviews(port)
    if pending:
        print(f"review: {pending} people to check at {url}; finish after the review updates the index", flush=True)
    else:
        print(f"review: nothing to check ({url}); run finish", flush=True)
    return 0


def finish(data_root: Path, operator_id: str) -> int:
    """After the review: people.csv and the index again (cached, so about free). The review server stays up;
    the agent runs `stop` once the user is done with the page."""
    operator_id = resolve_operator_id(operator_id)
    wait_for_index(data_root)  # a first build still reading people.csv finishes before realize rewrites it
    people_csv: Path = realize(data_root)
    command: list[str] = index_command(data_root, people_csv)
    print("index: " + " ".join(command), flush=True)
    code: int = subprocess.run(command, cwd=ROOT, env=index_env(operator_id)).returncode
    if code != 0:
        print("index: the Modal build did not complete; run finish again", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=("run", "review", "stop", "finish"))
    parser.add_argument("--data-root", type=Path, default=Path(".powerpacks"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="the review server's port")
    parser.add_argument("--msgvault-db", type=Path, default=DEFAULT_MSGVAULT_DB, help="the Gmail archive")
    parser.add_argument("--chat-db", type=Path, default=DEFAULT_CHAT_DB, help="an iMessage store other than this Mac's")
    parser.add_argument("--operator-id", default="", help="the Modal index's operator id; default POWERPACKS_OPERATOR_ID from .env")
    args = parser.parse_args(argv)
    data_root: Path = args.data_root.resolve()
    if args.command == "run":
        return run(data_root, args.port, args.msgvault_db, args.chat_db, args.operator_id)
    if args.command == "review":
        url: str = start_review(data_root, args.port)
        print(f"review: {pending_reviews(args.port)} people to check at {url}")
        return 0
    if args.command == "stop":
        print("review server stopped" if stop_review(data_root) else "no review server running")
        return 0
    return finish(data_root, args.operator_id)


if __name__ == "__main__":
    raise SystemExit(main())
