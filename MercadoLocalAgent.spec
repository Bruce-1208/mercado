# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_submodules


# local_agent.py 本身保持稳定；把 worker 加入分析仅用于一次性收集业务运行依赖。
# Agent 真正执行时会把服务器下载的 release 放在 sys.path 最前面，因此业务
# 模块仍来自最新 release，而不是这里随 EXE 收集的构建时副本。
hiddenimports = sorted(
    set(collect_submodules("requests") + ["local_agent_worker"])
)

analysis = Analysis(
    ["local_agent.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tests"],
    noarchive=False,
    optimize=0,
)
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
    console=True,
)
