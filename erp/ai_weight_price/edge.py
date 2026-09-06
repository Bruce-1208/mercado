"""Open the installed Edge visibly for manual login; never handle passwords."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .config import safe_url

LOGIN_URL = "https://meli.zying.net/#/login"


def edge_executable():
    candidates = [os.environ.get("AI_WEIGHT_PRICE_EDGE_EXECUTABLE", "")]
    if sys.platform == "win32":
        for root in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA"):
            candidates.append(str(Path(os.environ.get(root, "")) / "Microsoft/Edge/Application/msedge.exe"))
    elif sys.platform == "darwin":
        candidates.append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
    else:
        candidates.extend([shutil.which("microsoft-edge") or "", shutil.which("microsoft-edge-stable") or ""])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise ValueError("未找到本机 Microsoft Edge，请先安装 Edge")


def debugger_identity(cdp_url):
    safe_url(cdp_url, local=True)
    try:
        with build_opener(ProxyHandler({})).open(cdp_url.rstrip("/") + "/json/version", timeout=1) as response:
            version = json.load(response)
    except (OSError, ValueError):
        return None
    if "Edg/" not in version.get("User-Agent", "") and "Edge/" not in version.get("Browser", ""):
        raise ValueError("该调试端口属于其他浏览器，请为 Edge 配置一个空闲端口")
    websocket = version.get("webSocketDebuggerUrl", "")
    if not websocket:
        return None
    return hashlib.sha256(websocket.encode()).hexdigest()


def open_edge(cdp_url, root):
    """Dedicated persistent Edge profile avoids closing the user's everyday windows."""
    parsed = urlsplit(safe_url(cdp_url, local=True))
    if not parsed.port or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Edge连接地址应类似 http://127.0.0.1:9222")
    current = debugger_identity(cdp_url)
    if current:
        return current
    profile = Path(root) / "edge-profile"
    profile.mkdir(parents=True, exist_ok=True)
    subprocess.Popen([edge_executable(), "--remote-debugging-address=127.0.0.1",
                      f"--remote-debugging-port={parsed.port}", f"--user-data-dir={profile.resolve()}",
                      "--no-first-run", "--new-window", LOGIN_URL],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        identity = debugger_identity(cdp_url)
        if identity:
            return identity
        time.sleep(.25)
    raise ValueError("Edge已请求打开，但连接尚未就绪，请检查窗口或端口占用后重试")
