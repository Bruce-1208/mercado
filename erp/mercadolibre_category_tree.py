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
_background_jobs: set[str] = set()


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


def _read_saved_paths(site, *, allow_expired=False):
    try:
        saved = json.loads((_CACHE_DIR / f"{site}.json").read_text(encoding="utf-8"))
        if (allow_expired or saved["expires_at"] > time.time()) and isinstance(saved["paths"], dict):
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


def _warm_category_paths(site: str, category_ids: list[str]) -> None:
    """One background refresh per site; failures preserve usable saved names."""
    with _cache_lock:
        if site in _background_jobs:
            return
        _background_jobs.add(site)

    def run():
        try:
            category_paths_for_ids(category_ids)
        except Exception:
            logging.warning("分类目录或中文名称后台刷新失败：%s", site, exc_info=True)
        finally:
            with _cache_lock:
                _background_jobs.discard(site)

    threading.Thread(target=run, name=f"category-paths-{site}", daemon=True).start()


def category_paths_for_ids(category_ids: list[str], *, fast: bool = False) -> dict[str, list[dict[str, str]]]:
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
        if fast:
            # Even an expired catalog is useful while the official API refreshes.
            saved_paths = paths if paths is not None else _read_saved_paths(site, allow_expired=True)
            site_ids = [category_id for category_id in ids if category_id.startswith(site)]
            translations = _load_translations(_site_language(site))
            if saved_paths is None:
                _warm_category_paths(site, site_ids)
                continue
            if saved_paths is not None:
                if paths is None or any(category_id not in saved_paths for category_id in site_ids) or any(
                    node.get("name") not in translations
                    for category_id in site_ids for node in saved_paths.get(category_id, [])
                ):
                    _warm_category_paths(site, site_ids)
                result.update({category_id: saved_paths[category_id] for category_id in site_ids if category_id in saved_paths})
                continue
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
        # A cached catalog can predate newly assigned or imported categories.
        # Fill those paths from the official category detail instead of silently
        # keeping them absent until the full catalog expires.
        missing_ids = sorted(category_id for category_id in ids if category_id.startswith(site) and category_id not in paths)
        if missing_ids:
            paths = dict(paths)
            for category_id in missing_ids:
                try:
                    response = requests.get(
                        f"https://api.mercadolibre.com/categories/{category_id}",
                        timeout=(3, 10), headers={"Accept": "application/json"},
                    )
                    response.raise_for_status()
                    category = response.json()
                    path = category.get("path_from_root") if isinstance(category, dict) else None
                    if isinstance(path, list) and path and all(
                        isinstance(node, dict) and node.get("id") and node.get("name") for node in path
                    ):
                        paths[category_id] = [{"id": str(node["id"]), "name": str(node["name"])} for node in path]
                except Exception:
                    logging.warning("分类层级补读失败：%s", category_id, exc_info=True)
            with _cache_lock:
                _site_cache[site] = (time.monotonic() + _CACHE_SECONDS, paths)
            _save_paths(site, paths)
        result.update({category_id: paths[category_id] for category_id in ids if category_id in paths})
    translated_by_language = {}
    for language in {_site_language(category_id[:3]) for category_id in result}:
        names = {node["name"] for category_id, path in result.items()
                 if _site_language(category_id[:3]) == language for node in path if node.get("name")}
        if fast:
            translated_by_language[language] = dict(_load_translations(language))
        else:
            try:
                translated_by_language[language] = _translate_names(language, names)
            except Exception:
                logging.warning("分类中文名称翻译失败，保留官方名称", exc_info=True)
                translated_by_language[language] = dict(_load_translations(language))
    if fast:
        for site in {category_id[:3] for category_id in result}:
            site_ids = [category_id for category_id in result if category_id.startswith(site)]
            translations = translated_by_language[_site_language(site)]
            if any(node["name"] not in translations for category_id in site_ids for node in result[category_id]):
                _warm_category_paths(site, site_ids)
    return {
        category_id: [{**node, "name_source": node["name"],
                       "name": translated_by_language[_site_language(category_id[:3])].get(node["name"]) or node["name"]}
                      for node in path]
        for category_id, path in result.items()
    }


def category_filter_ids(value: str) -> list[str]:
    """Expand a selected official node to its descendants; never truncate a branch."""
    ids = list(dict.fromkeys(value.removeprefix("tree:").split(",")))
    if not ids or len(ids) > 100 or any(not _CATEGORY_ID.fullmatch(item) for item in ids):
        raise ValueError("美客多分类列表无效")
    # Fetch catalogs only when absent. Translation is unnecessary for filtering.
    for site in {item[:3] for item in ids}:
        with _cache_lock:
            cached = _site_cache.get(site)
        paths = cached[1] if cached else _read_saved_paths(site, allow_expired=True)
        if paths is None:
            category_paths_for_ids([item for item in ids if item.startswith(site)])
    result = set(ids)
    for site in {item[:3] for item in ids}:
        with _cache_lock:
            cached = _site_cache.get(site)
        paths = cached[1] if cached else _read_saved_paths(site, allow_expired=True)
        if paths is None:
            raise ValueError("分类目录暂未就绪，请稍后重试")
        selected = set(ids)
        result.update(category_id for category_id, path in paths.items()
                      if any(node.get("id") in selected for node in path))
    if len(result) > 20000:
        raise ValueError("分类范围过大，请继续选择下一级分类")
    return sorted(result)
