from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import time
from typing import Any, Iterable


PDF_IMPORT_ERROR = (
    "PDF text extraction requires pypdf. Install it or use the bundled workspace "
    "Python where pypdf is available."
)


METHOD_PATTERNS = {
    "difference-in-differences": re.compile(r"\b(diff(?:erence)?-?in-?diff(?:erences)?|did)\b", re.I),
    "event study": re.compile(r"\bevent stud(?:y|ies)\b", re.I),
    "instrumental variables": re.compile(r"\b(instrumental variable|two-stage|2sls|iv regression)\b", re.I),
    "regression discontinuity": re.compile(r"\b(regression discontinuity|rd design)\b", re.I),
    "text analysis / NLP": re.compile(r"\b(textual analysis|natural language|nlp|large language model|llm|bert|topic model)\b", re.I),
    "machine learning": re.compile(r"\b(machine learning|random forest|lasso|xgboost|neural network)\b", re.I),
    "structural model": re.compile(r"\b(structural model|dynamic model|calibration|estimated model)\b", re.I),
    "experiment / survey": re.compile(r"\b(experiment|survey experiment|randomized|randomised|rct)\b", re.I),
    "network analysis": re.compile(r"\b(network|centrality|connectedness|supply chain)\b", re.I),
}


DATA_PATTERNS = {
    "CRSP": re.compile(r"\bcrsp\b", re.I),
    "Compustat": re.compile(r"\bcompustat\b", re.I),
    "EDGAR / SEC filings": re.compile(r"\b(edgar|sec filing|10-k|10-q|8-k)\b", re.I),
    "13F / institutional holdings": re.compile(r"\b(13f|institutional holdings?)\b", re.I),
    "TAQ": re.compile(r"\btaq\b", re.I),
    "TRACE / bonds": re.compile(r"\b(trace|corporate bond)\b", re.I),
    "OptionMetrics / options": re.compile(r"\b(optionmetrics|option market|options trading)\b", re.I),
    "DealScan": re.compile(r"\bdealscan\b", re.I),
    "BoardEx / directors": re.compile(r"\b(boardex|director network|board network)\b", re.I),
    "ExecuComp": re.compile(r"\bexecucomp\b", re.I),
    "PitchBook / private markets": re.compile(r"\b(pitchbook|preqin|private equity|venture capital)\b", re.I),
    "Refinitiv / analyst data": re.compile(r"\b(ibes|i/b/e/s|refinitiv|analyst forecast)\b", re.I),
    "FactSet": re.compile(r"\bfactset\b", re.I),
    "S&P Capital IQ": re.compile(r"\b(capital iq|s&p global)\b", re.I),
    "account-level transactions": re.compile(r"\b(account-level|transaction-level|brokerage account|retail order)\b", re.I),
    "Penny Pilot / market structure": re.compile(r"\b(penny pilot|payment for order flow|pfof|wholesaler)\b", re.I),
}


TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_./&-]*")
QUERY_STOPWORDS = {
    "about",
    "answer",
    "around",
    "best",
    "could",
    "data",
    "dataset",
    "datasets",
    "does",
    "field",
    "fit",
    "for",
    "from",
    "general",
    "good",
    "have",
    "how",
    "identification",
    "journal",
    "journals",
    "method",
    "methods",
    "paper",
    "papers",
    "plausible",
    "question",
    "questions",
    "recent",
    "research",
    "should",
    "source",
    "sources",
    "studies",
    "studying",
    "that",
    "the",
    "this",
    "trend",
    "trends",
    "what",
    "where",
    "which",
    "with",
    "would",
}


def clean_text(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def chunk_text(text: str, max_chars: int = 2800, overlap: int = 250) -> list[str]:
    text = clean_text(text)
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            split = max(text.rfind(". ", start, end), text.rfind("; ", start, end))
            if split > start + max_chars // 2:
                end = split + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def reset_index(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM corpus_chunks_fts")
    conn.execute("DELETE FROM corpus_chunks")


def count_pdf_chunks(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM corpus_chunks WHERE chunk_kind = 'pdf'"
    ).fetchone()[0]


def _delete_existing(
    conn: sqlite3.Connection,
    source_type: str,
    source_id: str,
    chunk_kind: str,
) -> None:
    rows = conn.execute(
        """
        SELECT id FROM corpus_chunks
        WHERE source_type = ? AND source_id = ? AND chunk_kind = ?
        """,
        (source_type, source_id, chunk_kind),
    ).fetchall()
    for row in rows:
        conn.execute("DELETE FROM corpus_chunks_fts WHERE rowid = ?", (row["id"],))
    conn.execute(
        """
        DELETE FROM corpus_chunks
        WHERE source_type = ? AND source_id = ? AND chunk_kind = ?
        """,
        (source_type, source_id, chunk_kind),
    )


def index_chunks(
    conn: sqlite3.Connection,
    *,
    source_type: str,
    source_id: str,
    chunk_kind: str,
    chunks: Iterable[str],
    doi: str | None,
    lead_id: str | None,
    title: str | None,
    venue: str | None,
    publication_year: int | None,
    source_key: str | None,
    path: str | None = None,
) -> int:
    _delete_existing(conn, source_type, source_id, chunk_kind)
    count = 0
    for index, chunk in enumerate(chunks):
        chunk = clean_text(chunk)
        if not chunk:
            continue
        cursor = conn.execute(
            """
            INSERT INTO corpus_chunks (
                source_type, source_id, chunk_kind, chunk_index, doi, lead_id,
                title, venue, publication_year, source_key, path, text, text_hash,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                source_type,
                source_id,
                chunk_kind,
                index,
                doi,
                lead_id,
                title,
                venue,
                publication_year,
                source_key,
                path,
                chunk,
                text_hash(chunk),
            ),
        )
        rowid = cursor.lastrowid
        conn.execute(
            """
            INSERT INTO corpus_chunks_fts(rowid, title, text, venue, source_key)
            VALUES (?, ?, ?, ?, ?)
            """,
            (rowid, title or "", chunk, venue or "", source_key or ""),
        )
        count += 1
    return count


def index_abstracts(
    conn: sqlite3.Connection,
    *,
    source: str = "all",
    limit: int | None = None,
) -> int:
    indexed = 0
    if source in {"all", "works"}:
        sql = """
            SELECT doi, title, abstract, venue, publication_year, source_key
            FROM works
            WHERE abstract IS NOT NULL AND length(trim(abstract)) > 0
            ORDER BY publication_year DESC, updated_at DESC
        """
        rows = conn.execute(sql + (" LIMIT ?" if limit else ""), ((limit,) if limit else ())).fetchall()
        for row in rows:
            indexed += index_chunks(
                conn,
                source_type="work",
                source_id=row["doi"],
                chunk_kind="abstract",
                chunks=chunk_text(row["abstract"]),
                doi=row["doi"],
                lead_id=None,
                title=row["title"],
                venue=row["venue"],
                publication_year=row["publication_year"],
                source_key=row["source_key"],
            )

    if source in {"all", "leads"}:
        sql = """
            SELECT id, title, abstract, venue, publication_year, source_key, matched_doi
            FROM leads
            WHERE abstract IS NOT NULL AND length(trim(abstract)) > 0
            ORDER BY publication_year DESC, updated_at DESC
        """
        rows = conn.execute(sql + (" LIMIT ?" if limit else ""), ((limit,) if limit else ())).fetchall()
        for row in rows:
            indexed += index_chunks(
                conn,
                source_type="lead",
                source_id=row["id"],
                chunk_kind="abstract",
                chunks=chunk_text(row["abstract"]),
                doi=row["matched_doi"],
                lead_id=row["id"],
                title=row["title"],
                venue=row["venue"],
                publication_year=row["publication_year"],
                source_key=row["source_key"],
            )
    return indexed


def extract_pdf_text(path: Path, max_pages: int | None = None) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError(PDF_IMPORT_ERROR) from exc

    reader = PdfReader(str(path))
    page_count = len(reader.pages)
    if max_pages is not None:
        page_count = min(page_count, max_pages)
    parts = []
    for page in reader.pages[:page_count]:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return clean_text(" ".join(parts))


def index_pdfs(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    max_pages: int | None = None,
) -> tuple[int, int]:
    sql = """
        SELECT doi, title, venue, publication_year, source_key, pdf_path
        FROM works
        WHERE pdf_path IS NOT NULL AND length(trim(pdf_path)) > 0
        ORDER BY publication_year DESC, updated_at DESC
    """
    rows = conn.execute(sql + (" LIMIT ?" if limit else ""), ((limit,) if limit else ())).fetchall()
    indexed = 0
    missing = 0
    for row in rows:
        path = Path(row["pdf_path"])
        if not path.exists():
            missing += 1
            continue
        try:
            text = extract_pdf_text(path, max_pages=max_pages)
        except RuntimeError:
            raise  # pypdf itself is unavailable; retrying other files cannot help
        except Exception:
            # Encrypted, corrupt, or otherwise unreadable file. One bad PDF must
            # not abort a whole-corpus indexing run.
            missing += 1
            continue
        indexed += index_chunks(
            conn,
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
    return indexed, missing


def pdf_candidates(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    dois: list[str] | None = None,
    limit: int = 25,
    from_year: int | None = None,
    until_year: int | None = None,
    source_key: str | None = None,
    overwrite: bool = False,
) -> list[sqlite3.Row]:
    filters = [
        "w.best_pdf_url IS NOT NULL",
        "length(trim(w.best_pdf_url)) > 0",
    ]
    params: list[Any] = []
    if not overwrite:
        filters.append("(w.pdf_path IS NULL OR length(trim(w.pdf_path)) = 0)")
    if from_year is not None:
        filters.append("w.publication_year >= ?")
        params.append(from_year)
    if until_year is not None:
        filters.append("w.publication_year <= ?")
        params.append(until_year)
    if source_key:
        filters.append("w.source_key = ?")
        params.append(source_key)

    if dois:
        normalized = [doi.strip().lower() for doi in dois if doi and doi.strip()]
        if not normalized:
            return []
        placeholders = ", ".join("?" for _ in normalized)
        filters.append(f"w.doi IN ({placeholders})")
        params.extend(normalized)
        order = "ORDER BY w.publication_year DESC, w.updated_at DESC"
    elif query:
        matches = search_corpus(
            conn,
            query,
            limit=max(limit * 4, limit),
            from_year=from_year,
            until_year=until_year,
            source_key=source_key,
            chunk_kind="all",
        )
        dois = []
        seen = set()
        for row in matches:
            doi = row["doi"]
            if doi and doi not in seen:
                dois.append(doi)
                seen.add(doi)
        if not dois:
            return []
        placeholders = ", ".join("?" for _ in dois)
        filters.append(f"w.doi IN ({placeholders})")
        params.extend(dois)
        order = f"ORDER BY CASE w.doi {' '.join(f'WHEN ? THEN {i}' for i, _ in enumerate(dois))} ELSE {len(dois)} END"
        params.extend(dois)
    else:
        order = "ORDER BY w.publication_year DESC, w.updated_at DESC"

    params.append(limit)
    return conn.execute(
        f"""
        SELECT w.doi, w.title, w.venue, w.publication_year, w.source_key, w.best_pdf_url, w.pdf_path
        FROM works AS w
        WHERE {" AND ".join(filters)}
        {order}
        LIMIT ?
        """,
        params,
    ).fetchall()


# Hosts that block or booby-trap non-browser downloads. SSRN delivery links are
# access-controlled by design; skipping them is project policy, not a bug.
BLOCKED_PDF_HOSTS = ("papers.ssrn.com", "ssrn.com/sol3")


def oa_pdf_urls(conn: sqlite3.Connection, doi: str, best_pdf_url: str | None) -> list[str]:
    """All recorded OA PDF URLs for a DOI: best location first, then
    repository-hosted alternates (less likely to bot-block), then other
    publisher copies, from the stored raw Unpaywall response."""
    urls: list[str] = []
    if best_pdf_url:
        urls.append(best_pdf_url)
    row = conn.execute(
        "SELECT raw_json FROM raw_records WHERE provider = 'unpaywall' AND provider_id = ?",
        (doi,),
    ).fetchone()
    if row:
        try:
            locations = json.loads(row["raw_json"]).get("oa_locations") or []
        except (json.JSONDecodeError, AttributeError):
            locations = []
        for want_repository in (True, False):
            for location in locations:
                url = location.get("url_for_pdf")
                is_repository = location.get("host_type") == "repository"
                if url and is_repository == want_repository and url not in urls:
                    urls.append(url)
    return [url for url in urls if not any(host in url for host in BLOCKED_PDF_HOSTS)]


def download_candidate_pdfs(
    conn: sqlite3.Connection,
    *,
    query: str | None = None,
    dois: list[str] | None = None,
    limit: int = 25,
    from_year: int | None = None,
    until_year: int | None = None,
    source_key: str | None = None,
    overwrite: bool = False,
    index_after: bool = False,
    max_pdf_pages: int | None = None,
    pause: float = 0.5,
) -> dict[str, Any]:
    from .db import upsert_work
    from .fetchers import save_pdf
    from .settings import default_pdf_path

    stats: dict[str, Any] = {
        "candidates": 0,
        "downloaded": 0,
        "failed": 0,
        "indexed_chunks": 0,
        "results": [],
    }
    rows = pdf_candidates(
        conn,
        query=query,
        dois=dois,
        limit=limit,
        from_year=from_year,
        until_year=until_year,
        source_key=source_key,
        overwrite=overwrite,
    )
    stats["candidates"] = len(rows)
    pdf_dir = default_pdf_path()
    for row in rows:
        urls = oa_pdf_urls(conn, row["doi"], row["best_pdf_url"])
        path = None
        for url in urls:
            try:
                path = save_pdf(row["doi"], url, pdf_dir)
            except Exception:
                path = None
            if pause:
                time.sleep(pause)
            if path:
                break
        if not path:
            reason = "no usable OA PDF URL" if not urls else f"all {len(urls)} recorded OA locations failed"
            stats["failed"] += 1
            stats["results"].append({"doi": row["doi"], "title": row["title"], "status": reason})
            continue
        upsert_work(conn, {"doi": row["doi"], "pdf_path": str(path)})
        # Release the write lock before the slow PDF text extraction so a
        # concurrently running server or backfill is not starved out.
        conn.commit()
        stats["downloaded"] += 1
        chunk_count = 0
        if index_after:
            try:
                text = extract_pdf_text(path, max_pages=max_pdf_pages)
            except Exception:
                stats["results"].append({"doi": row["doi"], "title": row["title"], "status": "downloaded; text extraction failed"})
                continue
            chunk_count = index_chunks(
                conn,
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
            stats["indexed_chunks"] += chunk_count
        stats["results"].append(
            {
                "doi": row["doi"],
                "title": row["title"],
                "status": f"downloaded; {chunk_count} chunks indexed" if index_after else "downloaded",
            }
        )
        conn.commit()
    return stats


def fts_query(user_query: str) -> str:
    tokens = TOKEN_RE.findall(user_query)
    cleaned = []
    for token in tokens:
        token = token.strip("-_/&.")
        if len(token) < 3:
            continue
        if token.lower() in QUERY_STOPWORDS:
            continue
        cleaned.append(token.replace('"', ""))
    if not cleaned:
        return '""'
    return " OR ".join(f'"{token}"' for token in cleaned[:18])


def search_corpus(
    conn: sqlite3.Connection,
    query: str,
    *,
    limit: int = 12,
    from_year: int | None = None,
    until_year: int | None = None,
    source_key: str | None = None,
    chunk_kind: str | None = None,
    doi: str | None = None,
) -> list[sqlite3.Row]:
    where = ["corpus_chunks_fts MATCH ?"]
    params: list[Any] = [fts_query(query)]
    if doi:
        where.append("c.doi = ?")
        params.append(doi.strip().lower())
    if from_year is not None:
        where.append("c.publication_year >= ?")
        params.append(from_year)
    if until_year is not None:
        where.append("c.publication_year <= ?")
        params.append(until_year)
    if source_key:
        where.append("c.source_key = ?")
        params.append(source_key)
    if chunk_kind and chunk_kind != "all":
        where.append("c.chunk_kind = ?")
        params.append(chunk_kind)
    params.append(limit)
    return conn.execute(
        f"""
        SELECT
            c.id, c.source_type, c.source_id, c.chunk_kind, c.chunk_index,
            c.doi, c.lead_id, c.title, c.venue, c.publication_year,
            c.source_key, c.path, c.text,
            bm25(corpus_chunks_fts) AS rank
        FROM corpus_chunks_fts
        JOIN corpus_chunks AS c ON c.id = corpus_chunks_fts.rowid
        WHERE {" AND ".join(where)}
        ORDER BY rank
        LIMIT ?
        """,
        params,
    ).fetchall()


def corpus_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    year_row = conn.execute(
        "SELECT MIN(publication_year), MAX(publication_year) FROM works WHERE publication_year IS NOT NULL"
    ).fetchone()
    return {
        "works": conn.execute("SELECT COUNT(*) FROM works").fetchone()[0],
        "leads": conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0],
        "chunks": conn.execute("SELECT COUNT(*) FROM corpus_chunks").fetchone()[0],
        "works_with_abstract": conn.execute(
            "SELECT COUNT(*) FROM works WHERE abstract IS NOT NULL AND length(trim(abstract)) > 0"
        ).fetchone()[0],
        "pdf_chunks": conn.execute(
            "SELECT COUNT(*) FROM corpus_chunks WHERE chunk_kind = 'pdf'"
        ).fetchone()[0],
        "year_min": year_row[0],
        "year_max": year_row[1],
        "by_source": conn.execute(
            "SELECT source_key, COUNT(*) AS n FROM works GROUP BY source_key ORDER BY n DESC"
        ).fetchall(),
        "by_venue": conn.execute(
            """
            SELECT venue, COUNT(*) AS n FROM works
            WHERE venue IS NOT NULL AND length(trim(venue)) > 0
            GROUP BY venue ORDER BY n DESC LIMIT 15
            """
        ).fetchall(),
        "by_year": conn.execute(
            """
            SELECT publication_year AS year, COUNT(*) AS n FROM works
            WHERE publication_year IS NOT NULL
            GROUP BY publication_year ORDER BY publication_year
            """
        ).fetchall(),
    }


def corpus_trends(
    conn: sqlite3.Connection,
    query: str,
    *,
    from_year: int | None = None,
    until_year: int | None = None,
    source_key: str | None = None,
    sample_limit: int = 400,
) -> dict[str, Any]:
    """Aggregate matching papers by year, venue, and method/data signals.

    Deduplicates chunks to one row per paper so PDF-indexed works do not
    dominate the counts.
    """
    rows = search_corpus(
        conn,
        query,
        limit=sample_limit,
        from_year=from_year,
        until_year=until_year,
        source_key=source_key,
        chunk_kind="all",
    )
    papers: dict[str, sqlite3.Row] = {}
    for row in rows:
        key = row["doi"] or row["lead_id"] or f"{row['source_type']}:{row['source_id']}"
        papers.setdefault(key, row)

    by_year: Counter[int] = Counter()
    venues: Counter[str] = Counter()
    methods_by_year: dict[int, Counter[str]] = {}
    data_by_year: dict[int, Counter[str]] = {}
    for row in papers.values():
        year = row["publication_year"]
        venue = row["venue"] or row["source_key"] or "unknown"
        venues[venue] += 1
        if year is None:
            continue
        by_year[year] += 1
        text = f"{row['title'] or ''} {row['text'] or ''}"
        for label, pattern in METHOD_PATTERNS.items():
            if pattern.search(text):
                methods_by_year.setdefault(year, Counter())[label] += 1
        for label, pattern in DATA_PATTERNS.items():
            if pattern.search(text):
                data_by_year.setdefault(year, Counter())[label] += 1

    return {
        "matched_papers": len(papers),
        "sampled_chunks": len(rows),
        "by_year": dict(sorted(by_year.items())),
        "top_venues": venues.most_common(12),
        "methods_by_year": {year: counter.most_common(6) for year, counter in sorted(methods_by_year.items())},
        "data_by_year": {year: counter.most_common(6) for year, counter in sorted(data_by_year.items())},
    }


def signal_counts(rows: Iterable[sqlite3.Row]) -> tuple[Counter[str], Counter[str]]:
    method_counts: Counter[str] = Counter()
    data_counts: Counter[str] = Counter()
    for row in rows:
        text = f"{row['title'] or ''} {row['text'] or ''}"
        for label, pattern in METHOD_PATTERNS.items():
            if pattern.search(text):
                method_counts[label] += 1
        for label, pattern in DATA_PATTERNS.items():
            if pattern.search(text):
                data_counts[label] += 1
    return method_counts, data_counts


def snippet(text: str, max_chars: int = 420) -> str:
    text = clean_text(text)
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rsplit(" ", 1)[0] + "..."


def format_result(row: sqlite3.Row, index: int, *, include_snippet: bool = True) -> str:
    year = row["publication_year"] or "n.d."
    venue = row["venue"] or row["source_key"] or "unknown"
    ident = row["doi"] or row["lead_id"] or row["source_id"]
    lines = [
        f"{index}. {year} | {venue} | {row['title'] or 'Untitled'}",
        f"   {row['chunk_kind']} | {ident}",
    ]
    if include_snippet:
        lines.append(f"   {snippet(row['text'])}")
    return "\n".join(lines)
