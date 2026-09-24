from __future__ import annotations

import csv
from bisect import bisect_right
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urljoin

from .db import exact_title_match, save_raw_record, upsert_lead
from .http import download
from .text import clean_markup


REQUIRED_COLUMNS = {"title"}
OPTIONAL_COLUMNS = {
    "id",
    "authors",
    "abstract",
    "date",
    "year",
    "venue",
    "url",
    "pdf_url",
    "session",
}


AFA_PROGRAM_URLS = {
    2024: "https://afajof.org/management/full-program2024.html",
    2025: "https://afajof.org/management/full-program2025.html",
    2026: "https://afajof.org/management/full-program2026.html",
}
WFA_PROGRAM_URLS = {2026: "https://westernfinance-portal.org/conference"}
PAPER_LINK_RE = re.compile(
    r'<a\s+href=["\'](?P<href>viewp\.php\?n=(?P<id>\d+))["\'][^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
SESSION_RE = re.compile(r"<h5>\s*Session:\s*(?P<name>.*?)</h5>", re.IGNORECASE | re.DOTALL)
PARAGRAPH_RE = re.compile(r"<p[^>]*>(?P<content>.*?)</p>", re.IGNORECASE | re.DOTALL)
STRONG_RE = re.compile(r"<strong>(?P<name>.*?)</strong>", re.IGNORECASE | re.DOTALL)
WFA_SESSION_HREF_RE = re.compile(
    r'href=["\'](?P<href>conferencesession\?Session=(?P<id>\d+[^"\']*))["\']',
    re.IGNORECASE,
)
WFA_SESSION_TITLE_RE = re.compile(r"<h2>\s*<a[^>]*>(?P<title>.*?)</a>\s*</h2>", re.IGNORECASE | re.DOTALL)
WFA_DATE_RE = re.compile(
    r'<i\s+class=["\']fa fa-calendar["\'][^>]*></i>\s*(?P<date>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)
WFA_PAPER_RE = re.compile(
    r'<h5>\s*<a\s+href=["\'](?P<href>[^"\']*viewpaper\?n=(?P<id>\d+)[^"\']*)["\'][^>]*>(?P<title>.*?)<i\b',
    re.IGNORECASE | re.DOTALL,
)
WFA_HEADING_RE = re.compile(r"<h6>(?P<content>.*?)</h6>", re.IGNORECASE | re.DOTALL)
WFA_ABSTRACT_RE = re.compile(
    r'<p\s+class=["\']abstract["\'][^>]*>(?P<content>.*?)</p>',
    re.IGNORECASE | re.DOTALL,
)


def _clean_html(value: str | None) -> str | None:
    return clean_markup(value)


def parse_afa_program(year: int, url: str, html: str) -> list[dict]:
    """Extract paper entries from the public 2024+ AFA program layout."""
    sessions = [
        (match.start(), _clean_html(match.group("name")) or "AFA Paper Session")
        for match in SESSION_RE.finditer(html)
    ]
    positions = [position for position, _ in sessions]
    records = []
    for match in PAPER_LINK_RE.finditer(html):
        title = _clean_html(match.group("title"))
        if not title:
            continue
        paragraph = PARAGRAPH_RE.search(html, match.end())
        authors = []
        if paragraph and paragraph.start() - match.end() < 2000:
            authors = [
                name
                for name in (_clean_html(item.group("name")) for item in STRONG_RE.finditer(paragraph.group("content")))
                if name
            ]
        session_index = bisect_right(positions, match.start()) - 1
        session = sessions[session_index][1] if session_index >= 0 else "AFA Paper Session"
        records.append(
            {
                "provider": "afa",
                "provider_id": f"{year}:{match.group('id')}",
                "title": title,
                "authors_text": "; ".join(authors) or None,
                "event_date": f"{year}-01-01",
                "publication_year": year,
                "venue": f"AFA Annual Meeting {year}",
                "source_key": "afa",
                "source_tier": "conference",
                "url": urljoin(url, match.group("href")),
                "matched_doi": None,
                "metadata": {"program_url": url, "session": session},
            }
        )
    return records


def harvest_afa_programs(conn: sqlite3.Connection, years: list[int]) -> int:
    total = 0
    for year in years:
        try:
            url = AFA_PROGRAM_URLS[year]
        except KeyError as exc:
            available = ", ".join(str(value) for value in sorted(AFA_PROGRAM_URLS))
            raise ValueError(f"AFA program is not configured for {year}. Available years: {available}") from exc
        body, _ = download(url)
        html = body.decode("utf-8", errors="replace")
        for record in parse_afa_program(year, url, html):
            record["matched_doi"] = exact_title_match(conn, record["title"])
            upsert_lead(conn, record)
            save_raw_record(conn, "afa_program", record["provider_id"], record, doi=record["matched_doi"])
            total += 1
            if total % 100 == 0:
                conn.commit()
        conn.commit()
    return total


def _wfa_author_name(value: str) -> str | None:
    value = re.sub(r"<em>.*?</em>", "", value, flags=re.IGNORECASE | re.DOTALL)
    return (_clean_html(value) or "").rstrip(", ") or None


def parse_wfa_session(year: int, session_url: str, session_id: str, html: str) -> list[dict]:
    title_match = WFA_SESSION_TITLE_RE.search(html)
    session = _clean_html(title_match.group("title")) if title_match else "WFA Paper Session"
    date_match = WFA_DATE_RE.search(html)
    event_date = _clean_html(date_match.group("date")) if date_match else f"{year}-01-01"
    matches = list(WFA_PAPER_RE.finditer(html))
    records = []
    for index, match in enumerate(matches):
        title = _clean_html(match.group("title"))
        if not title:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(html)
        block = html[match.end() : end]
        authors = []
        for heading in WFA_HEADING_RE.finditer(block):
            content = heading.group("content")
            heading_text = _clean_html(content) or ""
            if heading_text.startswith(("Discussant:", "Chair:")):
                continue
            strong = STRONG_RE.search(content)
            if strong:
                author = _wfa_author_name(strong.group("name"))
                if author:
                    authors.append(author)
        abstract_match = WFA_ABSTRACT_RE.search(block)
        abstract = _clean_html(abstract_match.group("content")) if abstract_match else None
        if abstract and abstract.lower().startswith("abstract:"):
            abstract = abstract[len("abstract:") :].strip() or None
        records.append(
            {
                "provider": "wfa",
                "provider_id": f"{year}:{match.group('id')}",
                "title": title,
                "abstract": abstract,
                "authors_text": "; ".join(authors) or None,
                "event_date": event_date,
                "publication_year": year,
                "venue": f"WFA Annual Meeting {year}",
                "source_key": "wfa",
                "source_tier": "conference",
                "url": urljoin(session_url, match.group("href")),
                "matched_doi": None,
                "metadata": {"program_url": session_url, "session": session, "session_id": session_id},
            }
        )
    return records


def harvest_wfa_program(conn: sqlite3.Connection, year: int) -> int:
    try:
        program_url = WFA_PROGRAM_URLS[year]
    except KeyError as exc:
        available = ", ".join(str(value) for value in sorted(WFA_PROGRAM_URLS))
        raise ValueError(f"WFA program is not configured for {year}. Available years: {available}") from exc

    body, _ = download(program_url)
    program_html = body.decode("utf-8", errors="replace")
    sessions = list(dict.fromkeys(
        (match.group("id"), urljoin(program_url, match.group("href")))
        for match in WFA_SESSION_HREF_RE.finditer(program_html)
    ))
    total = 0
    for session_id, session_url in sessions:
        body, _ = download(session_url)
        html = body.decode("utf-8", errors="replace")
        for record in parse_wfa_session(year, session_url, session_id, html):
            record["matched_doi"] = exact_title_match(conn, record["title"])
            upsert_lead(conn, record)
            save_raw_record(conn, "wfa_program", record["provider_id"], record, doi=record["matched_doi"])
            total += 1
            if total % 100 == 0:
                conn.commit()
        conn.commit()
        time.sleep(0.05)
    return total


def import_conference_csv(
    conn: sqlite3.Connection,
    provider: str,
    path: Path,
    default_venue: str | None = None,
    source_tier: str = "conference",
) -> int:
    """Import a CSV saved from an official conference program or export.

    The sole required column is `title`. Optional columns are documented in the
    README and retained verbatim in raw_records for auditability.
    """
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")

        total = 0
        for row_number, row in enumerate(reader, start=2):
            title = clean_markup(row.get("title"))
            if not title:
                continue
            provider_id = (row.get("id") or f"{path.stem}:{row_number}").strip()
            year = row.get("year")
            try:
                publication_year = int(year) if year else None
            except ValueError:
                publication_year = None
            lead = {
                "provider": provider,
                "provider_id": provider_id,
                "title": title,
                "abstract": clean_markup(row.get("abstract")),
                "authors_text": clean_markup(row.get("authors")),
                "event_date": clean_markup(row.get("date")),
                "publication_year": publication_year,
                "venue": clean_markup(row.get("venue")) or default_venue,
                "source_key": provider,
                "source_tier": source_tier,
                "url": clean_markup(row.get("url")),
                "pdf_url": clean_markup(row.get("pdf_url")),
                "matched_doi": exact_title_match(conn, title),
                "metadata": {key: value for key, value in row.items() if key in OPTIONAL_COLUMNS},
            }
            upsert_lead(conn, lead)
            save_raw_record(conn, provider, provider_id, row, doi=lead["matched_doi"])
            total += 1
            if total % 100 == 0:
                conn.commit()
    conn.commit()
    return total
