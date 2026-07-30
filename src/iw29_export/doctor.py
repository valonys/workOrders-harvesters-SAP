"""Pre-flight checks, so a scheduled 06:00 run does not fail on something obvious."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .config import Config
from .logging_setup import get_logger

log = get_logger("doctor")

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""

    @property
    def symbol(self) -> str:
        return {OK: "[ok]  ", WARN: "[warn]", FAIL: "[fail]"}.get(self.status, "[?]   ")

    def __str__(self) -> str:
        return f"{self.symbol} {self.name}" + (f" - {self.detail}" if self.detail else "")


def run(config: Config) -> List[Check]:
    checks: List[Check] = [_python_version()]

    if config.export.mode == "native_xlsx":
        checks.append(_module("openpyxl", "reading SAP's own workbook"))

    if config.runtime.mock:
        checks.append(Check("Mock mode", WARN, "SAP will not be contacted."))
    else:
        checks.append(_module("win32com.client", "SAP GUI Scripting", package="pywin32"))
        checks.append(_saplogon(config))
        checks.append(_scripting_registry())
        checks.append(_credentials(config))

    checks.append(_writable(config.export.folder, "Export folder"))
    checks.append(_onedrive(config.export.folder))
    checks.append(_writable(config.staging_folder, "Staging folder"))
    checks.append(_writable(config.log_folder, "Log folder"))
    checks.append(_selection(config))
    checks.append(_notification_dates(config))
    return checks


def worst(checks: List[Check]) -> str:
    if any(check.status == FAIL for check in checks):
        return FAIL
    if any(check.status == WARN for check in checks):
        return WARN
    return OK


def _python_version() -> Check:
    version = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info < (3, 9):
        return Check("Python version", FAIL, f"{version} found, 3.9 or newer needed")
    return Check("Python version", OK, f"{version} ({'64' if sys.maxsize > 2**32 else '32'}-bit)")


def _module(module_name: str, purpose: str, package: Optional[str] = None) -> Check:
    label = f"{package or module_name} ({purpose})"
    try:
        __import__(module_name)
    except ImportError:
        return Check(
            label, FAIL, f"not installed - pip install {package or module_name}"
        )
    return Check(label, OK)


def _saplogon(config: Config) -> Check:
    path = Path(os.path.expandvars(config.sap.logon_path))
    if path.is_file():
        return Check("SAP Logon executable", OK, str(path))
    return Check(
        "SAP Logon executable",
        WARN,
        f"not found at {path}; fine if SAP Logon is already running, otherwise fix "
        "sap.logon_path",
    )


def _scripting_registry() -> Check:
    """Client-side scripting toggles live in the registry; report what we find."""
    try:
        import winreg
    except ImportError:
        return Check("SAP GUI scripting (client)", WARN, "not a Windows machine")

    key_path = r"Software\SAP\SAPGUI Front\SAP Frontend Server\Security"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            values = {}
            for index in range(winreg.QueryInfoKey(key)[1]):
                name, value, _ = winreg.EnumValue(key, index)
                values[name] = value
    except OSError:
        return Check(
            "SAP GUI scripting (client)",
            WARN,
            "registry settings not found; enable scripting under SAP Logon > Options "
            "> Accessibility & Scripting > Scripting",
        )

    warnings = [
        name
        for name in ("WarnOnAttach", "WarnOnConnection")
        if values.get(name) not in (0, "0", None)
    ]
    if warnings:
        return Check(
            "SAP GUI scripting (client)",
            WARN,
            "scripting will show a confirmation popup ("
            + ", ".join(warnings)
            + "); untick the notification options for unattended runs",
        )
    return Check("SAP GUI scripting (client)", OK, "no attach warnings configured")


def _credentials(config: Config) -> Check:
    if config.sap.auth == "sso":
        return Check("Credentials", OK, "single sign-on")
    if config.sap.auth == "prompt":
        return Check(
            "Credentials", WARN, "set to prompt, so unattended runs will hang"
        )
    try:
        import keyring
    except ImportError:
        return Check("Credentials", FAIL, "keyring is not installed")
    secret = keyring.get_password(config.sap.credential_service, config.sap.user)
    if not secret:
        return Check(
            "Credentials",
            FAIL,
            f"nothing stored for {config.sap.user}; run: iw29-export store-password",
        )
    return Check("Credentials", OK, f"password found for {config.sap.user}")


def _writable(folder: Path, label: str) -> Check:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / ".iw29_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return Check(label, FAIL, f"{folder} is not writable: {exc}")
    return Check(label, OK, str(folder))


def _onedrive(folder: Path) -> Check:
    text = str(folder).lower()
    if "onedrive" in text or "sharepoint" in text:
        return Check("SharePoint sync", OK, "the export folder looks OneDrive-synced")
    env_roots = [
        os.environ.get(name, "")
        for name in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer")
    ]
    for root in filter(None, env_roots):
        if text.startswith(root.lower()):
            return Check("SharePoint sync", OK, f"inside {root}")
    return Check(
        "SharePoint sync",
        WARN,
        "the export folder does not look like a OneDrive-synced library, so nothing "
        "will reach SharePoint",
    )


def _selection(config: Config) -> Check:
    selection = config.selection
    described = []
    if selection.variant:
        described.append(f"variant {selection.variant}")
    filters = sum(1 for item in selection.filters if item.values)
    described.append(f"{filters} filter(s)")
    if not selection.variant and not filters and not selection.raw:
        return Check(
            "Selection criteria",
            WARN,
            "no variant and no filters, so the report will run wide open",
        )
    return Check("Selection criteria", OK, ", ".join(described))


def _notification_dates(config: Config) -> Check:
    window = config.selection.notification_date
    if not window.enabled:
        return Check(
            "Notification dates",
            WARN,
            "left to the variant, so notifications created after the variant was "
            "saved may be missed",
        )
    if window.low:
        return Check(
            "Notification dates",
            WARN,
            f"lower bound {window.low} set, so notifications before it are excluded",
        )
    if window.high != "31.12.9999":
        return Check(
            "Notification dates",
            WARN,
            f"upper bound {window.high} is not open-ended, so recent notifications "
            "may be missed",
        )
    return Check(
        "Notification dates",
        OK,
        f"open-ended ({window.describe()}), so new notifications are always captured",
    )
