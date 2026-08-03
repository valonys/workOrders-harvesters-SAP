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
VKEY_CHOOSE = 2
VKEY_F3_BACK = 3
VKEY_EXECUTE = 8
VKEY_F12_CANCEL = 12
VKEY_GET_VARIANT = 17

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

    def text(self, element_id: str) -> str:
        try:
            return str(self.find(element_id).text or "")
        except SapError:
            raise
        except Exception:
            return ""

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
        wanted = code.strip().upper()
        self.set_text(OK_CODE, f"/n{wanted}")
        self.send_vkey(VKEY_ENTER)
        self.raise_on_error()

        # Confirm it landed. Without this a swallowed ok-code leaves the run on
        # whatever screen was already open, where the same element ids mean
        # different things and the failure surfaces much later as nonsense.
        actual = (self.transaction or "").strip().upper()
        if actual != wanted:
            raise SapError(
                f"Asked SAP for transaction {wanted} but it is showing "
                f"{actual or '<none>'} ({self.window_title() or 'no title'}). "
                "Something on screen refused the command."
            )
        log.debug("Transaction after start: %s", actual)

    def window_title(self) -> str:
        try:
            return str(self.find(WND0).text or "").strip()
        except Exception:
            return ""

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

    def wait_for_window(self, window: str = "wnd[1]", timeout_s: float = 8.0) -> bool:
        """Wait for a modal window to appear.

        `Busy` can read False in the gap between a press returning and SAP
        actually putting the dialog up, so checking `exists` once right after an
        action is a race. Everything that opens a popup goes through here.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            self.wait_ready()
            if self.exists(window):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.15)

    def wait_for_element(self, element_id: str, timeout_s: float = 8.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            self.wait_ready()
            if self.exists(element_id):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.15)

    def menu_id(self, labels: Sequence[str]) -> Optional[str]:
        """Resolve a menu path by its visible labels, e.g. System > List > Save.

        Menu indexes shift between releases and even between screens of the same
        transaction, so nothing here is allowed to hardcode menu[3]/menu[5].
        """
        node: Optional[Any] = self.optional("wnd[0]/mbar")
        if node is None:
            return None
        for label in labels:
            node = _child_menu(node, label)
            if node is None:
                return None
        try:
            return relative_id(str(node.Id))
        except Exception:
            return None

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

    @classmethod
    def from_config(cls, config: Any, password: str = "") -> "SapGui":
        sap = config.sap
        return cls(
            system=sap.system,
            connection_name=sap.connection_name,
            client=sap.client,
            user=sap.user,
            password=password,
            language=sap.language,
            logon_path=sap.logon_path,
            attach_timeout_s=sap.attach_timeout_s,
            step_timeout_s=sap.step_timeout_s,
            close_connection=sap.close_connection,
            reuse_existing_connection=sap.reuse_existing_connection,
            own_session=sap.own_session,
        )

    def __init__(
        self,
        system: str,
        connection_name: str = "",
        client: str = "",
        user: str = "",
        password: str = "",
        language: str = "EN",
        logon_path: str = "",
        attach_timeout_s: int = 90,
        step_timeout_s: int = 300,
        close_connection: bool = True,
        reuse_existing_connection: bool = True,
        own_session: bool = True,
    ):
        self.own_session = own_session
        self.system = system
        self.connection_name = connection_name
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
        self._created_session_id = ""

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

        com_session = self._pick_session()
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

    def _pick_session(self) -> Any:
        """Work in our own SAP session rather than the one the user is using.

        Session 0 is whatever the person at the keyboard has on screen. Driving it
        means the run and the user type over each other, and the run inherits
        whatever screen was left open - which is how a scheduled run once pressed
        "Get Variant" on a result list, where the same button id means
        "Change <-> Display".
        """
        first = self._connection.Children(0)
        if self._owns_connection or not self.own_session:
            return first

        before = _child_count(self._connection)
        try:
            first.createSession()
        except Exception as exc:
            log.warning(
                "Could not open a separate SAP session (%s); using the one already "
                "on screen. Avoid touching SAP while this runs.",
                exc,
            )
            return first

        created = self._wait_for_new_session(before)
        if created is None:
            log.warning(
                "SAP did not open a new session (the six-session limit may be "
                "reached); using the one already on screen."
            )
            return first

        self._created_session_id = str(created.Id)
        log.info("Working in a separate SAP session, leaving yours untouched.")
        return created

    def _wait_for_new_session(self, before: int, timeout_s: float = 30.0) -> Any:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if _child_count(self._connection) > before:
                return self._connection.Children(_child_count(self._connection) - 1)
            time.sleep(0.25)
        return None

    def disconnect(self) -> None:
        if self._created_session_id and not self._owns_connection:
            try:
                self._connection.closeSession(self._created_session_id)
                log.info("Closed the SAP session this run opened.")
            except Exception as exc:
                log.warning("Could not close the session this run opened: %s", exc)
            self._created_session_id = ""

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

        target = self.connection_name or self.system
        log.info("Opening a new SAP connection using '%s'.", target)
        try:
            connection = self._application.OpenConnection(target, True)
        except Exception as exc:
            hint = (
                "sap.connection_name must match the SAP Logon entry exactly. Note "
                "that this is the description shown in SAP Logon, not the three "
                f"letter system id: '{self.system}' on its own will not work."
            )
            raise SapError(f"Could not open a connection using '{target}'. {hint}") from exc
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


def relative_id(element_id: str) -> str:
    """Strip the connection/session prefix so an id can be reused with findById."""
    marker = "/ses[0]/"
    if marker in element_id:
        return element_id.split(marker, 1)[1]
    index = element_id.find("wnd[")
    return element_id[index:] if index >= 0 else element_id


def _child_menu(node: Any, label: str) -> Optional[Any]:
    wanted = _normalise_label(label)
    for child in _com_children(node):
        try:
            text = _normalise_label(str(child.Text or ""))
        except Exception:
            continue
        if text == wanted:
            return child
    return None


def _normalise_label(text: str) -> str:
    return text.replace("&", "").replace(".", "").replace(" ", "").strip().lower()


def _child_count(parent: Any) -> int:
    try:
        return int(parent.Children.Count)
    except Exception:
        return 0


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
