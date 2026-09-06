"""Build a versioned, integrity-checked Python business bundle for agents."""

from __future__ import annotations

import hashlib
import io
import json
import threading
import zipfile
from pathlib import Path


SOURCE_DIRECTORIES = (
    "AI_Agent",
    "bit",
    "bit_playwright",
    "erp",
    "mercado_api",
    "mercado_listing",
    "playwright_appeal",
    "ziniao",
)
ROOT_SOURCE_FILES = (
    "DataAnalysis.py",
    "DataAnalysis_db.py",
    "Utils.py",
    "local_agent_worker.py",
)
_BUNDLE_LOCK = threading.Lock()
_BUNDLE_CACHE = {}


def iter_business_source_files(project_root):
    project_root = Path(project_root).resolve()
    paths = []
    for directory_name in SOURCE_DIRECTORIES:
        directory = project_root / directory_name
        if not directory.is_dir():
            continue
        paths.extend(
            path
            for path in directory.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    paths.extend(
        path for name in ROOT_SOURCE_FILES if (path := project_root / name).is_file()
    )
    return sorted(set(paths), key=lambda path: path.relative_to(project_root).as_posix())


def business_source_version(project_root):
    project_root = Path(project_root).resolve()
    digest = hashlib.sha256()
    files = iter_business_source_files(project_root)
    for path in files:
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:24], files


def build_business_bundle(project_root):
    project_root = Path(project_root).resolve()
    version, files = business_source_version(project_root)
    with _BUNDLE_LOCK:
        cached = _BUNDLE_CACHE.get(version)
        if cached is not None:
            return cached
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            file_names = []
            for path in files:
                relative = path.relative_to(project_root).as_posix()
                file_names.append(relative)
                archive.writestr(relative, path.read_bytes())
            archive.writestr(
                "bundle-manifest.json",
                json.dumps(
                    {"version": version, "files": file_names},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8"),
            )
        content = buffer.getvalue()
        result = {
            "version": version,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
            "content": content,
        }
        _BUNDLE_CACHE.clear()
        _BUNDLE_CACHE[version] = result
        return result
