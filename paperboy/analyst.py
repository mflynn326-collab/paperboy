from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from .config import ROOT
from .knowledge import pdf_candidates, search_corpus, signal_counts, snippet


MEMORY_PATH = ROOT / "memory" / "paperboy_analyst.md"

DEFAULT_MEMORY = """# Paperboy Analyst Memory

## Mission

Paperboy is the user's research intelligence tool for academic scholarship. Its job is to help with idea generation, literature placement, methods, identification, data-source selection, target-journal strategy, and trend detection across the journals, working-paper series, and conferences in the user's corpus. The defaults below were written for empirical finance; edit them to match your field.

## Operating Principles

- Start from the local Paperboy corpus before making broad claims.
- Separate evidence from inference. Cite local records by result number, DOI, venue, and year when possible.
- Prefer research-useful synthesis over generic summary.
- Be candid about weak identification, stale literature, missing data, or thin evidence.
- For new ideas, evaluate novelty, feasible empirical settings, identification threats, likely datasets, and target journal fit.
- Treat SSRN and conference leads as early signals, not settled literature.
- Treat full-text PDF chunks as stronger evidence than abstracts when available, but do not assume the corpus has every PDF.
- Recommend web search only for questions whose answer likely changed after the last corpus refresh.

## Default Answer Shape

For open-ended research questions, use this structure unless the user asks otherwise:

1. Bottom line
2. Closest literatures and anchor papers
3. What seems crowded versus underexplored
4. Candidate empirical designs and identification threats
5. Best data sources, split into paid/proprietary and free/public where possible
6. Target-journal fit and positioning
7. Concrete next tests or corpus queries

## Journal Positioning Heuristics

- Journal of Finance, Journal of Financial Economics, and Review of Financial Studies usually require broad contribution, credible identification or theory, and clean placement in core finance debates.
- JFQA and Review of Finance can be excellent fits for strong empirical finance papers with narrower contribution or slightly less sweeping generality.
- Management Science is attractive for work with methodological novelty, operations/AI/data intensity, market design, organizational implications, or broad management relevance.
- Field journals are useful when the dataset or setting is strong but the contribution is more specialized.

## Data Source Taxonomy

Paid or institutionally licensed examples: CRSP, Compustat, Capital IQ, WRDS-linked products, TRACE, TAQ, OptionMetrics, IBES, Refinitiv, FactSet, BoardEx, ExecuComp, DealScan, PitchBook, Preqin, RavenPack, SDC/Refinitiv deals.

Free or public examples: SEC EDGAR filings, 13F filings, Form 4 insider trades, municipal disclosures, Federal Reserve data, FRED, Treasury data, FDIC call reports, NCUA, CFPB complaints, court records, patent data, GitHub/web archives where legally usable, public conference programs, OpenAlex/Crossref/Unpaywall metadata.

## Running Notes

- The corpus currently emphasizes 2020-present journals, finance-scoped SSRN through OpenAlex, AFA 2024-2026, and WFA 2026.
- Use Paperboy's bounded PDF workflow when a question needs deeper evidence: retrieve relevant abstracts first, then download/index only relevant OA PDFs.
"""


def ensure_memory(path: Path = MEMORY_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(DEFAULT_MEMORY, encoding="utf-8")
    return path


def read_memory(path: Path = MEMORY_PATH) -> str:
    ensure_memory(path)
    return path.read_text(encoding="utf-8")


def append_memory_note(note: str, path: Path = MEMORY_PATH) -> Path:
    ensure_memory(path)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"\n- {stamp}: {note.strip()}\n")
    return path


def _record_line(row: sqlite3.Row, index: int) -> str:
    year = row["publication_year"] or "n.d."
    venue = row["venue"] or row["source_key"] or "unknown venue"
    ident = row["doi"] or row["lead_id"] or row["source_id"]
    return (
        f"[R{index}] {year} | {venue} | {row['title'] or 'Untitled'}\n"
        f"Type: {row['chunk_kind']} | ID: {ident}\n"
        f"Evidence: {snippet(row['text'], 850)}"
    )


def build_context_pack(
    conn: sqlite3.Connection,
    question: str,
    *,
    limit: int,
    from_year: int | None,
    until_year: int | None,
    source_key: str | None,
    kind: str,
    include_pdf_candidates: bool,
) -> str:
    rows = search_corpus(
        conn,
        question,
        limit=limit,
        from_year=from_year,
        until_year=until_year,
        source_key=source_key,
        chunk_kind=kind,
    )
    method_counts, data_counts = signal_counts(rows)
    filters = []
    if from_year is not None:
        filters.append(f"from_year={from_year}")
    if until_year is not None:
        filters.append(f"until_year={until_year}")
    if source_key:
        filters.append(f"source_key={source_key}")
    if kind != "all":
        filters.append(f"kind={kind}")
    filter_text = ", ".join(filters) if filters else "none"

    parts = [
        "# Paperboy Analyst Context Pack",
        "",
        f"Question: {question}",
        f"Filters: {filter_text}",
        f"Retrieved records: {len(rows)}",
        "",
        "## Corpus Evidence",
    ]
    if rows:
        for index, row in enumerate(rows, start=1):
            parts.append(_record_line(row, index))
            parts.append("")
    else:
        parts.append("No indexed corpus matches were found.")
        parts.append("")

    parts.append("## Detected Method Signals")
    if method_counts:
        parts.extend(f"- {label}: {count}" for label, count in method_counts.most_common(10))
    else:
        parts.append("- No high-confidence method keywords detected in retrieved evidence.")
    parts.append("")

    parts.append("## Detected Data Signals")
    if data_counts:
        parts.extend(f"- {label}: {count}" for label, count in data_counts.most_common(10))
    else:
        parts.append("- No high-confidence data-source keywords detected in retrieved evidence.")
    parts.append("")

    if include_pdf_candidates:
        parts.append("## Relevant OA PDF Candidates")
        candidates = pdf_candidates(
            conn,
            query=question,
            limit=min(max(limit, 5), 25),
            from_year=from_year,
            until_year=until_year,
            source_key=source_key,
        )
        if candidates:
            for index, row in enumerate(candidates, start=1):
                parts.append(
                    f"- P{index}: {row['publication_year'] or 'n.d.'} | "
                    f"{row['source_key'] or 'unknown'} | {row['doi']} | {row['title']}"
                )
        else:
            parts.append("- No not-yet-downloaded OA PDF candidates found for this slice.")
        parts.append("")

    parts.extend(
        [
            "## Analyst Task",
            "Use the standing memory plus the corpus evidence above to answer the user's question. "
            "Do not pretend the retrieved evidence is exhaustive. Where you infer beyond the evidence, say so. "
            "For papers, cite result numbers like [R1] and include venue/year/DOI where helpful.",
        ]
    )
    return "\n".join(parts)


def _post_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    request = Request(url, data=body, headers={**headers, "Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API request failed with HTTP {exc.code}: {detail}") from exc


def _openai_text(data: dict[str, Any]) -> str:
    if data.get("output_text"):
        return str(data["output_text"])
    parts: list[str] = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                parts.append(text)
    return "\n".join(parts).strip() or json.dumps(data, indent=2)


def _anthropic_text(data: dict[str, Any]) -> str:
    parts = []
    for item in data.get("content", []):
        if item.get("type") == "text" and item.get("text"):
            parts.append(item["text"])
    return "\n".join(parts).strip() or json.dumps(data, indent=2)


def call_openai(prompt: str, memory: str, *, model: str, max_tokens: int) -> str:
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError("OPENAI_API_KEY is not set.")
    data = _post_json(
        "https://api.openai.com/v1/responses",
        {"Authorization": f"Bearer {key}"},
        {
            "model": model,
            "instructions": memory,
            "input": prompt,
            "max_output_tokens": max_tokens,
        },
    )
    return _openai_text(data)


def call_anthropic(prompt: str, memory: str, *, model: str, max_tokens: int) -> str:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not set.")
    data = _post_json(
        "https://api.anthropic.com/v1/messages",
        {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        {
            "model": model,
            "system": memory,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
        },
    )
    return _anthropic_text(data)


def default_model(provider: str) -> str:
    env_model = os.getenv("PAPERBOY_MODEL")
    if env_model:
        return env_model
    if provider == "anthropic":
        return "claude-sonnet-4-5"
    return "gpt-5.5"


def run_llm(provider: str, prompt: str, memory: str, *, model: str | None, max_tokens: int) -> str:
    if provider == "auto":
        if os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        elif os.getenv("ANTHROPIC_API_KEY"):
            provider = "anthropic"
        else:
            raise RuntimeError("No API key found. Set OPENAI_API_KEY or ANTHROPIC_API_KEY, or use --provider local.")

    chosen_model = model or default_model(provider)
    if provider == "openai":
        return call_openai(prompt, memory, model=chosen_model, max_tokens=max_tokens)
    if provider == "anthropic":
        return call_anthropic(prompt, memory, model=chosen_model, max_tokens=max_tokens)
    raise ValueError(f"Unknown LLM provider: {provider}")
