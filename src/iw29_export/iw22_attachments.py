"""Harvest GOS attachments from IW22 for a list of notifications.

Flow per notification number:
  /nIW22 → enter QMNUM → Enter → GOS View Attachments → pick row → save file

The recording used a favorites node (F00022); this driver never does — favorites
are user-specific. It opens IW22 directly and feeds each number from the list.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional

from .config import Config
from .credentials import resolve
from .errors import ConfigError, EmptyResultError, ExportError, SapError
from .logging_setup import get_logger
from .notif_list import load_notification_numbers
from .sap import (
    VKEY_ENTER,
    VKEY_F12_CANCEL,
    SapGui,
    SapSession,
)

log = get_logger("iw22")

ProgressFn = Callable[[str], None]

_QMNUM_CANDIDATES = (
    "wnd[0]/usr/ctxtRIWO00-QMNUM",
    "wnd[0]/usr/ctxtQMNUM",
    "wnd[0]/usr/txtRIWO00-QMNUM",
    "wnd[0]/usr/txtQMNUM",
)
_GOS_SHELL = "wnd[0]/titl/shellcont/shell"
_ATTACH_GRID = "wnd[1]/usr/cntlCONTAINER_0100/shellcont/shell"
_ATTACH_GRID_FALLBACKS = (
    _ATTACH_GRID,
    "wnd[1]/usr/cntlCONTAINER/shellcont/shell",
    "wnd[1]/usr/shellcont/shell",
)


@dataclass
class AttachmentResult:
    notification: str
    status: str  # saved | skipped | failed
    path: Optional[Path] = None
    detail: str = ""


@dataclass
class HarvestResult:
    started_at: datetime
    finished_at: datetime
    results: List[AttachmentResult] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return sum(1 for item in self.results if item.status == "saved")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "failed")

    @property
    def skipped(self) -> int:
        return sum(1 for item in self.results if item.status == "skipped")


def run(config: Config, progress: Optional[ProgressFn] = None) -> HarvestResult:
    emit = progress or (lambda message: log.info("%s", message))
    cfg = config.iw22_attachments
    if not cfg.enabled:
        raise ConfigError("iw22_attachments.enabled is false.")
    if not cfg.list_path:
        raise ConfigError("iw22_attachments.list_path must be set.")

    numbers = load_notification_numbers(
        cfg.list_path, column=cfg.list_column, sheet=cfg.list_sheet
    )
    if cfg.limit > 0:
        numbers = numbers[: cfg.limit]

    out_dir = cfg.output_folder or (config.export.folder / "iw22_attachments")
    out_dir.mkdir(parents=True, exist_ok=True)
    watch_dir = cfg.download_watch_folder or Path.home() / "Downloads"
    watch_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now()
    results: List[AttachmentResult] = []
    password = resolve(config)
    gui = SapGui.from_config(config, password)

    emit(f"IW22 attachment harvest: {len(numbers)} notification(s) → {out_dir}")
    with gui.session() as session:
        for index, number in enumerate(numbers, start=1):
            emit(f"[{index}/{len(numbers)}] IW22 {number}")
            try:
                result = _harvest_one(session, config, number, out_dir, watch_dir)
            except Exception as exc:
                log.exception("Failed on notification %s", number)
                result = AttachmentResult(
                    notification=number, status="failed", detail=str(exc)
                )
            results.append(result)
            _close_attachment_dialogs(session)

    finished = datetime.now()
    summary = HarvestResult(started_at=started, finished_at=finished, results=results)
    emit(
        f"Done: {summary.saved} saved, {summary.skipped} skipped, "
        f"{summary.failed} failed in {(finished - started).total_seconds():.1f}s"
    )
    if summary.saved == 0 and summary.failed == 0:
        raise EmptyResultError("No attachments were saved (all skipped or empty).")
    return summary


def _harvest_one(
    session: SapSession,
    config: Config,
    number: str,
    out_dir: Path,
    watch_dir: Path,
) -> AttachmentResult:
    cfg = config.iw22_attachments
    _open_notification(session, number)

    if not _open_attachment_list(session):
        return AttachmentResult(
            notification=number,
            status="skipped",
            detail="could not open GOS attachment list",
        )

    grid = _attachment_grid(session)
    if grid is None:
        return AttachmentResult(
            notification=number,
            status="skipped",
            detail="attachment list grid not found",
        )

    row = _pick_row(grid, cfg.attachment_mode, cfg.match_text)
    if row is None:
        return AttachmentResult(
            notification=number,
            status="skipped",
            detail=f"no attachment matched mode={cfg.attachment_mode!r}",
        )

    description = _cell_text(grid, row, "BITM_DESCR") or f"row{row}"
    before = _snapshot_files(watch_dir)

    if not _export_or_open(session, grid, row):
        return AttachmentResult(
            notification=number,
            status="failed",
            detail="could not export/open the attachment",
        )

    saved = _wait_for_new_file(watch_dir, before, timeout_s=cfg.download_timeout_s)
    if saved is None:
        # Some systems open the document in-place without writing to Downloads.
        return AttachmentResult(
            notification=number,
            status="failed",
            detail=(
                "attachment opened but no new file appeared in "
                f"{watch_dir}. Set iw22_attachments.download_watch_folder or "
                "export the attachment manually once to see where SAP writes it."
            ),
        )

    target = _destination(out_dir, number, description, saved)
    shutil.move(str(saved), str(target))
    log.info("Saved %s → %s", number, target.name)
    return AttachmentResult(
        notification=number,
        status="saved",
        path=target,
        detail=description,
    )


def _open_notification(session: SapSession, number: str) -> None:
    session.start_transaction("IW22")
    session.dismiss_popups()
    field = None
    for element_id in _QMNUM_CANDIDATES:
        if session.exists(element_id):
            field = element_id
            break
    if field is None:
        raise SapError(
            "IW22 notification number field not found. Run "
            "`python -m iw29_export inspect` after opening IW22 and set "
            "iw22_attachments.qmnum_field."
        )
    session.set_text(field, number)
    session.send_vkey(VKEY_ENTER)
    session.raise_on_error()
    session.dismiss_popups()


def _open_attachment_list(session: SapSession) -> bool:
    if not session.exists(_GOS_SHELL):
        log.info("GOS toolbox shell not on screen.")
        return False
    try:
        shell = session.find(_GOS_SHELL)
        shell.pressContextButton("%GOS_TOOLBOX")
        session.wait_ready()
        shell.selectContextMenuItem("%GOS_VIEW_ATTA")
        session.wait_ready()
    except Exception as exc:
        log.info("GOS View Attachments failed: %s", exc)
        return False

    if not session.wait_for_window("wnd[1]", timeout_s=8):
        # No attachments often leaves only a status message.
        status = session.status()
        log.info(
            "No attachment window after GOS (%s).",
            status.text or "no status",
        )
        return False
    return True


def _attachment_grid(session: SapSession) -> Optional[Any]:
    for element_id in _ATTACH_GRID_FALLBACKS:
        grid = session.optional(element_id)
        if grid is not None:
            return grid
    return None


def _pick_row(grid: Any, mode: str, match_text: str) -> Optional[int]:
    count = _row_count(grid)
    if count <= 0:
        return None
    mode = (mode or "first").strip().lower()
    if mode == "first":
        return 0
    needle = (match_text or "").strip().lower()
    if not needle:
        return 0
    for row in range(count):
        text = _cell_text(grid, row, "BITM_DESCR").lower()
        if needle in text:
            return row
    return None


def _export_or_open(session: SapSession, grid: Any, row: int) -> bool:
    """Prefer an export/save action; fall back to the recorded double-click."""
    try:
        grid.currentCellColumn = "BITM_DESCR"
        grid.selectedRows = str(row)
        grid.currentCellRow = row
    except Exception:
        pass

    for attempt in (
        lambda: grid.pressToolbarContextButton("&MB_EXPORT"),
        lambda: grid.selectContextMenuItem("&EXPORT"),
        lambda: grid.selectContextMenuItem("EXPORT"),
        lambda: grid.pressToolbarButton("&EXPORT"),
    ):
        try:
            attempt()
            session.wait_ready()
            if _try_save_dialog(session):
                return True
        except Exception:
            continue

    # Recorded behaviour: open the attachment (viewer / temp file).
    try:
        grid.doubleClickCurrentCell()
        session.wait_ready()
        session.dismiss_popups()
        _try_save_dialog(session)
        return True
    except Exception as exc:
        log.info("doubleClickCurrentCell failed: %s", exc)
        return False


def _try_save_dialog(session: SapSession) -> bool:
    """If SAP put up a local-file save dialog, confirm it."""
    for path_id in (
        "wnd[1]/usr/ctxtDY_PATH",
        "wnd[2]/usr/ctxtDY_PATH",
        "wnd[1]/usr/txtDY_PATH",
    ):
        if session.exists(path_id):
            try:
                session.send_vkey(VKEY_ENTER, path_id.split("/")[0])
                session.wait_ready()
                return True
            except Exception:
                return False
    return False


def _close_attachment_dialogs(session: SapSession) -> None:
    for _ in range(4):
        if not session.exists("wnd[1]"):
            break
        try:
            session.send_vkey(VKEY_F12_CANCEL, "wnd[1]")
        except Exception:
            break
    session.dismiss_popups()


def _row_count(grid: Any) -> int:
    for attr in ("RowCount", "VisibleRowCount"):
        try:
            value = int(getattr(grid, attr))
            if value >= 0:
                return value
        except Exception:
            continue
    return 0


def _cell_text(grid: Any, row: int, column: str) -> str:
    try:
        value = grid.GetCellValue(row, column)
        return str(value or "").strip()
    except Exception:
        return ""


def _snapshot_files(folder: Path) -> dict:
    found = {}
    try:
        for path in folder.iterdir():
            if path.is_file() and not path.name.startswith("~$"):
                try:
                    found[path.name] = path.stat().st_mtime
                except OSError:
                    continue
    except OSError:
        pass
    return found


def _wait_for_new_file(
    folder: Path, before: dict, timeout_s: float = 45.0
) -> Optional[Path]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            for path in folder.iterdir():
                if not path.is_file() or path.name.startswith("~$"):
                    continue
                previous = before.get(path.name)
                try:
                    mtime = path.stat().st_mtime
                except OSError:
                    continue
                if previous is None or mtime > previous + 0.5:
                    # Wait until size stops growing.
                    if _file_stable(path):
                        return path
        except OSError:
            pass
        time.sleep(0.4)
    return None


def _file_stable(path: Path, checks: int = 3, pause_s: float = 0.35) -> bool:
    try:
        size = path.stat().st_size
    except OSError:
        return False
    for _ in range(checks):
        time.sleep(pause_s)
        try:
            nxt = path.stat().st_size
        except OSError:
            return False
        if nxt != size or nxt == 0:
            size = nxt
            if nxt == 0:
                continue
            return False
        size = nxt
    return size > 0


def _destination(out_dir: Path, number: str, description: str, source: Path) -> Path:
    safe_desc = re_slug(description)[:60] or "attachment"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{number}_{safe_desc}_{stamp}{source.suffix.lower() or source.suffix}"
    target = out_dir / name
    if not target.exists():
        return target
    for index in range(2, 50):
        candidate = out_dir / f"{number}_{safe_desc}_{stamp}_{index}{source.suffix}"
        if not candidate.exists():
            return candidate
    return out_dir / f"{number}_{safe_desc}_{stamp}_{time.time_ns()}{source.suffix}"


def re_slug(text: str) -> str:
    cleaned = []
    for char in text.strip():
        if char.isalnum():
            cleaned.append(char)
        elif char in {"-", "_", ".", " "}:
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "attachment"
