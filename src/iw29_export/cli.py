"""Command line entry point. This is what Task Scheduler runs."""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__, archive, credentials, doctor, logging_setup, pipeline
from .config import Config
from .errors import EmptyResultError, Iw29Error
from .logging_setup import get_logger

log = get_logger("cli")


COMMANDS = (
    "run",
    "check",
    "inspect",
    "sync-master",
    "iw22-attachments",
    "iw22-merge-pdfs",
    "iw22-lookup",
    "iw38",
    "iw38-kpi",
    "archive",
    "gui",
    "store-password",
    "forget-password",
)

_EPILOG = """\
commands:
  run                 export the report (default when no command is given)
  check               verify SAP, folders and credentials without running anything
  inspect             open the transaction and dump the real screen element ids
  sync-master         copy the latest harvest A2:N into the master Open_NINC sheet
  iw22-attachments    open each notification in IW22 and harvest its GOS attachment
  iw22-merge-pdfs     merge notif(1).pdf + (2).pdf + … into notif.pdf per batch folder
  iw22-lookup         rebuild per-FPSO Excel lists with Scenario summaries
  iw38                harvest IW39 order lists for the configured PG2026 variants
  iw38-kpi            rebuild Performance/Backlog KPI workbook from the latest harvest
  archive             only file away old reports
  gui                 open the desktop app
  store-password      save the SAP password in Windows Credential Manager
  forget-password     remove the stored SAP password

examples:
  iw29-export --mock                     try the whole flow with generated data
  iw29-export check                      pre-flight the setup
  iw29-export run --days 7               last 7 days into the synced folder
  iw29-export sync-master                refresh Open_NINC from the latest harvest
  iw29-export iw22-attachments           harvest attachments for the configured list
  iw29-export iw22-merge-pdfs --batch GIR
  iw29-export iw22-lookup                rebuild GIR/DAL/PAZ/CLV lookup + Scenario
  iw29-export iw38                       download configured IW38/IW39 variants
  iw29-export iw38 --variants CLV-PG2026 CLV-only harvest + KPI dataset refresh
  iw29-export iw38                       harvest GIR+DAL+PAZ+CLV + FPSO_wo_fact.csv
  iw29-export iw38-kpi                   rebuild CLV Performance/Backlog smoke KPIs
  iw29-export iw38-kpi --variants GIR-PG2026 DAL-PG2026 PAZ-PG2026 CLV-PG2026
  iw29-export archive --dry-run          show what housekeeping would do
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="iw29-export",
        description="Export a SAP IW29 notification list into a SharePoint-synced folder.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "command", nargs="?", default="run", choices=COMMANDS, help=argparse.SUPPRESS
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "-c", "--config", type=Path, help="path to a TOML config (default: config.toml)"
    )
    parser.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], help="override log level"
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="no console output")
    parser.add_argument("--mock", action="store_true", help="use generated data, never touch SAP")
    parser.add_argument("--system", help="override sap.system, e.g. FR3")
    parser.add_argument("--variant", help="override selection.variant")
    parser.add_argument("--days", type=int, help="override selection.lookback_days")
    parser.add_argument("--date-from", help="override selection.date_from (YYYY-MM-DD)")
    parser.add_argument("--date-to", help="override selection.date_to (YYYY-MM-DD)")
    parser.add_argument("--out", type=Path, help="override export.folder")
    parser.add_argument("--no-archive", action="store_true", help="skip the archive pass")
    parser.add_argument("--no-dataset", action="store_true", help="skip the Power BI dataset")
    parser.add_argument(
        "--no-master",
        action="store_true",
        help="skip syncing the master dashboard Open_NINC sheet",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="archive command only: change nothing"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="inspect command only: run the report first and dump the result screen",
    )
    parser.add_argument(
        "--batch",
        action="append",
        dest="batches",
        help="iw22-attachments only: run one FPSO batch (repeatable: GIR, DAL, PAZ, CLV)",
    )
    parser.add_argument(
        "--variants",
        nargs="+",
        dest="iw38_variants",
        help="iw38 / iw38-kpi only: limit to these variants (e.g. CLV-PG2026)",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "run"

    try:
        config = _load_config(args)
    except Iw29Error as exc:
        print(f"Configuration problem: {exc}", file=sys.stderr)
        return exc.exit_code

    log_path = logging_setup.configure(
        config.log_folder, config.runtime.log_level, console=not args.quiet
    )

    handlers = {
        "run": _command_run,
        "check": _command_check,
        "inspect": _command_inspect,
        "sync-master": _command_sync_master,
        "iw22-attachments": _command_iw22_attachments,
        "iw22-merge-pdfs": _command_iw22_merge_pdfs,
        "iw22-lookup": _command_iw22_lookup,
        "iw38": _command_iw38,
        "iw38-kpi": _command_iw38_kpi,
        "archive": _command_archive,
        "gui": _command_gui,
        "store-password": _command_store_password,
        "forget-password": _command_forget_password,
    }

    try:
        return handlers[command](config, args)
    except EmptyResultError as exc:
        log.warning("%s", exc)
        return exc.exit_code
    except Iw29Error as exc:
        log.error("%s", exc)
        if args.quiet:
            print(f"Failed: {exc}", file=sys.stderr)
        print(f"Details in {log_path}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        log.warning("Interrupted by the user.")
        return 130
    except Exception as exc:  # pragma: no cover - last resort
        log.exception("Unexpected failure")
        print(f"Unexpected failure: {exc}\nDetails in {log_path}", file=sys.stderr)
        return 1


def _load_config(args: argparse.Namespace) -> Config:
    config = Config.load(args.config)
    overrides: Dict[str, Any] = {}
    if getattr(args, "mock", False):
        overrides["runtime.mock"] = True
    if getattr(args, "system", None):
        overrides["sap.system"] = args.system
    if getattr(args, "variant", None) is not None:
        overrides["selection.variant"] = args.variant
    if getattr(args, "days", None) is not None:
        overrides["selection.lookback_days"] = args.days
        overrides["selection.date_from"] = ""
    if getattr(args, "date_from", None):
        overrides["selection.date_from"] = args.date_from
    if getattr(args, "date_to", None):
        overrides["selection.date_to"] = args.date_to
    if getattr(args, "out", None):
        overrides["export.folder"] = Path(args.out).expanduser()
    if getattr(args, "no_dataset", False):
        overrides["dataset.enabled"] = False
    if getattr(args, "no_master", False):
        overrides["master_dashboard.enabled"] = False
    if getattr(args, "no_archive", False):
        overrides["archive.enabled"] = False
    if getattr(args, "log_level", None):
        overrides["runtime.log_level"] = args.log_level
    return config.with_overrides(**overrides) if overrides else config


def _command_run(config: Config, args: argparse.Namespace) -> int:
    failures = [check for check in doctor.run(config) if check.status == doctor.FAIL]
    if failures:
        for check in failures:
            log.error("Pre-flight check failed: %s", check)
        return 2

    # Progress goes through the logger, which already owns the console handler.
    result = pipeline.run(config)
    _print_summary(result, quiet=args.quiet)
    return 0


def _command_check(config: Config, args: argparse.Namespace) -> int:
    checks = doctor.run(config)
    for check in checks:
        print(check)
    verdict = doctor.worst(checks)
    print()
    print(
        {
            doctor.OK: "Ready to run.",
            doctor.WARN: "Runnable, but look at the warnings above.",
            doctor.FAIL: "Not ready: fix the failures above.",
        }[verdict]
    )
    del args
    return 0 if verdict != doctor.FAIL else 2


def _command_inspect(config: Config, args: argparse.Namespace) -> int:
    from .screen_dump import dump

    execute = bool(getattr(args, "execute", False))
    text = dump(config, execute=execute)
    name = "screen_dump_result.txt" if execute else "screen_dump.txt"
    destination = config.log_folder / name
    destination.write_text(text, encoding="utf-8")
    print(text)
    print(f"\nSaved to {destination}")
    return 0


def _command_sync_master(config: Config, args: argparse.Namespace) -> int:
    from . import master_sync

    del args
    if not config.master_dashboard.enabled:
        # Allow an explicit sync-master even if the daily run flag is off.
        config = config.with_overrides(**{"master_dashboard.enabled": True})
    result = master_sync.sync(config)
    print(
        f"Copied {result.rows_copied:,} rows from {result.source.name} "
        f"into {result.master.name}!{config.master_dashboard.dest_sheet}"
    )
    return 0


def _command_iw22_attachments(config: Config, args: argparse.Namespace) -> int:
    from . import iw22_attachments

    if not config.iw22_attachments.enabled:
        config = config.with_overrides(**{"iw22_attachments.enabled": True})
    result = iw22_attachments.run(config, batch_names=args.batches)
    print(
        f"IW22 attachments: {result.saved} saved, {result.skipped} skipped, "
        f"{result.failed} failed"
    )
    for item in result.results:
        paths = item.paths or ([item.path] if item.path else [])
        where = f" -> {', '.join(p.name for p in paths)}" if paths else ""
        detail = f" ({item.detail})" if item.detail else ""
        batch = f"{item.batch} " if item.batch else ""
        print(f"  [{item.status}] {batch}{item.notification}{where}{detail}")
    return 1 if result.failed and result.saved == 0 else 0


def _command_iw22_merge_pdfs(config: Config, args: argparse.Namespace) -> int:
    from . import iw22_attachments, pdf_merge

    if not config.iw22_attachments.enabled:
        config = config.with_overrides(**{"iw22_attachments.enabled": True})
    jobs = iw22_attachments._resolve_jobs(config, args.batches)
    if not jobs:
        print("No IW22 batch folders configured.", file=sys.stderr)
        return 2
    keep = config.iw22_attachments.merge_keep_parts
    exit_code = 0
    for name, _list_path, out_dir in jobs:
        result = pdf_merge.merge_folder(out_dir, keep_parts=keep)
        print(
            f"[{name}] merged {result.merged_count} in {out_dir} "
            f"(skipped {len(result.skipped)}, errors {len(result.errors)})"
        )
        for path in result.merged:
            print(f"  -> {path.name}")
        for err in result.errors:
            print(f"  [error] {err}", file=sys.stderr)
            exit_code = 1
    return exit_code


def _command_iw22_lookup(config: Config, args: argparse.Namespace) -> int:
    from . import iw22_attachments, iw22_lookup

    if not config.iw22_attachments.enabled:
        config = config.with_overrides(**{"iw22_attachments.enabled": True})
    cfg = config.iw22_attachments
    jobs = iw22_attachments._resolve_jobs(config, args.batches)
    if not jobs and cfg.batches and not args.batches:
        base = cfg.output_folder or (config.export.folder / "iw22_attachments")
        jobs = [
            (batch.name, batch.list_path, batch.output_folder or (base / batch.name))
            for batch in cfg.batches
        ]
    if not jobs:
        print("No IW22 batch folders configured.", file=sys.stderr)
        return 2
    destination = cfg.lookup_xlsx_path or (
        (cfg.output_folder or (config.export.folder / "iw22_attachments"))
        / "iw22_notification_lookup.xlsx"
    )
    result = iw22_lookup.build_lookup_xlsx(
        [(name, out_dir) for name, _list, out_dir in jobs],
        destination,
        sheet_name=cfg.lookup_sheet_name,
        fill_scenario=cfg.fill_scenario,
        scenario_max_pages=cfg.scenario_max_pages,
        split_by_fpso=cfg.lookup_split_by_fpso,
        write_combined=cfg.lookup_write_combined,
    )
    names = ", ".join(p.name for p in result.paths) or result.path.name
    print(f"IW22 lookup: {result.row_count} notification(s) -> {names}")
    return 0


def _command_iw38(config: Config, args: argparse.Namespace) -> int:
    from dataclasses import replace

    from . import iw38

    if not config.iw38.enabled:
        config = replace(config, iw38=replace(config.iw38, enabled=True))
    variants = getattr(args, "iw38_variants", None)
    if variants:
        config = replace(config, iw38=replace(config.iw38, variants=list(variants)))
    result = iw38.run(config)
    print(f"IW38: {result.saved} saved, {result.failed} failed")
    for item in result.results:
        where = f" -> {item.workbook.name}" if item.workbook else ""
        detail = f" ({item.detail})" if item.detail else ""
        print(f"  [{item.status}] {item.variant}{where}{detail}")
    return 1 if result.failed and result.saved == 0 else 0


def _command_iw38_kpi(config: Config, args: argparse.Namespace) -> int:
    from . import iw38_kpi

    variants = getattr(args, "iw38_variants", None) or config.iw38.variants or [
        "CLV-PG2026"
    ]
    exit_code = 0
    for variant in variants:
        try:
            result = iw38_kpi.build_for_variant(config, variant=variant)
        except Exception as exc:
            print(f"[{variant}] KPI failed: {exc}", file=sys.stderr)
            exit_code = 1
            continue
        print(
            f"[{variant}] performance {result.performance_pct:.1%} "
            f"({result.completed}/{result.total_orders} complete), "
            f"backlog {result.backlog}"
        )
        print(f"  fact: {result.fact_csv}")
    if exit_code == 0 or len(variants) > 1:
        try:
            folder = config.iw38.folder or Path.home() / "IW38"
            combined = iw38_kpi.write_combined_fpso_dataset(folder)
            fact = combined.get("fact")
            if fact:
                print(f"Combined FPSO fact: {fact}")
        except Exception as exc:
            print(f"Combined FPSO dataset skipped: {exc}", file=sys.stderr)
    return exit_code


def _command_archive(config: Config, args: argparse.Namespace) -> int:
    result = archive.run(config, dry_run=args.dry_run)
    print(("Dry run: " if args.dry_run else "") + result.summary)
    return 0


def _command_gui(config: Config, args: argparse.Namespace) -> int:
    from .gui import launch  # imported lazily so the CLI works without tkinter

    del args
    return launch(config)


def _command_store_password(config: Config, args: argparse.Namespace) -> int:
    del args
    first = getpass.getpass(f"SAP password for {config.sap.user}: ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        print("The two entries did not match.", file=sys.stderr)
        return 2
    credentials.store(config, first)
    print(
        f"Stored for {config.sap.user} under '{config.sap.credential_service}' in "
        "Windows Credential Manager."
    )
    return 0


def _command_forget_password(config: Config, args: argparse.Namespace) -> int:
    del args
    removed = credentials.forget(config)
    print("Removed." if removed else "Nothing was stored.")
    return 0


def _print_summary(result: pipeline.RunResult, quiet: bool) -> None:
    if quiet:
        return
    lines: List[str] = [
        "",
        f"Source        : {result.source}",
        f"Rows          : {result.row_count:,} x {result.column_count} columns",
        f"Workbook      : {result.workbook}",
    ]
    if result.dataset_file:
        lines.append(f"Dataset       : {result.dataset_file}")
    if result.master_synced:
        lines.append(
            f"Master        : {result.master_synced} ({result.master_rows:,} rows)"
        )
    if result.archived or result.deleted:
        lines.append(f"Housekeeping  : {result.archived} archived, {result.deleted} deleted")
    lines.append(f"Duration      : {result.duration_s:.1f}s")
    for warning in result.warnings:
        lines.append(f"Warning       : {warning}")
    print("\n".join(lines))
