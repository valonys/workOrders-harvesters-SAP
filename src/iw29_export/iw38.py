"""Harvest IW39 order lists for GIR/DAL/PAZ/CLV PG2026 variants.

Mirrors the Excel LaunchSAP.txt flow under OneDrive\\IW38:
  /nIW39 → Outstanding+Historical → Get Variant → Execute → List/Save/File

Published workbooks land in the IW38 folder as IW38_FR3_<variant>_YYYYMMDD.xlsx
so Power BI can build Planned / Backlog / Performance / Monthly Progress KPIs.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from . import convert, credentials, files, lock
from .config import Config
from .errors import ConfigError, EmptyResultError, SapError
from .iw29 import SapIw29Source
from .logging_setup import get_logger
from .source import ProgressFn

log = get_logger("iw38")

# Selection-screen markers for IW39 (order list). Match LaunchSAP.txt.
_IW39_MARKERS = (
    "wnd[0]/usr/chkDY_MAB",
    "wnd[0]/usr/chkDY_HIS",
    "wnd[0]/usr/chkDY_OFN",
    "wnd[0]/usr/ctxtAUART-LOW",
    "wnd[0]/usr/ctxtDATUV",
)


@dataclass
class VariantResult:
    variant: str
    status: str  # saved | skipped | failed
    workbook: Optional[Path] = None
    row_count: int = 0
    detail: str = ""


@dataclass
class Iw38HarvestResult:
    started_at: datetime
    finished_at: datetime
    results: List[VariantResult] = field(default_factory=list)

    @property
    def saved(self) -> int:
        return sum(1 for item in self.results if item.status == "saved")

    @property
    def failed(self) -> int:
        return sum(1 for item in self.results if item.status == "failed")


class SapIw38Source(SapIw29Source):
    """IW39 order-list driver driven by the LaunchSAP selection steps."""

    name = "sap-iw38"

    def _on_selection_screen(self, session) -> bool:
        return any(session.exists(marker) for marker in _IW39_MARKERS)

    def apply_selection(self, session, progress: ProgressFn) -> None:
        # LaunchSAP sets Outstanding/Historical before loading the variant.
        self._apply_checkboxes(session)
        self._apply_variant(session, progress)
        self._apply_layout(session)
        self._apply_filters(session, progress)
        self._apply_raw_steps(session)


def run(
    config: Config,
    progress: Optional[ProgressFn] = None,
    password: Optional[str] = None,
) -> Iw38HarvestResult:
    emit = progress or (lambda message: log.info("%s", message))
    cfg = config.iw38
    if not cfg.enabled:
        raise ConfigError("iw38.enabled is false.")
    if not cfg.variants:
        raise ConfigError("iw38.variants must list at least one variant name.")

    out_dir = cfg.folder or (Path.home() / "IW38")
    out_dir.mkdir(parents=True, exist_ok=True)
    staging = config.staging_folder
    staging.mkdir(parents=True, exist_ok=True)

    started = datetime.now()
    results: List[VariantResult] = []
    secret = password if password is not None else credentials.resolve(config)

    emit(
        f"IW38 harvest: {len(cfg.variants)} variant(s) via {cfg.transaction} → {out_dir}"
    )

    lock_path = staging / "iw38_run.lock"
    with lock.exclusive(lock_path, config.runtime.lock_timeout_s):
        for index, variant in enumerate(cfg.variants, start=1):
            emit(f"[{index}/{len(cfg.variants)}] {variant}")
            try:
                item = _harvest_variant(
                    config, variant, out_dir, staging, secret, emit
                )
            except EmptyResultError as exc:
                log.warning("%s: %s", variant, exc)
                item = VariantResult(
                    variant=variant, status="skipped", detail=str(exc)
                )
            except Exception as exc:
                log.exception("Failed on variant %s", variant)
                item = VariantResult(
                    variant=variant, status="failed", detail=str(exc)
                )
            results.append(item)

    finished = datetime.now()
    summary = Iw38HarvestResult(
        started_at=started, finished_at=finished, results=results
    )
    emit(
        f"Done: {summary.saved} saved, {summary.failed} failed in "
        f"{(finished - started).total_seconds():.1f}s"
    )
    return summary


def _harvest_variant(
    base: Config,
    variant: str,
    out_dir: Path,
    staging: Path,
    password: str,
    progress: ProgressFn,
) -> VariantResult:
    from dataclasses import replace

    cfg = base.iw38
    job = replace(
        base,
        selection=replace(
            base.selection,
            transaction=cfg.transaction,
            variant=variant,
            layout=cfg.layout,
            checkboxes=dict(cfg.checkboxes),
            filters=[],
            ranges=[],
            raw=[],
            notification_date=replace(
                base.selection.notification_date, enabled=False
            ),
        ),
        export=replace(
            base.export,
            folder=out_dir,
            mode=cfg.mode,
            sheet_name=cfg.sheet_name,
            filename_pattern=cfg.filename_pattern,
        ),
        dataset=replace(base.dataset, enabled=False),
        master_dashboard=replace(base.master_dashboard, enabled=False),
        archive=replace(base.archive, enabled=False),
    )

    source = SapIw38Source(job, password)
    extract = source.extract(staging, progress)
    table = (
        convert.read_xlsx(extract.path)
        if extract.kind == "xlsx"
        else convert.read_sap_text(extract.path)
    )

    stamp = datetime.now()
    filename = _render_filename(cfg.filename_pattern, job.sap.system, variant, stamp)
    with tempfile.TemporaryDirectory(prefix="iw38-build-", dir=str(staging)) as scratch:
        built = Path(scratch) / filename
        if extract.kind == "xlsx" and cfg.mode == "native_xlsx":
            shutil.copy2(extract.path, built)
        else:
            convert.write_xlsx(table, built, cfg.sheet_name)
        published = files.publish(built, out_dir / filename, overwrite=True)

    progress(f"Saved {variant} → {published.name} ({table.row_count:,} rows)")
    if cfg.build_kpi:
        try:
            from . import iw38_kpi

            lookup = iw38_kpi.load_item_class_lookup(out_dir, variant)
            enriched = iw38_kpi.ensure_item_class_column(table, lookup)
            # Overwrite the stable harvest with col A = Item Class (XLOOKUP values).
            with tempfile.TemporaryDirectory(
                prefix="iw38-enrich-", dir=str(staging)
            ) as scratch:
                enriched_path = Path(scratch) / published.name
                convert.write_xlsx(enriched, enriched_path, cfg.sheet_name)
                published = files.publish(
                    enriched_path, out_dir / published.name, overwrite=True
                )
            kpi = iw38_kpi.build_from_table(
                enriched, out_dir, variant, item_class_by_order=lookup
            )
            with_class = sum(
                1
                for row in enriched.rows
                if row and str(row[0] or "").strip()
            )
            progress(
                f"KPI {variant}: performance {kpi.performance_pct:.1%} "
                f"({kpi.completed}/{kpi.total_orders}), backlog {kpi.backlog}, "
                f"Item Class filled {with_class}/{enriched.row_count} "
                f"→ {kpi.dashboard_xlsx.name}"
            )
        except Exception as exc:
            log.exception("KPI build failed for %s", variant)
            progress(f"KPI build failed for {variant}: {exc}")
    return VariantResult(
        variant=variant,
        status="saved",
        workbook=published,
        row_count=table.row_count,
        detail=f"{table.row_count} rows",
    )


def _render_filename(
    pattern: str, system: str, variant: str, timestamp: datetime
) -> str:
    from .config import ConfigError, _sanitise_filename

    try:
        name = pattern.format(
            system=system or "SAP",
            variant=variant,
            timestamp=timestamp,
            date=timestamp.date(),
        )
    except (KeyError, ValueError) as exc:
        raise ConfigError(f"iw38.filename_pattern is not a valid pattern: {exc}") from exc
    return _sanitise_filename(name)
