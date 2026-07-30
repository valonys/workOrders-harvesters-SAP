"""A defensive wrapper around SAP GUI Scripting.

The original script's weak points were: a fixed ``sleep`` after launching
SAP Logon, blind ``sleep`` calls between round trips, no status-bar checking,
and no popup handling. Everything here exists to remove one of those.
"""

from __future__ import annotations

import os
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence

from .errors import SapError, SapMessageError, SapTimeoutError, ScriptingDisabledError
from .logging_setup import get_logger

log = get_logger("sap")

WND0 = "wnd[0]"
STATUS_BAR = "wnd[0]/sbar"
OK_CODE = "wnd[0]/tbar[0]/okcd"

VKEY_ENTER = 0
VKEY_F3_BACK = 3
VKEY_EXECUTE = 8
VKEY_F12_CANCEL = 12

# Popup titles/buttons we can safely answer without a human. Anything else is
# surfaced as an error rather than guessed at.
_POPUP_CONFIRM_BUTTONS = (
    "wnd[1]/tbar[0]/btn[0]",
    "wnd[1]/usr/btnSPOP-OPTION1",
    "wnd[1]/usr/btnBUTTON_1",
)


@dataclass
class StatusMessage:
    message_type: str
    text: str
    number: str = ""

    @property
    def is_error(self) -> bool:
        return self.message_type.upper() in {"E", "A", "X"}

    def __str__(self) -> str:
        label = f"{self.message_type}{self.number}" if self.number else self.message_type
        return f"[{label}] {self.text}" if self.text else f"[{label}]"


class SapSession:
    """Thin, typed facade over a ``GuiSession`` COM object."""

    def __init__(self, com_session: Any, step_timeout_s: int = 300):
        self._session = com_session
        self.step_timeout_s = step_timeout_s

    @property
    def com(self) -> Any:
        return self._session

    @property
    def system(self) -> str:
        return self._info_attr("SystemName")

    @property
    def client(self) -> str:
        return self._info_attr("Client")

    @property
    def user(self) -> str:
        return self._info_attr("User")

    @property
    def transaction(self) -> str:
        return self._info_attr("Transaction")

    def _info_attr(self, name: str) -> str:
        try:
            return str(getattr(self._session.Info, name) or "").strip()
        except Exception:
            return ""

    def find(self, element_id: str) -> Any:
        try:
            return self._session.findById(element_id)
        except Exception as exc:
            raise SapError(
                f"SAP GUI element not found: {element_id}. Record the transaction "
                "with Alt+F12 in your own system and put the recorded id in the "
                "config under [[selection.raw]]."
            ) from exc

    def optional(self, element_id: str) -> Optional[Any]:
        try:
            return self._session.findById(element_id, False)
        except TypeError:
            try:
                return self._session.findById(element_id)
            except Exception:
                return None
        except Exception:
            return None

    def exists(self, element_id: str) -> bool:
        return self.optional(element_id) is not None

    def set_text(self, element_id: str, value: str) -> None:
        self.find(element_id).text = value

    def set_checked(self, element_id: str, value: bool) -> None:
        self.find(element_id).selected = bool(value)

    def press(self, element_id: str) -> None:
        self.find(element_id).press()
        self.wait_ready()

    def select(self, element_id: str) -> None:
        self.find(element_id).select()
        self.wait_ready()

    def send_vkey(self, key: int, window: str = WND0) -> None:
        self.find(window).sendVKey(key)
        self.wait_ready()

    def start_transaction(self, code: str) -> None:
        """``/n`` prefix ends whatever is running, so this is safe mid-session."""
        self.set_text(OK_CODE, f"/n{code.strip().upper()}")
        self.send_vkey(VKEY_ENTER)
        self.raise_on_error()
        log.debug("Transaction after start: %s", self.transaction or "<unknown>")

    def maximise(self) -> None:
        try:
            self.find(WND0).maximize()
        except Exception:
            log.debug("Could not maximise the SAP window; continuing.")

    def wait_ready(self, timeout_s: Optional[int] = None) -> None:
        """Block until SAP has finished the round trip, instead of sleeping blindly."""
        deadline = time.monotonic() + (timeout_s or self.step_timeout_s)
        poll = 0.05
        while time.monotonic() < deadline:
            try:
                busy = bool(self._session.Busy)
            except Exception:
                busy = False
            if not busy:
                return
            time.sleep(poll)
            poll = min(poll * 1.5, 0.5)
        raise SapTimeoutError(
            f"SAP was still busy after {timeout_s or self.step_timeout_s}s."
        )

    def status(self) -> StatusMessage:
        bar = self.optional(STATUS_BAR)
        if bar is None:
            return StatusMessage("", "")
        try:
            return StatusMessage(
                message_type=str(getattr(bar, "messageType", "") or ""),
                text=str(getattr(bar, "text", "") or "").strip(),
                number=str(getattr(bar, "messageNumber", "") or ""),
            )
        except Exception:
            return StatusMessage("", "")

    def raise_on_error(self) -> StatusMessage:
        message = self.status()
        if message.is_error:
            raise SapMessageError(message.message_type, message.text, message.number)
        if message.text:
            log.info("SAP status %s", message)
        return message

    def popup_text(self) -> str:
        window = self.optional("wnd[1]")
        if window is None:
            return ""
        parts: List[str] = []
        for attr in ("text", "popupType"):
            try:
                value = str(getattr(window, attr, "") or "").strip()
            except Exception:
                value = ""
            if value:
                parts.append(value)
        for element_id in (
            "wnd[1]/usr/txtMESSTXT1",
            "wnd[1]/usr/txtSPOP-TEXTLINE1",
            "wnd[1]/usr/txtSPOP-TEXTLINE2",
        ):
            element = self.optional(element_id)
            if element is not None:
                try:
                    text = str(getattr(element, "text", "") or "").strip()
                except Exception:
                    text = ""
                if text:
                    parts.append(text)
        return " | ".join(dict.fromkeys(parts))

    def confirm_popup(self) -> bool:
        """Answer a modal dialog affirmatively. Returns False if none was open."""
        if not self.exists("wnd[1]"):
            return False
        text = self.popup_text()
        for button in _POPUP_CONFIRM_BUTTONS:
            if self.exists(button):
                log.info("Confirming SAP popup: %s", text or "<no text>")
                self.press(button)
                return True
        log.warning("Popup with no known confirm button: %s", text or "<no text>")
        self.send_vkey(VKEY_ENTER, "wnd[1]")
        return True

    def dismiss_popups(self, limit: int = 5) -> List[str]:
        seen: List[str] = []
        for _ in range(limit):
            if not self.exists("wnd[1]"):
                break
            seen.append(self.popup_text())
            if not self.confirm_popup():
                break
        if self.exists("wnd[1]"):
            raise SapError(
                "A SAP dialog stayed open and could not be answered automatically: "
                f"{self.popup_text() or '<no text>'}"
            )
        return seen


class SapGui:
    """Attaches to (or starts) SAP Logon and hands out a ready session."""

    def __init__(
        self,
        system: str,
        client: str = "",
        user: str = "",
        password: str = "",
        language: str = "EN",
        logon_path: str = "",
        attach_timeout_s: int = 90,
        step_timeout_s: int = 300,
        close_connection: bool = True,
        reuse_existing_connection: bool = True,
    ):
        self.system = system
        self.client = client
        self.user = user
        self._password = password
        self.language = language
        self.logon_path = logon_path
        self.attach_timeout_s = attach_timeout_s
        self.step_timeout_s = step_timeout_s
        self.close_connection = close_connection
        self.reuse_existing_connection = reuse_existing_connection

        self._application: Any = None
        self._connection: Any = None
        self._owns_connection = False
        self._launched_logon = False

    @contextmanager
    def session(self) -> Iterator[SapSession]:
        sap_session = self.connect()
        try:
            yield sap_session
        finally:
            self.disconnect()

    def connect(self) -> SapSession:
        self._application = self._attach_scripting_engine()
        self._connection = self._acquire_connection()

        if bool(getattr(self._connection, "DisabledByServer", False)):
            raise ScriptingDisabledError(
                "The SAP application server refuses scripting. Ask your Basis team "
                "to set profile parameter sapgui/user_scripting = TRUE on system "
                f"{self.system}."
            )

        com_session = self._connection.Children(0)
        sap_session = SapSession(com_session, self.step_timeout_s)
        sap_session.wait_ready(self.attach_timeout_s)

        self._log_on_if_needed(sap_session)
        sap_session.maximise()
        log.info(
            "Connected to %s client %s as %s",
            sap_session.system or self.system,
            sap_session.client or self.client or "?",
            sap_session.user or "<sso>",
        )
        return sap_session

    def disconnect(self) -> None:
        if self._connection is not None and self.close_connection and self._owns_connection:
            try:
                self._connection.closeConnection()
                log.info("Closed the SAP connection this run opened.")
            except Exception as exc:
                log.warning("Could not close the SAP connection cleanly: %s", exc)
        self._connection = None
        self._application = None

    def _attach_scripting_engine(self) -> Any:
        engine = self._try_get_engine()
        if engine is not None:
            return engine

        self._start_saplogon()

        deadline = time.monotonic() + self.attach_timeout_s
        while time.monotonic() < deadline:
            engine = self._try_get_engine()
            if engine is not None:
                log.info("SAP GUI scripting engine is available.")
                return engine
            time.sleep(0.5)

        raise SapTimeoutError(
            f"SAP GUI did not expose a scripting engine within {self.attach_timeout_s}s. "
            "Check SAP Logon > Options > Accessibility & Scripting > Scripting and "
            "enable 'Enable scripting'."
        )

    def _try_get_engine(self) -> Optional[Any]:
        try:
            import win32com.client  # noqa: PLC0415 - optional, Windows only
        except ImportError as exc:
            raise SapError(
                "pywin32 is required to talk to SAP GUI. Run "
                "pip install -r requirements.txt, or use --mock to test without SAP."
            ) from exc

        try:
            sap_gui_auto = win32com.client.GetObject("SAPGUI")
        except Exception:
            return None

        try:
            engine = sap_gui_auto.GetScriptingEngine
        except Exception as exc:
            raise ScriptingDisabledError(
                "SAP GUI is running but refuses to hand out its scripting engine. "
                "Enable scripting in SAP Logon > Options > Accessibility & Scripting."
            ) from exc

        if engine is None:
            return None
        return engine

    def _start_saplogon(self) -> None:
        if self._launched_logon:
            return
        path = Path(os.path.expandvars(self.logon_path or ""))
        if not path.is_file():
            raise SapError(
                f"SAP Logon was not running and saplogon.exe was not found at {path}. "
                "Fix sap.logon_path in the config, or start SAP Logon yourself."
            )
        log.info("Starting SAP Logon: %s", path)
        subprocess.Popen([str(path)], close_fds=True)
        self._launched_logon = True

    def _acquire_connection(self) -> Any:
        if self.reuse_existing_connection:
            existing = self._find_existing_connection()
            if existing is not None:
                log.info("Reusing the SAP connection already open for %s.", self.system)
                self._owns_connection = False
                return existing

        log.info("Opening a new SAP connection to %s.", self.system)
        try:
            connection = self._application.OpenConnection(self.system, True)
        except Exception as exc:
            raise SapError(
                f"Could not open a connection to '{self.system}'. The name must match "
                "an entry in SAP Logon exactly (or be a valid connection string)."
            ) from exc
        self._owns_connection = True
        return connection

    def _find_existing_connection(self) -> Optional[Any]:
        wanted = self.system.strip().upper()
        if not wanted:
            return None
        for connection in _com_children(self._application):
            for child in _com_children(connection):
                try:
                    info = child.Info
                    system = str(info.SystemName or "").strip().upper()
                    client = str(info.Client or "").strip()
                except Exception:
                    continue
                if system != wanted:
                    continue
                if self.client and client and client != self.client.strip():
                    continue
                return connection
        return None

    def _log_on_if_needed(self, sap_session: SapSession) -> None:
        """Fill the logon screen only when SAP actually shows one."""
        if not sap_session.exists("wnd[0]/usr/txtRSYST-BNAME"):
            if self.client and sap_session.exists("wnd[0]/usr/txtRSYST-MANDT"):
                sap_session.set_text("wnd[0]/usr/txtRSYST-MANDT", self.client)
            return

        if not self.user or not self._password:
            raise SapError(
                f"System {self.system} is asking for credentials but none are "
                "configured. Set sap.auth to 'credential_manager' and store the "
                "password with: iw29-export store-password"
            )

        if self.client and sap_session.exists("wnd[0]/usr/txtRSYST-MANDT"):
            sap_session.set_text("wnd[0]/usr/txtRSYST-MANDT", self.client)
        sap_session.set_text("wnd[0]/usr/txtRSYST-BNAME", self.user)
        sap_session.find("wnd[0]/usr/pwdRSYST-BCODE").text = self._password
        if self.language and sap_session.exists("wnd[0]/usr/txtRSYST-LANGU"):
            sap_session.set_text("wnd[0]/usr/txtRSYST-LANGU", self.language)
        sap_session.send_vkey(VKEY_ENTER)
        self._password = ""

        message = sap_session.status()
        if message.is_error:
            raise SapMessageError(message.message_type, message.text, message.number)

        # "This user is already logged on" — keep the existing sessions and continue.
        if sap_session.exists("wnd[1]/usr/radMULTI_LOGON_OPT2"):
            log.warning("User already logged on elsewhere; continuing in a new session.")
            sap_session.set_checked("wnd[1]/usr/radMULTI_LOGON_OPT2", True)
            sap_session.send_vkey(VKEY_ENTER, "wnd[1]")
        sap_session.dismiss_popups()


def _com_children(parent: Any) -> Sequence[Any]:
    try:
        count = int(parent.Children.Count)
    except Exception:
        return []
    children = []
    for index in range(count):
        try:
            children.append(parent.Children(index))
        except Exception:
            continue
    return children


def set_clipboard_text(value: str) -> None:
    """Used to bulk-load multiple selection values, which beats typing row by row."""
    try:
        import win32clipboard  # noqa: PLC0415 - Windows only
        import win32con
    except ImportError as exc:
        raise SapError("pywin32 is required for clipboard-based selections.") from exc

    last_error: Optional[Exception] = None
    for _ in range(10):
        try:
            win32clipboard.OpenClipboard()
        except Exception as exc:  # another process holds the clipboard
            last_error = exc
            time.sleep(0.1)
            continue
        try:
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, value)
            return
        finally:
            win32clipboard.CloseClipboard()
    raise SapError(f"Could not take ownership of the Windows clipboard: {last_error}")
