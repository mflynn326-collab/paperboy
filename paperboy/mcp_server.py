from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Callable

from .analyst import (
    build_context_pack,
    append_memory_note,
    read_memory,
)
from .db import connect
from .knowledge import (
    corpus_overview,
    corpus_trends,
    download_candidate_pdfs,
    format_result,
    pdf_candidates,
    search_corpus,
    snippet,
)


Json = dict[str, Any]


def text_content(text: str, *, is_error: bool = False) -> Json:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _as_int(value: Any, default: int | None = None) -> int | None:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "y", "on"}


def _fetch_chunk(conn: sqlite3.Connection, chunk_id: int) -> str:
    row = conn.execute(
        """
        SELECT id, source_type, source_id, chunk_kind, chunk_index, doi, lead_id,
               title, venue, publication_year, source_key, path, text
        FROM corpus_chunks
        WHERE id = ?
        """,
        (chunk_id,),
    ).fetchone()
    if not row:
        return f"No Paperboy chunk found for chunk:{chunk_id}"
    lines = [
        f"chunk:{row['id']}",
        f"{row['publication_year'] or 'n.d.'} | {row['venue'] or row['source_key'] or 'unknown'} | {row['title'] or 'Untitled'}",
        f"Type: {row['chunk_kind']} | source: {row['source_type']}:{row['source_id']}",
        f"DOI: {row['doi'] or 'none'}",
    ]
    if row["path"]:
        lines.append(f"Path: {row['path']}")
    lines.extend(["", row["text"]])
    return "\n".join(lines)


def _fetch_work(conn: sqlite3.Connection, doi: str) -> str:
    row = conn.execute(
        """
        SELECT doi, title, abstract, publication_date, publication_year, venue,
               source_key, source_tier, url, best_pdf_url, best_landing_url, pdf_path
        FROM works
        WHERE doi = ?
        """,
        (doi.lower(),),
    ).fetchone()
    if not row:
        return f"No Paperboy work found for doi:{doi}"
    authors = conn.execute(
        """
        SELECT given, family
        FROM authors
        WHERE doi = ?
        ORDER BY position
        LIMIT 12
        """,
        (doi.lower(),),
    ).fetchall()
    author_text = "; ".join(
        " ".join(part for part in [author["given"], author["family"]] if part)
        for author in authors
    )
    chunks = conn.execute(
        """
        SELECT id, chunk_kind, chunk_index, text
        FROM corpus_chunks
        WHERE doi = ?
        ORDER BY chunk_kind, chunk_index
        LIMIT 8
        """,
        (doi.lower(),),
    ).fetchall()
    lines = [
        f"doi:{row['doi']}",
        f"{row['publication_year'] or row['publication_date'] or 'n.d.'} | {row['venue'] or row['source_key'] or 'unknown'} | {row['title'] or 'Untitled'}",
    ]
    if author_text:
        lines.append(f"Authors: {author_text}")
    lines.extend(
        [
            f"Source: {row['source_key'] or 'unknown'} | tier: {row['source_tier'] or 'unknown'}",
            f"URL: {row['url'] or row['best_landing_url'] or 'none'}",
            f"PDF URL: {row['best_pdf_url'] or 'none'}",
            f"PDF path: {row['pdf_path'] or 'none'}",
            "",
            "Abstract:",
            row["abstract"] or "No abstract recorded.",
        ]
    )
    if chunks:
        lines.append("")
        lines.append("Indexed chunks:")
        for chunk in chunks:
            lines.append(
                f"- chunk:{chunk['id']} {chunk['chunk_kind']}#{chunk['chunk_index']}: "
                f"{snippet(chunk['text'], 220)}"
            )
    return "\n".join(lines)


def _fetch_lead(conn: sqlite3.Connection, lead_id: str) -> str:
    row = conn.execute(
        """
        SELECT id, provider, title, abstract, authors_text, event_date,
               publication_year, venue, source_key, url, pdf_url, matched_doi
        FROM leads
        WHERE id = ?
        """,
        (lead_id,),
    ).fetchone()
    if not row:
        return f"No Paperboy lead found for lead:{lead_id}"
    return "\n".join(
        [
            f"lead:{row['id']}",
            f"{row['publication_year'] or row['event_date'] or 'n.d.'} | {row['venue'] or row['source_key'] or row['provider']} | {row['title']}",
            f"Authors: {row['authors_text'] or 'none recorded'}",
            f"URL: {row['url'] or 'none'}",
            f"PDF URL: {row['pdf_url'] or 'none'}",
            f"Matched DOI: {row['matched_doi'] or 'none'}",
            "",
            row["abstract"] or "No abstract recorded.",
        ]
    )


class PaperboyMCP:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = connect(db_path)
        self.tools: dict[str, Callable[[Json], Json]] = {
            "search": self.tool_search,
            "fetch": self.tool_fetch,
            "paperboy_analyst_context": self.tool_analyst_context,
            "paperboy_memory": self.tool_memory,
            "paperboy_remember": self.tool_remember,
            "paperboy_pdf_candidates": self.tool_pdf_candidates,
            "paperboy_overview": self.tool_overview,
            "paperboy_trends": self.tool_trends,
            "paperboy_fetch_fulltext": self.tool_fetch_fulltext,
        }

    def tool_list(self) -> list[Json]:
        return [
            {
                "name": "search",
                "description": "Search the local Paperboy finance literature corpus. Returns ranked chunk IDs, papers, snippets, DOI/lead identifiers, and source metadata. Pass doi to search inside one paper's indexed full text (e.g. query 'identification instrument' with kind=pdf) so only the relevant passages are retrieved. Use fetch with a chunk:ID to read a full passage.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30},
                        "from_year": {"type": "integer"},
                        "until_year": {"type": "integer"},
                        "source_key": {"type": "string"},
                        "kind": {"type": "string", "enum": ["all", "abstract", "pdf"], "default": "all"},
                        "doi": {"type": "string", "description": "Restrict results to chunks from this one paper"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "fetch",
                "description": "Fetch a full Paperboy record or chunk by ID. Supports chunk:123, doi:10.xxxx, lead:provider:id, or raw DOI strings.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
            {
                "name": "paperboy_analyst_context",
                "description": "Build a memory-aware research context pack for literature placement, methods, data sources, trends, and target-journal advice.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "limit": {"type": "integer", "default": 12, "minimum": 1, "maximum": 40},
                        "from_year": {"type": "integer"},
                        "until_year": {"type": "integer"},
                        "source_key": {"type": "string"},
                        "kind": {"type": "string", "enum": ["all", "abstract", "pdf"], "default": "all"},
                        "include_pdf_candidates": {"type": "boolean", "default": True},
                    },
                    "required": ["question"],
                },
            },
            {
                "name": "paperboy_memory",
                "description": "Read Paperboy's standing analyst instructions and durable running notes.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "paperboy_remember",
                "description": "Append a durable note to Paperboy analyst memory for future research conversations.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"note": {"type": "string"}},
                    "required": ["note"],
                },
            },
            {
                "name": "paperboy_pdf_candidates",
                "description": "Find not-yet-downloaded open-access PDF candidates relevant to a query and bounded by optional year/source filters.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 30},
                        "from_year": {"type": "integer"},
                        "until_year": {"type": "integer"},
                        "source_key": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "paperboy_fetch_fulltext",
                "description": "Download and index the full text of specific open-access papers so their methods, data, and identification details become searchable. Pass dois for papers already found via search, or a query to fetch the top OA matches. Downloads go to the Google Drive PDF warehouse; text is chunk-indexed locally. After this succeeds, use search with kind=pdf (optionally with doi) to retrieve only the relevant passages. Keep limit small; each paper takes a few seconds.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "dois": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "DOIs of papers to fetch (preferred: target papers found via search)",
                        },
                        "query": {"type": "string", "description": "Alternative to dois: fetch top OA candidates matching this query"},
                        "limit": {"type": "integer", "default": 3, "minimum": 1, "maximum": 5},
                        "from_year": {"type": "integer"},
                        "until_year": {"type": "integer"},
                        "source_key": {"type": "string"},
                    },
                },
            },
            {
                "name": "paperboy_overview",
                "description": "Summarize what the Paperboy corpus contains: work/lead/chunk counts, year coverage, and paper counts by source and venue. Call this first when answering trend or coverage questions so claims are grounded in what the database actually holds.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "paperboy_trends",
                "description": "Aggregate corpus evidence for a topic: matched-paper counts by year, top venues, and method/data-source signals by year. Use for questions about how a topic is trending, which journals publish it, and how methods or datasets are shifting over time.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "from_year": {"type": "integer"},
                        "until_year": {"type": "integer"},
                        "source_key": {"type": "string"},
                    },
                    "required": ["query"],
                },
            },
        ]

    def tool_search(self, args: Json) -> Json:
        rows = search_corpus(
            self.conn,
            str(args.get("query", "")),
            limit=_as_int(args.get("limit"), 10) or 10,
            from_year=_as_int(args.get("from_year")),
            until_year=_as_int(args.get("until_year")),
            source_key=args.get("source_key") or None,
            chunk_kind=args.get("kind") or "all",
            doi=str(args.get("doi") or "").strip() or None,
        )
        if not rows:
            return text_content("No Paperboy corpus matches found.")
        blocks = []
        for index, row in enumerate(rows, start=1):
            blocks.append(f"chunk:{row['id']}\n{format_result(row, index)}")
        return text_content("\n\n".join(blocks))

    def tool_fetch(self, args: Json) -> Json:
        raw_id = str(args.get("id", "")).strip()
        if raw_id.startswith("chunk:"):
            return text_content(_fetch_chunk(self.conn, int(raw_id.split(":", 1)[1])))
        if raw_id.startswith("doi:"):
            return text_content(_fetch_work(self.conn, raw_id.split(":", 1)[1]))
        if raw_id.startswith("lead:"):
            return text_content(_fetch_lead(self.conn, raw_id.split(":", 1)[1]))
        if raw_id.startswith("10."):
            return text_content(_fetch_work(self.conn, raw_id))
        return text_content(
            "Unrecognized Paperboy ID. Use chunk:123, doi:10.xxxx, lead:provider:id, or a raw DOI.",
            is_error=True,
        )

    def tool_analyst_context(self, args: Json) -> Json:
        context = build_context_pack(
            self.conn,
            str(args.get("question", "")),
            limit=_as_int(args.get("limit"), 12) or 12,
            from_year=_as_int(args.get("from_year")),
            until_year=_as_int(args.get("until_year")),
            source_key=args.get("source_key") or None,
            kind=args.get("kind") or "all",
            include_pdf_candidates=_as_bool(args.get("include_pdf_candidates"), True),
        )
        return text_content(f"{read_memory()}\n\n{context}")

    def tool_memory(self, args: Json) -> Json:
        return text_content(read_memory())

    def tool_remember(self, args: Json) -> Json:
        path = append_memory_note(str(args.get("note", "")))
        return text_content(f"Added note to {path}")

    def tool_pdf_candidates(self, args: Json) -> Json:
        rows = pdf_candidates(
            self.conn,
            query=str(args.get("query", "")),
            limit=_as_int(args.get("limit"), 10) or 10,
            from_year=_as_int(args.get("from_year")),
            until_year=_as_int(args.get("until_year")),
            source_key=args.get("source_key") or None,
        )
        if not rows:
            return text_content("No not-yet-downloaded OA PDF candidates found.")
        lines = []
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"P{index}: {row['publication_year'] or 'n.d.'} | {row['source_key'] or 'unknown'} | "
                f"doi:{row['doi']} | {row['title']}\nPDF URL: {row['best_pdf_url']}"
            )
        return text_content("\n\n".join(lines))

    def _index_existing_pdf(self, row: sqlite3.Row) -> int:
        from .knowledge import chunk_text, extract_pdf_text, index_chunks

        path = Path(row["pdf_path"])
        if not path.exists():
            return -1
        text = extract_pdf_text(path, max_pages=120)
        count = index_chunks(
            self.conn,
            source_type="work",
            source_id=row["doi"],
            chunk_kind="pdf",
            chunks=chunk_text(text),
            doi=row["doi"],
            lead_id=None,
            title=row["title"],
            venue=row["venue"],
            publication_year=row["publication_year"],
            source_key=row["source_key"],
            path=str(path),
        )
        self.conn.commit()
        return count

    def tool_fetch_fulltext(self, args: Json) -> Json:
        raw_dois = args.get("dois") or []
        dois = []
        for value in raw_dois:
            doi = str(value).strip().lower()
            doi = doi.removeprefix("doi:").removeprefix("https://doi.org/")
            if doi:
                dois.append(doi)
        query = str(args.get("query") or "").strip()
        if not dois and not query:
            return text_content("Provide dois or a query.", is_error=True)
        limit = min(_as_int(args.get("limit"), 3) or 3, 5)

        report: list[str] = []
        to_download: list[str] | None = None
        if dois:
            dois = dois[:limit]
            placeholders = ", ".join("?" for _ in dois)
            rows = {
                row["doi"]: row
                for row in self.conn.execute(
                    f"""
                    SELECT doi, title, venue, publication_year, source_key, pdf_path, best_pdf_url,
                           (SELECT COUNT(*) FROM corpus_chunks AS c
                            WHERE c.doi = works.doi AND c.chunk_kind = 'pdf') AS pdf_chunks
                    FROM works WHERE doi IN ({placeholders})
                    """,
                    dois,
                ).fetchall()
            }
            to_download = []
            for doi in dois:
                row = rows.get(doi)
                if row is None:
                    report.append(f"- doi:{doi}: not in the Paperboy corpus")
                elif row["pdf_chunks"]:
                    report.append(f"- doi:{doi}: full text already indexed ({row['pdf_chunks']} chunks)")
                elif row["pdf_path"]:
                    count = self._index_existing_pdf(row)
                    if count < 0:
                        report.append(f"- doi:{doi}: recorded PDF file is missing on disk")
                    else:
                        report.append(f"- doi:{doi}: indexed existing PDF ({count} chunks)")
                elif not row["best_pdf_url"]:
                    report.append(
                        f"- doi:{doi}: no open-access PDF recorded (paywalled or not yet enriched via Unpaywall)"
                    )
                else:
                    to_download.append(doi)
            if not to_download:
                to_download = None

        if to_download or (query and not dois):
            stats = download_candidate_pdfs(
                self.conn,
                query=query if not dois else None,
                dois=to_download,
                limit=limit,
                from_year=_as_int(args.get("from_year")),
                until_year=_as_int(args.get("until_year")),
                source_key=args.get("source_key") or None,
                index_after=True,
                max_pdf_pages=120,
            )
            for result in stats["results"]:
                report.append(f"- doi:{result['doi']}: {result['status']} | {result['title'] or 'Untitled'}")
            if not stats["candidates"] and query and not dois:
                report.append("No not-yet-downloaded OA PDF candidates matched the query.")

        report.append(
            "\nNext: use search with kind=pdf (add doi to stay inside one paper) to pull only the passages you need."
        )
        return text_content("\n".join(report))

    def tool_overview(self, args: Json) -> Json:
        overview = corpus_overview(self.conn)
        lines = [
            "Paperboy corpus overview",
            f"Works (DOI records): {overview['works']} ({overview['works_with_abstract']} with abstracts)",
            f"Leads (conference/working-paper rows): {overview['leads']}",
            f"Indexed chunks: {overview['chunks']} ({overview['pdf_chunks']} from full-text PDFs)",
            f"Publication years: {overview['year_min']}-{overview['year_max']}",
            "",
            "Works by source:",
        ]
        for row in overview["by_source"]:
            lines.append(f"- {row['source_key'] or 'unknown'}: {row['n']}")
        lines.extend(["", "Top venues:"])
        for row in overview["by_venue"]:
            lines.append(f"- {row['venue']}: {row['n']}")
        lines.extend(["", "Works by year:"])
        for row in overview["by_year"]:
            lines.append(f"- {row['year']}: {row['n']}")
        return text_content("\n".join(lines))

    def tool_trends(self, args: Json) -> Json:
        query = str(args.get("query", ""))
        trends = corpus_trends(
            self.conn,
            query,
            from_year=_as_int(args.get("from_year")),
            until_year=_as_int(args.get("until_year")),
            source_key=args.get("source_key") or None,
        )
        if not trends["matched_papers"]:
            return text_content("No Paperboy corpus matches found for this trend query.")
        lines = [
            f"Trend evidence for: {query}",
            f"Matched papers: {trends['matched_papers']} "
            f"(deduplicated from {trends['sampled_chunks']} retrieved chunks; "
            "counts reflect corpus coverage, not the full literature)",
            "",
            "Matched papers by year:",
        ]
        for year, count in trends["by_year"].items():
            lines.append(f"- {year}: {count}")
        lines.extend(["", "Top venues among matches:"])
        for venue, count in trends["top_venues"]:
            lines.append(f"- {venue}: {count}")
        if trends["methods_by_year"]:
            lines.extend(["", "Method signals by year:"])
            for year, counts in trends["methods_by_year"].items():
                summary = ", ".join(f"{label} ({count})" for label, count in counts)
                lines.append(f"- {year}: {summary}")
        if trends["data_by_year"]:
            lines.extend(["", "Data-source signals by year:"])
            for year, counts in trends["data_by_year"].items():
                summary = ", ".join(f"{label} ({count})" for label, count in counts)
                lines.append(f"- {year}: {summary}")
        return text_content("\n".join(lines))

    def handle(self, message: Json) -> Json | None:
        request_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if request_id is None:
            return None
        try:
            if method == "initialize":
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "paperboy", "version": "0.2.0"},
                    },
                }
            if method == "ping":
                return {"jsonrpc": "2.0", "id": request_id, "result": {}}
            if method == "resources/list":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"resources": []}}
            if method == "prompts/list":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}}
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tool_list()}}
            if method == "tools/call":
                name = params.get("name")
                args = params.get("arguments") or {}
                if name not in self.tools:
                    raise ValueError(f"Unknown Paperboy tool: {name}")
                return {"jsonrpc": "2.0", "id": request_id, "result": self.tools[name](args)}
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32000, "message": str(exc)},
            }


def run_mcp_server(db_path: Path) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    # The protocol owns stdout. Point sys.stdout at stderr so a stray print()
    # inside a tool (or a library it calls) cannot corrupt the JSON-RPC stream.
    wire = sys.stdout
    sys.stdout = sys.stderr
    server = PaperboyMCP(db_path)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
            response = server.handle(message)
        except Exception as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": str(exc)},
            }
        if response is not None:
            wire.write(json.dumps(response, separators=(",", ":")) + "\n")
            wire.flush()
