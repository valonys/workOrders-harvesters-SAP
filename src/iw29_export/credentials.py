"""Password handling. Nothing is ever written to the config file or the log."""

from __future__ import annotations

import getpass
from typing import Callable, Optional

from .config import Config
from .errors import ConfigError
from .logging_setup import get_logger

log = get_logger("credentials")

PromptFn = Callable[[str], str]


def _keyring():
    try:
        import keyring  # noqa: PLC0415 - optional dependency
    except ImportError as exc:
        raise ConfigError(
            "sap.auth='credential_manager' needs the 'keyring' package. Run "
            "pip install -r requirements.txt."
        ) from exc
    return keyring


def resolve(config: Config, prompt: Optional[PromptFn] = None) -> str:
    """Return the password to use, or "" when single sign-on handles logon."""
    auth = config.sap.auth
    if auth == "sso":
        return ""

    user = config.sap.user.strip()
    service = config.sap.credential_service

    if auth == "credential_manager":
        secret = _keyring().get_password(service, user)
        if not secret:
            raise ConfigError(
                f"No password stored for '{user}' under '{service}'. Run: "
                "iw29-export store-password"
            )
        log.info("Password loaded from Windows Credential Manager for %s.", user)
        return secret

    asker = prompt or (lambda label: getpass.getpass(label))
    secret = asker(f"SAP password for {user} on {config.sap.system}: ")
    if not secret:
        raise ConfigError("No password was entered.")
    return secret


def store(config: Config, password: str) -> None:
    user = config.sap.user.strip()
    if not user:
        raise ConfigError("Set sap.user in the config before storing a password.")
    if not password:
        raise ConfigError("Refusing to store an empty password.")
    _keyring().set_password(config.sap.credential_service, user, password)
    log.info(
        "Password stored in Windows Credential Manager under '%s' for %s.",
        config.sap.credential_service,
        user,
    )


def forget(config: Config) -> bool:
    keyring = _keyring()
    user = config.sap.user.strip()
    try:
        keyring.delete_password(config.sap.credential_service, user)
    except Exception:
        return False
    log.info("Stored password for %s removed.", user)
    return True
