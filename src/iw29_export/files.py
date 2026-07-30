"""File helpers, mainly so OneDrive never sees a half-written report."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from .errors import ExportError
from .logging_setup import get_logger

log = get_logger("files")

# OneDrive and Office both skip names shaped like this, so the interim copy is
# very unlikely to be uploaded before the rename lands.
_TEMP_PREFIX = "~$"
_TEMP_SUFFIX = ".tmp"


def publish(source: Path, destination: Path, overwrite: bool = True) -> Path:
    """Copy into the synced folder under a temp name, then rename into place."""
    if not source.is_file():
        raise ExportError(f"Nothing to publish: {source} does not exist.")
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not overwrite:
        destination = _next_free_name(destination)

    staged = destination.parent / f"{_TEMP_PREFIX}{destination.name}{_TEMP_SUFFIX}"
    _unlink_quietly(staged)
    shutil.copy2(source, staged)

    expected = source.stat().st_size
    actual = staged.stat().st_size
    if expected != actual:
        _unlink_quietly(staged)
        raise ExportError(
            f"Copy of {source.name} came out at {actual} bytes instead of {expected}."
        )

    _replace_with_retry(staged, destination)
    log.info("Published %s (%s bytes)", destination, f"{actual:,}")
    return destination


def move_into(source: Path, folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    if target.exists():
        target = _next_free_name(target)
    shutil.move(str(source), str(target))
    return target


def age_in_days(path: Path) -> float:
    return max((time.time() - path.stat().st_mtime) / 86400.0, 0.0)


def prune_folder(folder: Path, keep: int = 20, pattern: str = "*") -> int:
    """Keep only the newest `keep` matching files. Returns how many were removed."""
    if not folder.is_dir():
        return 0
    candidates = sorted(
        (p for p in folder.glob(pattern) if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for path in candidates[keep:]:
        if _unlink_quietly(path):
            removed += 1
    return removed


def _next_free_name(path: Path) -> Path:
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{index:02d}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise ExportError(f"Could not find a free file name near {path}.")


def _replace_with_retry(source: Path, destination: Path, attempts: int = 10) -> None:
    """Retry the rename: OneDrive or Excel may hold the target open briefly."""
    last_error: Exception = ExportError("unknown error")
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.3 * (attempt + 1))
    _unlink_quietly(source)
    raise ExportError(
        f"Could not move the finished file into {destination}. It is probably open "
        f"in Excel or locked by OneDrive. Last error: {last_error}"
    )


def _unlink_quietly(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("Could not delete %s: %s", path, exc)
        return False
