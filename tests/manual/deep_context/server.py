"""Offline synthetic fixture for the opt-in Deep Context browser suite.

Serves the real review handler, SQLite writes, enrichment thread, and SSE.
Only enrichment's paid work is replaced; this does not test provider integration.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
import time
from collections import Counter
from dataclasses import asdict
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(REPO), str(REPO / "tests")]

from deep_context_sqlite_test_helpers import seed_identity  # noqa: E402
from packs.ingestion.primitives.deep_context.db.models import LinkRow, RowKind, WriterSource  # noqa: E402
from packs.ingestion.primitives.deep_context.db.store import Db  # noqa: E402
from packs.ingestion.primitives.deep_context.db.workflow_views import workflow_state  # noqa: E402
from packs.ingestion.primitives.deep_context.enrich import enrichment_pipeline  # noqa: E402
from packs.ingestion.primitives.deep_context.enrich.profiles.models import ProfileResult, ProfileTarget  # noqa: E402
from packs.ingestion.primitives.deep_context.enrich.profiles.projection import project_profile_results  # noqa: E402
from packs.ingestion.primitives.enrich.rapidapi_client import PROFILE_CONTENT  # noqa: E402
from packs.ingestion.primitives.deep_context.enrich.research_reconcile.models import EnrichmentProgress  # noqa: E402
from packs.ingestion.primitives.deep_context.manifests.receipt_counts import ReceiptCounts  # noqa: E402
from packs.ingestion.primitives.deep_context.review.server import make_handler  # noqa: E402

PEOPLE = (
    ("avery-alpha", "Avery Alpha"),
    ("casey-bravo", "Casey Bravo"),
    ("jordan-charlie", "Jordan Charlie"),
    ("riley-delta", "Riley Delta"),
    ("taylor-echo", "Taylor Echo"),
)


def _seed(db: Db, root: Path) -> None:
    for slug, name in PEOPLE:
        seed_identity(
            db,
            parent_id=f"qa-{slug}",
            person_id=f"candidate:{slug}",
            row_key=f"candidate:email:{slug}@example.com",
            display_slug=slug,
            name=name,
            machine_worth="maybe",
            kind=RowKind.CANDIDATE_EMAIL.value,
            candidate_people=True,
            link_updates={"candidate_origin": True, "raw_import": True},
            artifact_root=root,
            dossier_body=(f"# {name}\n\n## Relationship\n"
                          "Synthetic collaborator at Example Robotics.\n\n"
                          "## Work\nEngineer building small industrial robots.\n"),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--destination-delay-ms", type=int, default=0)
    parser.add_argument("--enrichment-delay-ms", type=int, default=2000)
    args = parser.parse_args()
    root = args.data_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        parser.error("Use an empty data directory; existing artifacts are never overwritten.")
    database = root / "deep-context.sqlite"
    db = Db(database)
    _seed(db, root)
    requests: Counter[str] = Counter()
    network_attempts: list[str] = []
    enrichment_runs = 0
    fail_next_save = False
    pipeline = None

    # Refuse outbound DNS and sockets before any provider can leave the process.
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def require_local(host: str) -> None:
        if host == "localhost":
            return
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            pass
        network_attempts.append(str(host))
        raise AssertionError(f"External network is disabled in browser QA: {host}")

    def local_dns(host, *positional, **keywords):
        require_local(host)
        return original_getaddrinfo(host, *positional, **keywords)

    def local_connect(sock, address):
        require_local(address[0])
        return original_connect(sock, address)

    def local_connect_ex(sock, address):
        require_local(address[0])
        return original_connect_ex(sock, address)

    def replay(self, budget, on_progress):
        nonlocal enrichment_runs, pipeline
        pipeline = self
        enrichment_runs += 1
        selected = db.query("SELECT parent_id FROM parents WHERE human_worth='yes' ORDER BY parent_id")
        total = len(selected)
        for done, row in enumerate(selected, start=1):
            time.sleep(args.enrichment_delay_ms / 1000 / max(total, 1))
            parent_id = row["parent_id"]
            slug = parent_id.removeprefix("qa-")
            name = dict(PEOPLE)[slug]
            db.project_rows((LinkRow(
                f"candidate:email:{slug}@example.com", parent_id,
                f"candidate:email:{slug}@example.com", RowKind.CANDIDATE_EMAIL.value,
                linkedin_url=f"https://www.linkedin.com/in/{slug}", display_name=name,
                candidate_origin=True, paid_profile=True, source=WriterSource.RECONCILE.value,
                machine_action="verify", machine_confidence=0.5,
                machine_judgment="needs_review", machine_reason="Synthetic identity needs review.",
                judgment_payload_json=json.dumps({"verdict": "needs_review", "confidence": 0.5}),
            ),))
            target = ProfileTarget(
                slug, f"https://www.linkedin.com/in/{slug}",
                f"candidate:email:{slug}@example.com", parent_id,
            )
            profile = ProfileResult.from_payload(slug, target.linkedin_url, {
                "state": PROFILE_CONTENT, "from_cache": True, "fetched": False,
                "public_identifier": slug, "linkedin_url": target.linkedin_url,
                "normalized_profile": {
                    "success": True, "full_name": name,
                    "headline": "Staff Engineer at Example Robotics",
                    "location_str": "Example City",
                    "experiences": [
                        {"title": "Staff Engineer", "company_name": "Example Robotics",
                         "starts_at": {"year": 2022}},
                        {"title": "Software Engineer", "company_name": "Sample Systems",
                         "starts_at": {"year": 2018}, "ends_at": {"year": 2022}},
                    ],
                    "education": [{"school_name": "Example University",
                                   "degree": "BS Computer Science"}],
                },
            })
            project_profile_results(db, ((target, profile),), root / "profile-cache")
            on_progress(EnrichmentProgress(
                "judging_retargets", ReceiptCounts.create(total=total, completed=done), done, total,
            ))

    with (
        patch.object(socket, "getaddrinfo", local_dns),
        patch.object(socket.socket, "connect", local_connect),
        patch.object(socket.socket, "connect_ex", local_connect_ex),
        patch.object(enrichment_pipeline, "ENRICH_MANIFEST", root / "enrichment" / "manifest.json"),
        patch.object(enrichment_pipeline.EnrichmentPipeline, "_run", replay),
    ):
        review_handler = make_handler(db=db, run_jobs=True)

        class Handler(review_handler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                requests[f"GET {self.path}"] += 1
                if parsed.path == "/__qa/status":
                    return self.send_json({
                        "progress": asdict(workflow_state(db).progress),
                        "worth": [dict(row) for row in db.query(
                            "SELECT parent_id, human_worth FROM parents ORDER BY parent_id")],
                        "links": [dict(row) for row in db.query(
                            "SELECT row_key, parent_id, decision_action, decision_approved, "
                            "replacement_url FROM links ORDER BY row_key")],
                        "enrichment_runs": enrichment_runs,
                        "network_attempts": network_attempts,
                        "requests": dict(requests),
                    })
                # Other app pages read the user's accounts/searches; this fixture
                # serves only review routes and its static assets.
                if parsed.path not in {
                    "/", "/healthz", "/directory", "/api/status", "/api/events",
                    "/api/enrichment", "/api/retargets", "/api/worth-table",
                    "/api/dossier", "/api/worth-card", "/api/linkedin-card",
                    "/api/person", "/api/avatar", "/assets/reconcile-review.css",
                    "/assets/reconcile-review.js",
                }:
                    return self.send_json({"error": "Route disabled in offline QA"}, 403)
                if parsed.path == "/" and parse_qs(parsed.query).get("stage") in (["enrich"], ["linkedin"]):
                    time.sleep(args.destination_delay_ms / 1000)
                return super().do_GET()

            def do_POST(self):  # noqa: N802
                nonlocal fail_next_save
                path = urlparse(self.path).path
                requests[f"POST {path}"] += 1
                if path.startswith("/__qa/"):
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    if path == "/__qa/fail-next-save":
                        fail_next_save = True
                    elif path == "/__qa/notify" and pipeline is not None:
                        pipeline.on_change()
                    else:
                        return self.send_json({"error": "Unknown QA control"}, 404)
                    return self.send_json({"ok": True})
                if path not in {"/worth", "/decide", "/approve-enrichment", "/complete"}:
                    return self.send_json({"error": "Route disabled in offline QA"}, 403)
                if fail_next_save and path in {"/worth", "/decide"}:
                    fail_next_save = False
                    self.rfile.read(int(self.headers.get("Content-Length", "0")))
                    return self.send_json({"error": "Injected save failure"}, 503)
                return super().do_POST()

        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
        print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}", "database": str(database)}), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()


if __name__ == "__main__":
    main()
