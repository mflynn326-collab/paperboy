from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from .config import DEFAULT_DB


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS works (
    doi TEXT PRIMARY KEY,
    title TEXT,
    abstract TEXT,
    publication_date TEXT,
    publication_year INTEGER,
    venue TEXT,
    source_key TEXT,
    source_tier TEXT,
    publisher TEXT,
    issn TEXT,
    url TEXT,
    crossref_url TEXT,
    openalex_id TEXT,
    is_oa INTEGER,
    oa_status TEXT,
    best_pdf_url TEXT,
    best_landing_url TEXT,
    license TEXT,
    pdf_path TEXT,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS authors (
    doi TEXT NOT NULL,
    position INTEGER NOT NULL,
    given TEXT,
    family TEXT,
    orcid TEXT,
    raw_json TEXT,
    PRIMARY KEY (doi, position),
    FOREIGN KEY (doi) REFERENCES works(doi)
);

CREATE TABLE IF NOT EXISTS raw_records (
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    doi TEXT,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (provider, provider_id)
);

CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    title TEXT NOT NULL,
    abstract TEXT,
    authors_text TEXT,
    event_date TEXT,
    publication_year INTEGER,
    venue TEXT,
    source_key TEXT,
    source_tier TEXT,
    url TEXT,
    pdf_url TEXT,
    matched_doi TEXT,
    metadata_json TEXT NOT NULL,
    first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider, provider_id),
    FOREIGN KEY (matched_doi) REFERENCES works(doi)
);

CREATE TABLE IF NOT EXISTS corpus_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    chunk_kind TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    doi TEXT,
    lead_id TEXT,
    title TEXT,
    venue TEXT,
    publication_year INTEGER,
    source_key TEXT,
    path TEXT,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_type, source_id, chunk_kind, chunk_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS corpus_chunks_fts USING fts5(
    title,
    text,
    venue,
    source_key,
    content='corpus_chunks',
    content_rowid='id'
);

CREATE INDEX IF NOT EXISTS idx_corpus_chunks_source
    ON corpus_chunks(source_type, source_id, chunk_kind);

CREATE INDEX IF NOT EXISTS idx_corpus_chunks_filters
    ON corpus_chunks(publication_year, source_key, chunk_kind);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    source_key TEXT,
    issn TEXT,
    from_date TEXT,
    until_date TEXT,
    status TEXT NOT NULL,
    records_seen INTEGER DEFAULT 0,
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
"""


def connect(db_path: Path = DEFAULT_DB) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # The MCP server and CLI backfills can write concurrently (WAL journal);
    # wait out the other writer's transaction instead of failing immediately.
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(db_path: Path = DEFAULT_DB) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip()
    doi = doi.removeprefix("https://doi.org/")
    doi = doi.removeprefix("http://doi.org/")
    return doi.lower()


def upsert_work(conn: sqlite3.Connection, work: dict[str, Any]) -> None:
    doi = normalize_doi(work.get("doi"))
    if not doi:
        return

    payload = {
        "doi": doi,
        "title": work.get("title"),
        "abstract": work.get("abstract"),
        "publication_date": work.get("publication_date"),
        "publication_year": work.get("publication_year"),
        "venue": work.get("venue"),
        "source_key": work.get("source_key"),
        "source_tier": work.get("source_tier"),
        "publisher": work.get("publisher"),
        "issn": work.get("issn"),
        "url": work.get("url"),
        "crossref_url": work.get("crossref_url"),
        "openalex_id": work.get("openalex_id"),
        "is_oa": work.get("is_oa"),
        "oa_status": work.get("oa_status"),
        "best_pdf_url": work.get("best_pdf_url"),
        "best_landing_url": work.get("best_landing_url"),
        "license": work.get("license"),
        "pdf_path": work.get("pdf_path"),
    }

    columns = list(payload)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(
        f"{column}=COALESCE(excluded.{column}, works.{column})"
        for column in columns
        if column != "doi"
    )
    conn.execute(
        f"""
        INSERT INTO works ({", ".join(columns)}, updated_at)
        VALUES ({placeholders}, CURRENT_TIMESTAMP)
        ON CONFLICT(doi) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP
        """,
        payload,
    )


def replace_authors(conn: sqlite3.Connection, doi: str, authors: list[dict[str, Any]]) -> None:
    doi = normalize_doi(doi)
    if not doi:
        return
    conn.execute("DELETE FROM authors WHERE doi = ?", (doi,))
    for position, author in enumerate(authors, start=1):
        conn.execute(
            """
            INSERT INTO authors (doi, position, given, family, orcid, raw_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                doi,
                position,
                author.get("given"),
                author.get("family"),
                author.get("orcid"),
                json.dumps(author, sort_keys=True),
            ),
        )


def save_raw_record(
    conn: sqlite3.Connection,
    provider: str,
    provider_id: str,
    raw: dict[str, Any],
    doi: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO raw_records (provider, provider_id, doi, raw_json)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(provider, provider_id) DO UPDATE SET
            doi=excluded.doi,
            fetched_at=CURRENT_TIMESTAMP,
            raw_json=excluded.raw_json
        """,
        (provider, provider_id, normalize_doi(doi), json.dumps(raw, sort_keys=True)),
    )


def upsert_lead(conn: sqlite3.Connection, lead: dict[str, Any]) -> None:
    provider = str(lead["provider"])
    provider_id = str(lead["provider_id"])
    lead_id = f"{provider}:{provider_id}"
    payload = {
        "id": lead_id,
        "provider": provider,
        "provider_id": provider_id,
        "title": lead["title"],
        "abstract": lead.get("abstract"),
        "authors_text": lead.get("authors_text"),
        "event_date": lead.get("event_date"),
        "publication_year": lead.get("publication_year"),
        "venue": lead.get("venue"),
        "source_key": lead.get("source_key"),
        "source_tier": lead.get("source_tier"),
        "url": lead.get("url"),
        "pdf_url": lead.get("pdf_url"),
        "matched_doi": normalize_doi(lead.get("matched_doi")),
        "metadata_json": json.dumps(lead.get("metadata", {}), sort_keys=True),
    }
    columns = list(payload)
    placeholders = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(
        f"{column}=COALESCE(excluded.{column}, leads.{column})"
        for column in columns
        if column not in {"id", "provider", "provider_id"}
    )
    conn.execute(
        f"""
        INSERT INTO leads ({", ".join(columns)}, updated_at)
        VALUES ({placeholders}, CURRENT_TIMESTAMP)
        ON CONFLICT(provider, provider_id) DO UPDATE SET {updates}, updated_at=CURRENT_TIMESTAMP
        """,
        payload,
    )


TITLE_KEY_RE = re.compile(r"[^a-z0-9]+")


def title_key(title: str | None) -> str:
    return TITLE_KEY_RE.sub(" ", (title or "").lower()).strip()


def exact_title_match(conn: sqlite3.Connection, title: str | None) -> str | None:
    key = title_key(title)
    if not key:
        return None
    rows = conn.execute("SELECT doi, title FROM works WHERE title IS NOT NULL").fetchall()
    for row in rows:
        if title_key(row["title"]) == key:
            return row["doi"]
    return None
