"""Build and safely publish a Windows Agent using a project-local Wine runtime."""
from __future__ import annotations

import argparse
import ast
import fcntl
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TOOLS = ROOT / ".tools" / "windows-agent"
WINE = TOOLS / "Wine Stable.app/Contents/Resources/wine/bin/wine"
PREFIX = TOOLS / "prefix"
PYTHON = PREFIX / "drive_c/Python312/python.exe"
REQUIREMENTS = ROOT / "requirements-agent-windows.txt"
STATE = TOOLS / "build-state.json"
WINE_URL = "https://github.com/Gcenx/macOS_Wine_builds/releases/download/11.0_1/wine-stable-11.0_1-osx64.tar.xz"
WINE_SHA256 = "b50dc50ec7f41d58b115a6b685d4d1315ba3c797bd3aa0f49213f2703cb82388"
PYTHON_URL = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"
PYTHON_SHA256 = "67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb"


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def log(message):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message, flush=True)


def winpath(path):
    return "Z:" + str(Path(path).resolve()).replace("/", "\\")


def environment():
    env = os.environ.copy()
    env.update(WINEPREFIX=str(PREFIX), WINEDEBUG="-all", WINEARCH="win64",
               PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PIP_DISABLE_PIP_VERSION_CHECK="1",
               MVK_CONFIG_LOG_LEVEL="0")
    # macOS system proxy settings also work for CLI downloads and Windows pip.
    proxy = urllib.request.getproxies().get("https")
    if proxy:
        env.setdefault("HTTPS_PROXY", proxy)
        env.setdefault("HTTP_PROXY", proxy)
    return env


def run(args, *, timeout=1800, capture=False):
    result = subprocess.run([str(a) for a in args], cwd=ROOT, env=environment(),
                            timeout=timeout, check=True, text=True,
                            stdout=subprocess.PIPE if capture else None)
    return result.stdout if capture else None


def download(url, *, sha256):
    destination = TOOLS / "downloads" / url.rsplit("/", 1)[-1]
    destination.parent.mkdir(parents=True, exist_ok=True)
    def valid():
        if not destination.is_file():
            return False
        return digest(destination) == sha256
    if not valid():
        partial = destination.with_suffix(destination.suffix + ".part")
        command = ["/usr/bin/curl", "-fL", "--retry", "2", "--max-time", "600"]
        proxy = urllib.request.getproxies().get("https")
        if proxy:
            command += ["--proxy", proxy]
        log("下载 " + destination.name)
        run([*command, url, "-o", partial], timeout=660)
        partial.replace(destination)
    if not valid():
        raise RuntimeError("下载文件校验失败：" + str(destination))
    return destination


def setup():
    if sys.platform != "darwin":
        raise RuntimeError("此入口用于 macOS；Windows 请使用 build_local_agent.bat")
    if not WINE.is_file():
        archive = download(WINE_URL, sha256=WINE_SHA256)
        log("解压独立 Wine 环境")
        with tarfile.open(archive) as source:
            source.extractall(TOOLS, filter="data")
    run([WINE, "--version"], timeout=30)
    if not PYTHON.is_file():
        installer = download(PYTHON_URL, sha256=PYTHON_SHA256)
        log("解包 Windows Python 3.12（含 Tcl/Tk）")
        names = ["core.msi", "exe.msi", "lib.msi", "dev.msi", "tcltk.msi", "pip.msi"]
        layout = TOOLS / "python-layout"
        layout.mkdir(exist_ok=True)
        sources = {name: next(PREFIX.rglob(name), None) for name in names}
        if not all(sources.values()):
            # /layout only caches packages; MSI administrative extraction avoids
            # installer custom actions that crash under Wine's WoW64 layer.
            run([WINE, winpath(installer), "/layout", winpath(layout), "/quiet",
                 "Include_launcher=0", "Include_test=0", "Include_doc=0"], timeout=600)
            sources = {name: layout / name for name in names}
        for name in names:
            run([WINE, "msiexec", "/a", winpath(sources[name]), "/qn", "TARGETDIR=C:\\Python312"], timeout=120)
    if not PYTHON.is_file():
        raise RuntimeError("Windows Python 安装未完成，请查看 .tools/windows-agent/python-install.log")
    run([WINE, PYTHON, "-c", "import sys, tkinter, ssl; assert sys.platform=='win32'; print(sys.version)"])
    marker = TOOLS / "requirements.sha256"
    wanted = digest(REQUIREMENTS)
    if not marker.is_file() or marker.read_text().strip() != wanted:
        log("安装 Windows 打包依赖")
        run([WINE, PYTHON, "-m", "ensurepip", "--upgrade"])
        cached_wheels = TOOLS / "wheels"
        cache_args = ["--find-links", winpath(cached_wheels)] if cached_wheels.is_dir() else []
        offline_marker = cached_wheels / "offline-ready"
        offline = offline_marker.is_file() and offline_marker.read_text().strip() == wanted
        if offline:
            run([WINE, PYTHON, "-m", "pip", "install", "--no-index", *cache_args, "setuptools", "wheel"])
            cache_args += ["--no-index", "--no-build-isolation"]
        run([WINE, PYTHON, "-m", "pip", "install", "--no-cache-dir", *cache_args, "-r", winpath(REQUIREMENTS)])
        run([WINE, PYTHON, "-m", "pip", "check"])
        verify_runtime()
        frozen = run([WINE, PYTHON, "-m", "pip", "freeze"], capture=True)
        (TOOLS / "installed-requirements.txt").write_text(frozen, encoding="utf-8")
        marker.write_text(wanted + "\n")
    else:
        verify_runtime()


def verify_runtime():
    # A killed Wine wrapper can report exit code zero. Require an on-disk
    # sentinel produced by the actual Windows interpreter after validation.
    sentinel = TOOLS / "runtime-verified.json"
    sentinel.unlink(missing_ok=True)
    code = (
        "import sys, json, tkinter, ssl, requests, PyInstaller, pandas, openpyxl, cryptography\n"
        "from pathlib import Path\nfrom importlib.metadata import version\n"
        "from packaging.requirements import Requirement\n"
        "assert sys.platform == 'win32'\n"
        f"lines = Path({winpath(REQUIREMENTS)!r}).read_text().splitlines()\n"
        "for line in lines:\n"
        " if line.strip() and not line.startswith('#'):\n"
        "  req = Requirement(line); assert version(req.name) in req.specifier, line\n"
        f"Path({winpath(sentinel)!r}).write_text(json.dumps({{'ok': True}}))\n"
    )
    run([WINE, PYTHON, "-c", code], timeout=180)
    if not sentinel.is_file() or json.loads(sentinel.read_text()).get("ok") is not True:
        raise RuntimeError("Windows 运行依赖校验未通过，不能构建或发布")


def build_fingerprint():
    from bit.local_agent_bundle import iter_business_source_files
    hasher = hashlib.sha256()
    for path in [ROOT / "local_agent.py", ROOT / "MercadoLocalAgent.spec", REQUIREMENTS,
                 Path(__file__), ROOT / "install-agent-forever.ps1",
                 ROOT / "bit/local_agent_distribution.py"]:
        hasher.update(path.read_bytes())
    # Business logic updates are downloaded by Agents; newly imported modules
    # require a fresh dependency collection even when the launcher is unchanged.
    imports = set()
    for path in iter_business_source_files(ROOT):
        if path.suffix != ".py":
            continue
        try:
            tree = ast.parse(path.read_bytes(), filename=str(path))
        except SyntaxError:
            # Some unused legacy files in existing business bundles are not
            # valid Python; track their bytes without blocking current jobs.
            hasher.update(path.relative_to(ROOT).as_posix().encode())
            hasher.update(path.read_bytes())
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(item.name for item in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add("." * node.level + node.module)
    hasher.update(json.dumps(sorted(imports)).encode())
    return hasher.hexdigest()


def validate_pe(path):
    with Path(path).open("rb") as stream:
        if stream.read(2) != b"MZ":
            raise RuntimeError("构建产物不是 Windows EXE")
        stream.seek(0x3C)
        offset = struct.unpack("<I", stream.read(4))[0]
        stream.seek(offset)
        if stream.read(6) != b"PE\0\0\x64\x86":
            raise RuntimeError("构建产物不是 Windows x64 PE")
        stream.seek(offset + 24 + 68)
        if struct.unpack("<H", stream.read(2))[0] != 2:
            raise RuntimeError("构建产物不是 Windows GUI 程序")


def agent_version():
    tree = ast.parse((ROOT / "local_agent.py").read_bytes())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "AGENT_VERSION" for t in node.targets):
            return ast.literal_eval(node.value)
    raise RuntimeError("未找到 AGENT_VERSION")


def smoke_test(executable):
    with tempfile.TemporaryDirectory(prefix="smoke-", dir=TOOLS) as temporary:
        folder = Path(temporary)
        output = folder / "result.json"
        (folder / "local_agent_worker.py").write_text(
            "import json, sys, tkinter, ssl, requests, pandas, openpyxl, cryptography\n"
            "from pathlib import Path\nfrom playwright.sync_api import sync_playwright\n"
            "import win32gui, win32con\n"
            "assert sys.platform == 'win32'\n"
            "assert getattr(sys, 'frozen', False)\n"
            "ssl.create_default_context(cafile=requests.certs.where())\n"
            "root = tkinter.Tk(); root.withdraw(); root.destroy()\n"
            f"Path({winpath(output)!r}).write_text(json.dumps({{'ok': True, 'platform': 'win32'}}))\n",
            encoding="utf-8")
        run([WINE, winpath(executable), "--worker", "--release-dir", winpath(folder),
             "--job-file", winpath(folder / "job.json"), "--cancel-file", winpath(folder / "cancel")], timeout=300)
        if not output.is_file() or json.loads(output.read_text()).get("ok") is not True:
            raise RuntimeError("EXE 运行验证失败，保留旧版本")


def atomic_write(destination, content):
    destination = Path(destination)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(destination)


def update_archive(executable):
    from bit.local_agent_distribution import _build_windows_archive
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _build_windows_archive(archive, ROOT, executable)
        archive.writestr("README-update.txt", (
            f"泽顺 Windows Agent {agent_version()} 更新包\n\n"
            "先退出 Agent。已安装守护任务的电脑先运行旧目录的 uninstall-agent.bat。\n"
            "将本包解压覆盖原 Agent 目录，保留原有 local-agent.json。\n"
            "运行 unblock-agent.bat，然后运行 start-agent.bat 或 install-agent.bat。\n"
            "本包不含注册凭证和用户配置；新电脑请从控制台下载正式安装包。\n"
            "电脑身份和业务数据保存在本机用户数据目录，更新不会删除。\n"))
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("更新 ZIP 校验失败")
    return buffer.getvalue()


def build(*, force=False):
    from bit.local_agent_bundle import build_business_bundle
    wanted = build_fingerprint()
    previous = json.loads(STATE.read_text()) if STATE.is_file() else {}
    destination = ROOT / "dist/MercadoLocalAgent.exe"
    archive_path = ROOT / "dist/Zeshun-MercadoLocalAgent-update.zip"
    rebuild = (force or previous.get("fingerprint") != wanted or not destination.is_file()
               or previous.get("exe_sha256") != digest(destination) or not archive_path.is_file()
               or previous.get("zip_sha256") != digest(archive_path))
    if rebuild:
        setup()
        with tempfile.TemporaryDirectory(prefix="build-", dir=TOOLS) as temporary:
            staging = Path(temporary)
            log("开始构建 Windows Agent " + agent_version())
            run([WINE, PYTHON, "-m", "PyInstaller", "--noconfirm", "--clean",
                 "--distpath", winpath(staging / "dist"), "--workpath", winpath(staging / "work"),
                 winpath(ROOT / "MercadoLocalAgent.spec")], timeout=3600)
            executable = staging / "dist/MercadoLocalAgent.exe"
            validate_pe(executable)
            log("验证 EXE、Windows 依赖和图形窗口")
            smoke_test(executable)
            content = update_archive(executable)
            # Do not publish a stale build when sources change during packaging.
            if wanted != build_fingerprint():
                raise RuntimeError("构建期间源文件发生变化；保留旧包，下次自动重试")
            destination.parent.mkdir(parents=True, exist_ok=True)
            for path in (destination, archive_path):
                if path.exists():
                    shutil.copy2(path, path.with_name(path.stem + "-previous" + path.suffix))
            atomic_write(destination, executable.read_bytes())
            atomic_write(archive_path, content)
            previous = {"fingerprint": wanted, "agent_version": agent_version(),
                        "exe_sha256": digest(destination), "zip_sha256": digest(archive_path),
                        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    bundle = build_business_bundle(ROOT)
    business_path = ROOT / "dist/Zeshun-Agent-business.zip"
    # ZIP timestamps can differ between processes even for identical source.
    # Keep metadata tied to the archive actually published on disk.
    if (previous.get("business_version") != bundle["version"]
            or not business_path.is_file()
            or previous.get("business_sha256") != digest(business_path)):
        destination.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(business_path, bundle["content"])
        previous["business_sha256"] = bundle["sha256"]
    previous.update(business_version=bundle["version"])
    atomic_write(STATE, json.dumps(previous, ensure_ascii=False, indent=2).encode())
    atomic_write(ROOT / "dist/windows-agent-build.json", json.dumps(previous, ensure_ascii=False, indent=2).encode())
    log("构建完成，控制台下载已使用新 EXE" if rebuild else "Agent 主程序未变化，已检查业务包")


def main():
    parser = argparse.ArgumentParser(description="Mac 本地构建 Windows Agent")
    parser.add_argument("--setup", action="store_true", help="仅安装独立 Wine、Windows Python 和打包依赖")
    parser.add_argument("--force", action="store_true", help="强制重建，否则仅在主程序或依赖变化时重建")
    args = parser.parse_args()
    TOOLS.mkdir(parents=True, exist_ok=True)
    with (TOOLS / "build.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("已有打包任务正在运行")
            return 0
        setup() if args.setup else build(force=args.force)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.SubprocessError, OSError, ValueError) as exc:
        log("打包失败：" + str(exc))
        raise SystemExit(1)
