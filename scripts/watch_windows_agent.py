"""Use macOS filesystem events to build Agent packages after source changes."""
from __future__ import annotations

import argparse
import fcntl
import os
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from bit.local_agent_bundle import SOURCE_DIRECTORIES, RESOURCE_DIRECTORIES, ROOT_SOURCE_FILES

LABEL = "com.zeshun.windows-agent-build-watch"
TOOLS = ROOT / ".tools/windows-agent"
PLIST = Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
CORE_FILES = {"local_agent.py", "MercadoLocalAgent.spec", "requirements-agent-windows.txt",
              "install-agent-forever.ps1", "scripts/build_windows_agent.py", *ROOT_SOURCE_FILES}


def log(message):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message, flush=True)


def relevant_path(path, root=ROOT):
    try:
        relative = Path(path).absolute().relative_to(root.resolve())
    except ValueError:
        return False
    if any(part in {"__pycache__", "__MACOSX", "runtime_locks"} or part.startswith(".") for part in relative.parts):
        return False
    name = relative.as_posix()
    if name in CORE_FILES:
        return True
    if any(name.startswith(directory + "/") for directory in RESOURCE_DIRECTORIES):
        return True
    return relative.suffix == ".py" and relative.parts[0] in SOURCE_DIRECTORIES


class AgentBuildWatcher:
    def __init__(self, root=ROOT, command=None, debounce=3):
        self.root = Path(root).resolve()
        self.command = command or [sys.executable, str(self.root / "scripts/build_windows_agent.py")]
        self.debounce = debounce
        self.condition = threading.Condition()
        self.pending = set()
        self.last_change = 0
        self.stopped = False
        self.observer = None

    def on_event(self, event):
        if event.is_directory or event.event_type not in {"created", "modified", "deleted", "moved"}:
            return
        paths = [event.src_path, getattr(event, "dest_path", "")]
        with self.condition:
            accepted = False
            for path in paths:
                if path and relevant_path(path, self.root):
                    self.pending.add(str(Path(path).relative_to(self.root)))
                    accepted = True
            if accepted:
                self.last_change = time.monotonic()
                self.condition.notify()

    def stop(self, *_):
        with self.condition:
            self.stopped = True
            self.condition.notify()

    def run(self):
        # Import the native FSEvents observer explicitly; no polling fallback.
        from watchdog.events import FileSystemEventHandler
        from watchdog.observers.fsevents import FSEventsObserver
        handler = FileSystemEventHandler()
        handler.on_any_event = self.on_event
        self.observer = FSEventsObserver()
        self.observer.schedule(handler, str(self.root), recursive=True)
        self.observer.start()
        log("文件事件监听已启动；仅在 Agent 源码变化后触发，空闲时不打包")
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.stopped or bool(self.pending))
                    if self.stopped:
                        return
                    # Wait only after a source event, to combine editor saves.
                    while not self.stopped:
                        remaining = self.debounce - (time.monotonic() - self.last_change)
                        if remaining <= 0:
                            break
                        self.condition.wait(timeout=remaining)
                    if self.stopped:
                        return
                    changed = sorted(self.pending)
                    self.pending.clear()
                log("源码变化触发打包：" + ", ".join(changed[:8]))
                result = subprocess.run(self.command, cwd=self.root, check=False)
                if result.returncode:
                    log("构建失败，保留旧包；等待下一次源码修改后重试")
                else:
                    log("本次源码变更检查和打包完成")
        finally:
            self.observer.stop()
            self.observer.join()


def install():
    if sys.platform != "darwin":
        raise RuntimeError("此监听服务用于 macOS")
    from watchdog.observers.fsevents import FSEventsObserver  # noqa: F401
    TOOLS.mkdir(parents=True, exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    configuration = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "--run"],
        "WorkingDirectory": str(ROOT), "RunAtLoad": True, "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(TOOLS / "watcher.log"),
        "StandardErrorPath": str(TOOLS / "watcher.log"),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"},
    }
    PLIST.write_bytes(plistlib.dumps(configuration))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain + "/" + LABEL], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    subprocess.run(["launchctl", "enable", domain + "/" + LABEL], check=True)
    log("已安装源码事件监听服务；无定时任务，也不需要保持 Codex 开启")


def uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    log("源码事件监听服务已移除；打包环境和现有产物保留")


def main():
    parser = argparse.ArgumentParser(description="Agent 源码变化触发 Windows 打包")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--install", action="store_true")
    modes.add_argument("--uninstall", action="store_true")
    modes.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.install:
        install()
    elif args.uninstall:
        uninstall()
    else:
        TOOLS.mkdir(parents=True, exist_ok=True)
        with (TOOLS / "watcher.lock").open("w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("源码监听服务已在运行")
                return
            watcher = AgentBuildWatcher()
            signal.signal(signal.SIGTERM, watcher.stop)
            signal.signal(signal.SIGINT, watcher.stop)
            watcher.run()


if __name__ == "__main__":
    main()
