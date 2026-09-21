"""Create and restore a small, verifiable production backup.

The command is intentionally explicit about restore because restoring a SQL
dump replaces business data.  It keeps the Agent queue beside the dump and
writes SHA-256 checksums so an operator can verify a copied backup before use.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _mysql_env():
    return {
        "host": os.environ.get("MYSQL_HOST", "127.0.0.1"),
        "port": os.environ.get("MYSQL_PORT", "3306"),
        "user": os.environ.get("MYSQL_USER", "root"),
        "password": os.environ.get("MYSQL_PASSWORD", ""),
        "database": os.environ.get("MYSQL_DATABASE", ""),
    }


def _agent_hub_path():
    configured = str(os.environ.get("BIT_LOCAL_AGENT_HUB_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    elif os.sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support"
    else:
        root = Path(
            os.environ.get("XDG_STATE_HOME")
            or os.environ.get("XDG_DATA_HOME")
            or (Path.home() / ".local" / "share")
        )
    return root / "Zeshun" / "MercadoWorkbench" / "local-agent-hub.sqlite3"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dump_mysql(destination: Path) -> Path:
    settings = _mysql_env()
    if not settings["database"]:
        raise RuntimeError("请设置 MYSQL_DATABASE 后再执行备份")
    executable = shutil.which("mysqldump")
    if not executable:
        raise RuntimeError("未找到 mysqldump，请安装 MySQL 客户端工具")
    dump_path = destination / "mysql.sql"
    command = [
        executable,
        "--single-transaction",
        "--routines",
        "--events",
        "--triggers",
        "--host", settings["host"],
        "--port", settings["port"],
        "--user", settings["user"],
        settings["database"],
    ]
    env = os.environ.copy()
    if settings["password"]:
        env["MYSQL_PWD"] = settings["password"]
    with dump_path.open("wb") as stream:
        subprocess.run(command, check=True, env=env, stdout=stream)
    return dump_path


def create_backup(output: Path, skip_mysql=False) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = output.expanduser().resolve() / stamp
    backup.mkdir(parents=True, exist_ok=False)
    files = []
    if not skip_mysql:
        files.append(_dump_mysql(backup))
    hub = _agent_hub_path()
    if hub.is_file():
        target = backup / "local-agent-hub.sqlite3"
        shutil.copy2(hub, target)
        files.append(target)
    manifest = {
        "created_at": int(time.time()),
        "mysql_database": _mysql_env()["database"],
        "agent_hub_source": str(hub),
        "files": {
            str(path.relative_to(backup)): _sha256(path)
            for path in files
        },
    }
    (backup / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return backup


def verify_backup(backup: Path):
    manifest_path = backup / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for name, expected in (manifest.get("files") or {}).items():
        path = backup / name
        if not path.is_file() or _sha256(path) != expected:
            raise RuntimeError(f"备份校验失败：{name}")
    return manifest


def restore_backup(backup: Path, confirm=False):
    if not confirm:
        raise RuntimeError("恢复会覆盖数据库，必须传入 --confirm-restore")
    manifest = verify_backup(backup)
    settings = _mysql_env()
    dump_path = backup / "mysql.sql"
    if dump_path.is_file():
        executable = shutil.which("mysql")
        if not executable:
            raise RuntimeError("未找到 mysql，请安装 MySQL 客户端工具")
        if not settings["database"]:
            raise RuntimeError("请设置 MYSQL_DATABASE 后再恢复")
        command = [
            executable, "--host", settings["host"], "--port", settings["port"],
            "--user", settings["user"], settings["database"],
        ]
        env = os.environ.copy()
        if settings["password"]:
            env["MYSQL_PWD"] = settings["password"]
        with dump_path.open("rb") as stream:
            subprocess.run(command, check=True, env=env, stdin=stream)
    saved_hub = backup / "local-agent-hub.sqlite3"
    if saved_hub.is_file():
        target = _agent_hub_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="agent-hub-restore-", suffix=".sqlite3", dir=target.parent
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            shutil.copy2(saved_hub, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description="泽顺工作台备份与恢复")
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--output", default=str(PROJECT_ROOT / "backups"))
    backup.add_argument("--skip-mysql", action="store_true")
    restore = sub.add_parser("restore")
    restore.add_argument("backup_dir")
    restore.add_argument("--confirm-restore", action="store_true")
    verify = sub.add_parser("verify")
    verify.add_argument("backup_dir")
    args = parser.parse_args(argv)
    if args.command == "backup":
        print(create_backup(Path(args.output), skip_mysql=args.skip_mysql))
    elif args.command == "verify":
        verify_backup(Path(args.backup_dir).expanduser().resolve())
        print("backup verified")
    else:
        restore_backup(Path(args.backup_dir).expanduser().resolve(), confirm=args.confirm_restore)
        print("backup restored")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
