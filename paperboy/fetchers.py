from __future__ import annotations

from pathlib import Path
import sqlite3
import time
from typing import Iterator
from urllib.error import HTTPError

from .config import Journal
from .db import exact_title_match, normalize_doi, replace_authors, save_raw_record, upsert_lead, upsert_work
from .http import download, get_json
from .settings import default_pdf_path
from .text import clean_abstract, date_parts_to_iso, first_text, openalex_abstract, safe_filename


CROSSREF_API = "https://api.crossref.org"
OPENALEX_API = "https://api.openalex.org"
UNPAYWALL_API = "https://api.unpaywall.org/v2"
SSRN_OPENALEX_SOURCE = "S4210172589"
# OpenAlex's broad Finance concept covers an impractically large set of adjacent
# disciplines. These two concepts are the high-signal finance-research subset.
FINANCE_CONCEPT_IDS = ("C106159729", "C80515813")


def iter_crossref_items(
    journal: Journal,
    from_date: str,
    until_date: str,
    mailto: str | None,
    rows: int = 100,
    max_pages: int | None = None,
) -> Iterator[tuple[str, dict]]:
    seen_dois: set[str] = set()
    rows = min(max(rows, 1), 100)

    # Crossref resolves a journal's print and online ISSNs to the same corpus.
    # Query the configured primary ISSN once to avoid duplicate pagination.
    for issn in journal.issns[:1]:
        cursor = "*"
        page = 0
        while True:
            params = {
                "filter": f"from-pub-date:{from_date},until-pub-date:{until_date},type:journal-article",
                "rows": rows,
                "cursor": cursor,
                "mailto": mailto,
            }
            data = get_json(f"{CROSSREF_API}/journals/{issn}/works", params=params)
            message = data.get("message", {})
            items = message.get("items", [])
            for item in items:
                doi = normalize_doi(item.get("DOI"))
                if not doi or doi in seen_dois:
                    continue
                seen_dois.add(doi)
                yield issn, item

            page += 1
            cursor = message.get("next-cursor")
            if not items or not cursor or (max_pages is not None and page >= max_pages):
                break
            time.sleep(0.1)


def crossref_to_work(journal: Journal, issn: str, item: dict) -> dict:
    published = (
        item.get("published-print")
        or item.get("published-online")
        or item.get("published")
        or item.get("created")
    )
    publication_date = date_parts_to_iso(published.get("date-parts") if published else None)
    return {
        "doi": item.get("DOI"),
        "title": first_text(item.get("title")),
        "abstract": clean_abstract(item.get("abstract")),
        "publication_date": publication_date,
        "publication_year": int(publication_date[:4]) if publication_date else None,
        "venue": first_text(item.get("container-title")) or journal.name,
        "source_key": journal.key,
        "source_tier": journal.tier,
        "publisher": item.get("publisher") or journal.publisher,
        "issn": issn,
        "url": item.get("URL"),
        "crossref_url": item.get("resource", {}).get("primary", {}).get("URL"),
    }


def harvest_crossref(
    conn: sqlite3.Connection,
    journals: list[Journal],
    from_date: str,
    until_date: str,
    mailto: str | None,
    rows: int,
    max_pages: int | None,
) -> int:
    total = 0
    for journal in journals:
        journal_count = 0
        for issn, item in iter_crossref_items(journal, from_date, until_date, mailto, rows, max_pages):
            work = crossref_to_work(journal, issn, item)
            doi = normalize_doi(work.get("doi"))
            if not doi:
                continue
            upsert_work(conn, work)
            replace_authors(conn, doi, item.get("author", []))
            save_raw_record(conn, "crossref", doi, item, doi=doi)
            total += 1
            journal_count += 1
            if journal_count % 100 == 0:
                conn.commit()
        conn.commit()
    return total


def iter_openalex_items(
    journal: Journal,
    from_date: str,
    until_date: str,
    mailto: str | None,
    api_key: str | None,
    per_page: int = 100,
    max_pages: int | None = None,
) -> Iterator[tuple[str, dict]]:
    seen_ids: set[str] = set()
    per_page = min(max(per_page, 1), 200)
    optional_params = {}
    if mailto:
        optional_params["mailto"] = mailto
    if api_key:
        optional_params["api_key"] = api_key

    # OpenAlex source records include print and online ISSNs together; querying
    # the configured primary ISSN avoids duplicate result pages.
    for issn in journal.issns[:1]:
        page = 1
        while True:
            params = {
                "filter": (
                    f"primary_location.source.issn:{issn},"
                    f"from_publication_date:{from_date},"
                    f"to_publication_date:{until_date}"
                ),
                "per-page": per_page,
                "page": page,
                "select": (
                    "id,doi,title,display_name,publication_date,publication_year,"
                    "primary_location,open_access,abstract_inverted_index,authorships"
                ),
                **optional_params,
            }
            data = get_json(f"{OPENALEX_API}/works", params=params)
            results = data.get("results", [])
            for item in results:
                item_id = item.get("id")
                if not item_id or item_id in seen_ids:
                    continue
                seen_ids.add(item_id)
                yield issn, item

            if not results or (max_pages is not None and page >= max_pages):
                break
            page += 1
            time.sleep(0.1)


def openalex_to_work(journal: Journal, issn: str, item: dict) -> dict:
    primary = item.get("primary_location") or {}
    source = primary.get("source") or {}
    oa = item.get("open_access") or {}
    return {
        "doi": item.get("doi"),
        "title": item.get("title") or item.get("display_name"),
        "abstract": openalex_abstract(item.get("abstract_inverted_index")),
        "publication_date": item.get("publication_date"),
        "publication_year": item.get("publication_year"),
        "venue": source.get("display_name") or journal.name,
        "source_key": journal.key,
        "source_tier": journal.tier,
        "publisher": journal.publisher,
        "issn": issn,
        "url": primary.get("landing_page_url") or item.get("doi"),
        "openalex_id": item.get("id"),
        "is_oa": 1 if oa.get("is_oa") else 0,
        "oa_status": oa.get("oa_status"),
        "best_pdf_url": primary.get("pdf_url") if primary.get("is_oa") else None,
        "best_landing_url": primary.get("landing_page_url"),
        "license": primary.get("license"),
    }


def openalex_authors(item: dict) -> list[dict]:
    authors = []
    for authorship in item.get("authorships", []):
        author = authorship.get("author") or {}
        display_name = author.get("display_name") or ""
        parts = display_name.rsplit(" ", 1)
        authors.append(
            {
                "given": parts[0] if len(parts) == 2 else None,
                "family": parts[-1] if parts else None,
                "orcid": author.get("orcid"),
                "openalex_id": author.get("id"),
                "raw_affiliations": authorship.get("raw_affiliation_strings", []),
            }
        )
    return authors


def harvest_openalex(
    conn: sqlite3.Connection,
    journals: list[Journal],
    from_date: str,
    until_date: str,
    mailto: str | None,
    api_key: str | None,
    per_page: int,
    max_pages: int | None,
) -> int:
    total = 0
    for journal in journals:
        journal_count = 0
        for issn, item in iter_openalex_items(
            journal, from_date, until_date, mailto, api_key, per_page, max_pages
        ):
            work = openalex_to_work(journal, issn, item)
            doi = normalize_doi(work.get("doi"))
            if not doi:
                continue
            upsert_work(conn, work)
            replace_authors(conn, doi, openalex_authors(item))
            save_raw_record(conn, "openalex", item["id"], item, doi=doi)
            total += 1
            journal_count += 1
            if journal_count % 100 == 0:
                conn.commit()
        conn.commit()
    return total


def enrich_unpaywall(
    conn: sqlite3.Connection,
    email: str,
    limit: int,
    download_pdfs: bool,
    pdf_dir: Path | None = None,
) -> int:
    pdf_dir = pdf_dir or default_pdf_path()
    rows = conn.execute(
        """
        SELECT w.doi FROM works AS w
        WHERE w.doi IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM raw_records AS r
              WHERE r.provider = 'unpaywall' AND r.provider_id = w.doi
          )
        ORDER BY w.publication_date DESC, w.updated_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()

    enriched = 0
    for row in rows:
        doi = row["doi"]
        try:
            data = get_json(f"{UNPAYWALL_API}/{doi}", params={"email": email})
        except HTTPError as exc:
            # A terminal API response is still a completed lookup. Persist it so
            # later resumptions do not repeatedly request an unavailable DOI.
            data = {"status": exc.code, "error": str(exc)}
        best = data.get("best_oa_location") or {}
        work = {
            "doi": doi,
            "is_oa": 1 if data.get("is_oa") else 0,
            "oa_status": data.get("oa_status"),
            "best_pdf_url": best.get("url_for_pdf"),
            "best_landing_url": best.get("url"),
            "license": best.get("license"),
        }

        if download_pdfs and work["best_pdf_url"]:
            path = save_pdf(doi, work["best_pdf_url"], pdf_dir)
            if path:
                work["pdf_path"] = str(path)

        upsert_work(conn, work)
        save_raw_record(conn, "unpaywall", doi, data, doi=doi)
        conn.commit()
        enriched += 1
        time.sleep(0.1)
    return enriched


def iter_ssrn_openalex_items(
    from_date: str,
    until_date: str,
    mailto: str | None,
    api_key: str | None,
    per_page: int = 100,
    max_pages: int | None = None,
) -> Iterator[dict]:
    per_page = min(max(per_page, 1), 200)
    optional_params = {}
    if mailto:
        optional_params["mailto"] = mailto
    if api_key:
        optional_params["api_key"] = api_key

    for concept_id in FINANCE_CONCEPT_IDS:
        cursor = "*"
        page = 0
        while True:
            params = {
                "filter": (
                    f"primary_location.source.id:{SSRN_OPENALEX_SOURCE},"
                    f"concepts.id:{concept_id},"
                    f"from_publication_date:{from_date},"
                    f"to_publication_date:{until_date}"
                ),
                "per-page": per_page,
                "cursor": cursor,
                "select": (
                    "id,doi,title,display_name,publication_date,publication_year,"
                    "primary_location,open_access,abstract_inverted_index,authorships"
                ),
                **optional_params,
            }
            data = get_json(f"{OPENALEX_API}/works", params=params)
            results = data.get("results", [])
            for item in results:
                yield item
            page += 1
            cursor = (data.get("meta") or {}).get("next_cursor")
            if not results or not cursor or (max_pages is not None and page >= max_pages):
                break
            time.sleep(0.1)


def harvest_ssrn_openalex(
    conn: sqlite3.Connection,
    from_date: str,
    until_date: str,
    mailto: str | None,
    api_key: str | None,
    per_page: int,
    max_pages: int | None,
) -> int:
    seen_ids: set[str] = set()
    total = 0
    for item in iter_ssrn_openalex_items(
        from_date, until_date, mailto, api_key, per_page, max_pages
    ):
        item_id = item.get("id")
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        primary = item.get("primary_location") or {}
        source = primary.get("source") or {}
        doi = normalize_doi(item.get("doi"))
        if doi:
            work = openalex_to_work(
                Journal("ssrn", "SSRN Electronic Journal", "SSRN", ("1556-5068",), "working_paper"),
                "1556-5068",
                item,
            )
            work["venue"] = source.get("display_name") or "SSRN Electronic Journal"
            upsert_work(conn, work)
            replace_authors(conn, doi, openalex_authors(item))

        title = item.get("title") or item.get("display_name")
        if title:
            authors = "; ".join(
                (entry.get("author") or {}).get("display_name", "")
                for entry in item.get("authorships", [])
                if (entry.get("author") or {}).get("display_name")
            )
            upsert_lead(
                conn,
                {
                    "provider": "ssrn_openalex",
                    "provider_id": item_id,
                    "title": title,
                    "abstract": openalex_abstract(item.get("abstract_inverted_index")),
                    "authors_text": authors or None,
                    "event_date": item.get("publication_date"),
                    "publication_year": item.get("publication_year"),
                    "venue": source.get("display_name") or "SSRN Electronic Journal",
                    "source_key": "ssrn",
                    "source_tier": "working_paper",
                    "url": primary.get("landing_page_url") or item.get("doi"),
                    "pdf_url": primary.get("pdf_url"),
                    "matched_doi": doi or exact_title_match(conn, title),
                    "metadata": item,
                },
            )
        save_raw_record(conn, "ssrn_openalex", item_id, item, doi=doi)
        total += 1
        if total % 100 == 0:
            conn.commit()
    conn.commit()
    return total


def save_pdf(doi: str, url: str, pdf_dir: Path) -> Path | None:
    pdf_dir.mkdir(parents=True, exist_ok=True)
    body, content_type = download(url)
    if content_type and "pdf" not in content_type.lower() and not body.startswith(b"%PDF"):
        return None
    path = pdf_dir / f"{safe_filename(doi)}.pdf"
    path.write_bytes(body)
    return path
