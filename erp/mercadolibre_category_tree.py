"""Small, on-demand category paths for the store-link filter."""

from __future__ import annotations

import re
import threading
import time
from typing import Any

import requests

_CATEGORY_ID = re.compile(r"^[A-Z]{3}[0-9]+$")
_site_cache: dict[str, tuple[float, dict[str, list[dict[str, str]]]]] = {}
_cache_lock = threading.Lock()
_CACHE_SECONDS = 24 * 60 * 60


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
        result.update({category_id: paths[category_id] for category_id in ids if category_id in paths})
    return result
