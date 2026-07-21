import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


RUNTIME_LOCK_DIR = Path(
    os.environ.get("BIT_RUNTIME_LOCK_DIR")
    or (Path(__file__).resolve().parent / "runtime_locks")
)
DEFAULT_STALE_SECONDS = int(os.environ.get("BIT_RUNTIME_LOCK_STALE_SECONDS", "86400"))

_REGISTRY_GUARD = threading.Lock()
_HELD_BY_THREAD = {}


def _safe_lock_name(value):
    text = re.sub(r"[^0-9A-Za-z_.-]+", "_", str(value or "").strip())
    return text.strip("._") or "unnamed"


def _pid_is_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_pid_is_running(pid)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (ProcessLookupError, OSError, OverflowError):
        return False


def _windows_pid_is_running(pid):
    """Check a PID without using os.kill(), which is unreliable on Windows."""
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    still_active = 259
    error_access_denied = 5
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    get_exit_code_process = kernel32.GetExitCodeProcess
    get_exit_code_process.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_exit_code_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    handle = open_process(process_query_limited_information, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return False
        if error == error_access_denied:
            return True
        # An unknown query failure is not enough evidence to delete another
        # process's lock.
        return True

    try:
        exit_code = wintypes.DWORD()
        if not get_exit_code_process(handle, ctypes.byref(exit_code)):
            return True
        return exit_code.value == still_active
    finally:
        close_handle(handle)


class InterProcessLock:
    """基于原子目录创建的跨进程锁，兼容 macOS 和 Windows。"""

    def __init__(self, key, owner="", metadata=None, stale_seconds=DEFAULT_STALE_SECONDS):
        self.key = _safe_lock_name(key)
        self.owner = str(owner or "unknown")
        self.metadata = dict(metadata or {})
        self.stale_seconds = max(60, int(stale_seconds or DEFAULT_STALE_SECONDS))
        self.token = uuid.uuid4().hex
        self.lock_path = RUNTIME_LOCK_DIR / f"{self.key}.lockdir"
        self.acquired = False
        self.thread_id = None

    @property
    def owner_path(self):
        return self.lock_path / "owner.json"

    def _owner_payload(self):
        return {
            "key": self.key,
            "owner": self.owner,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "token": self.token,
            "acquired_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "metadata": self.metadata,
        }

    def read_owner(self):
        try:
            return json.loads(self.owner_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _is_stale(self):
        owner = self.read_owner()
        if owner and _pid_is_running(owner.get("pid")):
            return False
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except OSError:
            return False
        # 无有效进程信息的锁只保留一个短保护期，避免创建过程中被误删。
        return age >= (2 if owner else min(self.stale_seconds, 300))

    def _remove_stale(self):
        if not self.lock_path.exists() or not self._is_stale():
            return False
        try:
            shutil.rmtree(self.lock_path)
            return True
        except OSError:
            return False

    def acquire(self, timeout=0, poll_interval=0.2):
        if self.acquired:
            return True
        RUNTIME_LOCK_DIR.mkdir(parents=True, exist_ok=True)
        deadline = None if timeout is None else time.monotonic() + max(0, float(timeout))

        while True:
            try:
                self.lock_path.mkdir()
                self.thread_id = threading.get_ident()
                try:
                    self.owner_path.write_text(
                        json.dumps(self._owner_payload(), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                except Exception:
                    shutil.rmtree(self.lock_path, ignore_errors=True)
                    raise
                self.acquired = True
                with _REGISTRY_GUARD:
                    _HELD_BY_THREAD[(self.thread_id, self.key)] = self
                return True
            except FileExistsError:
                if self._remove_stale():
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    return False
                time.sleep(max(0.05, float(poll_interval)))

    def release(self):
        if not self.acquired:
            return
        try:
            owner = self.read_owner()
            if not owner or owner.get("token") == self.token:
                shutil.rmtree(self.lock_path, ignore_errors=True)
        finally:
            with _REGISTRY_GUARD:
                registry_key = (self.thread_id, self.key)
                if _HELD_BY_THREAD.get(registry_key) is self:
                    _HELD_BY_THREAD.pop(registry_key, None)
            self.acquired = False

    def __enter__(self):
        self.acquire(timeout=0)
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


def window_lock_key(window_id):
    return f"bit_window_{_safe_lock_name(window_id)}"


def create_window_lease(window_id, owner, shop_name="", task_type=""):
    return InterProcessLock(
        window_lock_key(window_id),
        owner=owner,
        metadata={
            "window_id": str(window_id or ""),
            "shop_name": str(shop_name or ""),
            "task_type": str(task_type or ""),
        },
    )


def current_thread_window_lease(window_id):
    key = window_lock_key(window_id)
    with _REGISTRY_GUARD:
        lease = _HELD_BY_THREAD.get((threading.get_ident(), key))
    return lease if lease and lease.acquired else None


def get_lock_owner(key):
    lock = InterProcessLock(key)
    if not lock.lock_path.exists():
        return {}
    if lock._remove_stale():
        return {}
    return lock.read_owner() or {
        "key": lock.key,
        "owner": "initializing",
        "metadata": {},
    }
