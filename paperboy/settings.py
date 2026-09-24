from __future__ import annotations

from pathlib import Path
import tomllib

from .config import ROOT


DEFAULT_LOCAL_CONFIG = ROOT / "config" / "local.toml"


def load_local_settings(path: Path = DEFAULT_LOCAL_CONFIG) -> dict:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        return tomllib.load(handle)


def default_email() -> str | None:
    settings = load_local_settings()
    value = settings.get("account", {}).get("email")
    return str(value) if value else None


def default_drive_path() -> Path:
    settings = load_local_settings()
    value = settings.get("drive", {}).get("path")
    return Path(str(value)) if value else Path("G:/My Drive/Paperboy")


def default_pdf_path() -> Path:
    """Store downloaded PDFs in Drive rather than keeping a second local dump."""
    return default_drive_path() / "pdfs"
