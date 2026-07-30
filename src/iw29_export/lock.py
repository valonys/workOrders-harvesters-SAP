"""A single-run lock, so a scheduled run and a manual click cannot collide."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import LockError
from .logging_setup import get_logger

log = get_logger("lock")

STALE_AFTER_S = 3 * 3600


@contextmanager
def exclusive(lock_path: Path, timeout_s: int = 0) -> Iterator[Path]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(timeout_s, 0)
    handle = None

    while True:
        try:
            handle = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _clear_if_stale(lock_path):
                continue
            if time.monotonic() >= deadline:
                raise LockError(
                    f"Another run is in progress (lock file {lock_path}). Wait for it "
                    "to finish, or delete the lock file if you are sure it is stale."
                ) from None
            time.sleep(1.0)

    try:
        os.write(handle, f"pid={os.getpid()} started={time.strftime('%FT%T')}\n".encode())
        os.close(handle)
        handle = None
        yield lock_path
    finally:
        if handle is not None:
            os.close(handle)
        try:
            lock_path.unlink()
        except OSError as exc:
            log.warning("Could not remove the lock file %s: %s", lock_path, exc)


def _clear_if_stale(lock_path: Path) -> bool:
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True
    if age < STALE_AFTER_S:
        return False
    log.warning("Removing a stale lock file (%.0f minutes old).", age / 60)
    try:
        lock_path.unlink()
        return True
    except OSError:
        return False
