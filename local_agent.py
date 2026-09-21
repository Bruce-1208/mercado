"""Stable outbound runner for Zeshun local BitBrowser automation.

This file deliberately has no imports from the changing ``bit`` business
package.  A packaged copy can therefore stay stable while jobs run against a
versioned source bundle downloaded from the public workbench.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import platform
import queue
import random
import runpy
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import requests


AGENT_VERSION = "1.2.3"
DEFAULT_SERVER_URL = "https://zeshun.cc.cd"
DEFAULT_POLL_SECONDS = 10.0
DEFAULT_HEARTBEAT_SECONDS = 10.0
LOG_FLUSH_SECONDS = 2.0
LOG_UPLOAD_MIN_INTERVAL_SECONDS = 5.0
RATE_LIMIT_MIN_SECONDS = 60.0
RETRY_MAX_SECONDS = 300.0
JOB_CONTROL_LOSS_STOP_SECONDS = 12 * 60.0
AGENT_LOG_MAX_BYTES = 5 * 1024 * 1024
AGENT_LOG_BACKUP_COUNT = 3
STATUS_WINDOW_PLATFORMS = frozenset(("win32", "darwin"))


def terminate_worker_tree(pid):
    """Stop only the isolated worker and its descendants, including pipe owners."""
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW, timeout=5,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    else:
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


class WorkerProcessGuard:
    """Tie a worker tree to the Agent lifetime on Windows.

    Windows normally leaves child processes alive when their parent exits.  A
    kill-on-close Job Object makes an Agent crash or real window exit close the
    complete business-process tree as well.
    """

    def __init__(self, process):
        self.process = process
        self.handle = None
        if os.name == "nt":
            self.handle = self._assign_windows_job(process)

    @staticmethod
    def _assign_windows_job(process):
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = 0x00002000
            configured = kernel32.SetInformationJobObject(
                handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
            )
            assigned = configured and kernel32.AssignProcessToJobObject(
                handle, wintypes.HANDLE(int(process._handle))
            )
            if not assigned:
                kernel32.CloseHandle(handle)
                return None
            return handle
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def terminate(self):
        if self.process.poll() is None:
            terminate_worker_tree(self.process.pid)

    def close(self):
        if self.handle is None:
            return
        try:
            import ctypes

            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(self.handle)
        finally:
            self.handle = None


class AgentRuntimeLog:
    """Timestamp Agent control-plane messages and keep a small local history."""

    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._listeners = []

    def add_listener(self, listener):
        """Receive complete rendered log records without coupling logging to a UI."""
        with self._lock:
            self._listeners.append(listener)

    @staticmethod
    def _render(message):
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        lines = str(message or "").rstrip("\r\n").splitlines() or [""]
        return "\n".join(f"[{timestamp}] {line}" for line in lines)

    def _rotate(self, incoming_bytes):
        try:
            current_size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= AGENT_LOG_MAX_BYTES:
            return
        oldest = self.path.with_name(f"{self.path.name}.{AGENT_LOG_BACKUP_COUNT}")
        oldest.unlink(missing_ok=True)
        for index in range(AGENT_LOG_BACKUP_COUNT - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            if source.exists():
                os.replace(source, self.path.with_name(f"{self.path.name}.{index + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))

    def write(self, message):
        rendered = self._render(message)
        encoded = (rendered + "\n").encode("utf-8")
        with self._lock:
            # PyInstaller's windowed bootloader intentionally leaves stdout
            # unset.  The status window and agent.log remain the authoritative
            # outputs in that mode.
            if sys.stdout is not None:
                print(rendered, flush=True)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._rotate(len(encoded))
                with self.path.open("ab") as stream:
                    stream.write(encoded)
                    stream.flush()
            except OSError:
                # A read-only/full disk must not stop task polling; the timestamped
                # console copy remains available to the operator.
                pass
            listeners = tuple(self._listeners)
        for listener in listeners:
            try:
                listener(rendered)
            except Exception:
                # A status display must never be able to stop Agent logging.
                pass


def _tail_text(path, max_bytes=256 * 1024):
    """Read a bounded UTF-8 tail for the status window's initial history."""
    path = Path(path)
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - int(max_bytes)), os.SEEK_SET)
            content = stream.read()
    except OSError:
        return ""
    text = content.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    return text.rstrip("\r\n")


def _hide_windows_console():
    """Hide the packaging console after the graphical status window is ready."""
    if os.name != "nt":
        return
    try:
        import ctypes

        window = ctypes.windll.kernel32.GetConsoleWindow()
        if window:
            ctypes.windll.user32.ShowWindow(window, 0)
    except (AttributeError, OSError):
        pass


def _attach_worker_output_streams():
    """Restore redirected output for a worker launched by a windowed EXE.

    A PyInstaller ``console=False`` process has no normal Python stdout/stderr.
    Worker children are launched with OS pipes, however, so reopening file
    descriptors 1 and 2 keeps the existing live job-log capture working.
    """
    for name, descriptor in (("stdout", 1), ("stderr", 2)):
        if getattr(sys, name, None) is not None:
            continue
        try:
            stream = open(
                descriptor,
                "w",
                encoding="utf-8",
                errors="replace",
                buffering=1,
                closefd=False,
            )
        except OSError:
            continue
        setattr(sys, name, stream)


class AgentStatusWindow:
    """Small desktop dashboard showing wall-clock time and live Agent logs."""

    def __init__(self, agent):
        import tkinter as tk
        from tkinter import messagebox
        from tkinter.scrolledtext import ScrolledText

        self.agent = agent
        self.tk = tk
        self.messagebox = messagebox
        self.root = tk.Tk()
        self.root.title(f"泽顺 Mercado Local Agent {AGENT_VERSION}")
        self.root.geometry("960x620")
        self.root.minsize(720, 420)
        self.root.configure(background="#f4f6f8")
        self.log_queue = queue.Queue()
        self.closing = False
        self.agent_thread = None
        self.stopping_job_id = ""
        self.status_text = tk.StringVar(value="正在连接服务端…")
        self.clock_text = tk.StringVar(value="")

        header = tk.Frame(self.root, background="#17324d", padx=18, pady=14)
        header.pack(fill="x")
        tk.Label(
            header,
            text="Mercado Local Agent",
            font=("Microsoft YaHei UI", 16, "bold"),
            foreground="white",
            background="#17324d",
        ).pack(side="left")
        tk.Label(
            header,
            textvariable=self.clock_text,
            font=("Consolas", 15, "bold"),
            foreground="#d9ecff",
            background="#17324d",
        ).pack(side="right")

        details = tk.Frame(self.root, background="#f4f6f8", padx=18, pady=10)
        details.pack(fill="x")
        tk.Label(
            details,
            textvariable=self.status_text,
            font=("Microsoft YaHei UI", 10, "bold"),
            foreground="#157347",
            background="#f4f6f8",
        ).pack(side="left")
        tk.Label(
            details,
            text=f"Agent：{agent.config.name}    版本：{AGENT_VERSION}",
            font=("Microsoft YaHei UI", 9),
            foreground="#52606d",
            background="#f4f6f8",
        ).pack(side="right")

        body = tk.Frame(self.root, background="#f4f6f8", padx=18)
        body.pack(fill="both", expand=True)
        self.log_view = ScrolledText(
            body,
            wrap="word",
            state="disabled",
            font=("Consolas", 9),
            foreground="#d9e2ec",
            background="#102a43",
            insertbackground="white",
            padx=10,
            pady=10,
            relief="flat",
        )
        self.log_view.pack(fill="both", expand=True)

        footer = tk.Frame(self.root, background="#f4f6f8", padx=18, pady=12)
        footer.pack(fill="x")
        tk.Label(
            footer,
            text="结束任务后 Agent 会保持在线；关闭窗口会停止任务并退出 Agent。",
            font=("Microsoft YaHei UI", 9),
            foreground="#6b7280",
            background="#f4f6f8",
        ).pack(side="left")
        for label, command in (
            ("打开日志目录", self._open_log_directory),
            ("复制全部", self._copy_all),
            ("清空显示", self._clear_view),
        ):
            tk.Button(
                footer,
                text=label,
                command=command,
                font=("Microsoft YaHei UI", 9),
                padx=10,
                pady=4,
            ).pack(side="right", padx=(8, 0))
        self.stop_task_button = tk.Button(
            footer,
            text="暂无运行任务",
            command=self._on_stop_current_task,
            state="disabled",
            font=("Microsoft YaHei UI", 9, "bold"),
            foreground="#b42318",
            padx=10,
            pady=4,
        )
        self.stop_task_button.pack(side="right", padx=(8, 0))

        history = _tail_text(agent.runtime_log.path)
        if history:
            self._append_log(history)
        agent.runtime_log.add_listener(self.log_queue.put)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(0, self._tick_clock)
        self.root.after(100, self._drain_logs)

    def _append_log(self, content):
        self.log_view.configure(state="normal")
        self.log_view.insert("end", str(content).rstrip("\r\n") + "\n")
        # Bound the on-screen buffer; complete history remains in agent.log.
        if int(self.log_view.index("end-1c").split(".")[0]) > 6000:
            self.log_view.delete("1.0", "1001.0")
        self.log_view.see("end")
        self.log_view.configure(state="disabled")

    def _set_status_from_log(self, content):
        if "Agent 已停止" in content:
            self.status_text.set("Agent 已停止")
        elif "Agent 连接异常" in content:
            self.status_text.set("连接异常，正在自动重试")
        elif "收到任务" in content:
            self.status_text.set("任务执行中")
        elif "已结束" in content:
            self.status_text.set("在线，等待任务")
        elif "已启动" in content or "业务代码已更新" in content:
            self.status_text.set("在线，等待任务")

    def _drain_logs(self):
        for _index in range(200):
            try:
                content = self.log_queue.get_nowait()
            except queue.Empty:
                break
            self._append_log(content)
            self._set_status_from_log(content)
        self._refresh_task_button()
        self.root.after(100, self._drain_logs)

    def _refresh_task_button(self):
        job_id = self.agent.current_job_id()
        if not job_id:
            self.stopping_job_id = ""
            self.stop_task_button.configure(text="暂无运行任务", state="disabled")
        elif self.closing or self.stopping_job_id == job_id:
            self.stop_task_button.configure(text="正在结束任务", state="disabled")
        else:
            self.stop_task_button.configure(text="结束任务", state="normal")

    def _on_stop_current_task(self):
        job_id = self.agent.current_job_id()
        if not job_id:
            self._refresh_task_button()
            return
        if not self.messagebox.askyesno(
            "结束任务",
            "确定结束当前本地运行的任务吗？Agent 会保持在线并继续接收后续任务。",
            parent=self.root,
        ):
            return
        stopped_job_id = self.agent.request_stop_current_job("用户点击结束任务")
        if stopped_job_id:
            self.stopping_job_id = stopped_job_id
            self.status_text.set("正在结束当前任务…")
        self._refresh_task_button()

    def _tick_clock(self):
        self.clock_text.set(time.strftime("%Y-%m-%d  %H:%M:%S", time.localtime()))
        self.root.after(1000, self._tick_clock)

    def _open_log_directory(self):
        try:
            log_directory = str(self.agent.runtime_log.path.parent)
            if sys.platform == "darwin":
                subprocess.Popen(
                    ["open", log_directory],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.startfile(log_directory)
        except (AttributeError, OSError, subprocess.SubprocessError) as exc:
            self.messagebox.showerror("无法打开日志目录", str(exc), parent=self.root)

    def _copy_all(self):
        content = self.log_view.get("1.0", "end-1c")
        self.root.clipboard_clear()
        self.root.clipboard_append(content)

    def _clear_view(self):
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")

    def _on_close(self):
        if self.closing:
            return
        if not self.messagebox.askyesno(
            "退出 Agent",
            "确定退出 Agent 吗？正在执行的任务会被停止。",
            parent=self.root,
        ):
            return
        self.closing = True
        self.status_text.set("正在停止任务并退出…")
        self.agent.request_shutdown("用户关闭 Agent 窗口")
        self.root.after(100, self._wait_for_shutdown)

    def _wait_for_shutdown(self):
        if self.agent_thread is not None and self.agent_thread.is_alive():
            self.root.after(100, self._wait_for_shutdown)
            return
        self.root.destroy()

    def run(self):
        result = {"code": 1}

        def run_agent():
            try:
                result["code"] = self.agent.run()
            except BaseException as exc:
                self.agent.log(f"Agent 主进程异常退出：{exc}")
                result["code"] = 1
            finally:
                self.log_queue.put("[状态] Agent 已停止")

        self.agent_thread = threading.Thread(
            target=run_agent,
            name="agent-main",
            daemon=False,
        )
        self.agent_thread.start()
        _hide_windows_console()
        self.root.mainloop()
        return int(result["code"])


class AgentRateLimitError(RuntimeError):
    def __init__(self, message, retry_after):
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after_seconds(value):
    value = str(value or "").strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError, OSError):
            return 0.0
    return max(0.0, seconds) if math.isfinite(seconds) else 0.0


def _default_data_dir():
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "Zeshun" / "MercadoLocalAgent"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Zeshun" / "MercadoLocalAgent"
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / ".local-agent-data"
    return Path(__file__).resolve().parent / ".data" / "local-agent"


def _application_dir():
    return (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )


def _load_json(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"无法读取 Agent 配置 {path}：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Agent 配置 {path} 必须是 JSON 对象")
    return data


def _first_nonempty(*values):
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _validate_server_url(value, allow_http=False):
    value = str(value or "").strip().rstrip("/")
    parsed = urlsplit(value)
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("服务端地址格式无效")
    loopback = parsed.hostname in ("127.0.0.1", "::1", "localhost")
    if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http") and not (
        parsed.scheme == "http" and loopback
    ):
        raise ValueError("公网 Agent 服务端必须使用 HTTPS")
    return value


def _identity(data_dir, configured_id=""):
    identity_path = data_dir / "identity.json"
    current = _load_json(identity_path)
    agent_id = _first_nonempty(configured_id, current.get("agent_id"))
    if not agent_id:
        agent_id = "agent-" + uuid.uuid4().hex
    payload = dict(current)
    payload["agent_id"] = agent_id
    data_dir.mkdir(parents=True, exist_ok=True)
    temporary = identity_path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, identity_path)
    return payload


class AgentConfig:
    def __init__(self, args):
        config_path = Path(
            args.config
            or os.environ.get("BIT_LOCAL_AGENT_CONFIG")
            or (_application_dir() / "local-agent.json")
        )
        payload = _load_json(config_path)
        self.data_dir = Path(
            _first_nonempty(args.data_dir, payload.get("data_dir")) or _default_data_dir()
        ).expanduser().resolve()
        self.server_url = _validate_server_url(
            _first_nonempty(
                args.server,
                os.environ.get("BIT_LOCAL_AGENT_SERVER_URL"),
                payload.get("server_url"),
                DEFAULT_SERVER_URL,
            ),
            allow_http=args.allow_http,
        )
        identity = _identity(
            self.data_dir,
            _first_nonempty(args.agent_id, payload.get("agent_id")),
        )
        self.agent_id = str(identity["agent_id"])
        self.agent_token = _first_nonempty(
            args.token,
            os.environ.get("BIT_LOCAL_AGENT_TOKEN"),
            payload.get("agent_token"),
            identity.get("agent_token"),
            os.environ.get("BIT_DB_API_TOKEN"),
        )
        self.enrollment_token = _first_nonempty(
            payload.get("enrollment_token"),
            os.environ.get("BIT_LOCAL_AGENT_ENROLLMENT_TOKEN"),
        )
        self.db_api_token = _first_nonempty(
            args.db_api_token,
            os.environ.get("BIT_DB_API_TOKEN"),
            payload.get("db_api_token"),
            self.agent_token,
        )
        if not self.agent_token and not self.enrollment_token:
            raise RuntimeError(
                "缺少 Agent 注册凭证；请从泽顺控制台重新下载 Agent 安装包，"
                "或配置 BIT_LOCAL_AGENT_TOKEN"
            )
        self.name = _first_nonempty(
            args.name,
            os.environ.get("BIT_LOCAL_AGENT_NAME"),
            payload.get("name"),
            socket.gethostname(),
        )[:120]
        self.poll_seconds = max(
            10.0,
            float(_first_nonempty(payload.get("poll_seconds"), DEFAULT_POLL_SECONDS)),
        )
        self.heartbeat_seconds = max(
            3.0,
            float(
                _first_nonempty(
                    payload.get("heartbeat_seconds"), DEFAULT_HEARTBEAT_SECONDS
                )
            ),
        )
        self.once = bool(args.once)

    def save_agent_token(self, token):
        identity_path = self.data_dir / "identity.json"
        identity = _load_json(identity_path)
        identity.update({"agent_id": self.agent_id, "agent_token": str(token)})
        temporary = identity_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(identity, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(temporary, identity_path)
        previous_token = self.agent_token
        self.agent_token = str(token)
        if (
            not self.db_api_token
            or self.db_api_token == previous_token
            or self.db_api_token.startswith("agent:")
        ):
            self.db_api_token = self.agent_token


class AgentProcessLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, IOError):
            self.handle.close()
            self.handle = None
            return False

    def release(self):
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class LocalAgent:
    capabilities = ("appeal", "daily_task", "heartbeat_claim")

    def __init__(self, config):
        self.config = config
        self.runtime_log = AgentRuntimeLog(config.data_dir / "agent.log")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": f"MercadoLocalAgent/{AGENT_VERSION}"})
        if config.agent_token:
            self.session.headers["X-Local-Agent-Token"] = config.agent_token
        self.current_release = self._read_current_release()
        self._rate_limit_until = 0.0
        self._rate_limit_failures = 0
        self._rate_limit_message = ""
        self.session_id = uuid.uuid4().hex
        self.shutdown_event = threading.Event()
        self._worker_lock = threading.Lock()
        self._current_worker = None
        self._current_job_id = ""
        self._current_cancel_file = None
        self.queue_id = ""

    def log(self, message):
        self.runtime_log.write(message)

    def _observe_queue(self, payload):
        queue_id = str((payload or {}).get("queue_id") or "").strip()
        if not queue_id or queue_id == self.queue_id:
            return
        if self.queue_id:
            self.log(
                "警告：公网请求切换到了不同的 Agent 队列 "
                f"({self.queue_id[:12]} -> {queue_id[:12]})；请检查反向代理和服务端队列路径"
            )
        else:
            self.log(f"已连接 Agent 队列 {queue_id[:12]}")
        self.queue_id = queue_id

    def request_shutdown(self, reason=""):
        if self.shutdown_event.is_set():
            return
        self.log(f"Agent 收到退出请求：{reason or '正在退出'}")
        self.shutdown_event.set()
        self._stop_current_worker()

    def current_job_id(self):
        """Return the actively running local job without exposing worker internals."""
        with self._worker_lock:
            guard = self._current_worker
            if guard is None or guard.process.poll() is not None:
                return ""
            return self._current_job_id

    def request_stop_current_job(self, reason=""):
        """Request cancellation of the current job while keeping the Agent online."""
        with self._worker_lock:
            guard = self._current_worker
            job_id = self._current_job_id
            cancel_file = self._current_cancel_file
            if (
                guard is None
                or guard.process.poll() is not None
                or not job_id
                or cancel_file is None
            ):
                return ""
            try:
                cancel_file.write_text(reason or "local stop", encoding="utf-8")
            except OSError as exc:
                self.log(f"无法结束本机任务 {job_id}：{exc}")
                return ""
        self.log(f"正在结束本机任务 {job_id}：{reason or '本机用户请求'}")
        return job_id

    def _set_current_worker(self, job_id, guard, cancel_file=None):
        with self._worker_lock:
            self._current_job_id = str(job_id or "")
            self._current_worker = guard
            self._current_cancel_file = Path(cancel_file) if cancel_file else None

    def _stop_current_worker(self):
        with self._worker_lock:
            guard = self._current_worker
        if guard is not None:
            guard.terminate()

    def _clear_current_worker(self, guard):
        with self._worker_lock:
            if self._current_worker is guard:
                self._current_worker = None
                self._current_job_id = ""
                self._current_cancel_file = None
        guard.close()

    def ensure_enrolled(self):
        if self.config.agent_token:
            return
        data = self._request(
            "POST",
            "/api/local-agents/enroll",
            headers={"Authorization": f"Bearer {self.config.enrollment_token}"},
            json={
                "agent_id": self.config.agent_id,
                "name": self.config.name,
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "agent_version": AGENT_VERSION,
                "capabilities": list(self.capabilities),
                "session_id": self.session_id,
            },
            timeout=30,
        )
        token = str(data.get("agent_token") or "")
        if not token:
            raise RuntimeError("Agent 注册接口未返回长期凭证")
        self.config.save_agent_token(token)
        self.session.headers["X-Local-Agent-Token"] = token

    def _cooldown_remaining(self):
        return max(0.0, self._rate_limit_until - time.monotonic())

    def _check_cooldown(self):
        remaining = self._cooldown_remaining()
        if remaining:
            # Do not sleep here: the worker monitor must keep draining stdout
            # and handling local cancellation while server requests are paused.
            raise AgentRateLimitError(self._rate_limit_message, remaining)

    def _check_response(self, response):
        if response.status_code < 400:
            return
        try:
            payload = response.json()
            message = payload.get("message") if isinstance(payload, dict) else ""
        except (TypeError, ValueError):
            message = ""
        tunnel_limit = response.status_code in (502, 503) and (
            "connections exceed" in response.text.lower()
        )
        if response.status_code == 429 or tunnel_limit:
            self._rate_limit_failures += 1
            backoff = min(
                RETRY_MAX_SECONDS,
                RATE_LIMIT_MIN_SECONDS * 2 ** min(self._rate_limit_failures - 1, 3),
            )
            delay = max(backoff, _retry_after_seconds(response.headers.get("Retry-After")))
            delay += random.uniform(0.0, 5.0)
            self._rate_limit_until = time.monotonic() + delay
            reason = "公网隧道连接数超限" if tunnel_limit else "服务端或公网隧道请求限流"
            self._rate_limit_message = f"{reason}：HTTP {response.status_code}"
            if message:
                self._rate_limit_message += f"；{message}"
            raise AgentRateLimitError(self._rate_limit_message, delay)
        raise RuntimeError(message or f"服务端请求失败：HTTP {response.status_code}")

    def _request(self, method, path, **kwargs):
        self._check_cooldown()
        response = self.session.request(
            method,
            self.config.server_url + path,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
        self._check_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("服务端返回了无效 JSON") from exc
        if not isinstance(payload, dict) or payload.get("status") != "success":
            message = payload.get("message") if isinstance(payload, dict) else ""
            raise RuntimeError(str(message or "服务端请求失败"))
        return payload.get("data") or {}

    def _read_current_release(self):
        path = self.config.data_dir / "current-release.json"
        data = _load_json(path)
        version = str(data.get("version") or "").strip()
        release_dir = self.config.data_dir / "releases" / version
        return version if version and (release_dir / "local_agent_worker.py").is_file() else ""

    def _release_history_path(self):
        return self.config.data_dir / "release-history.json"

    def rollback_release(self, version=""):
        """Activate the last known-good local business release atomically."""
        requested = str(version or "").strip()
        current = self.current_release
        releases_dir = self.config.data_dir / "releases"
        candidates = []
        if requested:
            candidates.append(requested)
        try:
            history = _load_json(self._release_history_path())
        except Exception:
            history = {}
        for item in history.get("releases") or []:
            candidate = str((item or {}).get("version") or "").strip()
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        candidates.extend(
            path.name
            for path in sorted(
                releases_dir.glob("*"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            if path.is_dir() and path.name not in candidates
        )
        target = next(
            (
                candidate
                for candidate in candidates
                if candidate != current
                and (releases_dir / candidate / "local_agent_worker.py").is_file()
            ),
            "",
        )
        if not target:
            raise RuntimeError("没有可回退的本地业务版本")
        self._activate_release(target, reason="manual-rollback")
        self.log(f"本地业务版本已回退：{current or '无'} -> {target}")
        return target

    def heartbeat(self, current_job_id=""):
        data = self._request(
            "POST",
            "/api/local-agents/heartbeat",
            json={
                "agent_id": self.config.agent_id,
                "name": self.config.name,
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "agent_version": AGENT_VERSION,
                "business_version": self.current_release,
                "capabilities": list(self.capabilities),
                "session_id": self.session_id,
                "current_job_id": str(current_job_id or ""),
            },
            timeout=20,
        )
        refreshed_token = str(data.get("agent_token") or "")
        if refreshed_token and refreshed_token != self.config.agent_token:
            self.config.save_agent_token(refreshed_token)
            self.session.headers["X-Local-Agent-Token"] = refreshed_token
        self._observe_queue(data)
        return data

    def _safe_extract(self, archive_path, destination):
        destination = Path(destination).resolve()
        with zipfile.ZipFile(archive_path) as archive:
            for member in archive.infolist():
                target = (destination / member.filename).resolve()
                if destination != target and destination not in target.parents:
                    raise RuntimeError("业务包包含不安全的文件路径")
            archive.extractall(destination)

    def ensure_release(self, bundle):
        wanted_version = str((bundle or {}).get("version") or "").strip()
        wanted_sha = str((bundle or {}).get("sha256") or "").strip().lower()
        if not wanted_version or not wanted_sha:
            raise RuntimeError("服务端未返回有效业务包版本")
        release_dir = self.config.data_dir / "releases" / wanted_version
        if (release_dir / "local_agent_worker.py").is_file():
            self._activate_release(wanted_version)
            return release_dir

        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self._check_cooldown()
        response = self.session.get(
            self.config.server_url + "/api/local-agents/business-bundle",
            timeout=120,
        )
        self._check_response(response)
        content = response.content
        actual_sha = hashlib.sha256(content).hexdigest()
        response_version = str(response.headers.get("X-Business-Version") or wanted_version)
        expected_sha = str(response.headers.get("X-Bundle-SHA256") or wanted_sha).lower()
        if actual_sha != expected_sha:
            raise RuntimeError("业务包完整性校验失败")

        releases_dir = self.config.data_dir / "releases"
        releases_dir.mkdir(parents=True, exist_ok=True)
        temporary_dir = Path(tempfile.mkdtemp(prefix="release-", dir=str(releases_dir)))
        archive_path = temporary_dir / "bundle.zip"
        archive_path.write_bytes(content)
        extract_dir = temporary_dir / "content"
        extract_dir.mkdir()
        try:
            self._safe_extract(archive_path, extract_dir)
            manifest = _load_json(extract_dir / "bundle-manifest.json")
            if str(manifest.get("version") or "") != response_version:
                raise RuntimeError("业务包版本与清单不一致")
            target_dir = releases_dir / response_version
            if target_dir.exists():
                shutil.rmtree(target_dir)
            os.replace(extract_dir, target_dir)
        finally:
            shutil.rmtree(temporary_dir, ignore_errors=True)
        self._activate_release(response_version)
        self._prune_releases(keep=2)
        self.log(f"业务代码已更新到版本 {response_version}")
        return releases_dir / response_version

    def _activate_release(self, version, reason="activate"):
        current_path = self.config.data_dir / "current-release.json"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        previous = self.current_release
        temporary = current_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": version,
                    "previous_version": previous,
                    "reason": reason,
                    "activated_at": int(time.time()),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, current_path)
        self.current_release = version
        history_path = self._release_history_path()
        history = _load_json(history_path)
        entries = [
            item for item in (history.get("releases") or [])
            if str((item or {}).get("version") or "") != version
        ]
        entries.insert(
            0,
            {
                "version": version,
                "previous_version": previous,
                "reason": reason,
                "activated_at": int(time.time()),
            },
        )
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_tmp = history_path.with_suffix(".tmp")
        history_tmp.write_text(
            json.dumps({"releases": entries[:10]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(history_tmp, history_path)

    def _prune_releases(self, keep=2):
        releases_dir = self.config.data_dir / "releases"
        releases = sorted(
            (path for path in releases_dir.iterdir() if path.is_dir()),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        for path in releases[max(1, keep) :]:
            shutil.rmtree(path, ignore_errors=True)

    def claim_job(self):
        data = self._request(
            "POST",
            "/api/local-agents/jobs/claim",
            json={
                "agent_id": self.config.agent_id,
                "session_id": self.session_id,
            },
            timeout=20,
        )
        self._observe_queue(data)
        return data.get("job")

    def send_event(self, job_id, **payload):
        return self._request(
            "POST",
            f"/api/local-agents/jobs/{job_id}/events",
            json={"agent_id": self.config.agent_id, **payload},
            timeout=30,
        )

    def send_event_until_success(self, job_id, **payload):
        failures = 0
        while True:
            try:
                return self.send_event(job_id, **payload)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failures += 1
                delay = self._retry_delay_with_cooldown(failures)
                self.log(f"任务 {job_id} 的结果暂时无法上传：{exc}；{delay:.0f} 秒后重试")
                if self.shutdown_event.wait(delay):
                    self.log(f"Agent 正在退出，任务 {job_id} 的结果留待服务端租约回收")
                    return None

    @staticmethod
    def _retry_delay(failures):
        return min(RETRY_MAX_SECONDS, 10.0 * 2 ** min(5, max(0, int(failures) - 1)))

    def _retry_delay_with_cooldown(self, failures):
        return max(self._retry_delay(failures), self._cooldown_remaining())

    def _worker_command(self, release_dir, job_file, cancel_file):
        arguments = [
            "--worker",
            "--release-dir",
            str(release_dir),
            "--job-file",
            str(job_file),
            "--cancel-file",
            str(cancel_file),
        ]
        if getattr(sys, "frozen", False):
            return [sys.executable, *arguments]
        return [sys.executable, str(Path(__file__).resolve()), *arguments]

    def run_job(self, job):
        job_id = str(job.get("job_id") or "")
        release_dir = self.config.data_dir / "releases" / self.current_release
        job_dir = self.config.data_dir / "jobs" / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job_file = job_dir / "job.json"
        cancel_file = job_dir / "cancel.requested"
        result_file = job_dir / "result.json"
        result_file.unlink(missing_ok=True)
        if cancel_file.exists():
            cancel_file.unlink()
        job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        environment = dict(os.environ)
        environment.update(
            {
                "BIT_RUNTIME_ROLE": "client",
                "BIT_DB_API_BASE_URL": self.config.server_url,
                "BIT_DB_API_TOKEN": self.config.db_api_token,
                "BIT_EXECUTION_TARGET": "agent",
                "BIT_EXECUTION_AGENT_ID": str(
                    getattr(self.config, "agent_id", "") or job.get("agent_id") or ""
                ),
                "BIT_EXECUTION_AGENT_NAME": str(
                    getattr(self.config, "name", "") or ""
                ),
                "BIT_EXECUTION_HOSTNAME": socket.gethostname(),
                "PYTHONUNBUFFERED": "1",
                "PYTHONIOENCODING": "utf-8",
            }
        )
        process = subprocess.Popen(
            self._worker_command(release_dir, job_file, cancel_file),
            cwd=str(release_dir),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=os.name != "nt",
        )
        worker_guard = WorkerProcessGuard(process)
        self._set_current_worker(job_id, worker_guard, cancel_file)
        output_queue = queue.Queue()

        def read_output():
            try:
                for line in process.stdout or ():
                    if line.strip():
                        self.log(f"任务 {job_id}｜{line.rstrip()}")
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        threading.Thread(
            target=read_output,
            name=f"agent-output-{job_id}",
            daemon=True,
        ).start()
        next_heartbeat_at = 0.0
        heartbeat_failures = 0
        cancel_started = 0.0
        reader_done = False
        pending_logs = ""
        last_log_flush = time.monotonic()
        next_log_upload_at = last_log_flush
        log_upload_failures = 0
        worker_exited_at = 0.0
        last_heartbeat_success = time.monotonic()
        while process.poll() is None or not reader_done:
            try:
                item = output_queue.get(timeout=0.25)
                if item is None:
                    reader_done = True
                else:
                    pending_logs += item
            except queue.Empty:
                pass
            now = time.monotonic()
            if self.shutdown_event.is_set() and not cancel_file.exists():
                cancel_file.write_text("agent shutdown", encoding="utf-8")
                cancel_started = now
            if cancel_file.exists() and not cancel_started:
                cancel_started = now
            if (
                pending_logs
                and now >= next_log_upload_at
                and (
                    len(pending_logs) >= 32 * 1024
                    or now - last_log_flush >= LOG_FLUSH_SECONDS
                )
            ):
                # 64K characters stay below the server's 512 KiB UTF-8 limit,
                # even when every code point uses four bytes.
                chunk = pending_logs[: 64 * 1024]
                try:
                    self.send_event(job_id, content=chunk)
                except Exception as exc:
                    log_upload_failures += 1
                    delay = self._retry_delay_with_cooldown(log_upload_failures)
                    next_log_upload_at = time.monotonic() + delay
                    self.log(
                        "任务日志暂时无法上传，将继续保留并重试："
                        f"{exc}；{delay:.0f} 秒后重试"
                    )
                else:
                    pending_logs = pending_logs[len(chunk) :]
                    last_log_flush = now
                    next_log_upload_at = now + LOG_UPLOAD_MIN_INTERVAL_SECONDS
                    log_upload_failures = 0
            if now >= next_heartbeat_at:
                try:
                    heartbeat = self.heartbeat(current_job_id=job_id)
                except Exception as exc:
                    heartbeat_failures += 1
                    delay = self._retry_delay_with_cooldown(heartbeat_failures)
                    next_heartbeat_at = time.monotonic() + delay
                    self.log(f"任务运行中，心跳暂时失败：{exc}；{delay:.0f} 秒后重试")
                    if (
                        now - last_heartbeat_success >= JOB_CONTROL_LOSS_STOP_SECONDS
                        and not cancel_file.exists()
                    ):
                        self.log("任务控制连接长时间中断，为避免失控执行，正在停止本机任务")
                        cancel_file.write_text("control lease expired", encoding="utf-8")
                        cancel_started = now
                else:
                    heartbeat_failures = 0
                    last_heartbeat_success = now
                    next_heartbeat_at = time.monotonic() + min(
                        1.0, self.config.heartbeat_seconds
                    )
                    if not pending_logs:
                        self._rate_limit_failures = 0
                    cancel_ids = set(heartbeat.get("cancel_job_ids") or ())
                    if job_id in cancel_ids and not cancel_file.exists():
                        cancel_file.write_text("stop", encoding="utf-8")
                        cancel_started = now
            if cancel_started and process.poll() is None:
                worker_guard.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
            if process.poll() is not None and not worker_exited_at:
                worker_exited_at = now
                # Closing the Job Object also removes any descendants that
                # inherited the output pipe after the main worker returned.
                worker_guard.close()
                if os.name != "nt" and not reader_done:
                    terminate_worker_tree(process.pid)
            if worker_exited_at and not reader_done and now - worker_exited_at >= 5:
                # A badly behaved descendant must not hold the Agent's stdout
                # pipe and block all subsequently queued jobs forever.
                try:
                    if process.stdout is not None:
                        process.stdout.close()
                except OSError:
                    pass
                reader_done = True
            if process.poll() is not None and reader_done:
                break
        return_code = int(process.wait())
        result = _load_json(result_file)
        stopped = cancel_file.exists() or return_code == 2
        status = "stopped" if stopped else "success" if return_code == 0 else "error"
        message = (
            "本机任务已停止"
            if stopped
            else "本机任务执行完成"
            if return_code == 0
            else f"本机业务进程异常退出：{return_code}"
        )
        final_message = message if stopped or return_code else result.get("message") or message
        while pending_logs:
            chunk = pending_logs[: 64 * 1024]
            self.send_event_until_success(job_id, content=chunk)
            pending_logs = pending_logs[len(chunk) :]
        self.log(f"任务 {job_id} 已结束：{final_message}")
        self.send_event_until_success(
            job_id,
            status=status,
            message=final_message,
            result={**result, "return_code": return_code},
        )
        self._clear_current_worker(worker_guard)

    def run(self):
        self.log(
            f"泽顺本机 Agent {AGENT_VERSION} 已启动：{self.config.name} "
            f"({self.config.agent_id})；本地日志：{self.runtime_log.path}"
        )
        failures = 0
        while True:
            if self.shutdown_event.is_set():
                self.log("Agent 已停止")
                return 0
            try:
                self.ensure_enrolled()
                heartbeat = self.heartbeat()
                heartbeat_claim_supported = "job" in heartbeat
                job = heartbeat.get("job") if heartbeat_claim_supported else None
                previous_release = self.current_release
                self.ensure_release(heartbeat.get("bundle") or {})
                if self.current_release != previous_release:
                    refreshed_heartbeat = self.heartbeat()
                    if not heartbeat_claim_supported and "job" in refreshed_heartbeat:
                        heartbeat_claim_supported = True
                    if job is None and "job" in refreshed_heartbeat:
                        job = refreshed_heartbeat.get("job")
                if not heartbeat_claim_supported:
                    job = self.claim_job()
                if job:
                    self.log(f"收到任务 {job.get('job_id')}：{job.get('job_type')}")
                    try:
                        self.run_job(job)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        job_id = str(job.get("job_id") or "")
                        self.log(f"任务 {job_id} 启动或监控失败：{exc}")
                        self.send_event_until_success(
                            job_id,
                            status="error",
                            message=f"本机 Agent 启动或监控任务失败：{exc}",
                            result={"agent_error": str(exc)},
                        )
                    finally:
                        with self._worker_lock:
                            active_guard = self._current_worker
                        if active_guard is not None:
                            active_guard.terminate()
                            self._clear_current_worker(active_guard)
                failures = 0
                self._rate_limit_failures = 0
                if self.config.once:
                    return 0
                if self.shutdown_event.wait(self.config.poll_seconds):
                    self.log("Agent 已停止")
                    return 0
            except KeyboardInterrupt:
                self.log("Agent 已停止")
                return 0
            except Exception as exc:
                failures += 1
                if self.config.once:
                    self.log(f"Agent 连接异常：{exc}")
                    return 1
                delay = self._retry_delay_with_cooldown(failures)
                self.log(f"Agent 连接异常：{exc}；{delay:.0f} 秒后重试")
                if self.shutdown_event.wait(delay):
                    self.log("Agent 已停止")
                    return 0


def _run_external_worker(args):
    release_dir = Path(args.release_dir).resolve()
    worker_path = release_dir / "local_agent_worker.py"
    if not worker_path.is_file():
        raise RuntimeError(f"业务包缺少执行入口：{worker_path}")
    sys.path.insert(0, str(release_dir))
    sys.argv = [
        str(worker_path),
        "--job-file",
        str(Path(args.job_file).resolve()),
        "--cancel-file",
        str(Path(args.cancel_file).resolve()),
    ]
    runpy.run_path(str(worker_path), run_name="__main__")
    return 0


def build_argument_parser():
    parser = argparse.ArgumentParser(description="泽顺本机自动化 Agent")
    parser.add_argument("--config", default="")
    parser.add_argument("--server", default="")
    parser.add_argument("--token", default="")
    parser.add_argument("--db-api-token", default="")
    parser.add_argument("--name", default="")
    parser.add_argument("--agent-id", default="")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--rollback",
        nargs="?",
        const="",
        default=None,
        metavar="VERSION",
        help="激活上一个本地业务版本，或指定版本后退出",
    )
    parser.add_argument(
        "--no-window",
        action="store_true",
        help="不显示图形运行状态窗口，仅写控制台和本地日志",
    )
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--job-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--cancel-file", default="", help=argparse.SUPPRESS)
    return parser


def _status_window_enabled(args):
    return (
        sys.platform in STATUS_WINDOW_PLATFORMS
        and not args.no_window
        and not args.once
        and not args.worker
    )


def _install_shutdown_handlers(agent):
    if threading.current_thread() is not threading.main_thread():
        return

    def handle_signal(signum, _frame):
        agent.request_shutdown(f"收到系统信号 {signum}")

    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(signum, handle_signal)
        except (OSError, ValueError):
            pass


def main(argv=None):
    args = build_argument_parser().parse_args(argv)
    if args.worker:
        _attach_worker_output_streams()
        return _run_external_worker(args)
    config = AgentConfig(args)
    agent = LocalAgent(config)
    _install_shutdown_handlers(agent)
    process_lock = AgentProcessLock(config.data_dir / "agent.lock")
    if not process_lock.acquire():
        agent.log("泽顺本机 Agent 已在运行，本次重复启动退出。")
        return 2
    try:
        if args.rollback is not None:
            try:
                version = agent.rollback_release(args.rollback)
                agent.log(f"回退完成，当前版本：{version}")
                return 0
            except Exception as exc:
                agent.log(f"回退失败：{exc}")
                return 1
        if _status_window_enabled(args):
            try:
                return AgentStatusWindow(agent).run()
            except Exception as exc:
                agent.log(f"状态窗口启动失败，已切换到控制台模式：{exc}")
        return agent.run()
    finally:
        process_lock.release()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
