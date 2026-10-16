"""Exercise a built Agent's updater in an isolated directory with no real jobs.

Usage: python3 scripts/smoke_agent_update.py [--windows]
Windows uses the existing project-local Wine runtime; macOS runs natively.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from local_agent import AGENT_VERSION


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", action="store_true")
    parser.add_argument("--rollback", action="store_true", help="验证不可启动的新文件会回退到原程序")
    args = parser.parse_args()
    platform_name = "windows" if args.windows else "macos"
    executable = ROOT / ("dist/MercadoLocalAgent.exe" if args.windows else "dist/macos/MercadoLocalAgent")
    metadata = json.loads((ROOT / "dist" / f"{platform_name}-agent-build.json").read_text())
    assert metadata["agent_version"] == AGENT_VERSION, "正式包尚未构建到当前源码版本"
    assert hashlib.sha256(executable.read_bytes()).hexdigest() == metadata["exe_sha256"]
    if args.windows:
        from scripts.build_windows_agent import WINE, environment, winpath
        command_prefix, env, path_string = [str(WINE)], environment(), winpath
    else:
        command_prefix, env, path_string = [], os.environ.copy(), str
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    with tempfile.TemporaryDirectory(prefix="agent-update-smoke-", dir=ROOT / "artifacts") as temporary:
        folder = Path(temporary)
        data_dir = folder / "data"
        data_dir.mkdir()
        directory = folder / ".agent-update-test"
        directory.mkdir()
        target = folder / executable.name
        helper = directory / ("updater.exe" if args.windows else "updater")
        for destination in (target, helper, directory / target.name):
            shutil.copy2(executable, destination)
        if args.rollback:
            (directory / target.name).write_bytes(b"intentionally invalid executable for offline rollback test")
        config = folder / "local-agent.json"
        config.write_text('{"name": "offline update smoke"}')
        transaction = {
            "token": "offline-smoke", "created_at": time.time(), "version": AGENT_VERSION,
            "target": path_string(target), "data_dir": path_string(data_dir),
            "directory": path_string(directory), "cwd": path_string(folder),
            "sha256": hashlib.sha256((directory / target.name).read_bytes()).hexdigest(),
            "arguments": ["--config", path_string(config), "--data-dir", path_string(data_dir),
                          "--server", "http://127.0.0.1:1", "--token", "offline-test", "--no-window", "--once"],
        }
        for path in (directory / "transaction.json", data_dir / "agent-update.json"):
            path.write_text(json.dumps(transaction), encoding="utf-8")
        result = subprocess.run([*command_prefix, path_string(helper), "--apply-update",
                                 path_string(directory / "transaction.json")],
                                env=env, cwd=folder, timeout=240, capture_output=True)
        assert result.returncode == (1 if args.rollback else 0), result.stderr.decode(errors="replace")
        if args.rollback:
            assert hashlib.sha256(target.read_bytes()).hexdigest() == metadata["exe_sha256"], "旧程序没有恢复"
        else:
            assert (directory / "healthy.json").is_file(), "新版进程没有确认启动"
            assert (directory / "previous").is_file(), "没有保留旧程序"
        assert not (data_dir / "agent-update.json").exists(), "更新事务未提交"
        assert config.read_text() == '{"name": "offline update smoke"}'
        # --once exits after its intentionally unavailable loopback heartbeat.
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if "Agent 连接异常" in (data_dir / "agent.log").read_text(encoding="utf-8"):
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("隔离测试进程没有按预期完成一次离线心跳")
        for attempt in range(40):
            try:
                shutil.rmtree(folder)
                break
            except PermissionError:
                if attempt == 39:
                    raise
                time.sleep(0.5)
        print(json.dumps({"platform": platform_name, "version": AGENT_VERSION,
                          "frozen_rollback_restart" if args.rollback else "frozen_update_restart": "passed",
                          "config_preserved": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
