"""Resolve configured environment credentials without logging or persisting them."""
import os
import re
import sys


def windows_user_environment(name):
    # A desktop launcher may retain the environment from before the key was
    # saved. Read only this configured variable from the current user's store.
    if sys.platform != "win32":
        return ""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as registry:
            value, kind = winreg.QueryValueEx(registry, name)
        if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
            return value.strip()
    except (ImportError, OSError):
        pass
    return ""


def api_key(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        return ""
    # An explicit process value takes precedence over the persisted user value.
    return os.environ.get(name, "").strip() or windows_user_environment(name)
