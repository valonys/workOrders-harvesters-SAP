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
  archive             only file away old reports
  gui                 open the desktop app
  store-password      save the SAP password in Windows Credential Manager
  forget-password     remove the stored SAP password

examples:
  iw29-export --mock                     try the whole flow with generated data
  iw29-export check                      pre-flight the setup
  iw29-export run --days 7               last 7 days into the synced folder
  iw29-export sync-master                refresh Open_NINC from the latest harvest
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
