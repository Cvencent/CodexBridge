"""Keep the Windows Apps & features entry in step with the running build.

Inno Setup only writes the uninstall key values at the very end of its
install step. An upgrade that gets cut short before that write leaves the new
files on disk while Windows keeps showing the previous version, so the app
repairs the record itself whenever a packaged build starts.
"""

from __future__ import annotations

import datetime

from codexbridge import VERSION

# Inno Setup writes the entry under this AppId. A non-administrative install
# appends the "_is1" suffix to the key name, a per-machine install does not.
APP_ID = "{F7B2F7D4-6B1E-4A6C-9C6C-5A6A2D4E9C41}"
UNINSTALL_ROOT = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"
KEY_SUFFIXES = (APP_ID + "_is1", APP_ID)


def _winreg():
    import winreg

    return winreg


def _version_numbers(version: str) -> tuple[int, int] | None:
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def _repair_key(module, hive, subkey, version, install_date, numbers) -> bool:
    path = f"{UNINSTALL_ROOT}\\{subkey}"
    key = None
    try:
        key = module.OpenKey(
            hive,
            path,
            0,
            module.KEY_QUERY_VALUE | module.KEY_SET_VALUE,
        )
    except OSError:
        return False
    try:
        try:
            display, _ = module.QueryValueEx(key, "DisplayVersion")
        except OSError:
            display = None
        if isinstance(display, str) and display == version:
            return False
        module.SetValueEx(key, "DisplayVersion", 0, module.REG_SZ, version)
        module.SetValueEx(key, "InstallDate", 0, module.REG_SZ, install_date)
        if numbers is not None:
            module.SetValueEx(key, "VersionMajor", 0, module.REG_DWORD, numbers[0])
            module.SetValueEx(key, "VersionMinor", 0, module.REG_DWORD, numbers[1])
        return True
    except OSError:
        return False
    finally:
        try:
            module.CloseKey(key)
        except OSError:
            pass


def sync_uninstall_record(
    version: str | None = None,
    winreg=None,
    today: str | None = None,
) -> list[str]:
    """Rewrite stale uninstall values and return the keys that were repaired."""

    module = _winreg() if winreg is None else winreg
    wanted = VERSION if version is None else version
    install_date = today or datetime.date.today().strftime("%Y%m%d")
    numbers = _version_numbers(wanted)

    repaired: list[str] = []
    for hive_name, hive in (
        ("HKCU", module.HKEY_CURRENT_USER),
        ("HKLM", module.HKEY_LOCAL_MACHINE),
    ):
        for subkey in KEY_SUFFIXES:
            if _repair_key(module, hive, subkey, wanted, install_date, numbers):
                repaired.append(f"{hive_name}\\{UNINSTALL_ROOT}\\{subkey}")
    return repaired