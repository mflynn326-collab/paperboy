from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .config import DEFAULT_DB, load_journals
from .analyst import (
    append_memory_note,
    build_context_pack,
    ensure_memory,
    read_memory,
    run_llm,
)
from .db import connect, init_db
from .drive import DriveSyncError, check_drive_path, setup_message, sync_to_drive_folder
from .conference import harvest_afa_programs, harvest_wfa_program, import_conference_csv
from .fetchers import harvest_crossref, harvest_openalex, harvest_ssrn_openalex, enrich_unpaywall
from .knowledge import (
    format_result,
    download_candidate_pdfs,
    index_abstracts,
    index_pdfs,
    reset_index,
    search_corpus,
    signal_counts,
)
from .mcp_server import run_mcp_server
from .settings import default_drive_path, default_email


def selected_journals(keys: str | None):
    journals = load_journals()
    if not keys:
        return journals
    wanted = {key.strip() for key in keys.split(",") if key.strip()}
    return [journal for journal in journals if journal.key in wanted]


def print_ask_response(
    conn,
    question: str,
    *,
    limit: int,
    from_year: int | None,
    until_year: int | None,
    source_key: str | None,
    kind: str,
) -> None:
    rows = search_corpus(
        conn,
        question,
        limit=limit,
        from_year=from_year,
        until_year=until_year,
        source_key=source_key,
        chunk_kind=kind,
    )
    print(f"Question: {question}")
    print()
    print("Closest corpus evidence:")
    if not rows:
        print("No indexed matches. Run `python -m paperboy index-corpus --rebuild` first, or broaden the query.")
        return
    for index, row in enumerate(rows, start=1):
        print(format_result(row, index))
    method_counts, data_counts = signal_counts(rows)
    print()
    print("Method signals in retrieved evidence:")
    if method_counts:
        for label, count in method_counts.most_common(8):
            print(f"- {label}: {count}")
    else:
        print("- No high-confidence method keywords detected in this slice")
    print()
    print("Data signals in retrieved evidence:")
    if data_counts:
        for label, count in data_counts.most_common(8):
            print(f"- {label}: {count}")
    else:
        print("- No high-confidence data-source keywords detected in this slice")


def run_chat(
    conn,
    *,
    limit: int,
    from_year: int | None,
    until_year: int | None,
    source_key: str | None,
    kind: str,
) -> None:
    print("Paperboy chat. Type a research question, or `exit` to quit.")
    while True:
        try:
            question = input("paperboy> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not question:
            continue
        if question.lower() in {"exit", "quit", "q"}:
            return
        print_ask_response(
            conn,
            question,
            limit=limit,
            from_year=from_year,
            until_year=until_year,
            source_key=source_key,
            kind=kind,
        )
        print()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    parser = argparse.ArgumentParser(prog="paperboy")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db")
    subparsers.add_parser("mcp-server")
    subparsers.add_parser("mcp-config")

    crossref = subparsers.add_parser("harvest-crossref")
    crossref.add_argument("--from-date", required=True)
    crossref.add_argument("--until-date", required=True)
    crossref.add_argument("--mailto", default=default_email())
    crossref.add_argument("--journals", help="Comma-separated journal keys from config/sources.toml")
    crossref.add_argument("--rows", type=int, default=100)
    crossref.add_argument("--max-pages", type=int)

    openalex = subparsers.add_parser("harvest-openalex")
    openalex.add_argument("--from-date", required=True)
    openalex.add_argument("--until-date", required=True)
    openalex.add_argument("--mailto", default=default_email())
    openalex.add_argument("--api-key", default=os.getenv("OPENALEX_API_KEY"))
    openalex.add_argument("--journals", help="Comma-separated journal keys from config/sources.toml")
    openalex.add_argument("--per-page", type=int, default=100)
    openalex.add_argument("--max-pages", type=int)

    unpaywall = subparsers.add_parser("enrich-unpaywall")
    unpaywall.add_argument("--email", default=default_email(), required=default_email() is None)
    unpaywall.add_argument("--limit", type=int, default=100)
    unpaywall.add_argument("--all", action="store_true", help="Resume batches until every DOI has a recorded lookup")
    unpaywall.add_argument("--download-pdfs", action="store_true")

    ssrn = subparsers.add_parser("harvest-ssrn")
    ssrn.add_argument("--from-date", required=True)
    ssrn.add_argument("--until-date", required=True)
    ssrn.add_argument("--mailto", default=default_email())
    ssrn.add_argument("--api-key", default=os.getenv("OPENALEX_API_KEY"))
    ssrn.add_argument("--per-page", type=int, default=100)
    ssrn.add_argument("--max-pages", type=int)

    conference = subparsers.add_parser("import-conference-csv")
    conference.add_argument("--provider", required=True, help="Short source key, e.g. afa or wfa")
    conference.add_argument("--file", required=True, type=Path)
    conference.add_argument("--venue", help="Default venue when CSV rows omit it")

    afa = subparsers.add_parser("harvest-afa")
    afa.add_argument("--years", default="2024,2025,2026", help="Comma-separated available program years")

    wfa = subparsers.add_parser("harvest-wfa")
    wfa.add_argument("--year", type=int, default=2026)

    recent = subparsers.add_parser("recent")
    recent.add_argument("--limit", type=int, default=20)

    index_corpus = subparsers.add_parser("index-corpus")
    index_corpus.add_argument("--rebuild", action="store_true", help="Clear and rebuild the search index")
    index_corpus.add_argument("--source", choices=["all", "works", "leads"], default="all")
    index_corpus.add_argument("--limit", type=int, help="Limit abstract rows per selected source")
    index_corpus.add_argument("--include-pdfs", action="store_true", help="Also index local PDFs recorded in works.pdf_path")
    index_corpus.add_argument("--max-pdf-pages", type=int, help="Optional page cap for PDF text extraction")

    search = subparsers.add_parser("search")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=12)
    search.add_argument("--from-year", type=int)
    search.add_argument("--until-year", type=int)
    search.add_argument("--source-key")
    search.add_argument("--kind", choices=["all", "abstract", "pdf"], default="all")
    search.add_argument("--no-snippets", action="store_true")

    ask = subparsers.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--limit", type=int, default=12)
    ask.add_argument("--from-year", type=int)
    ask.add_argument("--until-year", type=int)
    ask.add_argument("--source-key")
    ask.add_argument("--kind", choices=["all", "abstract", "pdf"], default="all")

    chat = subparsers.add_parser("chat", aliases=["talk"])
    chat.add_argument("--limit", type=int, default=8)
    chat.add_argument("--from-year", type=int)
    chat.add_argument("--until-year", type=int)
    chat.add_argument("--source-key")
    chat.add_argument("--kind", choices=["all", "abstract", "pdf"], default="all")

    download_pdfs = subparsers.add_parser("download-pdfs")
    download_pdfs.add_argument("--query", help="Bound downloads to indexed corpus matches for this query")
    download_pdfs.add_argument("--limit", type=int, default=25)
    download_pdfs.add_argument("--from-year", type=int)
    download_pdfs.add_argument("--until-year", type=int)
    download_pdfs.add_argument("--source-key")
    download_pdfs.add_argument("--overwrite", action="store_true")
    download_pdfs.add_argument("--index", action="store_true", help="Index downloaded PDF text immediately")
    download_pdfs.add_argument("--max-pdf-pages", type=int)

    memory = subparsers.add_parser("memory")
    memory.add_argument("--show", action="store_true", help="Print the current analyst memory file")

    remember = subparsers.add_parser("remember")
    remember.add_argument("note", help="Append a durable note to the analyst memory file")

    analyst = subparsers.add_parser("analyst")
    analyst.add_argument("question")
    analyst.add_argument("--provider", choices=["local", "auto", "openai", "anthropic"], default="local")
    analyst.add_argument("--model")
    analyst.add_argument("--max-tokens", type=int, default=2500)
    analyst.add_argument("--limit", type=int, default=12)
    analyst.add_argument("--from-year", type=int)
    analyst.add_argument("--until-year", type=int)
    analyst.add_argument("--source-key")
    analyst.add_argument("--kind", choices=["all", "abstract", "pdf"], default="all")
    analyst.add_argument("--no-pdf-candidates", action="store_true")
    analyst.add_argument("--save-context", type=Path, help="Write the retrieved context pack to a Markdown file")

    drive_setup = subparsers.add_parser("drive-setup")
    drive_setup.add_argument("--path", type=Path, default=default_drive_path())

    drive_check = subparsers.add_parser("drive-check")
    drive_check.add_argument("--path", type=Path, default=default_drive_path())

    drive_sync = subparsers.add_parser("drive-sync")
    drive_sync.add_argument("--path", type=Path, default=default_drive_path())
    drive_sync.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "init-db":
        init_db(args.db)
        print(f"Initialized {args.db}")
        return

    if args.command == "mcp-server":
        run_mcp_server(args.db)
        return

    if args.command == "mcp-config":
        config = {
            "mcpServers": {
                "paperboy": {
                    "command": sys.executable,
                    "args": ["-m", "paperboy", "mcp-server"],
                }
            }
        }
        print(json.dumps(config, indent=2))
        return

    if args.command == "drive-setup":
        print(setup_message(args.path))
        return

    if args.command == "drive-check":
        try:
            check_drive_path(args.path)
        except DriveSyncError as exc:
            print(exc)
            raise SystemExit(1) from exc
        print(f"Google Drive target is ready: {args.path.resolve()}")
        return

    if args.command == "drive-sync":
        try:
            code = sync_to_drive_folder(args.path, db_path=args.db, dry_run=args.dry_run)
        except DriveSyncError as exc:
            print(exc)
            raise SystemExit(1) from exc
        raise SystemExit(code)

    if args.command == "memory":
        path = ensure_memory()
        if args.show:
            print(read_memory(path))
        else:
            print(f"Analyst memory file: {path}")
        return

    if args.command == "remember":
        path = append_memory_note(args.note)
        print(f"Added note to {path}")
        return

    init_db(args.db)
    with connect(args.db) as conn:
        if args.command == "harvest-crossref":
            count = harvest_crossref(
                conn,
                selected_journals(args.journals),
                args.from_date,
                args.until_date,
                args.mailto,
                args.rows,
                args.max_pages,
            )
            print(f"Harvested {count} Crossref records")
        elif args.command == "harvest-openalex":
            count = harvest_openalex(
                conn,
                selected_journals(args.journals),
                args.from_date,
                args.until_date,
                args.mailto,
                args.api_key,
                args.per_page,
                args.max_pages,
            )
            print(f"Harvested {count} OpenAlex records")
        elif args.command == "enrich-unpaywall":
            count = 0
            while True:
                batch = enrich_unpaywall(conn, args.email, args.limit, args.download_pdfs)
                count += batch
                if not args.all or batch < args.limit:
                    break
                print(f"Enriched {count} Unpaywall records so far")
            print(f"Enriched {count} Unpaywall records")
        elif args.command == "harvest-ssrn":
            count = harvest_ssrn_openalex(
                conn,
                args.from_date,
                args.until_date,
                args.mailto,
                args.api_key,
                args.per_page,
                args.max_pages,
            )
            print(f"Harvested {count} SSRN records through OpenAlex")
        elif args.command == "import-conference-csv":
            count = import_conference_csv(conn, args.provider, args.file, args.venue)
            print(f"Imported {count} {args.provider} conference records")
        elif args.command == "harvest-afa":
            years = [int(value.strip()) for value in args.years.split(",") if value.strip()]
            count = harvest_afa_programs(conn, years)
            print(f"Harvested {count} AFA conference records")
        elif args.command == "harvest-wfa":
            count = harvest_wfa_program(conn, args.year)
            print(f"Harvested {count} WFA conference records")
        elif args.command == "recent":
            rows = conn.execute(
                """
                SELECT publication_date, venue, title, doi, oa_status, best_pdf_url
                FROM works
                ORDER BY publication_date DESC, updated_at DESC
                LIMIT ?
                """,
                (args.limit,),
            ).fetchall()
            for row in rows:
                pdf = " PDF" if row["best_pdf_url"] else ""
                print(
                    f"{row['publication_date'] or 'unknown'} | {row['venue'] or 'unknown'} | "
                    f"{row['title'] or 'untitled'} | {row['doi']}{pdf}"
                )
        elif args.command == "index-corpus":
            if args.rebuild:
                reset_index(conn)
            abstract_count = index_abstracts(conn, source=args.source, limit=args.limit)
            pdf_count = 0
            missing_pdfs = 0
            if args.include_pdfs:
                pdf_count, missing_pdfs = index_pdfs(
                    conn,
                    limit=args.limit,
                    max_pages=args.max_pdf_pages,
                )
            conn.commit()
            print(f"Indexed {abstract_count} abstract chunks")
            if args.include_pdfs:
                print(f"Indexed {pdf_count} PDF chunks; skipped {missing_pdfs} missing PDF files")
        elif args.command == "search":
            rows = search_corpus(
                conn,
                args.query,
                limit=args.limit,
                from_year=args.from_year,
                until_year=args.until_year,
                source_key=args.source_key,
                chunk_kind=args.kind,
            )
            for index, row in enumerate(rows, start=1):
                print(format_result(row, index, include_snippet=not args.no_snippets))
        elif args.command == "ask":
            print_ask_response(
                conn,
                args.question,
                limit=args.limit,
                from_year=args.from_year,
                until_year=args.until_year,
                source_key=args.source_key,
                kind=args.kind,
            )
        elif args.command in {"chat", "talk"}:
            run_chat(
                conn,
                limit=args.limit,
                from_year=args.from_year,
                until_year=args.until_year,
                source_key=args.source_key,
                kind=args.kind,
            )
        elif args.command == "download-pdfs":
            stats = download_candidate_pdfs(
                conn,
                query=args.query,
                limit=args.limit,
                from_year=args.from_year,
                until_year=args.until_year,
                source_key=args.source_key,
                overwrite=args.overwrite,
                index_after=args.index,
                max_pdf_pages=args.max_pdf_pages,
            )
            print(
                "PDF candidates: {candidates}; downloaded: {downloaded}; "
                "failed/skipped: {failed}; indexed PDF chunks: {indexed_chunks}".format(**stats)
            )
        elif args.command == "analyst":
            memory_text = read_memory()
            context = build_context_pack(
                conn,
                args.question,
                limit=args.limit,
                from_year=args.from_year,
                until_year=args.until_year,
                source_key=args.source_key,
                kind=args.kind,
                include_pdf_candidates=not args.no_pdf_candidates,
            )
            if args.save_context:
                args.save_context.parent.mkdir(parents=True, exist_ok=True)
                args.save_context.write_text(context, encoding="utf-8")
                print(f"Wrote context pack to {args.save_context}")
            if args.provider == "local":
                print(memory_text)
                print()
                print(context)
            else:
                try:
                    print(run_llm(args.provider, context, memory_text, model=args.model, max_tokens=args.max_tokens))
                except RuntimeError as exc:
                    print(exc)
                    raise SystemExit(1) from exc
