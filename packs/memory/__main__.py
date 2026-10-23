"""JSON CLI for isolated import, generation, hybrid query and evidence inspection."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from packs.memory.actions import act
from packs.memory.directory import Directory, Origin, Selection, Selector
from packs.memory.generate import Generate
from packs.memory.discovery import Discover
from packs.memory.models import Claim, Filters, Kind
from packs.memory.provider import OpenAIProvider, NeedsApproval
from packs.ingestion.primitives.common.gates import EXIT_NEEDS_APPROVAL
from packs.memory.query import Query
from packs.memory.sources import V2
from packs.memory.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="separate derived SQLite, outside source roots")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("inventory", "import"):
        source = commands.add_parser(command)
        source.add_argument("--v2-root", type=Path, action="append", default=[])
    resolve = commands.add_parser("resolve")
    resolve.add_argument("text")
    resolve.add_argument("--embedding-model", default="", help="omit for lexical retrieval")
    enumerate_people = commands.add_parser("enumerate")
    enumerate_people.add_argument("origin", choices=list(Origin))
    enumerate_people.add_argument("value")
    enumerate_people.add_argument("--after", default="")
    enumerate_people.add_argument("--limit", type=int, default=50)
    for command in ("hide", "remove-label"):
        action = commands.add_parser(command)
        action.add_argument("--selection", type=Path, required=True, help="enumerated JSON page with explicit people")
        action.add_argument("--v2-root", type=Path, required=True)
        action.add_argument("--confirm", action="store_true", help="apply the exact previewed selection")
        if command == "remove-label":
            action.add_argument("origin", choices=list(Origin))
            action.add_argument("value")
    generate = commands.add_parser("generate")
    generate.add_argument("--model", required=True)
    generate.add_argument("--embedding-model", required=True)
    generate.add_argument("--limit", type=int, required=True, help="maximum total fresh generation + embedding calls; use 1 for pilot")
    generate.add_argument("--dry-run", action="store_true", help="token and cache estimate; no provider requests")
    discovery = commands.add_parser("discover")
    discovery.add_argument("--model", required=True)
    discovery.add_argument("--embedding-model", default="", help="use cached neighbors and embed written tags; omit for no embeddings")
    discovery.add_argument("--limit", type=int, required=True)
    discovery.add_argument("--min-people", type=int, default=3)
    discovery.add_argument("--phrase", default="")
    discovery.add_argument("--after", default="", help="continue after preview/run cursor")
    discovery.add_argument("--dry-run", action="store_true")
    search = commands.add_parser("search", help="everyone the words name, warmest first, with matching evidence")
    search.add_argument("text")
    search.add_argument("--layer", default="", help="an owner label such as family or past coworker")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--embedding-model", default="", help="omit for lexical and label retrieval only")
    query = commands.add_parser("query")
    query.add_argument("text", nargs="?", default="")
    query.add_argument("--embedding-model", default="", help="omit for free lexical/typed retrieval")
    query.add_argument("--person", action="append", default=[])
    query.add_argument("--kind", choices=list(Kind))
    query.add_argument("--topic", default="")
    query.add_argument("--scope", default="")
    query.add_argument("--object", default="")
    query.add_argument("--limit", type=int, default=10)
    query.add_argument("--context-chars", type=int, default=16000)
    lookup = commands.add_parser("lookup")
    lookup.add_argument("identity")
    opened = commands.add_parser("open")
    opened.add_argument("claim_id", type=int)
    correction = commands.add_parser("correct")
    correction.add_argument("claim_id", type=int)
    correction.add_argument("--replacement", type=Path, help="Claim JSON; omit to retract")
    correction.add_argument("--reason", required=True)
    alias = commands.add_parser("confirm-alias")
    alias.add_argument("alias")
    alias.add_argument("topic")
    overlap = commands.add_parser("overlap")
    overlap.add_argument("identity")
    overlap.add_argument("--organization", default="")
    for command_parser in (resolve, generate, discovery, query, search):
        command_parser.add_argument("--approve-spend", action="store_true")
    args = parser.parse_args(argv)
    store = None
    provider = None
    try:
        result: object
        if args.command in ("inventory", "import"):
            if len(args.v2_root) != 1:
                raise ValueError("supply exactly one source corpus per derived database")
            adapter = V2(args.v2_root[0].resolve())
            if args.db.resolve().is_relative_to(adapter.root.resolve()):
                raise ValueError("derived database must be outside source roots")
            if args.command == "inventory":
                result = [adapter.inventory()]
            else:
                store = Store(args.db, create=True)
                result = [asdict(store.import_source(adapter))]
        else:
            store = Store(args.db)
            if args.command == "resolve":
                result = [asdict(meaning) for meaning in Directory(store).resolve(args.text, embedding_model=args.embedding_model, approve_spend=args.approve_spend)]
            elif args.command == "enumerate":
                result = asdict(Directory(store).enumerate(Selector(Origin(args.origin), args.value), after=args.after, limit=args.limit))
            elif args.command in ("hide", "remove-label"):
                selection = Selection.parse(json.loads(args.selection.read_text()))
                selector = Selector(Origin(args.origin), args.value) if args.command == "remove-label" else None
                result = asdict(act(store, V2(args.v2_root.resolve()), selection, selector=selector, confirm=args.confirm))
            elif args.command == "generate":
                provider = OpenAIProvider(args.model, args.embedding_model, approve_spend=args.approve_spend)
                generator = Generate(store, provider)
                result = generator.estimate(limit=args.limit) if args.dry_run else asdict(generator.run(limit=args.limit))
            elif args.command == "discover":
                provider = OpenAIProvider(args.model, args.embedding_model, approve_spend=args.approve_spend)
                discover = Discover(store, provider)
                result = asdict(discover.preview(min_people=args.min_people, phrase=args.phrase, after=args.after, limit=args.limit)) if args.dry_run else asdict(
                    discover.run(limit=args.limit, min_people=args.min_people, phrase=args.phrase, after=args.after))
            elif args.command == "search":
                provider = OpenAIProvider("", args.embedding_model, approve_spend=args.approve_spend)
                result = asdict(Query(store, provider).people(args.text, layer=args.layer, limit=args.limit))
            elif args.command == "query":
                provider = OpenAIProvider("", args.embedding_model, approve_spend=args.approve_spend)
                parents = [_one_parent(store, identity) for identity in args.person]
                filters = Filters(tuple(parents), Kind(args.kind) if args.kind else None, args.topic, args.scope, args.object)
                result = asdict(Query(store, provider).run(args.text, filters=filters, limit=args.limit, context_chars=args.context_chars))
            elif args.command == "lookup":
                result = [asdict(match) for match in store.lookup(args.identity)]
            elif args.command == "open":
                result = asdict(store.open(args.claim_id))
            elif args.command == "correct":
                replacement = Claim.parse(json.loads(args.replacement.read_text())) if args.replacement else None
                result = {"replacement_id": store.correct(args.claim_id, replacement, args.reason)}
            elif args.command == "confirm-alias":
                store.confirm_alias(args.alias, args.topic)
                result = {"confirmed": True}
            else:
                provider = OpenAIProvider("", "")
                result = [[asdict(left), asdict(right)] for left, right in
                          Query(store, provider).overlap(_one_parent(store, args.identity), organization=args.organization)]
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return int(isinstance(result, dict) and bool(result.get("failures")))
    except NeedsApproval as exc:
        print(json.dumps({"status": "needs_approval", "needs_approval": exc.payload}))
        return EXIT_NEEDS_APPROVAL
    except (ValueError, OSError, KeyError, sqlite3.Error) as exc:
        # Errors are types/status only: no input bodies, provider bodies or contact data in traces.
        print(json.dumps({"error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}), file=sys.stderr)
        return 1
    finally:
        if provider:
            provider.close()
        if store:
            store.close()


def _one_parent(store: Store, identity: str) -> str:
    parents = {match.parent for match in store.lookup(identity)}
    if len(parents) != 1:
        raise ValueError("identity must resolve to exactly one canonical parent")
    return next(iter(parents))


if __name__ == "__main__":
    raise SystemExit(main())
