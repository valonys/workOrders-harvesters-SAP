"""Exception hierarchy shared by every layer of the app."""

from __future__ import annotations


class Iw29Error(Exception):
    """Base class for every error this app raises deliberately."""

    exit_code = 1


class ConfigError(Iw29Error):
    exit_code = 2


class SapError(Iw29Error):
    exit_code = 3


class ScriptingDisabledError(SapError):
    """SAP GUI scripting is off on the client or blocked by the server."""

    exit_code = 4


class SapTimeoutError(SapError):
    exit_code = 5


class SapMessageError(SapError):
    """The SAP status bar reported an error or abort message."""

    exit_code = 6

    def __init__(self, message_type: str, text: str, number: str = "") -> None:
        self.message_type = message_type
        self.text = text
        self.number = number
        label = f"{message_type}{number}" if number else message_type
        super().__init__(f"SAP message [{label}]: {text}")


class ExportError(Iw29Error):
    exit_code = 7


class EmptyResultError(Iw29Error):
    """The report ran fine but selected no data."""

    exit_code = 8


class LockError(Iw29Error):
    exit_code = 9
