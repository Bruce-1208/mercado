"""Download Mercado Libre's category dump and extract required attributes.

The official ``withAttributes`` dump is intentionally processed one category
at a time.  This avoids loading the (potentially very large) decompressed JSON
document into the workbench process.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, TextIO

import requests

from erp.mercadolibre_attribute_rules import (
    is_read_only_attribute,
    is_required_attribute,
)


API_BASE_URL = "https://api.mercadolibre.com"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / ".data"
SITE_ID_PATTERN = re.compile(r"^[A-Z]{3}$")


def _site_id(value: str) -> str:
    site_id = str(value or "").strip().upper()
    if not SITE_ID_PATTERN.fullmatch(site_id):
        raise ValueError(f"无效的 Mercado Libre 站点 ID: {value!r}")
    return site_id


def _download_dump(
    site_id: str,
    destination: Path,
    *,
    access_token: str = "",
    session: requests.Session | None = None,
    timeout: int = 180,
) -> Mapping[str, str]:
    http = session or requests.Session()
    headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    response = http.get(
        f"{API_BASE_URL}/sites/{site_id}/categories/all",
        params={"withAttributes": "true"},
        headers=headers,
        timeout=timeout,
        stream=True,
    )
    try:
        response.raise_for_status()
        # Keep the wire-level gzip stream. requests would otherwise expand the
        # full dump while copying it to disk.
        response.raw.decode_content = False
        with destination.open("wb") as stream:
            shutil.copyfileobj(response.raw, stream, length=1024 * 1024)
        return {
            "content_created": str(response.headers.get("X-Content-Created") or ""),
            "content_md5": str(response.headers.get("X-Content-MD5") or ""),
            "content_encoding": str(response.headers.get("Content-Encoding") or ""),
        }
    finally:
        response.close()


@contextmanager
def _open_dump_text(path: Path) -> Iterator[TextIO]:
    with path.open("rb") as raw:
        magic = raw.read(2)
    if magic == b"\x1f\x8b":
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            yield stream
    else:
        with path.open("rt", encoding="utf-8") as stream:
            yield stream


def iter_top_level_object(stream: TextIO, chunk_size: int = 1024 * 1024):
    """Yield key/value pairs from a top-level JSON object incrementally."""
    decoder = json.JSONDecoder()
    buffer = ""
    position = 0
    eof = False

    def compact() -> None:
        nonlocal buffer, position
        if position > chunk_size:
            buffer = buffer[position:]
            position = 0

    def refill() -> bool:
        nonlocal buffer, eof
        if eof:
            return False
        compact()
        chunk = stream.read(chunk_size)
        if not chunk:
            eof = True
            return False
        buffer += chunk
        return True

    def skip_whitespace() -> None:
        nonlocal position
        while True:
            while position < len(buffer) and buffer[position].isspace():
                position += 1
            if position < len(buffer) or not refill():
                return

    def decode_next() -> Any:
        nonlocal position
        while True:
            skip_whitespace()
            try:
                value, new_position = decoder.raw_decode(buffer, position)
            except json.JSONDecodeError:
                if refill():
                    continue
                raise
            position = new_position
            return value

    skip_whitespace()
    if position >= len(buffer) and not refill():
        raise ValueError("类目 dump 是空文件")
    skip_whitespace()
    if buffer[position] != "{":
        raise ValueError("类目 dump 顶层不是 JSON 对象")
    position += 1
    while True:
        skip_whitespace()
        if position < len(buffer) and buffer[position] == "}":
            return
        key = decode_next()
        if not isinstance(key, str):
            raise ValueError("类目 dump 包含非字符串键")
        skip_whitespace()
        if position >= len(buffer) and not refill():
            raise ValueError("类目 dump 在键后意外结束")
        skip_whitespace()
        if buffer[position] != ":":
            raise ValueError(f"类目 dump 的 {key} 后缺少冒号")
        position += 1
        yield key, decode_next()
        skip_whitespace()
        if position >= len(buffer) and not refill():
            raise ValueError("类目 dump 意外结束")
        skip_whitespace()
        delimiter = buffer[position]
        position += 1
        if delimiter == "}":
            return
        if delimiter != ",":
            raise ValueError(f"类目 dump 包含无效分隔符: {delimiter!r}")


def required_attribute_rows(category: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for definition in category.get("attributes") or []:
        if (
            not isinstance(definition, Mapping)
            or not definition.get("id")
            or not is_required_attribute(definition)
            or is_read_only_attribute(definition)
        ):
            continue
        rows.append(dict(definition))
    return rows


def build_required_attribute_catalog(
    dump_path: Path,
    output_path: Path,
    *,
    site_id: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create an atomic gzip JSONL catalog, including categories with no requirements."""
    site_id = _site_id(site_id)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_handle, temporary_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".tmp", dir=output_path.parent
    )
    os.close(temporary_handle)
    temporary_path = Path(temporary_name)
    category_count = 0
    required_category_count = 0
    required_attribute_count = 0
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        with _open_dump_text(dump_path) as source, gzip.open(
            temporary_path, "wt", encoding="utf-8"
        ) as destination:
            destination.write(json.dumps({
                "record_type": "metadata",
                "site_id": site_id,
                "generated_at": generated_at,
                **dict(metadata or {}),
            }, ensure_ascii=False) + "\n")
            for category_key, value in iter_top_level_object(source):
                if not isinstance(value, Mapping):
                    continue
                category_id = str(value.get("id") or category_key).strip().upper()
                required = required_attribute_rows(value)
                category_count += 1
                required_attribute_count += len(required)
                if required:
                    required_category_count += 1
                destination.write(json.dumps({
                    "record_type": "category",
                    "site_id": site_id,
                    "category_id": category_id,
                    "category_name": str(value.get("name") or ""),
                    "is_leaf": not bool(value.get("children_categories")),
                    "required_attributes": required,
                }, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temporary_path, output_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return {
        "site_id": site_id,
        "output_path": str(output_path.resolve()),
        "category_count": category_count,
        "required_category_count": required_category_count,
        "required_attribute_count": required_attribute_count,
        "generated_at": generated_at,
    }


def sync_required_attribute_catalog(
    site_id: str = "CBT",
    *,
    output_path: Path | None = None,
    access_token: str = "",
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Download the official dump and atomically replace the local catalog."""
    site_id = _site_id(site_id)
    output = output_path or (
        DEFAULT_OUTPUT_DIR / f"mercadolibre_required_attributes_{site_id}.jsonl.gz"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    handle, dump_name = tempfile.mkstemp(
        prefix=f"mercadolibre_categories_{site_id}_", suffix=".json.gz"
    )
    os.close(handle)
    dump_path = Path(dump_name)
    try:
        metadata = _download_dump(
            site_id,
            dump_path,
            access_token=access_token,
            session=session,
        )
        return build_required_attribute_catalog(
            dump_path, output, site_id=site_id, metadata=metadata
        )
    finally:
        dump_path.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="同步 Mercado Libre 全分类必填属性及合法枚举值"
    )
    parser.add_argument("--site", default="CBT", help="站点 ID，默认 CBT")
    parser.add_argument("--output", type=Path, help="输出 .jsonl.gz 文件")
    parser.add_argument(
        "--access-token-env",
        default="MELI_ACCESS_TOKEN",
        help="可选 access token 环境变量名",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    result = sync_required_attribute_catalog(
        args.site,
        output_path=args.output,
        access_token=os.getenv(args.access_token_env, "").strip(),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
