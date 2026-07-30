"""Orchestration: one function that does the whole job and reports on it."""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from . import archive as archive_module
from . import convert, credentials, dataset, files, lock
from .config import Config
from .errors import Iw29Error
from .logging_setup import get_logger
from .source import Extract, MockReportSource, ReportSource

log = get_logger("pipeline")

ProgressFn = Callable[[str], None]


@dataclass
class RunResult:
    started_at: datetime
    finished_at: datetime
    source: str
    workbook: Optional[Path] = None
    row_count: int = 0
    column_count: int = 0
    dataset_file: Optional[Path] = None
    archived: int = 0
    deleted: int = 0
    warnings: List[str] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    def describe(self) -> str:
        parts = [
            f"{self.row_count:,} rows x {self.column_count} columns",
            f"in {self.duration_s:.1f}s",
        ]
        if self.workbook:
            parts.append(f"-> {self.workbook}")
        return " ".join(parts)


def run(
    config: Config,
    progress: Optional[ProgressFn] = None,
    password: Optional[str] = None,
    skip_archive: bool = False,
) -> RunResult:
    emit = _progress_adapter(progress)
    started = datetime.now()
    staging = config.staging_folder
    staging.mkdir(parents=True, exist_ok=True)

    lock_path = staging / "run.lock"
    with lock.exclusive(lock_path, config.runtime.lock_timeout_s):
        source = _build_source(config, password)
        emit(f"Source: {source.name}")

        extract = source.extract(staging, emit)
        table = _load_table(extract, emit)

        timestamp = datetime.now()
        filename = config.export.render_filename(config.sap.system, timestamp)
        emit(f"Building workbook {filename}")

        with tempfile.TemporaryDirectory(prefix="iw29-build-", dir=str(staging)) as scratch:
            built = Path(scratch) / filename
            if extract.kind == "xlsx" and config.export.mode == "native_xlsx":
                shutil.copy2(extract.path, built)
            else:
                convert.write_xlsx(table, built, config.export.sheet_name)
            published = files.publish(built, config.export.folder / filename, config.export.overwrite)

        emit(f"Saved to the synced folder: {published}")

        result = RunResult(
            started_at=started,
            finished_at=datetime.now(),
            source=source.name,
            workbook=published,
            row_count=table.row_count,
            column_count=len(table.headers),
        )

        if extract.reported_by_sap not in (None, table.row_count):
            warning = (
                f"SAP reported {extract.reported_by_sap} rows but the export "
                f"contained {table.row_count}. Check for a row limit on the layout."
            )
            log.warning(warning)
            result.warnings.append(warning)

        if config.dataset.enabled:
            emit("Refreshing the Power BI dataset...")
            result.dataset_file = dataset.build(config, table, published, timestamp)

        if config.archive.enabled and not skip_archive:
            emit("Archiving older reports...")
            archive_result = archive_module.run(config)
            result.archived = len(archive_result.archived)
            result.deleted = len(archive_result.deleted)

        removed = files.prune_folder(staging, keep=10, pattern="*_raw_*")
        if removed:
            log.info("Cleaned %d old staging file(s).", removed)

        result.finished_at = datetime.now()
        emit(f"Done: {result.describe()}")
        return result


def _build_source(config: Config, password: Optional[str]) -> ReportSource:
    if config.runtime.mock:
        date_from, date_to = config.selection.resolved_dates()
        plants = _values_for(config, ("SWERK", "IWERK", "WERKS"))
        work_centers = _values_for(config, ("ARBPL", "GEWRK"))
        return MockReportSource(
            plants=plants or None,
            work_centers=work_centers or None,
            date_from=datetime.strptime(date_from, "%d.%m.%Y").date(),
            date_to=datetime.strptime(date_to, "%d.%m.%Y").date(),
            seed=1234,
        )

    from .iw29 import SapIw29Source  # imported lazily: pywin32 is Windows-only

    secret = password if password is not None else credentials.resolve(config)
    return SapIw29Source(config, secret)


def _values_for(config: Config, field_names: tuple) -> List[str]:
    for item in config.selection.filters:
        if item.field_name in field_names:
            return list(item.values)
    return []


def _load_table(extract: Extract, emit: ProgressFn) -> convert.Table:
    if extract.kind == "xlsx":
        emit("Reading the workbook SAP produced...")
        return convert.read_xlsx(extract.path)
    emit("Parsing the exported list...")
    return convert.read_sap_text(extract.path)


def _progress_adapter(progress: Optional[ProgressFn]) -> ProgressFn:
    def emit(message: str) -> None:
        log.info(message)
        if progress is not None:
            try:
                progress(message)
            except Exception:
                pass

    return emit


def run_safely(
    config: Config,
    progress: Optional[ProgressFn] = None,
    password: Optional[str] = None,
) -> tuple:
    """Return (result, error). Used by the GUI, which must never crash on failure."""
    try:
        return run(config, progress, password), None
    except Iw29Error as exc:
        log.error("%s", exc)
        return None, exc
    except Exception as exc:  # unexpected: keep the traceback in the log file
        log.exception("Unexpected failure")
        return None, exc
