"""Stable outbound runner for Zeshun local BitBrowser automation.

This file deliberately has no imports from the changing ``bit`` business
package.  A packaged copy can therefore stay stable while jobs run against a
versioned source bundle downloaded from the public workbench.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import queue
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import requests


AGENT_VERSION = "1.0.0"
DEFAULT_SERVER_URL = "https://zeshun.nat100.top"
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_HEARTBEAT_SECONDS = 10.0


def _default_data_dir():
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if root:
            return Path(root) / "Zeshun" / "MercadoLocalAgent"
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
            0.5,
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
    capabilities = ("appeal",)

    def __init__(self, config):
        self.config = config
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": f"MercadoLocalAgent/{AGENT_VERSION}"})
        if config.agent_token:
            self.session.headers["X-Local-Agent-Token"] = config.agent_token
        self.current_release = self._read_current_release()

    def ensure_enrolled(self):
        if self.config.agent_token:
            return
        response = self.session.post(
            self.config.server_url + "/api/local-agents/enroll",
            headers={"Authorization": f"Bearer {self.config.enrollment_token}"},
            json={
                "agent_id": self.config.agent_id,
                "name": self.config.name,
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "agent_version": AGENT_VERSION,
                "capabilities": list(self.capabilities),
            },
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError("Agent 注册接口返回无效内容") from exc
        if response.status_code >= 400 or payload.get("status") != "success":
            raise RuntimeError(payload.get("message") or "Agent 注册失败")
        token = str((payload.get("data") or {}).get("agent_token") or "")
        if not token:
            raise RuntimeError("Agent 注册接口未返回长期凭证")
        self.config.save_agent_token(token)
        self.session.headers["X-Local-Agent-Token"] = token

    def _request(self, method, path, **kwargs):
        response = self.session.request(
            method,
            self.config.server_url + path,
            timeout=kwargs.pop("timeout", 30),
            **kwargs,
        )
        if response.status_code >= 400:
            try:
                message = response.json().get("message")
            except (TypeError, ValueError):
                message = ""
            raise RuntimeError(message or f"服务端请求失败：HTTP {response.status_code}")
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

    def heartbeat(self):
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
            },
            timeout=20,
        )
        refreshed_token = str(data.get("agent_token") or "")
        if refreshed_token and refreshed_token != self.config.agent_token:
            self.config.save_agent_token(refreshed_token)
            self.session.headers["X-Local-Agent-Token"] = refreshed_token
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
        response = self.session.get(
            self.config.server_url + "/api/local-agents/business-bundle",
            timeout=120,
        )
        response.raise_for_status()
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
        print(f"业务代码已更新到版本 {response_version}", flush=True)
        return releases_dir / response_version

    def _activate_release(self, version):
        current_path = self.config.data_dir / "current-release.json"
        current_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = current_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"version": version}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, current_path)
        self.current_release = version

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
        return self._request(
            "POST",
            "/api/local-agents/jobs/claim",
            json={"agent_id": self.config.agent_id},
            timeout=20,
        ).get("job")

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
                delay = min(30.0, max(2.0, failures * 2.0))
                print(
                    f"任务 {job_id} 的结果暂时无法上传：{exc}；{delay:.0f} 秒后重试",
                    flush=True,
                )
                time.sleep(delay)

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
        if cancel_file.exists():
            cancel_file.unlink()
        job_file.write_text(json.dumps(job, ensure_ascii=False), encoding="utf-8")
        environment = dict(os.environ)
        environment.update(
            {
                "BIT_RUNTIME_ROLE": "client",
                "BIT_DB_API_BASE_URL": self.config.server_url,
                "BIT_DB_API_TOKEN": self.config.db_api_token,
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
        )
        output_queue = queue.Queue()

        def read_output():
            try:
                for line in process.stdout or ():
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        threading.Thread(
            target=read_output,
            name=f"agent-output-{job_id}",
            daemon=True,
        ).start()
        last_heartbeat = 0.0
        cancel_started = 0.0
        reader_done = False
        pending_logs = ""
        while process.poll() is None or not reader_done:
            try:
                item = output_queue.get(timeout=0.25)
                if item is None:
                    reader_done = True
                else:
                    pending_logs += item
            except queue.Empty:
                pass
            if len(pending_logs) >= 32 * 1024:
                # 64K characters stay below the server's 512 KiB UTF-8 limit,
                # even when every code point uses four bytes.
                chunk = pending_logs[: 64 * 1024]
                try:
                    self.send_event(job_id, content=chunk)
                except Exception as exc:
                    print(f"任务日志暂时无法上传，将继续保留并重试：{exc}", flush=True)
                else:
                    pending_logs = pending_logs[len(chunk) :]
            now = time.monotonic()
            if now - last_heartbeat >= self.config.heartbeat_seconds:
                last_heartbeat = now
                try:
                    heartbeat = self.heartbeat()
                except Exception as exc:
                    print(f"任务运行中，心跳暂时失败：{exc}", flush=True)
                else:
                    cancel_ids = set(heartbeat.get("cancel_job_ids") or ())
                    if job_id in cancel_ids and not cancel_file.exists():
                        cancel_file.write_text("stop", encoding="utf-8")
                        cancel_started = now
                    if (
                        cancel_started
                        and process.poll() is None
                        and now - cancel_started > 45
                    ):
                        process.terminate()
            if process.poll() is not None and reader_done:
                break
        while pending_logs:
            chunk = pending_logs[: 64 * 1024]
            self.send_event_until_success(job_id, content=chunk)
            pending_logs = pending_logs[len(chunk) :]
        return_code = int(process.wait())
        stopped = cancel_file.exists() or return_code == 2
        status = "stopped" if stopped else "success" if return_code == 0 else "error"
        message = (
            "本机任务已停止"
            if stopped
            else "本机任务执行完成"
            if return_code == 0
            else f"本机业务进程异常退出：{return_code}"
        )
        self.send_event_until_success(
            job_id,
            status=status,
            message=message,
            result={"return_code": return_code},
        )

    def run(self):
        self.ensure_enrolled()
        print(
            f"泽顺本机 Agent 已启动：{self.config.name} ({self.config.agent_id})",
            flush=True,
        )
        failures = 0
        while True:
            try:
                heartbeat = self.heartbeat()
                previous_release = self.current_release
                self.ensure_release(heartbeat.get("bundle") or {})
                if self.current_release != previous_release:
                    self.heartbeat()
                job = self.claim_job()
                if job:
                    print(f"收到任务 {job.get('job_id')}：{job.get('job_type')}", flush=True)
                    try:
                        self.run_job(job)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        job_id = str(job.get("job_id") or "")
                        print(f"任务 {job_id} 启动或监控失败：{exc}", flush=True)
                        self.send_event_until_success(
                            job_id,
                            status="error",
                            message=f"本机 Agent 启动或监控任务失败：{exc}",
                            result={"agent_error": str(exc)},
                        )
                failures = 0
                if self.config.once:
                    return 0
                time.sleep(self.config.poll_seconds)
            except KeyboardInterrupt:
                print("Agent 已停止", flush=True)
                return 0
            except Exception as exc:
                failures += 1
                print(f"Agent 连接异常：{exc}", flush=True)
                if self.config.once:
                    return 1
                time.sleep(min(30.0, max(2.0, failures * 2.0)))


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
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--release-dir", default="", help=argparse.SUPPRESS)
    parser.add_argument("--job-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--cancel-file", default="", help=argparse.SUPPRESS)
    return parser


def main(argv=None):
    args = build_argument_parser().parse_args(argv)
    if args.worker:
        return _run_external_worker(args)
    config = AgentConfig(args)
    process_lock = AgentProcessLock(config.data_dir / "agent.lock")
    if not process_lock.acquire():
        print("泽顺本机 Agent 已在运行，本次重复启动退出。")
        return 2
    try:
        return LocalAgent(config).run()
    finally:
        process_lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
