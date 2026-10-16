"""Translate Mercado Libre commerce text with a cached offline backend."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import threading
import time
from functools import lru_cache
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
    "auto": "自动识别原文语言",
    "zh-CN": "简体中文",
    "en": "英语",
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
_TRANSLATION_CACHE_LOCK = threading.Lock()
_TRANSLATION_CACHE: dict[str, tuple[float, list[str]]] = {}
_TRANSLATION_KEY_LOCKS: dict[str, threading.Lock] = {}
_TRANSLATION_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
_ARGOS_TRANSLATION_LOCK = threading.Lock()
_ARGOS_LANGUAGE_CODES = {
    "zh-CN": "zh",
    "en": "en",
    "es": "es",
    # Argos' direct Spanish/Portuguese model uses the ISO 639-1 code ``pt``.
    # It is preferable to an English pivot for commerce copy because it avoids
    # translating the same text twice and losing product-specific wording.
    "pt-BR": "pt",
}


class ListingTranslationError(RuntimeError):
    """Listing text could not be translated safely."""


@lru_cache(maxsize=1)
def _title_language_identifier():
    try:
        from langid.langid import LanguageIdentifier, model
    except ImportError as exc:
        raise ListingTranslationError("服务器缺少语言识别组件") from exc
    # Listing titles mix platform language with brands and character names.
    # Use a separate classifier: changing langid's global languages would also
    # change customer-message language detection and translation routing.
    identifier = LanguageIdentifier.from_modelstring(model, norm_probs=True)
    identifier.set_languages(['en', 'es', 'pt'])
    return identifier


@lru_cache(maxsize=4096)
def _listing_title_language(text):
    return _title_language_identifier().classify(text)[0]


def title_language_issue(text, language):
    """Conservative validation: keep brands/numbers, reject obvious wrong copy."""
    text = str(text or "").strip()
    if language in {"es", "pt-BR"} and re.search(r"[\u3400-\u9fff]", text):
        return "标题含中文，请在编辑器中修正目标语言后重新校验"
    if language in {"es", "pt-BR"} and len(re.findall(r"[A-Za-zÀ-ÿ]+", text)) >= 5:
        try:
            if _listing_title_language(text) == "en":
                return "标题疑似整段英文，请修正为目标语言并保留品牌、型号"
        except ListingTranslationError:
            return "标题语言无法核实，请检查语言识别服务后重试"
    return ""


def commerce_glossary(text, source, target):
    if target != "zh-CN" or source not in {"pt-BR", "auto"}:
        return None
    phrase = re.sub(r"[?？.!¡¿]", "", str(text).strip().casefold()).strip()
    return {"são de dar corda": "是上发条的吗？", "é de dar corda": "是上发条的吗？"}.get(phrase)


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


def _load_argos_translate_module():
    try:
        from argostranslate import translate as argos_translate
    except ImportError as exc:
        raise ListingTranslationError(
            "macOS 服务器未安装本地翻译组件；请执行 "
            "python3 -m pip install -r bit/requirements-server.txt"
        ) from exc
    return argos_translate


def _get_argos_translation(source_code: str, target_code: str):
    """Load one installed direct Argos model without downloading at runtime."""
    argos_translate = _load_argos_translate_module()

    # Models may be installed by the deployment script while the workbench is
    # running. Argos caches this list process-wide, so refresh it before lookup
    # to make a product-detail retry see newly installed models immediately.
    get_installed_languages = argos_translate.get_installed_languages
    clear_cache = getattr(get_installed_languages, "cache_clear", None)
    if callable(clear_cache):
        clear_cache()
    languages = {
        str(language.code): language
        for language in get_installed_languages()
    }
    source_language = languages.get(source_code)
    target_language = languages.get(target_code)
    translation = (
        source_language.get_translation(target_language)
        if source_language is not None and target_language is not None
        else None
    )
    if translation is None:
        raise ListingTranslationError(
            f"macOS 服务器未安装本地翻译模型 {source_code}→{target_code}；请执行 "
            "python3 scripts/install_argos_translation_models.py"
        )
    return translation


def _argos_batch_translate(
    texts: list[str], source_language: str, target_language: str
) -> list[str]:
    if not texts:
        return []
    source_code = _ARGOS_LANGUAGE_CODES.get(source_language)
    target_code = _ARGOS_LANGUAGE_CODES.get(target_language)
    if source_code is None or target_code is None:
        raise ListingTranslationError(
            f"本地翻译暂不支持 {source_language}→{target_language}"
        )

    # Model instances are cached internally by Argos/CTranslate2. Serializing
    # access avoids oversubscribing a macOS CPU when several publish jobs run.
    with _ARGOS_TRANSLATION_LOCK:
        try:
            if target_code == "zh" and source_code in {"es", "pt"}:
                # The supported Argos catalog exposes Spanish/Portuguese →
                # English and English → Chinese models, so use that offline
                # route for Mercado Libre's localized category names.
                first_leg = _get_argos_translation(source_code, "en")
                second_leg = _get_argos_translation("en", target_code)
                return [
                    str(second_leg.translate(first_leg.translate(text)) or "").strip()
                    for text in texts
                ]
            if source_code == "zh" and target_code in {"es", "pt"}:
                first_leg = _get_argos_translation("zh", "en")
                second_leg = _get_argos_translation("en", target_code)
                return [str(second_leg.translate(first_leg.translate(text)) or "").strip() for text in texts]
            translation = _get_argos_translation(source_code, target_code)
            return [str(translation.translate(text) or "").strip() for text in texts]
        except ListingTranslationError:
            raise
        except Exception as exc:
            raise ListingTranslationError(f"本地翻译失败: {exc}") from exc


@lru_cache(maxsize=4096)
def detect_text_language(text: str, local_language: str = "") -> str:
    try:
        import langid
    except ImportError as exc:
        raise ListingTranslationError("服务器缺少语言识别组件，请安装 requirements-server.txt") from exc
    code, _ = langid.classify(text)
    language = {"zh": "zh-CN", "pt": "pt-BR"}.get(code, code)
    if language in {"es", "pt-BR"} and local_language in {"es", "pt-BR"}:
        language = local_language
    if language not in _ARGOS_LANGUAGE_CODES:
        raise ListingTranslationError(f"暂不支持识别到的消息语言 {language}")
    return language


def _cached_default_translate(
    texts: list[str], source_language: str, target_language: str
) -> list[str]:
    if source_language == "auto":
        results = []
        for value in texts:
            source = detect_text_language(value)
            results.append(value if source == target_language else _cached_default_translate([value], source, target_language)[0])
        return results
    # Cache individual messages: an appended reply or changed batch boundary
    # must not force the entire conversation through the local models again.
    keys = [hashlib.sha256(json.dumps([source_language, target_language, text],
            ensure_ascii=False).encode("utf-8")).hexdigest() for text in texts]
    with _TRANSLATION_CACHE_LOCK:
        key_lock = _TRANSLATION_KEY_LOCKS.setdefault(
            f"{source_language}:{target_language}", threading.Lock())
    with key_lock:
        now = time.monotonic()
        found = {}
        with _TRANSLATION_CACHE_LOCK:
            for key in keys:
                cached = _TRANSLATION_CACHE.get(key)
                if cached and now - cached[0] < _TRANSLATION_CACHE_TTL_SECONDS:
                    found[key] = cached[1][0]
        missing = {key: text for key, text in zip(keys, texts) if key not in found}
        if missing:
            translated = _argos_batch_translate(list(missing.values()), source_language, target_language)
            if len(translated) != len(missing) or any(not str(value or "").strip() for value in translated):
                raise ListingTranslationError("翻译结果数量不一致或包含空文本")
            with _TRANSLATION_CACHE_LOCK:
                for key, value in zip(missing, translated):
                    found[key] = value
                    _TRANSLATION_CACHE[key] = (time.monotonic(), [value])
                while len(_TRANSLATION_CACHE) > 5000:
                    oldest = min(_TRANSLATION_CACHE, key=lambda key: _TRANSLATION_CACHE[key][0])
                    _TRANSLATION_CACHE.pop(oldest, None)
        return [found[key] for key in keys]


def translate_texts(
    texts: list[str],
    source_language: str,
    target_language: str,
    *,
    translator: BatchTranslator | None = None,
) -> list[str]:
    """Translate a bounded text batch while preserving order and item count."""
    source = str(source_language or "").strip()
    target = str(target_language or "").strip()
    if source not in LANGUAGE_NAMES or target not in LANGUAGE_NAMES:
        raise ListingTranslationError("不支持的翻译语言")
    values = [str(value or "").strip() for value in texts]
    if not values:
        return []
    if len(values) > 100:
        raise ListingTranslationError("单次最多翻译 100 条文本")
    if any(not value for value in values):
        raise ListingTranslationError("待翻译文本不能为空")
    if any(len(value) > 50000 for value in values):
        raise ListingTranslationError("单条待翻译文本不能超过 50,000 字符")
    if source == target:
        return values
    glossary = [commerce_glossary(value, source, target) for value in values]
    if all(glossary):
        return glossary
    translated_values = (
        translator(values, source, target)
        if translator is not None
        else _cached_default_translate(values, source, target)
    )
    if not isinstance(translated_values, list) or len(translated_values) != len(values):
        raise ListingTranslationError("翻译结果数量与原文不一致")
    result = [str(value or "").strip() for value in translated_values]
    result = [known or translated for known, translated in zip(glossary, result)]
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
    translated_values = translate_texts(
        texts,
        source_language,
        str(target_language),
        translator=translator,
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
    "translate_texts",
    "translate_listing_content",
]
