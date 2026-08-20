"""A single-run lock, so a scheduled run and a manual click cannot collide."""

from __future__ import annotations

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

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
    """Remove the lock if the process that wrote it has gone, or it is ancient."""
    try:
        age = time.time() - lock_path.stat().st_mtime
    except OSError:
        return True

    holder = _recorded_pid(lock_path)
    if holder is not None and not _process_alive(holder):
        log.warning("Lock was held by process %d, which no longer exists.", holder)
    elif age < STALE_AFTER_S:
        return False
    else:
        log.warning("Removing a stale lock file (%.0f minutes old).", age / 60)

    try:
        lock_path.unlink()
        return True
    except OSError:
        return False


def _recorded_pid(lock_path: Path) -> Optional[int]:
    try:
        text = lock_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = re.search(r"pid=(\d+)", text)
    return int(match.group(1)) if match else None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) is unreliable on Windows (WinError 87 / SystemError
        # for dead PIDs). Prefer OpenProcess.
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False
