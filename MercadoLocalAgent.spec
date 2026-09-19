# -*- mode: python ; coding: utf-8 -*-

import sys

from PyInstaller.utils.hooks import collect_submodules


# local_agent.py 本身保持稳定；把 worker 加入分析仅用于一次性收集业务运行依赖。
# Agent 真正执行时会把服务器下载的 release 放在 sys.path 最前面，因此业务
# 模块仍来自最新 release，而不是这里随 EXE 收集的构建时副本。
hiddenimports = sorted(
    set(collect_submodules("requests") + ["local_agent_worker"])
)

# ``bit_interface`` exposes both the Windows Agent routes and server-only
# publishing/translation routes.  PyInstaller follows imports inside all of
# those functions, even though the Agent never executes them.  In particular,
# Argos pulls the full Stanza/spaCy/Torch NLP stack into the executable, and
# Pandas' optional plotting modules pull Matplotlib.  Keep the browser, Excel,
# OCR and ONNX dependencies used by Agent jobs, but omit these server/dev-only
# dependency families.
agent_excludes = [
    "_pytest",
    "argostranslate",
    "ctranslate2",
    "matplotlib",
    "pytest",
    "spacy",
    "srsly",
    "stanza",
    "sympy",
    "thinc",
    "torch",
    "torchaudio",
    "torchvision",
    "triton",
]

analysis = Analysis(
    ["local_agent.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests", *agent_excludes],
    noarchive=False,
    optimize=0,
)

# Selenium's hook ships driver-manager executables for every desktop OS.  Keep
# only the binary matching the platform that is building this Agent.
native_selenium_platform = (
    "windows" if sys.platform == "win32" else "macos" if sys.platform == "darwin" else "linux"
)


def keep_native_selenium_manager(item):
    destination = item[0].replace("/", "\\")
    prefix = "selenium\\webdriver\\common\\"
    return not destination.startswith(prefix) or destination.startswith(
        f"{prefix}{native_selenium_platform}\\"
    )


analysis.datas = [item for item in analysis.datas if keep_native_selenium_manager(item)]
analysis.binaries = [item for item in analysis.binaries if keep_native_selenium_manager(item)]
pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="MercadoLocalAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    # The Agent owns a Tk status window.  Building it as a Windows GUI
    # executable prevents a second console window from being created.
    console=False,
)
