from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "CodexBridge"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "CodexBridge"


def _winreg():
    import winreg

    return winreg


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{(Path(sys.executable)).resolve()}"'
    return f'"{Path(sys.executable).resolve()}" -m codexbridge'


def is_startup_enabled(winreg=None) -> bool:
    module = _winreg() if winreg is None else winreg
    key = None
    try:
        key = module.OpenKey(
            module.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            module.KEY_QUERY_VALUE,
        )
        module.QueryValueEx(key, RUN_VALUE_NAME)
        return True
    except (FileNotFoundError, OSError):
        return False
    finally:
        if key is not None:
            try:
                module.CloseKey(key)
            except Exception:
                pass


def set_startup_enabled(enabled: bool, winreg=None) -> bool:
    module = _winreg() if winreg is None else winreg
    key = None
    try:
        if enabled:
            key = module.CreateKeyEx(
                module.HKEY_CURRENT_USER,
                RUN_KEY_PATH,
                0,
                module.KEY_SET_VALUE,
            )
            module.SetValueEx(
                key,
                RUN_VALUE_NAME,
                0,
                module.REG_SZ,
                startup_command(),
            )
            return True
        key = module.OpenKey(
            module.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            module.KEY_SET_VALUE,
        )
        module.DeleteValue(key, RUN_VALUE_NAME)
        return False
    except FileNotFoundError:
        return False
    finally:
        if key is not None:
            try:
                module.CloseKey(key)
            except Exception:
                pass
