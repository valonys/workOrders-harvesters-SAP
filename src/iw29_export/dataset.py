"""Maintains one flat CSV that Power BI can point at, instead of dated workbooks."""

from __future__ import annotations

import csv
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Any, List, Optional, Sequence

from .config import Config
from .convert import Table
from .files import publish
from .logging_setup import get_logger

log = get_logger("dataset")

RUN_TIMESTAMP_COLUMN = "run_timestamp"
SOURCE_FILE_COLUMN = "source_file"


def build(
    config: Config,
    table: Table,
    source_file: Path,
    run_timestamp: Optional[datetime] = None,
) -> Optional[Path]:
    if not config.dataset.enabled:
        return None

    run_timestamp = run_timestamp or datetime.now()
    destination = config.dataset_folder / config.dataset.filename
    headers = list(table.headers)
    if config.dataset.add_run_columns:
        headers += [RUN_TIMESTAMP_COLUMN, SOURCE_FILE_COLUMN]

    append = config.dataset.mode == "append" and destination.is_file()
    existing_headers = _read_headers(destination) if append else None
    if append and existing_headers and existing_headers != headers:
        log.warning(
            "Column layout changed since the last run, so the dataset is being "
            "rewritten instead of appended to."
        )
        append = False

    extra: List[Any] = (
        [run_timestamp.isoformat(timespec="seconds"), source_file.name]
        if config.dataset.add_run_columns
        else []
    )

    with tempfile.TemporaryDirectory(prefix="iw29-dataset-") as scratch:
        staged = Path(scratch) / config.dataset.filename
        if append:
            _copy_existing(destination, staged)
        with staged.open("a" if append else "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, lineterminator="\r\n")
            if not append:
                writer.writerow(headers)
            for row in table.rows:
                writer.writerow([_render(cell) for cell in row] + extra)
        published = publish(staged, destination, overwrite=True)

    mode = "appended to" if append else "written"
    log.info("Dataset %s: %s (%d new rows)", mode, published, table.row_count)
    return published


def _read_headers(path: Path) -> Optional[List[str]]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.reader(handle):
                return [cell.strip() for cell in row]
    except OSError as exc:
        log.warning("Could not read the existing dataset header: %s", exc)
    return None


def _copy_existing(source: Path, destination: Path) -> None:
    destination.write_bytes(source.read_bytes())


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


def preview(table: Table, limit: int = 5) -> Sequence[Sequence[Any]]:
    return table.rows[:limit]
