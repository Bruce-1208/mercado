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
_BUNDLE_HISTORY_LIMIT = 5
_SOURCE_CACHE_LOCK = threading.Lock()
_SOURCE_CACHE = {}


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
    # Metadata reads are cheap; hash file contents only after a source change.
    # ctime catches replacements even when a deployment preserves mtime/size.
    signature = tuple(
        (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        for path in files for stat in (path.stat(),)
    )
    with _SOURCE_CACHE_LOCK:
        cached = _SOURCE_CACHE.get(str(project_root))
        if cached and cached[0] == signature:
            return cached[1], files
    for path in files:
        relative = path.relative_to(project_root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    version = digest.hexdigest()[:24]
    with _SOURCE_CACHE_LOCK:
        _SOURCE_CACHE[str(project_root)] = (signature, version)
        if len(_SOURCE_CACHE) > 8:
            del _SOURCE_CACHE[next(iter(_SOURCE_CACHE))]
    return version, files


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
        _BUNDLE_CACHE[version] = result
        # Keep a small in-process release history so a rolling deployment can
        # serve a known-good bundle while operators investigate a bad publish.
        # The Agent also keeps two local releases, so this is a server-side
        # safety net rather than the only rollback mechanism.
        for stale_version in list(_BUNDLE_CACHE)[:-_BUNDLE_HISTORY_LIMIT]:
            _BUNDLE_CACHE.pop(stale_version, None)
        return result


def cached_business_bundle(version=""):
    """Return a previously built release, or ``None`` when it is unavailable."""
    with _BUNDLE_LOCK:
        return _BUNDLE_CACHE.get(str(version or "").strip())


def cached_business_versions():
    with _BUNDLE_LOCK:
        return tuple(_BUNDLE_CACHE)
