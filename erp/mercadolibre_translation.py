"""Translate Mercado Libre listing text between Spanish and Brazilian Portuguese."""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Iterable, Mapping


MARKETPLACE_SITES = {
    "MLM": {"name": "墨西哥", "language": "es"},
    "MLB": {"name": "巴西", "language": "pt-BR"},
    "MLA": {"name": "阿根廷", "language": "es"},
    "MLC": {"name": "智利", "language": "es"},
    "MCO": {"name": "哥伦比亚", "language": "es"},
    "MLU": {"name": "乌拉圭", "language": "es"},
}
LANGUAGE_NAMES = {
    "es": "拉丁美洲西班牙语",
    "pt-BR": "巴西葡萄牙语",
}
PROTECTED_ATTRIBUTE_IDS = {
    "BRAND",
    "GTIN",
    "MPN",
    "MODEL",
    "SELLER_SKU",
    "SKU",
    "ITEM_CONDITION",
    "EMPTY_GTIN_REASON",
}
BatchTranslator = Callable[[list[str], str, str], list[str]]


class ListingTranslationError(RuntimeError):
    """Listing text could not be translated safely."""


def normalize_marketplace_site(site_id: Any) -> str:
    normalized = str(site_id or "").strip().upper()
    if normalized not in MARKETPLACE_SITES:
        supported = "、".join(
            f"{details['name']}({key})" for key, details in MARKETPLACE_SITES.items()
        )
        raise ValueError(f"不支持的目标站点 {normalized or '(empty)'}；可选：{supported}")
    return normalized


def marketplace_site_name(site_id: Any) -> str:
    normalized = normalize_marketplace_site(site_id)
    return str(MARKETPLACE_SITES[normalized]["name"])


def marketplace_language(site_id: Any) -> str | None:
    normalized = str(site_id or "").strip().upper()
    details = MARKETPLACE_SITES.get(normalized)
    return str(details["language"]) if details else None


def _extract_json_object(value: str) -> Mapping[str, Any]:
    raw = str(value or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise ListingTranslationError("翻译服务没有返回有效 JSON")
        try:
            decoded = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ListingTranslationError("翻译服务返回的 JSON 无法解析") from exc
    if not isinstance(decoded, Mapping):
        raise ListingTranslationError("翻译服务返回格式错误")
    return decoded


def _deepseek_batch_translate(
    texts: list[str], source_language: str, target_language: str
) -> list[str]:
    if not texts:
        return []
    try:
        from AI_Agent.deepseek import chat_deepseek

        response = chat_deepseek(
            [
                {
                    "role": "system",
                    "content": (
                        "你是 Mercado Libre 跨境电商翻译器。只做准确翻译，不扩写、不删减，"
                        "保留品牌、型号、人物名、数字、单位、SKU 和 HTML 以外的原有结构。"
                        "返回且只返回 JSON：{\"translations\":[\"...\"]}，顺序和数量必须与输入一致。"
                    ),
                },
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "source_language": LANGUAGE_NAMES[source_language],
                            "target_language": LANGUAGE_NAMES[target_language],
                            "texts": texts,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0,
            max_tokens=4096,
        )
    except ListingTranslationError:
        raise
    except Exception as exc:
        raise ListingTranslationError(f"自动翻译服务调用失败: {exc}") from exc
    decoded = _extract_json_object(response)
    translations = decoded.get("translations")
    if not isinstance(translations, list) or len(translations) != len(texts):
        raise ListingTranslationError("翻译结果数量与原文不一致")
    result = [str(value or "").strip() for value in translations]
    if any(not value for value in result):
        raise ListingTranslationError("翻译结果包含空文本")
    return result


def _translatable_attribute(attribute: Mapping[str, Any]) -> bool:
    attribute_id = str(attribute.get("id") or "").upper()
    if (
        not attribute_id
        or attribute_id in PROTECTED_ATTRIBUTE_IDS
        or attribute_id.startswith(("PACKAGE_", "SELLER_PACKAGE_"))
        or attribute.get("value_id") not in (None, "")
    ):
        return False
    value = str(attribute.get("value_name") or "").strip()
    return bool(value and re.search(r"[A-Za-zÀ-ÿ]", value))


def _attribute_collections(source: Mapping[str, Any]) -> Iterable[list[dict[str, Any]]]:
    attributes = source.get("attributes")
    if isinstance(attributes, list):
        yield attributes
    for variation in source.get("variations") or []:
        if not isinstance(variation, Mapping):
            continue
        for key in ("attribute_combinations", "attributes"):
            values = variation.get(key)
            if isinstance(values, list):
                yield values


def translate_listing_content(
    source: Mapping[str, Any],
    description: Mapping[str, Any],
    *,
    destination_site_id: str,
    translator: BatchTranslator | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return translated copies and metadata; leave same-language listings unchanged."""
    destination = normalize_marketplace_site(destination_site_id)
    translated_source = copy.deepcopy(dict(source))
    translated_description = copy.deepcopy(dict(description))
    item_site = str(source.get("id") or "")[:3].upper()
    declared_site = str(source.get("site_id") or "")[:3].upper()
    source_site = item_site if item_site in MARKETPLACE_SITES else declared_site
    source_language = marketplace_language(source_site)
    target_language = marketplace_language(destination)
    metadata = {
        "source_site_id": source_site,
        "destination_site_id": destination,
        "source_language": source_language or "",
        "target_language": target_language or "",
        "translated": False,
        "translated_field_count": 0,
    }
    if not source_language or source_language == target_language:
        return translated_source, translated_description, metadata

    texts: list[str] = []
    setters: list[Callable[[str], None]] = []

    title = str(translated_source.get("title") or "").strip()
    if title:
        texts.append(title)
        setters.append(lambda value: translated_source.__setitem__("title", value))

    for key in ("plain_text", "text"):
        description_text = str(translated_description.get(key) or "").strip()
        if description_text:
            texts.append(description_text)
            setters.append(
                lambda value, field=key: translated_description.__setitem__(field, value)
            )
            break

    for attributes in _attribute_collections(translated_source):
        for attribute in attributes:
            if not isinstance(attribute, dict) or not _translatable_attribute(attribute):
                continue
            texts.append(str(attribute["value_name"]).strip())
            setters.append(
                lambda value, target=attribute: target.__setitem__("value_name", value)
            )

    if not texts:
        return translated_source, translated_description, metadata
    translated_values = (translator or _deepseek_batch_translate)(
        texts, source_language, str(target_language)
    )
    if not isinstance(translated_values, list) or len(translated_values) != len(texts):
        raise ListingTranslationError("翻译结果数量与原文不一致")
    for setter, value in zip(setters, translated_values):
        translated = str(value or "").strip()
        if not translated:
            raise ListingTranslationError("翻译结果包含空文本")
        setter(translated)
    metadata["translated"] = bool(texts)
    metadata["translated_field_count"] = len(texts)
    return translated_source, translated_description, metadata


__all__ = [
    "BatchTranslator",
    "ListingTranslationError",
    "MARKETPLACE_SITES",
    "marketplace_language",
    "marketplace_site_name",
    "normalize_marketplace_site",
    "translate_listing_content",
]
