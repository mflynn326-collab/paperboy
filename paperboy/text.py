from __future__ import annotations

import html
import re


TAG_RE = re.compile(r"<[^>]+>")
SPACE_RE = re.compile(r"\s+")


def clean_markup(value: str | None) -> str | None:
    if not value:
        return None
    text = TAG_RE.sub(" ", value)
    text = html.unescape(text)
    text = SPACE_RE.sub(" ", text).strip()
    return text or None


def clean_abstract(value: str | None) -> str | None:
    return clean_markup(value)


def first_text(value: object) -> str | None:
    if isinstance(value, list) and value:
        return clean_markup(str(value[0]))
    if isinstance(value, str):
        return clean_markup(value)
    return None


def date_parts_to_iso(parts: list[list[int]] | None) -> str | None:
    if not parts:
        return None
    date = parts[0]
    if not date:
        return None
    year = f"{date[0]:04d}"
    month = f"{date[1]:02d}" if len(date) > 1 else "01"
    day = f"{date[2]:02d}" if len(date) > 2 else "01"
    return f"{year}-{month}-{day}"


def openalex_abstract(index: dict[str, list[int]] | None) -> str | None:
    if not index:
        return None
    words: list[tuple[int, str]] = []
    for word, positions in index.items():
        for position in positions:
            words.append((position, word))
    return " ".join(word for _, word in sorted(words)) or None


def safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return cleaned.strip("_")[:180] or "paper"
