"""1688 -> Mercado Libre AI-original product preparation.

The browser extension only captures facts that are visible on 1688.  This
module owns the irreversible preparation step: generate a separate marketplace
listing (attributes, titles and descriptions), enforce the title policy, and
create an AI-edited white-background first image.
"""

from __future__ import annotations

import json
import os
import re
import base64
import threading
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[1]
IMAGE_DIR = Path(
    os.environ.get("AI_ORIGINAL_IMAGE_DIR")
    or PROJECT_ROOT / ".data" / "ai-original-images"
)
IMAGE_BASE_URL = str(
    os.environ.get("AI_ORIGINAL_IMAGE_BASE_URL") or "http://127.0.0.1:5000"
).rstrip("/")
MAX_TITLE_CHARS = 60
LOCAL_BACKGROUND_MODEL = "isnet-general-use"
WHITE_BACKGROUND_METHODS = frozenset({"ai_image_edit", "local_background_removal"})
_background_removal_session = None
_background_removal_session_lock = threading.Lock()


def extract_1688_item_id(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"/(?:offer/)?(\d{5,})(?:\.html)?(?:[?#/]|$)", text)
    if not match:
        match = re.search(r"(?:offerId|offer_id|itemId)=(\d{5,})", text, re.I)
    if not match and re.fullmatch(r"\d{5,}", text):
        return text[:28]
    return match.group(1)[:28] if match else ""


def _normalize_1688_variations(value: Any) -> list[dict[str, Any]]:
    """Keep the supplier SKU matrix in a stable, editable JSON shape."""
    if not isinstance(value, list):
        return []
    rows: list[dict[str, Any]] = []
    for raw in value[:200]:
        if not isinstance(raw, Mapping):
            continue
        # Do not discard supplier-specific keys: 1688 has emitted several
        # different SKU shapes over time and the editor can round-trip them.
        item = dict(raw)
        for key in ("attribute_combinations", "attributes", "properties"):
            if isinstance(item.get(key), list):
                item[key] = [dict(entry) for entry in item[key][:30]
                             if isinstance(entry, Mapping)]
        rows.append(item)
    return rows


def normalize_1688_product(product: Mapping[str, Any]) -> dict[str, Any]:
    row = dict(product or {})
    source_url = str(row.get("source_url") or row.get("final_url") or "").strip()
    host = (urlparse(source_url).hostname or "").lower()
    item_id = extract_1688_item_id(row.get("source_item_id") or source_url)
    title = re.sub(r"\s+", " ", str(row.get("title") or "")).strip()
    if not item_id or not (host == "1688.com" or host.endswith(".1688.com")):
        raise ValueError("未识别到有效的 1688 商品详情链接")
    if not title:
        raise ValueError("1688 商品标题不能为空")
    images = []
    for raw in row.get("images") or []:
        url = str(raw.get("url") if isinstance(raw, Mapping) else raw or "").strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith(("http://", "https://")) and url not in images:
            images.append(url)
    main_image = str(row.get("main_image_url") or (images[0] if images else "")).strip()
    if main_image.startswith("//"):
        main_image = "https:" + main_image
    if main_image and main_image not in images:
        images.insert(0, main_image)
    properties = row.get("properties") if isinstance(row.get("properties"), list) else []
    variations = row.get("variations")
    if not isinstance(variations, list):
        variations = row.get("skus")
    description = str(row.get("description_text") or row.get("description") or "").strip()
    return {
        "source_item_id": f"1688{item_id}",
        "source_1688_item_id": item_id,
        "source_url": source_url[:1500],
        "title": title[:255],
        "price": row.get("price"),
        "currency_id": "CNY",
        "main_image_url": main_image[:1500],
        "images": images[:20],
        "properties": properties[:100],
        "variations": _normalize_1688_variations(variations),
        "description_text": description[:50000],
        "category_id": str(row.get("category_id") or "").strip()[:64],
        "category_name": str(row.get("category_name") or row.get("category") or "").strip()[:255],
        "scrape_status": str(row.get("scrape_status") or "ok").strip()[:32],
        "error_message": str(row.get("error_message") or "").strip()[:1000],
        "weight_g": row.get("weight_g"),
        "package_length_cm": row.get("package_length_cm"),
        "package_width_cm": row.get("package_width_cm"),
        "package_height_cm": row.get("package_height_cm"),
        "collected_at": str(row.get("collected_at") or "")[:64],
    }


def _json_object(text: str) -> dict[str, Any]:
    value = str(text or "").strip()
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", value, flags=re.I)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("AI 未返回 JSON 格式的商品文案")
    decoded = json.loads(value[start : end + 1])
    if not isinstance(decoded, dict):
        raise ValueError("AI 商品文案格式无效")
    return decoded


def _brand_candidates(product: Mapping[str, Any], generated: Mapping[str, Any]) -> list[str]:
    candidates = []
    brand_keys = ("品牌", "brand", "marca", "商标")
    for prop in product.get("properties") or []:
        if not isinstance(prop, Mapping):
            continue
        name = str(prop.get("name") or prop.get("key") or "").strip().lower()
        if any(key in name for key in brand_keys):
            candidates.append(str(prop.get("value") or "").strip())
    raw_terms = generated.get("brand_terms") or []
    if isinstance(raw_terms, str):
        raw_terms = re.split(r"[,，;/]", raw_terms)
    candidates.extend(str(value or "").strip() for value in raw_terms)
    ignored = {"", "无", "无品牌", "其他", "other", "generic", "oem", "none"}
    return sorted({value for value in candidates if value.lower() not in ignored}, key=len, reverse=True)


def _clean_title(value: Any, brands: list[str]) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip(" -–—,，.;；")
    for brand in brands:
        title = re.sub(re.escape(brand), "", title, flags=re.I)
    title = re.sub(r"\s+", " ", title).strip(" -–—,，.;；")
    if len(title) > MAX_TITLE_CHARS:
        clipped = title[:MAX_TITLE_CHARS]
        if " " in clipped and len(clipped.rsplit(" ", 1)[0]) >= 35:
            clipped = clipped.rsplit(" ", 1)[0]
        title = clipped.rstrip(" -–—,，.;；")
    if not title:
        raise ValueError("AI 生成的标题为空")
    if len(title) > MAX_TITLE_CHARS:
        raise ValueError("AI 生成的标题超过 60 个字符")
    for brand in brands:
        if brand and brand.casefold() in title.casefold():
            raise ValueError(f"AI 标题仍包含品牌词：{brand}")
    return title


def _normalize_ai_attributes(value: Any) -> list[dict[str, str]]:
    """Normalize AI-produced listing attributes without inventing empty values.

    Mercado's category API ultimately resolves the stable attribute IDs.  The
    model therefore returns a human-readable name/value pair (and may return a
    best-effort ID), which lets the publisher map localized names to the target
    category schema while keeping the generated listing self-contained.
    """
    if isinstance(value, Mapping):
        value = [
            {"name": name, "value_name": raw}
            for name, raw in value.items()
        ]
    if not isinstance(value, list):
        return []
    attributes: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value, start=1):
        if isinstance(raw, Mapping):
            name = str(
                raw.get("name")
                or raw.get("name_es")
                or raw.get("name_pt")
                or raw.get("label")
                or raw.get("attribute")
                or ""
            ).strip()
            value_name = str(
                raw.get("value_name")
                or raw.get("value_name_es")
                or raw.get("value_name_pt")
                or raw.get("value")
                or raw.get("text")
                or raw.get("valueName")
                or ""
            ).strip()
            raw_id = str(raw.get("id") or raw.get("attribute_id") or "").strip()
            value_id = str(raw.get("value_id") or "").strip()
        else:
            continue
        if not name or not value_name:
            continue
        dedupe_key = f"{name.casefold()}\x00{value_name.casefold()}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        attribute_id = re.sub(r"[^A-Za-z0-9_]+", "_", raw_id.upper()).strip("_")
        if not attribute_id:
            attribute_id = re.sub(r"[^A-Za-z0-9_]+", "_", name.upper()).strip("_")
        if not attribute_id:
            attribute_id = f"AI_ATTRIBUTE_{index}"
        item = {
            "id": attribute_id[:80],
            "name": name[:120],
            "value_name": value_name[:240],
        }
        for key in ("name_es", "name_pt", "value_name_es", "value_name_pt"):
            translated = str(raw.get(key) or "").strip() if isinstance(raw, Mapping) else ""
            if translated:
                item[key] = translated[:240]
        if value_id and value_id not in {"-1", "none", "null"}:
            item["value_id"] = value_id[:80]
        attributes.append(item)
        if len(attributes) >= 50:
            break
    return attributes


def build_copy_prompt(product: Mapping[str, Any]) -> str:
    facts = {
        "中文标题": product.get("title"),
        "商品属性": product.get("properties") or [],
        "原始详情摘要": str(product.get("description_text") or "")[:6000],
        "原始类目": product.get("category_name") or product.get("category_id") or "",
    }
    return (
        "你是 Mercado Libre 拉美电商文案专家。根据 1688 商品事实生成全新、准确、"
        "不侵权的刊登文案。不要照抄原详情，不要编造规格。标题不得出现任何品牌、商标、"
        "店铺名、厂家名或 OEM 字样；西班牙语和巴西葡萄牙语标题各不超过 60 个字符。"
        "详情分别用自然的拉美西班牙语和巴西葡萄牙语重写，包含卖点、规格、包装内容和"
        "使用提示，但不要使用 HTML。还要生成可以用于 Mercado Libre 刊登的商品属性，"
        "只填写源商品事实明确支持的属性；不确定的属性不要猜。属性使用数组，每项包含"
        "name_es、name_pt、value_name_es、value_name_pt（无法翻译时也要保留 name、value_name），"
        "可选 id、value_id。必须返回 JSON，字段为 title_es、title_pt、"
        "description_es、description_pt、attributes、brand_terms（识别到的品牌词数组）。\n"
        + json.dumps(facts, ensure_ascii=False)
    )


def generate_marketplace_copy(
    product: Mapping[str, Any],
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    chat: Callable[..., str] | None = None,
) -> dict[str, Any]:
    if chat is None:
        from AI_Agent.deepseek import chat_deepseek

        chat = chat_deepseek
    response = chat(
        [{"role": "user", "content": build_copy_prompt(product)}],
        api_key=str(api_key or "").strip() or None,
        model=str(model or "").strip() or None,
        base_url=str(base_url or "").strip() or None,
        temperature=0.25,
        max_tokens=2400,
        response_format={"type": "json_object"},
    )
    generated = _json_object(response)
    brands = _brand_candidates(product, generated)
    title_es = _clean_title(generated.get("title_es"), brands)
    title_pt = _clean_title(generated.get("title_pt"), brands)
    description_es = str(generated.get("description_es") or "").strip()
    description_pt = str(generated.get("description_pt") or "").strip()
    attributes = _normalize_ai_attributes(generated.get("attributes"))
    if not description_es or not description_pt:
        raise ValueError("AI 必须同时生成西班牙语和葡萄牙语详情")
    original_description = re.sub(
        r"\s+", " ", str(product.get("description_text") or "")
    ).strip().casefold()
    if original_description and any(
        re.sub(r"\s+", " ", value).strip().casefold() == original_description
        for value in (description_es, description_pt)
    ):
        raise ValueError("AI 返回了原始详情，必须重新生成原创描述")
    return {
        "title_es": title_es,
        "title_pt": title_pt,
        "description_es": description_es[:50000],
        "description_pt": description_pt[:50000],
        "attributes": attributes,
    }


def _local_background_removal_session():
    """Create one reusable local ONNX session, downloading weights once."""
    global _background_removal_session
    if _background_removal_session is not None:
        return _background_removal_session
    with _background_removal_session_lock:
        if _background_removal_session is not None:
            return _background_removal_session
        try:
            from rembg import new_session
        except ImportError as exc:
            raise RuntimeError(
                "缺少本地白底图依赖；请运行 python -m pip install "
                "-r bit/requirements-server.txt"
            ) from exc
        try:
            _background_removal_session = new_session(LOCAL_BACKGROUND_MODEL)
        except Exception as exc:
            raise RuntimeError(
                f"本地白底图模型 {LOCAL_BACKGROUND_MODEL} 初始化失败：{exc}"
            ) from exc
    return _background_removal_session


def _remove_background_locally(image):
    """Return an RGBA cutout without using a paid or remote inference API."""
    try:
        from rembg import remove
    except ImportError as exc:
        raise RuntimeError(
            "缺少本地白底图依赖；请运行 python -m pip install "
            "-r bit/requirements-server.txt"
        ) from exc
    return remove(image, session=_local_background_removal_session())


def create_white_background_image(
    source_url: str,
    item_id: str,
    *,
    image_dir: Path | None = None,
    http_get: Callable[..., Any] | None = None,
    background_remove: Callable[[Any], Any] | None = None,
) -> tuple[Path, str]:
    """Remove the background locally and place the product on a white square."""
    from PIL import Image, ImageOps

    parsed_source = urlparse(str(source_url or ""))
    source_host = (parsed_source.hostname or "").lower()
    if parsed_source.scheme not in {"http", "https"} or not (
        source_host == "1688.com"
        or source_host.endswith(".1688.com")
        or source_host == "alicdn.com"
        or source_host.endswith(".alicdn.com")
    ):
        raise ValueError("1688 商品缺少可用的首图")
    response = (http_get or requests.get)(
        source_url,
        timeout=45,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://detail.1688.com/",
        },
    )
    response.raise_for_status()
    if len(response.content) > 15 * 1024 * 1024:
        raise ValueError("1688 首图超过 15 MB")
    with Image.open(BytesIO(response.content)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    max_side = max(image.size)
    if max_side > 1600:
        scale = 1600 / max_side
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    removed = (background_remove or _remove_background_locally)(image)
    if isinstance(removed, (bytes, bytearray)):
        with Image.open(BytesIO(bytes(removed))) as opened:
            cutout = ImageOps.exif_transpose(opened).convert("RGBA")
    elif hasattr(removed, "convert"):
        cutout = ImageOps.exif_transpose(removed).convert("RGBA")
    else:
        raise ValueError("本地白底图模型返回格式无效")
    bounds = cutout.getchannel("A").getbbox()
    if not bounds:
        raise ValueError("本地白底图模型未识别到商品主体")
    cutout = cutout.crop(bounds)
    canvas_size = 1024
    cutout.thumbnail(
        (round(canvas_size * 0.9), round(canvas_size * 0.9)),
        Image.Resampling.LANCZOS,
    )
    canvas = Image.new("RGB", (canvas_size, canvas_size), "white")
    position = (
        (canvas_size - cutout.width) // 2,
        (canvas_size - cutout.height) // 2,
    )
    canvas.paste(cutout.convert("RGB"), position, cutout.getchannel("A"))
    target_dir = Path(image_dir or IMAGE_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"1688-{extract_1688_item_id(item_id) or re.sub(r'\W+', '', item_id)}-ai-white.jpg"
    path = target_dir / filename
    canvas.save(path, format="JPEG", quality=94, optimize=True)
    return path, f"{IMAGE_BASE_URL}/api/ai-original-products/images/{filename}"


def generate_ai_white_background_image(
    source_url: str,
    item_id: str,
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
    image_generate: Callable[..., Any] | None = None,
    image_dir: Path | None = None,
    http_get: Callable[..., Any] | None = None,
) -> tuple[Path, str]:
    """Generate a new square white-background image through an image model.

    The endpoint follows the OpenAI-compatible ``/images/edits`` contract. A
    callback is supported for local providers and tests. The returned asset is
    still normalized into a square JPEG so Mercado receives a predictable file.
    """
    from PIL import Image, ImageOps

    parsed_source = urlparse(str(source_url or ""))
    source_host = (parsed_source.hostname or "").lower()
    if parsed_source.scheme not in {"http", "https"} or not (
        source_host == "1688.com"
        or source_host.endswith(".1688.com")
        or source_host == "alicdn.com"
        or source_host.endswith(".alicdn.com")
    ):
        raise ValueError("1688 商品缺少可用的 AI 主图输入")
    getter = http_get or requests.get
    source_response = getter(
        source_url,
        timeout=45,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://detail.1688.com/",
        },
    )
    source_response.raise_for_status()
    source_bytes = bytes(source_response.content or b"")
    if not source_bytes or len(source_bytes) > 15 * 1024 * 1024:
        raise ValueError("1688 主图无效或超过 15 MB")

    generated: Any = None
    if image_generate is not None:
        generated = image_generate(
            source_url=source_url,
            item_id=item_id,
            source_bytes=source_bytes,
        )
    else:
        key = str(api_key or "").strip()
        endpoint_base = str(base_url or "").strip().rstrip("/")
        image_model = str(model or "").strip()
        if not key or not endpoint_base or not image_model:
            raise ValueError("请配置 AI 图片模型 Token、地址和模型后再生成白底主图")
        response = requests.post(
            endpoint_base + "/images/edits",
            headers={"Authorization": f"Bearer {key}"},
            files={"image": ("1688-source.jpg", source_bytes, "image/jpeg")},
            data={
                "model": image_model,
                "prompt": (
                    "Create a new commercial product main image from this reference. "
                    "Keep only the product facts and shape, remove logos, brands and text, "
                    "place one product centered on a pure white background, no watermark, "
                    "no collage, no extra objects, square marketplace photo."
                ),
                "size": "1024x1024",
                "response_format": "b64_json",
            },
            timeout=180,
        )
        response.raise_for_status()
        payload = response.json() if hasattr(response, "json") else {}
        first = (payload.get("data") or [{}])[0]
        generated = first.get("b64_json") or first.get("url")
    if not generated:
        raise ValueError("AI 图片模型没有返回图片")
    if isinstance(generated, str) and generated.startswith("data:"):
        generated = generated.split(",", 1)[-1]
    if isinstance(generated, str) and generated.startswith(("http://", "https://")):
        image_response = getter(generated, timeout=60)
        image_response.raise_for_status()
        generated = image_response.content
    elif isinstance(generated, str):
        try:
            generated = base64.b64decode(generated, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("AI 图片模型返回格式无效") from exc
    if not isinstance(generated, (bytes, bytearray)):
        raise ValueError("AI 图片模型返回格式无效")

    with Image.open(BytesIO(bytes(generated))) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    canvas_size = max(1024, max(image.size))
    canvas = Image.new("RGB", (canvas_size, canvas_size), "white")
    image.thumbnail((round(canvas_size * 0.9), round(canvas_size * 0.9)), Image.Resampling.LANCZOS)
    canvas.paste(image, ((canvas_size - image.width) // 2, (canvas_size - image.height) // 2))
    target_dir = Path(image_dir or IMAGE_DIR)
    target_dir.mkdir(parents=True, exist_ok=True)
    filename = f"1688-{extract_1688_item_id(item_id) or re.sub(r'\W+', '', item_id)}-ai-white.jpg"
    path = target_dir / filename
    canvas.save(path, format="JPEG", quality=95, optimize=True)
    return path, f"{IMAGE_BASE_URL}/api/ai-original-products/images/{filename}"


def prepare_ai_original_product(
    row: Mapping[str, Any],
    *,
    api_key: str = "",
    model: str = "",
    base_url: str = "",
    image_api_key: str = "",
    image_base_url: str = "",
    image_model: str = "",
    image_generate: Callable[..., Any] | None = None,
    chat: Callable[..., str] | None = None,
    image_dir: Path | None = None,
    http_get: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    raw_snapshot = row.get("source_snapshot_json") or {}
    snapshot = dict(raw_snapshot) if isinstance(raw_snapshot, Mapping) else json.loads(str(raw_snapshot))
    original = dict(snapshot.get("original_1688") or {})
    if not original:
        raise ValueError("产品缺少 1688 原始快照，请重新采集")
    copy = generate_marketplace_copy(
        original, api_key=api_key, model=model, base_url=base_url, chat=chat
    )
    image_source = original.get("main_image_url") or row.get("main_image_url")
    image_item_id = original.get("source_1688_item_id") or row.get("source_item_id")
    if image_generate is not None or str(image_model or "").strip():
        _path, white_url = generate_ai_white_background_image(
            image_source,
            image_item_id,
            api_key=image_api_key or api_key,
            base_url=image_base_url or base_url,
            model=image_model,
            image_generate=image_generate,
            image_dir=image_dir,
            http_get=http_get,
        )
        image_generation_method = "ai_image_edit"
    else:
        _path, white_url = create_white_background_image(
            image_source,
            image_item_id,
            image_dir=image_dir,
            http_get=http_get,
        )
        image_generation_method = "local_background_removal"
    generated_attributes = list(copy.get("attributes") or [])
    # These are safe marketplace defaults; category-specific attributes are
    # resolved and validated against Mercado's live schema during publication.
    existing_ids = {str(item.get("id") or "").upper() for item in generated_attributes}
    if "BRAND" not in existing_ids:
        generated_attributes.insert(0, {"id": "BRAND", "name": "Brand", "value_name": "Generic"})
    if "ITEM_CONDITION" not in existing_ids:
        generated_attributes.append({"id": "ITEM_CONDITION", "name": "Condition", "value_name": "New"})
    output = {
        **copy,
        "attributes": generated_attributes,
        # Keep the collected SKU matrix available to the manual editor and
        # the final marketplace payload. Translation is applied separately so
        # prices, stock and stable value IDs are never changed by the model.
        "variations": list(original.get("variations") or []),
        "main_image_url": white_url,
        "image_generation_method": image_generation_method,
        "status": "completed",
        "error": "",
    }
    snapshot["ai_original"] = output
    snapshot["source"] = {
        "id": f"CBT{original.get('source_1688_item_id')}",
        "site_id": "CBT",
        "title": copy["title_es"],
        "price": row.get("price"),
        "currency_id": row.get("currency_id") or "USD",
        "category_id": row.get("category_id") or "",
        "category_name": row.get("category_name") or "",
        "permalink": row.get("source_url") or original.get("source_url"),
        "pictures": [{"source": white_url}] + [
            {"source": url} for url in original.get("images") or []
            if str(url).strip() and str(url).strip() != original.get("main_image_url")
        ],
        "attributes": generated_attributes,
        "variations": list(output.get("variations") or []),
    }
    snapshot["description"] = {"plain_text": copy["description_es"]}
    return {
        "title": copy["title_es"],
        "description_text": copy["description_es"],
        "main_image_url": white_url,
        "source_snapshot_json": json.dumps(snapshot, ensure_ascii=False),
        "ai_original": output,
    }


__all__ = [
    "IMAGE_DIR",
    "MAX_TITLE_CHARS",
    "create_white_background_image",
    "generate_ai_white_background_image",
    "extract_1688_item_id",
    "generate_marketplace_copy",
    "normalize_1688_product",
    "prepare_ai_original_product",
]
