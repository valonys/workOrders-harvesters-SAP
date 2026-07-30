"""Keeps the SharePoint folder tidy: recent reports at the top, old ones filed away."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List

from .config import Config
from .files import age_in_days, move_into
from .logging_setup import get_logger

log = get_logger("archive")


@dataclass
class ArchiveResult:
    archived: List[Path] = field(default_factory=list)
    deleted: List[Path] = field(default_factory=list)
    skipped: List[Path] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return (
            f"{len(self.archived)} archived, {len(self.deleted)} deleted, "
            f"{len(self.skipped)} left in place"
        )


def run(config: Config, dry_run: bool = False) -> ArchiveResult:
    result = ArchiveResult()
    if not config.archive.enabled:
        log.info("Archiving is disabled.")
        return result

    export_folder = config.export.folder
    archive_folder = config.archive_folder
    if not export_folder.is_dir():
        log.info("Export folder %s does not exist yet; nothing to archive.", export_folder)
        return result

    cutoff = config.archive.archive_after_days
    for path in sorted(export_folder.glob("*.xls*")):
        if not path.is_file() or _is_temp(path):
            continue
        if _is_within(path, archive_folder):
            continue
        age = age_in_days(path)
        if age < cutoff:
            result.skipped.append(path)
            continue
        target_folder = archive_folder / _bucket(path)
        if dry_run:
            log.info("[dry run] would archive %s -> %s", path.name, target_folder)
            result.archived.append(path)
            continue
        moved = move_into(path, target_folder)
        log.info("Archived %s (%.0f days old) -> %s", path.name, age, moved.parent)
        result.archived.append(moved)

    retention = config.archive.delete_after_days
    if retention and archive_folder.is_dir():
        for path in sorted(archive_folder.rglob("*.xls*")):
            if not path.is_file() or _is_temp(path):
                continue
            age = age_in_days(path)
            if age < retention:
                continue
            if dry_run:
                log.info("[dry run] would delete %s (%.0f days old)", path.name, age)
                result.deleted.append(path)
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.warning("Could not delete %s: %s", path, exc)
                continue
            log.info("Deleted %s (%.0f days old)", path.name, age)
            result.deleted.append(path)
        _remove_empty_dirs(archive_folder, dry_run)

    log.info("Archive pass: %s", result.summary)
    return result


def _bucket(path: Path) -> Path:
    stamp = datetime.fromtimestamp(path.stat().st_mtime)
    return Path(f"{stamp:%Y}") / f"{stamp:%m}"


def _is_temp(path: Path) -> bool:
    return path.name.startswith("~$") or path.suffix.lower() == ".tmp"


def _is_within(path: Path, folder: Path) -> bool:
    try:
        path.resolve().relative_to(folder.resolve())
        return True
    except (ValueError, OSError):
        return False


def _remove_empty_dirs(root: Path, dry_run: bool) -> None:
    for directory in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if not directory.is_dir():
            continue
        if any(directory.iterdir()):
            continue
        if dry_run:
            log.info("[dry run] would remove empty folder %s", directory)
            continue
        try:
            directory.rmdir()
        except OSError:
            pass
