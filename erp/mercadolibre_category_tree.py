"""Small, on-demand category paths for the store-link filter."""

from __future__ import annotations

import re
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import requests

_CATEGORY_ID = re.compile(r"^[A-Z]{3}[0-9]+$")
_site_cache: dict[str, tuple[float, dict[str, list[dict[str, str]]]]] = {}
_cache_lock = threading.Lock()
_CACHE_SECONDS = 24 * 60 * 60
_CACHE_DIR = Path(__file__).resolve().parents[1] / "runtime_logs" / "category_paths"
_TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"
_translated_names: dict[str, dict[str, str]] = {}
_translation_lock = threading.RLock()


def _site_language(site: str) -> str:
    if site == "CBT":
        return "en"
    if site == "MLB":
        return "pt"
    return "es"


def _load_translations(language: str) -> dict[str, str]:
    if language not in _translated_names:
        try:
            data = json.loads((_CACHE_DIR / f"names_{language}_zh.json").read_text(encoding="utf-8"))
            _translated_names[language] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _translated_names[language] = {}
    return _translated_names[language]


def _translate_names(language: str, names: set[str]) -> dict[str, str]:
    """Translate official catalog labels to Chinese and keep a durable cache."""
    if language == "zh":
        return {name: name for name in names}
    with _translation_lock:
        cache = _load_translations(language)
        missing = sorted(name for name in names if name and name not in cache)
        batches: list[list[str]] = []
        batch: list[str] = []
        size = 0
        for name in missing:
            if batch and size + len(name) + 1 > 3500:
                batches.append(batch)
                batch, size = [], 0
            batch.append(name)
            size += len(name) + 1
        if batch:
            batches.append(batch)
        for batch in batches:
            response = requests.get(
                _TRANSLATE_URL,
                params={"client": "gtx", "sl": language, "tl": "zh-CN", "dt": "t", "q": "\n".join(batch)},
                timeout=30,
            )
            response.raise_for_status()
            payload = response.json()
            translated = "".join(str(part[0] or "") for part in payload[0] if isinstance(part, list) and part)
            lines = translated.splitlines()
            if len(lines) != len(batch) or any(not line.strip() for line in lines):
                raise ValueError("分类名称中文翻译结果不完整")
            cache.update(zip(batch, lines))
        if missing:
            try:
                _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                target = _CACHE_DIR / f"names_{language}_zh.json"
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=_CACHE_DIR, delete=False) as stream:
                    temporary = stream.name
                    json.dump(cache, stream, ensure_ascii=False)
                os.replace(temporary, target)
            except OSError:
                logging.warning("分类中文名称缓存保存失败", exc_info=True)
                if "temporary" in locals() and os.path.exists(temporary):
                    os.unlink(temporary)
        return {name: cache.get(name, "") for name in names}


def _read_saved_paths(site):
    try:
        saved = json.loads((_CACHE_DIR / f"{site}.json").read_text(encoding="utf-8"))
        if saved["expires_at"] > time.time() and isinstance(saved["paths"], dict):
            return saved["paths"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _save_paths(site, paths):
    temporary = None
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=_CACHE_DIR, delete=False) as stream:
            temporary = stream.name
            json.dump({"expires_at": time.time() + _CACHE_SECONDS, "paths": paths}, stream, ensure_ascii=False)
        os.replace(temporary, _CACHE_DIR / f"{site}.json")
    except OSError:
        logging.warning("分类路径缓存保存失败", exc_info=True)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def category_paths_for_ids(category_ids: list[str]) -> dict[str, list[dict[str, str]]]:
    """Return official paths without loading a site catalog during page startup."""
    ids = {str(value).strip().upper() for value in category_ids}
    ids = {value for value in ids if _CATEGORY_ID.fullmatch(value)}
    if len(ids) > 20000:
        raise ValueError("分类数量过多")
    result: dict[str, list[dict[str, str]]] = {}
    for site in sorted({value[:3] for value in ids}):
        with _cache_lock:
            cached = _site_cache.get(site)
            if cached and cached[0] > time.monotonic():
                paths = cached[1]
            else:
                paths = None
        if paths is None:
            paths = _read_saved_paths(site)
            if paths is not None:
                with _cache_lock:
                    _site_cache[site] = (time.monotonic() + _CACHE_SECONDS, paths)
        if paths is None:
            response = requests.get(
                f"https://api.mercadolibre.com/sites/{site}/categories/all",
                timeout=30,
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            catalog: Any = response.json()
            if not isinstance(catalog, dict):
                raise ValueError("美客多分类目录格式无效")
            paths = {}
            for category_id, category in catalog.items():
                if not isinstance(category, dict):
                    continue
                path = category.get("path_from_root")
                if not isinstance(path, list):
                    continue
                nodes = [
                    {"id": str(node.get("id") or ""), "name": str(node.get("name") or "")}
                    for node in path if isinstance(node, dict) and node.get("id")
                ]
                if nodes:
                    paths[str(category_id)] = nodes
            with _cache_lock:
                _site_cache[site] = (time.monotonic() + _CACHE_SECONDS, paths)
            _save_paths(site, paths)
        result.update({category_id: paths[category_id] for category_id in ids if category_id in paths})
    source_names = {
        str(node.get("name") or "")
        for path in result.values()
        for node in path
        if node.get("name")
    }
    languages = {_site_language(category_id[:3]) for category_id in result}
    if len(languages) == 1:
        translations = _translate_names(next(iter(languages)), source_names)
        return {
            category_id: [{**node, "name_source": node["name"], "name": translations.get(node["name"], "")}
                          for node in path]
            for category_id, path in result.items()
        }
    translated_by_language = {
        language: _translate_names(language, {
            str(node.get("name") or "") for category_id, path in result.items()
            if _site_language(category_id[:3]) == language for node in path if node.get("name")
        })
        for language in languages
    }
    return {
        category_id: [{**node, "name_source": node["name"], "name": translated_by_language[_site_language(category_id[:3])].get(node["name"], "")}
                      for node in path]
        for category_id, path in result.items()
    }
