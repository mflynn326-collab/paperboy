from __future__ import annotations

from datetime import datetime
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .config import DEFAULT_DB, ROOT


DEFAULT_STAGE_DIR = ROOT / ".paperboy-sync" / "drive"
DEFAULT_DRIVE_ROOTS = (Path("G:/My Drive"), Path("G:/Shared drives"))


class DriveSyncError(RuntimeError):
    pass


def discover_drive_roots() -> list[Path]:
    candidates = [
        Path("G:/My Drive"),
        Path.home() / "Google Drive",
        Path.home() / "My Drive",
        Path.home() / "Google Drive" / "My Drive",
    ]
    return [path for path in candidates if path.exists()]


def ensure_safe_stage(stage_dir: Path) -> Path:
    resolved = stage_dir.resolve()
    allowed_root = (ROOT / ".paperboy-sync").resolve()
    if allowed_root != resolved and allowed_root not in resolved.parents:
        raise DriveSyncError(f"Refusing to use stage directory outside {allowed_root}: {resolved}")
    return resolved


def backup_database(db_path: Path, dest_path: Path) -> None:
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if not db_path.exists():
        return
    source = sqlite3.connect(db_path)
    try:
        target = sqlite3.connect(dest_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def prepare_stage(
    db_path: Path = DEFAULT_DB,
    stage_dir: Path = DEFAULT_STAGE_DIR,
) -> Path:
    stage_dir = ensure_safe_stage(stage_dir)
    stage_dir.parent.mkdir(parents=True, exist_ok=True)
    # A cloud client or antivirus scanner can transiently lock an old staging
    # folder on Windows. A fresh sibling is safer than mutating that folder.
    stage_dir = Path(tempfile.mkdtemp(prefix=f"{stage_dir.name}-", dir=stage_dir.parent))

    data_dir = stage_dir / "data"
    backup_database(db_path, data_dir / "paperboy.sqlite")

    config_dir = stage_dir / "config"
    config_dir.mkdir()
    sources = ROOT / "config" / "sources.toml"
    if sources.exists():
        shutil.copy2(sources, config_dir / "sources.toml")

    readme = ROOT / "README.md"
    if readme.exists():
        shutil.copy2(readme, stage_dir / "README.md")

    pyproject = ROOT / "pyproject.toml"
    if pyproject.exists():
        shutil.copy2(pyproject, stage_dir / "pyproject.toml")

    memory_source = ROOT / "memory" / "paperboy_analyst.md"
    if memory_source.exists():
        memory_dir = stage_dir / "memory"
        memory_dir.mkdir()
        shutil.copy2(memory_source, memory_dir / "paperboy_analyst.md")

    manifest = stage_dir / "manifest.txt"
    manifest.write_text(
        "Paperboy Google Drive Desktop snapshot\n"
        f"created_at={datetime.now().isoformat(timespec='seconds')}\n",
        encoding="utf-8",
    )
    return stage_dir


def copy_tree_contents(source: Path, target: Path) -> int:
    copied = 0
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        destination = target / relative
        if item.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, destination)
        copied += 1
    return copied


def check_drive_path(path: Path) -> None:
    if not path.exists():
        raise DriveSyncError(
            f"Google Drive target does not exist yet: {path}\n"
            "Create it in Drive for desktop, or pass --path with the folder you want Paperboy to use."
        )
    if not path.is_dir():
        raise DriveSyncError(f"Google Drive target is not a folder: {path}")


def sync_to_drive_folder(
    path: Path,
    db_path: Path = DEFAULT_DB,
    stage_dir: Path = DEFAULT_STAGE_DIR,
    dry_run: bool = False,
) -> int:
    stage = prepare_stage(db_path=db_path, stage_dir=stage_dir)
    target = path.resolve()
    try:
        if dry_run:
            files = [item for item in stage.rglob("*") if item.is_file()]
            print(f"Would copy {len(files)} files to {target}")
            for item in files:
                print(item.relative_to(stage))
            return 0

        target.mkdir(parents=True, exist_ok=True)
        copied = copy_tree_contents(stage, target)
        print(f"Copied {copied} files to {target}")
        return 0
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def setup_message(path: Path) -> str:
    roots = discover_drive_roots()
    lines = [
        "Paperboy now syncs through Google Drive for desktop.",
        f"Configured target: {path}",
    ]
    if roots:
        lines.append("Detected Drive roots:")
        lines.extend(f"  {root}" for root in roots)
    else:
        lines.append("No local Google Drive root was detected. Check that Drive for desktop is running.")
    lines.append("")
    lines.append("Use:")
    lines.append("  python -m paperboy drive-check")
    lines.append("  python -m paperboy drive-sync --dry-run")
    lines.append("  python -m paperboy drive-sync")
    return "\n".join(lines)
