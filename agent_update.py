"""Confirmed desktop Agent updates; no business code or configuration is replaced.

The frozen Agent copies its *current* executable as a detached updater. The
updater waits for the Agent lock, replaces only the executable, and requires a
startup acknowledgement before committing. A short-lived marker fences off
supervisor restarts during the transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import urlsplit


MAX_BINARY_BYTES = 1024 * 1024 * 1024
TRANSACTION_SECONDS = 600


def version_tuple(value):
    if not re.fullmatch(r"\d+\.\d+\.\d+", str(value)):
        raise ValueError("更新版本号无效")
    return tuple(map(int, value.split(".")))


def architecture(value):
    return {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(
        str(value).lower(), str(value).lower())


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def update_marker(data_dir):
    return Path(data_dir) / "agent-update.json"


def read_pending(data_dir):
    try:
        data = json.loads(update_marker(data_dir).read_text(encoding="utf-8"))
        if 0 <= time.time() - float(data["created_at"]) < TRANSACTION_SECONDS:
            return data
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def startup_allowed(data_dir, token=""):
    pending = read_pending(data_dir)
    return not pending or bool(token and token == pending.get("token"))


def clear_pending(data_dir, token):
    marker = update_marker(data_dir)
    try:
        if json.loads(marker.read_text(encoding="utf-8")).get("token") == token:
            marker.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def acknowledge_startup(data_dir, token, version):
    pending = read_pending(data_dir)
    if pending and token == pending.get("token") and version == pending.get("version"):
        write_json(Path(pending["directory"]) / "healthy.json", {"token": token})


def child_environment():
    env = os.environ.copy()
    # PyInstaller children must unpack independently of the exiting parent's
    # temporary runtime directory (including the same-executable rollback).
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    return env


def spawn(command, cwd):
    options = dict(cwd=str(cwd), env=child_environment(), stdin=subprocess.DEVNULL,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(command, **options)


def prepare_update(agent, requested_version):
    """Download on the Agent thread, between jobs; never touch the installed EXE."""
    if not getattr(sys, "frozen", False) or sys.platform not in ("win32", "darwin"):
        raise RuntimeError("一键更新仅支持正式 Windows/macOS Agent，源码版请由管理员更新")
    target = Path(sys.executable).resolve()
    server = urlsplit(agent.config.server_url)
    if server.scheme != "https" and server.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("程序更新必须使用 HTTPS 服务端地址")
    target_platform = "macos" if sys.platform == "darwin" else "windows"
    endpoint = "/api/local-agents/client-update?platform=" + target_platform
    manifest = agent._request("GET", endpoint, timeout=60, allow_redirects=False)
    if manifest.get("version") != requested_version:
        raise RuntimeError("服务端版本已变化，请等待新版提示后重新确认")
    if manifest.get("platform") != target_platform:
        raise RuntimeError("更新包与当前操作系统不匹配")
    if version_tuple(requested_version) <= version_tuple(agent.version):
        raise RuntimeError("更新版本必须高于当前版本")
    if architecture(manifest.get("architecture")) != architecture(platform.machine()):
        raise RuntimeError("更新包与当前电脑架构不匹配")
    size = int(manifest.get("size", 0))
    digest = str(manifest.get("sha256", ""))
    if not 0 < size <= MAX_BINARY_BYTES or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise RuntimeError("更新清单无效")
    # Same filesystem as the installed executable, enabling atomic replacement
    # and proving write access before the running Agent is asked to exit.
    directory = Path(tempfile.mkdtemp(prefix=".agent-update-", dir=target.parent))
    transaction = None
    try:
        candidate = directory / target.name
        checksum = hashlib.sha256()
        received = 0
        agent.update_status = "正在下载更新…"
        agent._check_cooldown()
        with agent.session.get(agent.config.server_url + endpoint + "&binary=1",
                               timeout=(15, 60), stream=True, allow_redirects=False) as response:
            agent._check_response(response)
            if response.status_code != 200:
                raise RuntimeError("更新下载未返回有效文件")
            with candidate.open("wb") as output:
                for chunk in response.iter_content(1024 * 1024):
                    if agent.shutdown_event.is_set():
                        raise RuntimeError("更新下载已取消")
                    received += len(chunk)
                    if received > size:
                        raise RuntimeError("更新包大小不匹配")
                    output.write(chunk)
                    checksum.update(chunk)
                    agent.update_status = f"正在下载更新：{received * 100 // size}%"
                output.flush()
                os.fsync(output.fileno())
        if received != size or checksum.hexdigest() != digest:
            raise RuntimeError("更新包校验失败，已保留当前版本")
        candidate.chmod(target.stat().st_mode & 0o777)
        helper = directory / ("updater.exe" if os.name == "nt" else "updater")
        shutil.copy2(target, helper)
        transaction = {
            "created_at": time.time(), "token": uuid.uuid4().hex,
            "version": requested_version, "sha256": digest,
            "directory": str(directory), "target": str(target),
            "data_dir": str(agent.config.data_dir),
            "arguments": agent.restart_arguments, "cwd": os.getcwd(),
        }
        write_json(directory / "transaction.json", transaction)
        write_json(update_marker(agent.config.data_dir), transaction)
        process = spawn([str(helper), "--apply-update", str(directory / "transaction.json")], target.parent)
        # Do not exit until the helper has actually loaded successfully.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            if (directory / "ready").exists():
                return
            if process.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError("更新器未能启动，已保留当前版本")
    except BaseException:
        if transaction:
            clear_pending(agent.config.data_dir, transaction["token"])
        # A helper that starts late must check the transaction marker.
        shutil.rmtree(directory, ignore_errors=True)
        raise


def apply_update(transaction_path, lock_class, log_class):
    transaction_path = Path(transaction_path).resolve()
    data = json.loads(transaction_path.read_text(encoding="utf-8"))
    directory = transaction_path.parent
    target = Path(data["target"])
    data_dir = Path(data["data_dir"])
    log = log_class(data_dir / "agent.log").write
    pending = read_pending(data_dir)
    if not pending or pending.get("token") != data["token"]:
        return 1
    candidate = directory / target.name
    if hashlib.sha256(candidate.read_bytes()).hexdigest() != data["sha256"]:
        return 1
    backup = directory / "previous"
    lock = lock_class(data_dir / "agent.lock")
    acquired = False
    replaced = False
    child = None
    success = False
    restart_old = False
    try:
        (directory / "ready").touch()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            pending = read_pending(data_dir)
            if not pending or pending.get("token") != data["token"]:
                return 1
            if lock.acquire():
                acquired = True
                break
            time.sleep(0.2)
        if not acquired:
            raise RuntimeError("等待旧 Agent 退出超时")
        restart_old = True
        shutil.copy2(target, backup)
        # The PyInstaller parent may still have its EXE open after Python has
        # released agent.lock. Retry without killing it or its business jobs.
        deadline = time.monotonic() + 60
        while True:
            try:
                os.replace(candidate, target)
                replaced = True
                restart_old = False
                break
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)
        lock.release()
        acquired = False
        child = spawn([str(target), *data["arguments"], "--update-token", data["token"]], data["cwd"])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if (directory / "healthy.json").is_file():
                healthy = json.loads((directory / "healthy.json").read_text())
                if healthy.get("token") == data["token"] and child.poll() is None:
                    success = True
                    log(f"Agent 已一键更新到 {data['version']}，配置与设备身份已保留")
                    return 0
            if child.poll() is not None:
                break
            time.sleep(0.2)
        raise RuntimeError("新版 Agent 启动验证失败")
    except Exception as exc:
        log(f"Agent 一键更新失败：{exc}")
        if child is not None and child.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(child.pid), "/T", "/F"],
                               capture_output=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
            else:
                child.terminate()
            child.wait(timeout=30)
        if replaced:
            restart_old = False
            deadline = time.monotonic() + 30
            while True:
                try:
                    os.replace(backup, target)
                    restart_old = True
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        log(f"自动回退受阻，旧程序保存在：{backup}")
                        raise
                    time.sleep(0.5)
            log("已恢复更新前的 Agent 程序")
        return 1
    finally:
        if acquired:
            lock.release()
        clear_pending(data_dir, data["token"])
        if not success and restart_old:
            spawn([str(target), *data["arguments"]], data["cwd"])
        # The helper executable is still running (locked on Windows). Leave the
        # directory, including the rollback copy, for the next successful run.


def cleanup_updates(target):
    """Only remove old updater-owned directories, never user data or configs."""
    for directory in Path(target).parent.glob(".agent-update-*"):
        try:
            transaction = directory / "transaction.json"
            data = json.loads(transaction.read_text(encoding="utf-8"))
            if data.get("target") == str(Path(target).resolve()) and time.time() - float(data["created_at"]) > 86400:
                shutil.rmtree(directory)
        except (OSError, ValueError, KeyError, TypeError):
            pass
