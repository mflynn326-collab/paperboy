from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "sources.toml"
DEFAULT_DB = ROOT / "data" / "paperboy.sqlite"


@dataclass(frozen=True)
class Journal:
    key: str
    name: str
    publisher: str
    issns: tuple[str, ...]
    tier: str


def load_journals(config_path: Path = DEFAULT_CONFIG) -> list[Journal]:
    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    journals = []
    for key, item in raw.get("journals", {}).items():
        journals.append(
            Journal(
                key=key,
                name=item["name"],
                publisher=item.get("publisher", ""),
                issns=tuple(item["issns"]),
                tier=item.get("tier", ""),
            )
        )
    return journals
