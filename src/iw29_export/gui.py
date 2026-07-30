"""The light desktop app: one window, one button, a live log."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from dataclasses import replace
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import List, Optional, Tuple

from . import __version__, doctor, logging_setup, pipeline
from .config import Config, Filter
from .errors import EmptyResultError, Iw29Error

PLANT_FIELDS = ("SWERK", "IWERK", "WERKS")
WORK_CENTER_FIELDS = ("ARBPL", "GEWRK")

_LEVEL_COLOURS = {
    "DEBUG": "#7a7a7a",
    "INFO": "#1f2933",
    "WARNING": "#9a6700",
    "ERROR": "#b42318",
    "CRITICAL": "#b42318",
}


class App(ttk.Frame):
    def __init__(self, master: tk.Tk, config: Config):
        super().__init__(master, padding=14)
        self.master_window = master
        self.base_config = config
        self.messages: "queue.Queue[Tuple[str, str]]" = queue.Queue()
        self.results: "queue.Queue[Tuple[Optional[pipeline.RunResult], Optional[Exception]]]" = (
            queue.Queue()
        )
        self.worker: Optional[threading.Thread] = None
        self.log_handler = None
        self.last_output: Optional[Path] = None

        self.plant_field = _pick_field(config, PLANT_FIELDS, "SWERK")
        self.work_center_field = _pick_field(config, WORK_CENTER_FIELDS, "ARBPL")

        self._build_variables()
        self._build_layout()
        self.grid(sticky="nsew")
        self.after(120, self._drain_messages)

    # ------------------------------------------------------------------ widgets

    def _build_variables(self) -> None:
        selection = self.base_config.selection
        date_from, date_to = selection.resolved_dates()
        self.var_system = tk.StringVar(value=self.base_config.sap.system)
        self.var_variant = tk.StringVar(value=selection.variant)
        self.var_date_from = tk.StringVar(value=date_from)
        self.var_date_to = tk.StringVar(value=date_to)
        self.var_plant = tk.StringVar(value=_joined(self.base_config, PLANT_FIELDS))
        self.var_work_centers = tk.StringVar(
            value=_joined(self.base_config, WORK_CENTER_FIELDS)
        )
        self.var_folder = tk.StringVar(value=str(self.base_config.export.folder))
        self.var_mock = tk.BooleanVar(value=self.base_config.runtime.mock)
        self.var_archive = tk.BooleanVar(value=self.base_config.archive.enabled)
        self.var_dataset = tk.BooleanVar(value=self.base_config.dataset.enabled)
        self.var_status = tk.StringVar(value="Idle.")

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(
            header, text="SAP IW29 export", font=("Segoe UI Semibold", 15)
        ).grid(row=0, column=0, sticky="w")
        ttk.Label(
            header,
            text="Runs the notification list and drops a workbook into the synced folder.",
            foreground="#5b6472",
        ).grid(row=1, column=0, sticky="w")
        ttk.Label(header, text=f"v{__version__}", foreground="#8b95a1").grid(
            row=0, column=1, sticky="e"
        )

        self._build_form().grid(row=1, column=0, sticky="ew")
        self._build_log().grid(row=2, column=0, sticky="nsew", pady=(12, 8))
        self._build_actions().grid(row=3, column=0, sticky="ew")

    def _build_form(self) -> ttk.Widget:
        frame = ttk.LabelFrame(self, text="Selection", padding=12)
        for column in (1, 3):
            frame.columnconfigure(column, weight=1)

        rows = (
            ("SAP system", self.var_system, "Variant", self.var_variant),
            ("Date from", self.var_date_from, "Date to", self.var_date_to),
            (
                f"Plant ({self.plant_field})",
                self.var_plant,
                f"Work centres ({self.work_center_field})",
                self.var_work_centers,
            ),
        )
        for index, (left_label, left_var, right_label, right_var) in enumerate(rows):
            ttk.Label(frame, text=left_label).grid(
                row=index, column=0, sticky="w", padx=(0, 8), pady=4
            )
            ttk.Entry(frame, textvariable=left_var).grid(
                row=index, column=1, sticky="ew", padx=(0, 16), pady=4
            )
            ttk.Label(frame, text=right_label).grid(
                row=index, column=2, sticky="w", padx=(0, 8), pady=4
            )
            ttk.Entry(frame, textvariable=right_var).grid(
                row=index, column=3, sticky="ew", pady=4
            )

        ttk.Label(frame, text="Save into").grid(row=3, column=0, sticky="w", pady=4)
        folder_row = ttk.Frame(frame)
        folder_row.grid(row=3, column=1, columnspan=3, sticky="ew", pady=4)
        folder_row.columnconfigure(0, weight=1)
        ttk.Entry(folder_row, textvariable=self.var_folder).grid(row=0, column=0, sticky="ew")
        ttk.Button(folder_row, text="Browse...", command=self._choose_folder, width=11).grid(
            row=0, column=1, padx=(8, 0)
        )

        toggles = ttk.Frame(frame)
        toggles.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        ttk.Checkbutton(
            toggles, text="Mock mode (no SAP)", variable=self.var_mock
        ).grid(row=0, column=0, padx=(0, 18))
        ttk.Checkbutton(toggles, text="Archive old reports", variable=self.var_archive).grid(
            row=0, column=1, padx=(0, 18)
        )
        ttk.Checkbutton(toggles, text="Refresh Power BI dataset", variable=self.var_dataset).grid(
            row=0, column=2
        )

        hint = "Leave the variant blank to use the fields above; a variant overrides them."
        ttk.Label(frame, text=hint, foreground="#8b95a1").grid(
            row=5, column=0, columnspan=4, sticky="w", pady=(8, 0)
        )
        return frame

    def _build_log(self) -> ttk.Widget:
        frame = ttk.LabelFrame(self, text="Activity", padding=(8, 6))
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)

        self.log_view = tk.Text(
            frame,
            height=13,
            wrap="word",
            font=("Consolas", 9),
            background="#fbfbfd",
            relief="flat",
            state="disabled",
        )
        self.log_view.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.log_view.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_view.configure(yscrollcommand=scrollbar.set)
        for level, colour in _LEVEL_COLOURS.items():
            self.log_view.tag_configure(level, foreground=colour)
        return frame

    def _build_actions(self) -> ttk.Widget:
        frame = ttk.Frame(self)
        frame.columnconfigure(1, weight=1)

        self.progress = ttk.Progressbar(frame, mode="indeterminate", length=180)
        self.progress.grid(row=0, column=0, sticky="w")
        ttk.Label(frame, textvariable=self.var_status, foreground="#3f4a5a").grid(
            row=0, column=1, sticky="w", padx=12
        )

        buttons = ttk.Frame(frame)
        buttons.grid(row=0, column=2, sticky="e")
        self.button_check = ttk.Button(buttons, text="Check setup", command=self._on_check)
        self.button_check.grid(row=0, column=0, padx=(0, 8))
        self.button_open = ttk.Button(
            buttons, text="Open folder", command=self._open_folder, state="disabled"
        )
        self.button_open.grid(row=0, column=1, padx=(0, 8))
        self.button_run = ttk.Button(
            buttons, text="Run export", command=self._on_run, style="Accent.TButton"
        )
        self.button_run.grid(row=0, column=2)
        return frame

    # ------------------------------------------------------------------ actions

    def _choose_folder(self) -> None:
        chosen = filedialog.askdirectory(
            title="Choose the OneDrive-synced folder", initialdir=self.var_folder.get() or None
        )
        if chosen:
            self.var_folder.set(str(Path(chosen)))

    def _on_check(self) -> None:
        try:
            config = self._config_from_form()
        except Iw29Error as exc:
            messagebox.showerror("Configuration problem", str(exc))
            return
        self._append("INFO", "Pre-flight checks")
        for check in doctor.run(config):
            level = {doctor.OK: "INFO", doctor.WARN: "WARNING", doctor.FAIL: "ERROR"}[
                check.status
            ]
            self._append(level, f"  {check}")
        self.var_status.set("Checks finished.")

    def _on_run(self) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        try:
            config = self._config_from_form()
        except Iw29Error as exc:
            messagebox.showerror("Configuration problem", str(exc))
            return

        self._set_busy(True)
        self.var_status.set("Running...")
        self._append("INFO", "-" * 60)
        self.log_handler = logging_setup.add_callback_handler(
            lambda level, text: self.messages.put((level, text)),
            config.runtime.log_level,
        )

        def work() -> None:
            self.results.put(pipeline.run_safely(config))

        self.worker = threading.Thread(target=work, name="iw29-run", daemon=True)
        self.worker.start()

    def _finish(self, result: Optional[pipeline.RunResult], error: Optional[Exception]) -> None:
        logging_setup.remove_handler(self.log_handler)
        self.log_handler = None
        self._set_busy(False)
        if result is not None:
            self.last_output = result.workbook
            self.button_open.configure(state="normal")
            self.var_status.set(f"Done: {result.row_count:,} rows in {result.duration_s:.1f}s.")
            for warning in result.warnings:
                self._append("WARNING", warning)
        elif isinstance(error, EmptyResultError):
            self.var_status.set("No rows matched the selection.")
            self._append("WARNING", str(error))
            messagebox.showinfo("Nothing to export", str(error))
        else:
            self.var_status.set("Failed. See the activity log.")
            self._append("ERROR", str(error))
            messagebox.showerror("Export failed", str(error))

    def _open_folder(self) -> None:
        target = self.last_output.parent if self.last_output else Path(self.var_folder.get())
        if not target.exists():
            messagebox.showwarning("Not there yet", f"{target} does not exist.")
            return
        if self.last_output and self.last_output.exists():
            subprocess.Popen(["explorer", "/select,", str(self.last_output)])
        else:
            os.startfile(str(target))  # noqa: S606 - Windows only, path is ours

    def _set_busy(self, busy: bool) -> None:
        state = "disabled" if busy else "normal"
        self.button_run.configure(state=state)
        self.button_check.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ------------------------------------------------------------------- plumbing

    def _config_from_form(self) -> Config:
        overrides = {
            "sap.system": self.var_system.get().strip(),
            "export.folder": Path(self.var_folder.get().strip() or "."),
            "runtime.mock": bool(self.var_mock.get()),
            "archive.enabled": bool(self.var_archive.get()),
            "dataset.enabled": bool(self.var_dataset.get()),
        }
        config = self.base_config.with_overrides(**overrides)

        selection = config.selection
        keep = {self.plant_field, self.work_center_field}
        filters: List[Filter] = [f for f in selection.filters if f.field_name not in keep]
        for field_name, raw in (
            (self.plant_field, self.var_plant.get()),
            (self.work_center_field, self.var_work_centers.get()),
        ):
            values = _split(raw)
            if values:
                filters.append(Filter(field_name=field_name, values=values))

        config = replace(
            config,
            selection=replace(
                selection,
                variant=self.var_variant.get().strip(),
                filters=filters,
                date_from=_iso(self.var_date_from.get()),
                date_to=_iso(self.var_date_to.get()),
            ),
        )
        config.validate()
        return config

    def _drain_messages(self) -> None:
        """Everything from the worker thread reaches Tk through these queues."""
        try:
            while True:
                level, text = self.messages.get_nowait()
                self._append(level, text)
        except queue.Empty:
            pass
        try:
            result, error = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            self._finish(result, error)
        self.after(120, self._drain_messages)

    def _append(self, level: str, text: str) -> None:
        self.log_view.configure(state="normal")
        self.log_view.insert("end", text + "\n", level if level in _LEVEL_COLOURS else "INFO")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")


def launch(config: Config) -> int:
    # Idempotent, so the app works whether or not the CLI already set this up.
    logging_setup.configure(config.log_folder, config.runtime.log_level, console=False)
    _enable_dpi_awareness()
    root = tk.Tk()
    root.title("SAP IW29 export")
    root.minsize(880, 640)
    _apply_theme(root)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)

    app = App(root, config)
    app.grid(row=0, column=0, sticky="nsew")

    def on_close() -> None:
        if app.worker is not None and app.worker.is_alive():
            if not messagebox.askokcancel(
                "Still running", "An export is in progress. Close anyway?"
            ):
                return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()
    return 0


def _enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll

        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


def _apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    for candidate in ("vista", "winnative", "clam"):
        if candidate in style.theme_names():
            style.theme_use(candidate)
            break
    style.configure("TLabelframe.Label", font=("Segoe UI Semibold", 9))
    style.configure("Accent.TButton", font=("Segoe UI Semibold", 9))


def _pick_field(config: Config, candidates: Tuple[str, ...], default: str) -> str:
    for item in config.selection.filters:
        if item.field_name in candidates:
            return item.field_name
    return default


def _joined(config: Config, candidates: Tuple[str, ...]) -> str:
    for item in config.selection.filters:
        if item.field_name in candidates:
            return ", ".join(item.values)
    return ""


def _split(raw: str) -> List[str]:
    parts = [chunk.strip() for chunk in raw.replace(";", ",").replace("\n", ",").split(",")]
    return [part for part in parts if part]


def _iso(value: str) -> str:
    """The form shows dd.mm.yyyy; the config layer parses either form."""
    return value.strip()
