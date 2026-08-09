"""Harvest GOS attachments from IW22 for a list of notifications.

Flow per notification number:
  /nIW22 → enter QMNUM → Enter → GOS View Attachments → pick row → save file

The recording used a favorites node (F00022); this driver never does — favorites
are user-specific. It opens IW22 directly and feeds each number from the list.
"""

from __future__ import annotations

import os
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
    paths: List[Path] = field(default_factory=list)
    detail: str = ""
    batch: str = ""


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


def run(
    config: Config,
    progress: Optional[ProgressFn] = None,
    batch_names: Optional[List[str]] = None,
) -> HarvestResult:
    emit = progress or (lambda message: log.info("%s", message))
    cfg = config.iw22_attachments
    if not cfg.enabled:
        raise ConfigError("iw22_attachments.enabled is false.")

    jobs = _resolve_jobs(config, batch_names)
    if not jobs:
        raise ConfigError(
            "No IW22 harvest jobs configured. Set [[iw22_attachments.batches]] "
            "or list_path."
        )

    watch_dir = cfg.download_watch_folder or Path.home() / "Downloads"
    watch_dir.mkdir(parents=True, exist_ok=True)

    started = datetime.now()
    results: List[AttachmentResult] = []
    password = resolve(config)
    gui = SapGui.from_config(config, password)

    with gui.session() as session:
        for job_name, list_path, out_dir in jobs:
            numbers = load_notification_numbers(
                list_path, column=cfg.list_column, sheet=cfg.list_sheet
            )
            if cfg.limit > 0:
                numbers = numbers[: cfg.limit]
            out_dir.mkdir(parents=True, exist_ok=True)
            emit(
                f"IW22 [{job_name}]: {len(numbers)} notification(s) → {out_dir}"
            )
            for index, number in enumerate(numbers, start=1):
                emit(f"[{job_name} {index}/{len(numbers)}] IW22 {number}")
                try:
                    result = _harvest_one(
                        session, config, number, out_dir, watch_dir
                    )
                except Exception as exc:
                    log.exception("Failed on notification %s", number)
                    result = AttachmentResult(
                        notification=number,
                        status="failed",
                        detail=str(exc),
                        batch=job_name,
                    )
                result.batch = job_name
                results.append(result)
                _close_attachment_dialogs(session)

            if cfg.merge_pdfs:
                from . import pdf_merge

                merge = pdf_merge.merge_folder(
                    out_dir,
                    keep_parts=cfg.merge_keep_parts,
                    notifications=numbers,
                )
                emit(
                    f"IW22 [{job_name}] PDF merge: {merge.merged_count} combined, "
                    f"{len(merge.skipped)} skipped, {len(merge.errors)} error(s)"
                )
                for err in merge.errors:
                    log.warning("PDF merge %s: %s", job_name, err)

    if cfg.build_lookup_xlsx:
        from . import iw22_lookup

        lookup_jobs = _resolve_jobs(config, batch_names)
        batch_folders = [(name, out_dir) for name, _list, out_dir in lookup_jobs]
        # When a subset of batches is requested, still rebuild from those folders;
        # for a full run include every configured batch folder.
        if not batch_names and cfg.batches:
            base = cfg.output_folder or (config.export.folder / "iw22_attachments")
            batch_folders = [
                (batch.name, batch.output_folder or (base / batch.name))
                for batch in cfg.batches
            ]
        destination = cfg.lookup_xlsx_path or (
            (cfg.output_folder or (config.export.folder / "iw22_attachments"))
            / "iw22_notification_lookup.xlsx"
        )
        try:
            lookup = iw22_lookup.build_lookup_xlsx(
                batch_folders,
                destination,
                sheet_name=cfg.lookup_sheet_name,
                fill_scenario=cfg.fill_scenario,
                scenario_max_pages=cfg.scenario_max_pages,
                split_by_fpso=cfg.lookup_split_by_fpso,
                write_combined=cfg.lookup_write_combined,
            )
            names = ", ".join(p.name for p in lookup.paths) or lookup.path.name
            emit(
                f"IW22 lookup workbook: {lookup.row_count} notification(s) → "
                f"{names}"
            )
        except Exception as exc:
            log.exception("Failed building IW22 lookup workbook")
            emit(f"IW22 lookup workbook failed: {exc}")

    finished = datetime.now()
    summary = HarvestResult(started_at=started, finished_at=finished, results=results)
    emit(
        f"Done: {summary.saved} saved, {summary.skipped} skipped, "
        f"{summary.failed} failed in {(finished - started).total_seconds():.1f}s"
    )
    if summary.saved == 0 and summary.failed == 0 and summary.skipped == 0:
        raise EmptyResultError("No attachments were saved (all skipped or empty).")
    return summary


def _resolve_jobs(
    config: Config, batch_names: Optional[List[str]]
) -> List[tuple]:
    """Return (name, list_path, output_folder) jobs to run."""
    cfg = config.iw22_attachments
    base = cfg.output_folder or (config.export.folder / "iw22_attachments")
    wanted = {name.strip().upper() for name in (batch_names or []) if name.strip()}

    jobs: List[tuple] = []
    if cfg.batches:
        for batch in cfg.batches:
            name = batch.name.strip()
            if wanted and name.upper() not in wanted:
                continue
            out = batch.output_folder or (base / name)
            jobs.append((name, batch.list_path, out))
        return jobs

    if cfg.list_path:
        jobs.append(("default", cfg.list_path, base))
    return jobs


def _harvest_one(
    session: SapSession,
    config: Config,
    number: str,
    out_dir: Path,
    watch_dir: Path,
) -> AttachmentResult:
    cfg = config.iw22_attachments
    skip_exts = {ext.lower() for ext in cfg.skip_extensions}
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

    rows = _pick_rows(grid, cfg.attachment_mode, cfg.match_text)
    if not rows:
        return AttachmentResult(
            notification=number,
            status="skipped",
            detail=f"no attachment matched mode={cfg.attachment_mode!r}",
        )

    saved_paths: List[Path] = []
    already: List[Path] = []
    errors: List[str] = []
    skipped_noise = 0
    watch_dirs = _watch_dirs(watch_dir)
    seq = 0

    for row in rows:
        description = _cell_text(grid, row, "BITM_DESCR") or f"row{row}"
        if _is_noise_attachment(description, skip_exts):
            skipped_noise += 1
            log.info(
                "Skipping noise attachment on %s: %s", number, description
            )
            continue

        seq += 1
        existing = _existing_destination(out_dir, number, seq, skip_exts)
        if existing is not None:
            already.append(existing)
            log.info("Already have %s — skipping index %s.", existing.name, seq)
            continue

        grid = _ensure_attachment_grid(session, grid)
        if grid is None:
            errors.append(f"index {seq}: attachment list unavailable")
            break

        before = {folder: _snapshot_files(folder) for folder in watch_dirs}
        before[out_dir] = _snapshot_files(out_dir)

        if not _export_or_open(
            session, grid, row, out_dir, number, seq, description
        ):
            errors.append(f"row {row}: could not export/open")
            grid = None
            continue

        saved = _wait_for_new_file_any(
            before, timeout_s=cfg.download_timeout_s, skip_exts=skip_exts
        )
        if saved is None:
            errors.append(
                f"row {row}: no new non-noise file under "
                + ", ".join(str(p) for p in [*watch_dirs, out_dir])
            )
            grid = None
            continue

        if _is_noise_path(saved, skip_exts):
            try:
                saved.unlink()
            except OSError:
                pass
            skipped_noise += 1
            log.info("Deleted noise download %s", saved.name)
            grid = None
            continue

        target = _destination(out_dir, number, seq, saved)
        if _is_noise_path(target, skip_exts):
            try:
                saved.unlink()
            except OSError:
                pass
            skipped_noise += 1
            grid = None
            continue

        if saved.resolve() != target.resolve():
            if target.exists():
                try:
                    saved.unlink()
                except OSError:
                    pass
                already.append(target)
                grid = None
                continue
            shutil.move(str(saved), str(target))
        saved_paths.append(target)
        log.info("Saved %s → %s (%s)", number, target.name, description)
        grid = None

    all_paths = already + saved_paths
    if saved_paths:
        names = ", ".join(path.name for path in saved_paths)
        return AttachmentResult(
            notification=number,
            status="saved",
            path=saved_paths[0],
            paths=all_paths,
            detail=names,
        )
    if already and not errors:
        names = ", ".join(path.name for path in already)
        return AttachmentResult(
            notification=number,
            status="skipped",
            path=already[0],
            paths=already,
            detail=f"already harvested: {names}",
        )
    if skipped_noise and not errors and not already:
        return AttachmentResult(
            notification=number,
            status="skipped",
            detail=f"only noise attachments (.log/.txt) found ({skipped_noise})",
        )
    return AttachmentResult(
        notification=number,
        status="failed",
        paths=all_paths,
        detail="; ".join(errors) or "no attachments saved",
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


def _ensure_attachment_grid(session: SapSession, grid: Optional[Any]) -> Optional[Any]:
    """Return a live attachment grid, reopening GOS if the list was closed."""
    if grid is not None:
        try:
            _ = _row_count(grid)
            return grid
        except Exception:
            pass
    _close_attachment_dialogs(session)
    if not _open_attachment_list(session):
        return None
    return _attachment_grid(session)


def _pick_rows(grid: Any, mode: str, match_text: str) -> List[int]:
    count = _row_count(grid)
    if count <= 0:
        return []
    mode = (mode or "all").strip().lower()
    if mode == "all":
        return list(range(count))
    if mode == "first":
        return [0]
    needle = (match_text or "").strip().lower()
    if not needle:
        return [0]
    for row in range(count):
        text = _cell_text(grid, row, "BITM_DESCR").lower()
        if needle in text:
            return [row]
    return []


def _export_or_open(
    session: SapSession,
    grid: Any,
    row: int,
    out_dir: Path,
    number: str,
    seq: int,
    description: str,
) -> bool:
    """Prefer an export/save action; fall back to the recorded double-click."""
    try:
        grid.currentCellColumn = "BITM_DESCR"
        grid.selectedRows = str(row)
        grid.currentCellRow = row
    except Exception:
        pass

    for label, attempt in (
        ("toolbar export menu", lambda: grid.pressToolbarContextButton("&MB_EXPORT")),
        ("context &EXPORT", lambda: grid.selectContextMenuItem("&EXPORT")),
        ("context EXPORT", lambda: grid.selectContextMenuItem("EXPORT")),
        ("toolbar &EXPORT", lambda: grid.pressToolbarButton("&EXPORT")),
        ("toolbar %ATTA_EXPORT", lambda: grid.pressToolbarButton("%ATTA_EXPORT")),
    ):
        try:
            attempt()
            session.wait_ready()
            if _try_save_dialog(session, out_dir, number, seq, description):
                log.info("Exported via %s.", label)
                return True
        except Exception as exc:
            log.info("Export via %s not available: %s", label, exc)

    # Recorded behaviour: open the attachment (viewer / temp file).
    try:
        grid.doubleClickCurrentCell()
        session.wait_ready()
        # Do not dismiss popups here — wnd[1] may still be the attachment list.
        _try_save_dialog(session, out_dir, number, seq, description)
        return True
    except Exception as exc:
        log.info("doubleClickCurrentCell failed: %s", exc)
        return False


def _try_save_dialog(
    session: SapSession,
    out_dir: Path,
    number: str,
    seq: int,
    description: str,
) -> bool:
    """If SAP put up a local-file save dialog, point it at our output folder."""
    preferred = f"{number}({seq})"
    for window in ("wnd[1]", "wnd[2]", "wnd[3]"):
        path_id = f"{window}/usr/ctxtDY_PATH"
        name_id = f"{window}/usr/ctxtDY_FILENAME"
        if not session.exists(path_id):
            path_id = f"{window}/usr/txtDY_PATH"
            name_id = f"{window}/usr/txtDY_FILENAME"
        if not session.exists(path_id):
            continue
        try:
            session.set_text(path_id, str(out_dir))
            if session.exists(name_id):
                current = session.text(name_id).strip()
                suffix = Path(current).suffix if current else ""
                session.set_text(name_id, f"{preferred}{suffix or '.bin'}")
            session.send_vkey(VKEY_ENTER, window)
            session.wait_ready()
            return True
        except Exception as exc:
            log.info("Save dialog fill failed: %s", exc)
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


def _watch_dirs(primary: Path) -> List[Path]:
    candidates = [
        primary,
        Path.home() / "Downloads",
        Path(os.environ.get("TEMP", Path.home() / "AppData" / "Local" / "Temp")),
        Path(os.environ.get("TMP", Path.home() / "AppData" / "Local" / "Temp")),
    ]
    found: List[Path] = []
    seen = set()
    for folder in candidates:
        try:
            resolved = folder.resolve()
        except OSError:
            continue
        if resolved in seen or not folder.exists():
            continue
        seen.add(resolved)
        found.append(folder)
    return found


def _wait_for_new_file_any(
    before_by_folder: dict,
    timeout_s: float = 45.0,
    skip_exts: Optional[set] = None,
) -> Optional[Path]:
    skip_exts = {ext.lower() for ext in (skip_exts or set())}
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for folder, before in before_by_folder.items():
            try:
                for path in folder.iterdir():
                    if not path.is_file() or path.name.startswith("~$"):
                        continue
                    # Ignore trivial temp noise and configured skip extensions.
                    if path.suffix.lower() in {
                        ".tmp",
                        ".partial",
                        ".crdownload",
                        *skip_exts,
                    }:
                        continue
                    previous = before.get(path.name)
                    try:
                        mtime = path.stat().st_mtime
                    except OSError:
                        continue
                    if previous is None or mtime > previous + 0.5:
                        if _file_stable(path):
                            return path
            except OSError:
                continue
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


def _destination(out_dir: Path, number: str, seq: int, source: Path) -> Path:
    """Governed name: <notification>(n).<ext>."""
    suffix = source.suffix.lower() or source.suffix or ".bin"
    return out_dir / f"{number}({seq}){suffix}"


def _existing_destination(
    out_dir: Path,
    number: str,
    seq: int,
    skip_exts: Optional[set] = None,
) -> Optional[Path]:
    """Return an already-harvested non-noise file for this notification index."""
    skip_exts = {ext.lower() for ext in (skip_exts or set())}
    prefix = f"{number}({seq})"
    try:
        for path in out_dir.iterdir():
            if not path.is_file():
                continue
            if _is_noise_path(path, skip_exts):
                continue
            if path.stem == prefix or path.name.startswith(prefix + "."):
                return path
    except OSError:
        pass
    return None


def _is_noise_path(path: Path, skip_exts: set) -> bool:
    return path.suffix.lower() in skip_exts


def _is_noise_attachment(description: str, skip_exts: set) -> bool:
    text = (description or "").strip().lower()
    if not text:
        return False
    suffix = Path(text).suffix.lower()
    if suffix in skip_exts:
        return True
    return any(text.endswith(ext) for ext in skip_exts)


def re_slug(text: str) -> str:
    cleaned = []
    for char in text.strip():
        if char.isalnum():
            cleaned.append(char)
        elif char in {"-", "_", ".", " "}:
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "attachment"
