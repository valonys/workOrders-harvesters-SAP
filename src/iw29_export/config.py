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
class Iw22BatchConfig:
    """One FPSO workhorse list → dedicated output folder."""

    name: str
    list_path: Path
    output_folder: Optional[Path] = None


@dataclass
class Iw22AttachmentsConfig:
    """Harvest GOS attachments from IW22 for each number in a list file."""

    enabled: bool = False
    # Legacy single-list mode (used when batches is empty).
    list_path: Optional[Path] = None
    list_column: str = "Notification"
    list_sheet: str = ""
    # first = row 0; match = BITM_DESCR contains match_text; all = every row
    attachment_mode: str = "all"
    match_text: str = "REPORT"
    output_folder: Optional[Path] = None
    download_watch_folder: Optional[Path] = None
    download_timeout_s: int = 45
    limit: int = 0  # 0 = all
    # Skip SAP noise files (e.g. GOS .log / .txt sidecars).
    skip_extensions: List[str] = field(
        default_factory=lambda: [".log", ".txt"]
    )
    # De-facto workhorse harvesters: GIR / DAL / PAZ / CLV.
    batches: List[Iw22BatchConfig] = field(default_factory=list)
    # After harvest, merge notif(1).pdf + notif(2).pdf + … → notif.pdf
    merge_pdfs: bool = True
    # False = delete the split parts after a successful merge (saves OneDrive space).
    merge_keep_parts: bool = False
    # Excel list of harvested notification PDFs for XLOOKUP.
    build_lookup_xlsx: bool = True
    lookup_xlsx_path: Optional[Path] = None
    lookup_sheet_name: str = "Lookup"
    # Split into iw22_notification_lookup_{GIR,DAL,PAZ,CLV}.xlsx beside the combined file.
    lookup_split_by_fpso: bool = True
    lookup_write_combined: bool = True
    # Fill Scenario from PDF text (one natural-language sentence per notification).
    fill_scenario: bool = True
    scenario_max_pages: int = 6


@dataclass
class Iw38Config:
    """Multi-variant IW39 order-list harvest for Power BI (GIR/DAL/PAZ/CLV)."""

    enabled: bool = False
    # LaunchSAP.txt uses IW39 even though the OneDrive folder is named IW38.
    transaction: str = "IW39"
    variants: List[str] = field(
        default_factory=lambda: [
            "GIR-PG2026",
            "DAL-PG2026",
            "PAZ-PG2026",
            "CLV-PG2026",
        ]
    )
    layout: str = ""
    folder: Optional[Path] = None
    filename_pattern: str = "IW38_{system}_{variant}.xlsx"
    sheet_name: str = "IW38"
    mode: str = "text_then_convert"
    # After each harvest, rebuild Performance / Backlog KPI outputs.
    build_kpi: bool = True
    # Matching LaunchSAP.txt: Outstanding (MAB) + Historical (HIS).
    checkboxes: Dict[str, bool] = field(
        default_factory=lambda: {"DY_MAB": True, "DY_HIS": True}
    )


@dataclass
class SharePointAttachmentsConfig:
    """Search a SharePoint site for Fame+/equipment tags and harvest documents."""

    enabled: bool = False
    site_url: str = ""
    list_path: Optional[Path] = None
    output_folder: Optional[Path] = None
    tenant_id: str = ""  # blank = "organizations"
    client_id: str = ""  # blank = Microsoft Graph CLI public client
    max_results_per_tag: int = 50
    merge_pdfs: bool = True
    merge_keep_parts: bool = False
    limit: int = 0


@dataclass
class PowerBiConfig:
    """After IW38 harvest, refresh the local PBIX and publish to the workspace."""

    # Workspace consumers already use. Blank = last workspace chosen in Desktop.
    workspace: str = "My workspace"
    fpso_report: str = "FPSO_Inspection"
    clv_report: str = "CLV_Inspection"
    publish: bool = True
    close_after: bool = True
    open_timeout_s: int = 180
    refresh_timeout_s: int = 600
    publish_timeout_s: int = 360


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
    iw22_attachments: Iw22AttachmentsConfig = field(
        default_factory=Iw22AttachmentsConfig
    )
    iw38: Iw38Config = field(default_factory=Iw38Config)
    sharepoint_attachments: SharePointAttachmentsConfig = field(
        default_factory=SharePointAttachmentsConfig
    )
    powerbi: PowerBiConfig = field(default_factory=PowerBiConfig)
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
            # Strip a UTF-8 BOM if an editor (or PowerShell Set-Content) added one;
            # tomllib rejects it as "Invalid statement" at line 1.
            raw_bytes = path.read_bytes().lstrip(b"\xef\xbb\xbf")
            data = tomllib.loads(raw_bytes.decode("utf-8"))
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
            iw22_attachments=_build_iw22_attachments(
                _section(data, "iw22_attachments", path)
            ),
            iw38=_build_iw38(_section(data, "iw38", path)),
            sharepoint_attachments=_build_sharepoint_attachments(
                _section(data, "sharepoint_attachments", path)
            ),
            powerbi=_build_powerbi(_section(data, "powerbi", path)),
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
        if self.iw22_attachments.enabled:
            cfg = self.iw22_attachments
            if not cfg.batches and not cfg.list_path:
                raise ConfigError(
                    "iw22_attachments needs list_path or [[iw22_attachments.batches]]."
                )
            for batch in cfg.batches:
                if not batch.name.strip():
                    raise ConfigError("iw22_attachments.batches.name must be set.")
                if not str(batch.list_path).strip():
                    raise ConfigError(
                        f"iw22_attachments batch '{batch.name}' needs list_path."
                    )
            mode = cfg.attachment_mode.strip().lower()
            if mode not in {"first", "match", "all"}:
                raise ConfigError(
                    "iw22_attachments.attachment_mode must be "
                    "'first', 'match' or 'all'."
                )
            self.iw22_attachments.attachment_mode = mode
            self.iw22_attachments.skip_extensions = [
                ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                for ext in cfg.skip_extensions
            ]
        if self.iw38.enabled:
            if not self.iw38.variants:
                raise ConfigError("iw38.variants must list at least one name.")
            if not self.iw38.transaction.strip():
                raise ConfigError("iw38.transaction must be set (usually IW39).")
            if self.iw38.mode not in VALID_EXPORT_MODES:
                raise ConfigError(
                    f"iw38.mode must be one of {VALID_EXPORT_MODES}, "
                    f"got '{self.iw38.mode}'."
                )
        if self.sharepoint_attachments.enabled:
            if not self.sharepoint_attachments.site_url.strip():
                raise ConfigError(
                    "sharepoint_attachments.site_url must be set when enabled."
                )
        if self.powerbi.open_timeout_s <= 0 or self.powerbi.refresh_timeout_s <= 0:
            raise ConfigError("powerbi timeouts must be positive.")
        if self.powerbi.publish_timeout_s <= 0:
            raise ConfigError("powerbi.publish_timeout_s must be positive.")
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


def _build_iw38(raw: Dict[str, Any]) -> Iw38Config:
    folder = _str(raw, "iw38.folder", "").strip()
    variants = [str(item).strip() for item in _sequence(raw, "variants") if str(item).strip()]
    checkboxes_raw = raw.get("checkboxes") or {"DY_MAB": True, "DY_HIS": True}
    if not isinstance(checkboxes_raw, dict):
        raise ConfigError("iw38.checkboxes must be a table of name = true/false.")
    checkboxes = {str(key): bool(value) for key, value in checkboxes_raw.items()}
    return Iw38Config(
        enabled=_bool(raw, "iw38.enabled", False),
        transaction=_str(raw, "iw38.transaction", "IW39").strip() or "IW39",
        variants=variants
        or ["GIR-PG2026", "DAL-PG2026", "PAZ-PG2026", "CLV-PG2026"],
        layout=_str(raw, "iw38.layout", "").strip(),
        folder=Path(folder).expanduser() if folder else None,
        filename_pattern=_str(
            raw,
            "iw38.filename_pattern",
            "IW38_{system}_{variant}.xlsx",
        ).strip(),
        sheet_name=_str(raw, "iw38.sheet_name", "IW38").strip() or "IW38",
        mode=_str(raw, "iw38.mode", "text_then_convert").strip().lower(),
        build_kpi=_bool(raw, "iw38.build_kpi", True),
        checkboxes=checkboxes,
    )


def _build_sharepoint_attachments(raw: Dict[str, Any]) -> SharePointAttachmentsConfig:
    list_path = _str(raw, "sharepoint_attachments.list_path", "").strip()
    output = _str(raw, "sharepoint_attachments.output_folder", "").strip()
    return SharePointAttachmentsConfig(
        enabled=_bool(raw, "sharepoint_attachments.enabled", False),
        site_url=_str(raw, "sharepoint_attachments.site_url", "").strip(),
        list_path=Path(list_path).expanduser() if list_path else None,
        output_folder=Path(output).expanduser() if output else None,
        tenant_id=_str(raw, "sharepoint_attachments.tenant_id", "").strip(),
        client_id=_str(raw, "sharepoint_attachments.client_id", "").strip(),
        max_results_per_tag=_int(
            raw, "sharepoint_attachments.max_results_per_tag", 50
        ),
        merge_pdfs=_bool(raw, "sharepoint_attachments.merge_pdfs", True),
        merge_keep_parts=_bool(
            raw, "sharepoint_attachments.merge_keep_parts", False
        ),
        limit=_int(raw, "sharepoint_attachments.limit", 0),
    )


def _build_iw22_attachments(raw: Dict[str, Any]) -> Iw22AttachmentsConfig:
    list_path = _str(raw, "iw22_attachments.list_path", "").strip()
    output = _str(raw, "iw22_attachments.output_folder", "").strip()
    watch = _str(raw, "iw22_attachments.download_watch_folder", "").strip()
    lookup_path = _str(raw, "iw22_attachments.lookup_xlsx_path", "").strip()
    skip_raw = raw.get("skip_extensions", [".log", ".txt"])
    if isinstance(skip_raw, str):
        skip_exts = [skip_raw]
    elif isinstance(skip_raw, (list, tuple)):
        skip_exts = [str(item).strip() for item in skip_raw if str(item).strip()]
    else:
        raise ConfigError("iw22_attachments.skip_extensions must be a list.")
    batches: List[Iw22BatchConfig] = []
    for entry in _list_of_tables(raw, "iw22_attachments.batches"):
        name = _str(entry, "iw22_attachments.batches.name", "").strip()
        batch_list = _str(entry, "iw22_attachments.batches.list_path", "").strip()
        batch_out = _str(entry, "iw22_attachments.batches.output_folder", "").strip()
        if not name and not batch_list:
            continue
        batches.append(
            Iw22BatchConfig(
                name=name,
                list_path=Path(batch_list).expanduser(),
                output_folder=Path(batch_out).expanduser() if batch_out else None,
            )
        )
    return Iw22AttachmentsConfig(
        enabled=_bool(raw, "iw22_attachments.enabled", False),
        list_path=Path(list_path).expanduser() if list_path else None,
        list_column=_str(raw, "iw22_attachments.list_column", "Notification").strip(),
        list_sheet=_str(raw, "iw22_attachments.list_sheet", "").strip(),
        attachment_mode=_str(
            raw, "iw22_attachments.attachment_mode", "all"
        ).strip().lower(),
        match_text=_str(raw, "iw22_attachments.match_text", "REPORT").strip(),
        output_folder=Path(output).expanduser() if output else None,
        download_watch_folder=Path(watch).expanduser() if watch else None,
        download_timeout_s=_int(raw, "iw22_attachments.download_timeout_s", 45),
        limit=_int(raw, "iw22_attachments.limit", 0),
        skip_extensions=skip_exts or [".log", ".txt"],
        batches=batches,
        merge_pdfs=_bool(raw, "iw22_attachments.merge_pdfs", True),
        merge_keep_parts=_bool(raw, "iw22_attachments.merge_keep_parts", False),
        build_lookup_xlsx=_bool(raw, "iw22_attachments.build_lookup_xlsx", True),
        lookup_xlsx_path=(
            Path(lookup_path).expanduser() if lookup_path else None
        ),
        lookup_sheet_name=_str(
            raw, "iw22_attachments.lookup_sheet_name", "Lookup"
        ).strip()
        or "Lookup",
        lookup_split_by_fpso=_bool(
            raw, "iw22_attachments.lookup_split_by_fpso", True
        ),
        lookup_write_combined=_bool(
            raw, "iw22_attachments.lookup_write_combined", True
        ),
        fill_scenario=_bool(raw, "iw22_attachments.fill_scenario", True),
        scenario_max_pages=_int(raw, "iw22_attachments.scenario_max_pages", 6),
    )


def _build_powerbi(raw: Dict[str, Any]) -> PowerBiConfig:
    return PowerBiConfig(
        workspace=_str(raw, "powerbi.workspace", "My workspace").strip()
        or "My workspace",
        fpso_report=_str(raw, "powerbi.fpso_report", "FPSO_Inspection").strip()
        or "FPSO_Inspection",
        clv_report=_str(raw, "powerbi.clv_report", "CLV_Inspection").strip()
        or "CLV_Inspection",
        publish=_bool(raw, "powerbi.publish", True),
        close_after=_bool(raw, "powerbi.close_after", True),
        open_timeout_s=_int(raw, "powerbi.open_timeout_s", 180),
        refresh_timeout_s=_int(raw, "powerbi.refresh_timeout_s", 600),
        publish_timeout_s=_int(raw, "powerbi.publish_timeout_s", 360),
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
