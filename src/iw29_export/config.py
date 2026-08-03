"""Typed configuration loaded from a TOML file."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .errors import ConfigError

# SAP's own date entry format, including the open-ended 31.12.9999.
_SAP_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")

try:  # pragma: no cover - depends on interpreter version
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise ConfigError(
            "Reading TOML needs Python 3.11+ or the 'tomli' package "
            "(pip install -r requirements.txt)."
        ) from exc

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_NAMES = ("config.toml", "config.example.toml")

VALID_AUTH = ("sso", "credential_manager", "prompt")
VALID_EXPORT_MODES = ("text_then_convert", "native_xlsx")
VALID_DATASET_MODES = ("replace", "append")
VALID_RAW_ACTIONS = ("set_text", "set_checked", "press", "send_vkey", "select")


@dataclass
class SapConfig:
    system: str = ""
    # The SAP Logon entry name, which is usually not the system id. Needed only
    # to open a fresh connection; matching an already-open one uses `system`.
    connection_name: str = ""
    client: str = ""
    user: str = ""
    language: str = "EN"
    auth: str = "sso"
    credential_service: str = "sap-iw29-export"
    logon_path: str = r"C:\Program Files (x86)\SAP\FrontEnd\SAPgui\saplogon.exe"
    attach_timeout_s: int = 90
    step_timeout_s: int = 300
    close_connection: bool = True
    reuse_existing_connection: bool = True
    # Open a separate SAP session instead of driving the one on screen, so a
    # scheduled run and the person at the keyboard never fight over it.
    own_session: bool = True


@dataclass
class Filter:
    field_name: str
    values: List[str] = field(default_factory=list)
    kind: str = "ctxt"


@dataclass
class Range:
    field_name: str
    low: str = ""
    high: str = ""
    kind: str = "ctxt"


@dataclass
class RawStep:
    element_id: str
    action: str
    value: str = ""


@dataclass
class NotificationDateWindow:
    """IW29's 'Notification date' pair.

    These are two plain fields, not a select-option, so they cannot be driven by
    [[selection.ranges]]. They are also the one thing a saved variant must not be
    trusted with: a variant carries whatever dates it was saved with, which
    silently drops notifications created since. Leaving `low` blank and `high` at
    SAP's open-ended 31.12.9999 keeps the report open-ended, so anything newly
    created is always picked up.
    """

    enabled: bool = True
    low: str = ""
    high: str = "31.12.9999"
    field_low: str = "DATUV"
    field_high: str = "DATUB"

    def describe(self) -> str:
        return f"{self.low or '<blank>'} to {self.high or '<blank>'}"


@dataclass
class SelectionConfig:
    transaction: str = "IW29"
    variant: str = ""
    layout: str = ""
    lookback_days: int = 30
    date_from: str = ""
    date_to: str = ""
    notification_date: NotificationDateWindow = field(
        default_factory=NotificationDateWindow
    )
    filters: List[Filter] = field(default_factory=list)
    ranges: List[Range] = field(default_factory=list)
    checkboxes: Dict[str, bool] = field(default_factory=dict)
    raw: List[RawStep] = field(default_factory=list)

    def resolved_dates(self, today: Optional[date] = None) -> tuple[str, str]:
        """Return (from, to) as SAP-friendly ``dd.mm.yyyy`` strings."""
        today = today or date.today()
        end = _parse_iso_date(self.date_to, "selection.date_to") or today
        start = _parse_iso_date(self.date_from, "selection.date_from")
        if start is None:
            start = end - timedelta(days=max(self.lookback_days, 0))
        if start > end:
            raise ConfigError(
                f"selection.date_from ({start}) is after selection.date_to ({end})."
            )
        return _sap_date(start), _sap_date(end)


@dataclass
class ExportConfig:
    folder: Path = Path.home() / "IW29"
    filename_pattern: str = "IW29_{system}_{timestamp:%Y%m%d}.xlsx"
    mode: str = "text_then_convert"
    sheet_name: str = "IW29"
    encoding: str = "utf-8-sig"
    staging_folder: Optional[Path] = None
    overwrite: bool = True

    def render_filename(self, system: str, timestamp: datetime) -> str:
        try:
            name = self.filename_pattern.format(
                system=system or "SAP",
                timestamp=timestamp,
                date=timestamp.date(),
            )
        except (KeyError, ValueError) as exc:
            raise ConfigError(
                f"export.filename_pattern is not a valid pattern: {exc}"
            ) from exc
        return _sanitise_filename(name)


@dataclass
class ArchiveConfig:
    enabled: bool = True
    archive_after_days: int = 7
    delete_after_days: int = 365
    folder: Optional[Path] = None


@dataclass
class DatasetConfig:
    enabled: bool = True
    folder: Optional[Path] = None
    filename: str = "iw29_dataset.csv"
    mode: str = "replace"
    add_run_columns: bool = True


@dataclass
class MasterDashboardConfig:
    """After harvest, paste IW29!A2:N into the master workbook's Open_NINC sheet."""

    enabled: bool = False
    path: Optional[Path] = None
    source_sheet: str = "IW29"
    dest_sheet: str = "Open_NINC"
    # Last helper-formula column on Open_NINC (AL = 38 on the current workbook).
    formula_last_col: int = 38


@dataclass
class RuntimeConfig:
    mock: bool = False
    log_folder: Optional[Path] = None
    log_level: str = "INFO"
    lock_timeout_s: int = 0


@dataclass
class Config:
    sap: SapConfig = field(default_factory=SapConfig)
    selection: SelectionConfig = field(default_factory=SelectionConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    archive: ArchiveConfig = field(default_factory=ArchiveConfig)
    dataset: DatasetConfig = field(default_factory=DatasetConfig)
    master_dashboard: MasterDashboardConfig = field(
        default_factory=MasterDashboardConfig
    )
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    source_path: Optional[Path] = None

    @property
    def staging_folder(self) -> Path:
        if self.export.staging_folder is not None:
            return self.export.staging_folder
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "sap-iw29-export" / "staging"

    @property
    def archive_folder(self) -> Path:
        return self.archive.folder or (self.export.folder / "archive")

    @property
    def dataset_folder(self) -> Path:
        return self.dataset.folder or (self.export.folder / "dataset")

    @property
    def log_folder(self) -> Path:
        return self.runtime.log_folder or (PROJECT_ROOT / "logs")

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = _resolve_config_path(path)
        try:
            with path.open("rb") as handle:
                data = tomllib.load(handle)
        except OSError as exc:
            raise ConfigError(f"Cannot read config file {path}: {exc}") from exc
        except Exception as exc:  # tomllib raises TOMLDecodeError
            raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

        cfg = cls(
            sap=_build_sap(_section(data, "sap", path)),
            selection=_build_selection(_section(data, "selection", path)),
            export=_build_export(_section(data, "export", path)),
            archive=_build_archive(_section(data, "archive", path)),
            dataset=_build_dataset(_section(data, "dataset", path)),
            master_dashboard=_build_master_dashboard(
                _section(data, "master_dashboard", path)
            ),
            runtime=_build_runtime(_section(data, "runtime", path)),
            source_path=path,
        )
        cfg.validate()
        return cfg

    def with_overrides(self, **kwargs: Any) -> "Config":
        """Return a copy with dotted overrides applied, e.g. ``runtime.mock=True``."""
        sections: Dict[str, Any] = {}
        for dotted, value in kwargs.items():
            if value is None:
                continue
            section_name, _, attr = dotted.partition(".")
            if not attr or not hasattr(self, section_name):
                raise ConfigError(f"Unknown config override '{dotted}'.")
            sections.setdefault(section_name, {})[attr] = value
        updated = {
            name: replace(getattr(self, name), **values)
            for name, values in sections.items()
        }
        clone = replace(self, **updated)
        clone.validate()
        return clone

    def validate(self) -> None:
        if not self.runtime.mock and not self.sap.system.strip():
            raise ConfigError("sap.system must be set (e.g. \"FR3\").")
        if self.sap.auth not in VALID_AUTH:
            raise ConfigError(
                f"sap.auth must be one of {VALID_AUTH}, got '{self.sap.auth}'."
            )
        if self.sap.auth != "sso" and not self.sap.user.strip():
            raise ConfigError(f"sap.user is required when sap.auth = '{self.sap.auth}'.")
        if self.export.mode not in VALID_EXPORT_MODES:
            raise ConfigError(
                f"export.mode must be one of {VALID_EXPORT_MODES}, "
                f"got '{self.export.mode}'."
            )
        if self.dataset.mode not in VALID_DATASET_MODES:
            raise ConfigError(
                f"dataset.mode must be one of {VALID_DATASET_MODES}, "
                f"got '{self.dataset.mode}'."
            )
        if self.master_dashboard.enabled:
            if not self.master_dashboard.path:
                raise ConfigError(
                    "master_dashboard.path must be set when master_dashboard.enabled "
                    "is true."
                )
            if self.master_dashboard.formula_last_col < 14:
                raise ConfigError(
                    "master_dashboard.formula_last_col must be >= 14 (column N)."
                )
        if not self.selection.transaction.strip():
            raise ConfigError("selection.transaction must be set.")
        if not str(self.export.folder).strip():
            raise ConfigError("export.folder must be set.")
        if self.sap.attach_timeout_s <= 0 or self.sap.step_timeout_s <= 0:
            raise ConfigError("SAP timeouts must be positive.")
        if (
            self.archive.enabled
            and self.archive.delete_after_days
            and self.archive.delete_after_days < self.archive.archive_after_days
        ):
            raise ConfigError(
                "archive.delete_after_days must be >= archive.archive_after_days."
            )
        for step in self.selection.raw:
            if step.action not in VALID_RAW_ACTIONS:
                raise ConfigError(
                    f"selection.raw action must be one of {VALID_RAW_ACTIONS}, "
                    f"got '{step.action}'."
                )
        self.selection.resolved_dates()


def _resolve_config_path(path: Optional[Path]) -> Path:
    if path is not None:
        candidate = Path(path).expanduser()
        if not candidate.is_file():
            raise ConfigError(f"Config file not found: {candidate}")
        return candidate
    env = os.environ.get("IW29_CONFIG")
    if env:
        return _resolve_config_path(Path(env))
    for name in DEFAULT_CONFIG_NAMES:
        candidate = PROJECT_ROOT / name
        if candidate.is_file():
            return candidate
    raise ConfigError(
        f"No config found. Copy config.example.toml to config.toml in {PROJECT_ROOT}."
    )


def _section(data: Dict[str, Any], name: str, path: Path) -> Dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ConfigError(f"[{name}] in {path} must be a table.")
    return value


def _build_sap(raw: Dict[str, Any]) -> SapConfig:
    return SapConfig(
        system=_str(raw, "sap.system", ""),
        connection_name=_str(raw, "sap.connection_name", ""),
        client=_str(raw, "sap.client", ""),
        user=_str(raw, "sap.user", ""),
        language=_str(raw, "sap.language", "EN"),
        auth=_str(raw, "sap.auth", "sso").strip().lower(),
        credential_service=_str(raw, "sap.credential_service", "sap-iw29-export"),
        logon_path=_str(raw, "sap.logon_path", SapConfig.logon_path),
        attach_timeout_s=_int(raw, "sap.attach_timeout_s", 90),
        step_timeout_s=_int(raw, "sap.step_timeout_s", 300),
        close_connection=_bool(raw, "sap.close_connection", True),
        reuse_existing_connection=_bool(raw, "sap.reuse_existing_connection", True),
        own_session=_bool(raw, "sap.own_session", True),
    )


def _build_notification_date(raw: Dict[str, Any]) -> NotificationDateWindow:
    entry = raw.get("notification_date", {})
    if not isinstance(entry, dict):
        raise ConfigError("[selection.notification_date] must be a table.")
    defaults = NotificationDateWindow()
    window = NotificationDateWindow(
        enabled=_bool(entry, "selection.notification_date.enabled", defaults.enabled),
        low=_str(entry, "selection.notification_date.from", defaults.low).strip(),
        high=_str(entry, "selection.notification_date.to", defaults.high).strip(),
        field_low=_str(
            entry, "selection.notification_date.field_from", defaults.field_low
        ).strip().upper(),
        field_high=_str(
            entry, "selection.notification_date.field_to", defaults.field_high
        ).strip().upper(),
    )
    for label, value in (("from", window.low), ("to", window.high)):
        if value and not _SAP_DATE.match(value):
            raise ConfigError(
                f"selection.notification_date.{label} must be blank or a SAP date "
                f"like 31.12.9999, not {value!r}."
            )
    if window.enabled and not (window.field_low and window.field_high):
        raise ConfigError(
            "selection.notification_date needs both field_from and field_to, or "
            "set enabled = false."
        )
    return window


def _build_selection(raw: Dict[str, Any]) -> SelectionConfig:
    filters = []
    for entry in _list_of_tables(raw, "selection.filters"):
        name = _str(entry, "selection.filters.field", "").strip().upper()
        if not name:
            raise ConfigError("Every [[selection.filters]] needs a 'field'.")
        filters.append(
            Filter(
                field_name=name,
                values=[str(v).strip() for v in _sequence(entry, "values") if str(v).strip()],
                kind=_str(entry, "selection.filters.kind", "ctxt"),
            )
        )

    ranges = []
    for entry in _list_of_tables(raw, "selection.ranges"):
        name = _str(entry, "selection.ranges.field", "").strip().upper()
        if not name:
            raise ConfigError("Every [[selection.ranges]] needs a 'field'.")
        ranges.append(
            Range(
                field_name=name,
                low=_str(entry, "selection.ranges.low", ""),
                high=_str(entry, "selection.ranges.high", ""),
                kind=_str(entry, "selection.ranges.kind", "ctxt"),
            )
        )

    steps = []
    for entry in _list_of_tables(raw, "selection.raw"):
        element_id = _str(entry, "selection.raw.id", "").strip()
        if not element_id:
            raise ConfigError("Every [[selection.raw]] needs an 'id'.")
        steps.append(
            RawStep(
                element_id=element_id,
                action=_str(entry, "selection.raw.action", "set_text").strip().lower(),
                value=_str(entry, "selection.raw.value", ""),
            )
        )

    checkboxes_raw = raw.get("checkboxes", {})
    if not isinstance(checkboxes_raw, dict):
        raise ConfigError("[selection.checkboxes] must be a table of name = true/false.")

    return SelectionConfig(
        notification_date=_build_notification_date(raw),
        transaction=_str(raw, "selection.transaction", "IW29").strip().upper(),
        variant=_str(raw, "selection.variant", "").strip(),
        layout=_str(raw, "selection.layout", "").strip(),
        lookback_days=_int(raw, "selection.lookback_days", 30),
        date_from=_str(raw, "selection.date_from", "").strip(),
        date_to=_str(raw, "selection.date_to", "").strip(),
        filters=filters,
        ranges=ranges,
        checkboxes={str(k).strip().upper(): bool(v) for k, v in checkboxes_raw.items()},
        raw=steps,
    )


def _build_export(raw: Dict[str, Any]) -> ExportConfig:
    staging = _str(raw, "export.staging_folder", "").strip()
    return ExportConfig(
        folder=_path(raw, "export.folder", ExportConfig.folder),
        filename_pattern=_str(
            raw, "export.filename_pattern", ExportConfig.filename_pattern
        ),
        mode=_str(raw, "export.mode", "text_then_convert").strip().lower(),
        sheet_name=_str(raw, "export.sheet_name", "IW29"),
        encoding=_str(raw, "export.encoding", "utf-8-sig"),
        staging_folder=Path(staging).expanduser() if staging else None,
        overwrite=_bool(raw, "export.overwrite", True),
    )


def _build_archive(raw: Dict[str, Any]) -> ArchiveConfig:
    folder = _str(raw, "archive.folder", "").strip()
    return ArchiveConfig(
        enabled=_bool(raw, "archive.enabled", True),
        archive_after_days=_int(raw, "archive.archive_after_days", 7),
        delete_after_days=_int(raw, "archive.delete_after_days", 365),
        folder=Path(folder).expanduser() if folder else None,
    )


def _build_dataset(raw: Dict[str, Any]) -> DatasetConfig:
    folder = _str(raw, "dataset.folder", "").strip()
    return DatasetConfig(
        enabled=_bool(raw, "dataset.enabled", True),
        folder=Path(folder).expanduser() if folder else None,
        filename=_sanitise_filename(_str(raw, "dataset.filename", "iw29_dataset.csv")),
        mode=_str(raw, "dataset.mode", "replace").strip().lower(),
        add_run_columns=_bool(raw, "dataset.add_run_columns", True),
    )


def _build_master_dashboard(raw: Dict[str, Any]) -> MasterDashboardConfig:
    path_text = _str(raw, "master_dashboard.path", "").strip()
    return MasterDashboardConfig(
        enabled=_bool(raw, "master_dashboard.enabled", False),
        path=Path(path_text).expanduser() if path_text else None,
        source_sheet=_str(raw, "master_dashboard.source_sheet", "IW29").strip(),
        dest_sheet=_str(raw, "master_dashboard.dest_sheet", "Open_NINC").strip(),
        formula_last_col=_int(raw, "master_dashboard.formula_last_col", 38),
    )


def _build_runtime(raw: Dict[str, Any]) -> RuntimeConfig:
    folder = _str(raw, "runtime.log_folder", "").strip()
    return RuntimeConfig(
        mock=_bool(raw, "runtime.mock", False),
        log_folder=Path(folder).expanduser() if folder else None,
        log_level=_str(raw, "runtime.log_level", "INFO").strip().upper(),
        lock_timeout_s=_int(raw, "runtime.lock_timeout_s", 0),
    )


def _str(raw: Dict[str, Any], dotted: str, default: str) -> str:
    value = raw.get(dotted.rsplit(".", 1)[-1], default)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if not isinstance(value, str):
        raise ConfigError(f"{dotted} must be text, got {type(value).__name__}.")
    return value


def _int(raw: Dict[str, Any], dotted: str, default: int) -> int:
    value = raw.get(dotted.rsplit(".", 1)[-1], default)
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            return int(str(value))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"{dotted} must be a whole number.") from exc
    return value


def _bool(raw: Dict[str, Any], dotted: str, default: bool) -> bool:
    value = raw.get(dotted.rsplit(".", 1)[-1], default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false", "yes", "no"}:
        return value.strip().lower() in {"true", "yes"}
    raise ConfigError(f"{dotted} must be true or false.")


def _path(raw: Dict[str, Any], dotted: str, default: Path) -> Path:
    value = _str(raw, dotted, str(default)).strip()
    if not value:
        return default
    return Path(os.path.expandvars(value)).expanduser()


def _sequence(raw: Dict[str, Any], key: str) -> Sequence[Any]:
    value = raw.get(key, [])
    if value in (None, ""):
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    if not isinstance(value, (list, tuple)):
        raise ConfigError(f"'{key}' must be a list of values.")
    return value


def _list_of_tables(raw: Dict[str, Any], dotted: str) -> List[Dict[str, Any]]:
    value = raw.get(dotted.rsplit(".", 1)[-1], [])
    if not value:
        return []
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value):
        raise ConfigError(f"[[{dotted}]] must be a list of tables.")
    return value


def _parse_iso_date(value: str, label: str) -> Optional[date]:
    text = (value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ConfigError(f"{label} must look like 2026-07-30, got '{value}'.")


def _sap_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def _sanitise_filename(name: str) -> str:
    cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip()
    if not cleaned:
        raise ConfigError("Resolved file name is empty.")
    return cleaned
