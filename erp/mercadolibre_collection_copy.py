"""Generate bilingual, brand-free listing copy for collected Mercado items."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping


_IGNORED_BRANDS = {
    "", "无", "无品牌", "其他", "other", "generic", "oem", "none",
    "n/a", "null", "undefined", "no aplica", "sin marca", "sem marca",
}


def _json_object(text: str) -> dict[str, Any]:
    value = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.I)
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("DeepSeek 未返回有效 JSON")
    result = json.loads(value[start : end + 1])
    if not isinstance(result, dict):
        raise ValueError("DeepSeek 返回的文案格式无效")
    return result


def _source_brand_terms(source: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("brand", "brand_name", "brandName", "marca", "品牌", "商标"):
        value = source.get(key)
        if isinstance(value, Mapping):
            value = value.get("name") or value.get("value_name") or value.get("value")
        if isinstance(value, str):
            candidates.append(value.strip())
    attributes = source.get("attributes") or source.get("properties") or []
    if isinstance(attributes, Mapping):
        attributes = [{"name": key, "value_name": value} for key, value in attributes.items()]
    if isinstance(attributes, list):
        for attribute in attributes:
            if not isinstance(attribute, Mapping):
                continue
            name = str(attribute.get("name") or attribute.get("id") or attribute.get("key") or "").casefold()
            if any(label in name for label in ("brand", "marca", "品牌", "商标")):
                value = attribute.get("value_name") or attribute.get("value") or attribute.get("name_value")
                if isinstance(value, str):
                    candidates.append(value.strip())
    return sorted(
        {value for value in candidates if value and value.casefold() not in _IGNORED_BRANDS},
        key=len,
        reverse=True,
    )


def _brand_terms(generated: Mapping[str, Any], source: Mapping[str, Any]) -> list[str]:
    values = _source_brand_terms(source)
    generated_terms = generated.get("brand_terms") or []
    if isinstance(generated_terms, str):
        generated_terms = re.split(r"[,，;/|]", generated_terms)
    if isinstance(generated_terms, list):
        values.extend(str(value or "").strip() for value in generated_terms)
    return sorted(
        {value for value in values if value and value.casefold() not in _IGNORED_BRANDS},
        key=len,
        reverse=True,
    )


def _clean_title(value: Any, brands: list[str]) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip(" -–—,，.;；")
    for brand in brands:
        # Match complete Latin brand terms while still removing CJK brand names.
        pattern = re.escape(brand)
        if re.search(r"[A-Za-z0-9]", brand):
            pattern = rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])"
        title = re.sub(pattern, " ", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip(" -–—,，.;；")


def _product_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    def decode(value: Any, default: Any):
        if isinstance(value, (dict, list)):
            return value
        try:
            result = json.loads(str(value or ""))
        except (TypeError, ValueError):
            return default
        return result if isinstance(result, type(default)) else default

    source = decode(row.get("source_json"), {})
    description = decode(row.get("description_json"), {})
    description_text = str(
        row.get("description_text")
        or (description.get("plain_text") or description.get("text") if isinstance(description, Mapping) else "")
        or ""
    ).strip()
    attributes = source.get("attributes") or source.get("properties") or []
    if isinstance(attributes, list):
        attributes = attributes[:100]
    elif isinstance(attributes, Mapping):
        attributes = dict(list(attributes.items())[:100])
    variations = []
    raw_variations = source.get("variations") or []
    if not isinstance(raw_variations, list):
        raw_variations = []
    for variation in raw_variations[:40]:
        if not isinstance(variation, Mapping):
            continue
        variations.append({
            key: variation.get(key)
            for key in (
                "name", "title", "label", "sku", "seller_sku", "attributes",
                "attribute_combinations", "properties",
            )
            if variation.get(key) not in (None, "", [])
        })
    return {
        "产品编号": str(row.get("source_item_id") or ""),
        "当前标题": str(row.get("title") or "").strip(),
        "当前描述": description_text[:6000],
        "类目": str(row.get("category_name") or row.get("category_id") or ""),
        "价格和币种": {"price": row.get("price"), "currency": row.get("currency_id")},
        "重量与包装尺寸": {
            "weight_g": row.get("weight_g"),
            "length_cm": row.get("package_length_cm"),
            "width_cm": row.get("package_width_cm"),
            "height_cm": row.get("package_height_cm"),
        },
        "商品属性与规格": attributes,
        "变体": variations,
    }, source


def generate_collection_copy(
    row: Mapping[str, Any],
    *,
    api_key: str,
    model: str,
    base_url: str,
    chat=None,
) -> dict[str, Any]:
    """Return validated 50–60 character ES/PT titles and rewritten descriptions."""
    if chat is None:
        from AI_Agent.deepseek import chat_deepseek

        chat = chat_deepseek
    facts, source = _product_facts(row)
    if not facts["当前标题"]:
        raise ValueError("当前产品没有标题，无法生成文案")
    known_brands = _source_brand_terms(source)
    prompt = (
        "你是 Mercado Libre 拉美商品文案编辑。只根据提供的商品事实重写文案，不得编造属性、规格、"
        "认证、功效或包装内容。生成自然、便于搜索的西班牙语（拉美）和巴西葡萄牙语标题与描述。"
        "两个标题都必须严格为 50 至 60 个字符（含空格和标点，按 Unicode 字符计数），不得出现任何品牌、"
        "商标、店铺名、厂家名或 OEM；不要为了凑字数重复关键词。描述也不得加入品牌名或虚构信息。"
        "描述用纯文本、不要 HTML 或超链接，简明准确地改写商品特点和已知规格。必须只返回 JSON 对象，字段为 title_es、title_pt、"
        "description_es、description_pt、brand_terms；brand_terms 填入从商品资料识别出的全部品牌词，"
        "没有时返回空数组。\n商品资料："
        + json.dumps(facts, ensure_ascii=False, default=str)
    )
    feedback = ""
    last_error = None
    for attempt in range(3):
        response = chat(
            [{"role": "user", "content": prompt + feedback}],
            api_key=api_key,
            model=model,
            base_url=base_url,
            temperature=0.2,
            max_tokens=3500,
            response_format={"type": "json_object"},
            thinking=False,
        )
        try:
            generated = _json_object(response)
            brands = _brand_terms(generated, source)
            title_es = _clean_title(generated.get("title_es"), brands)
            title_pt = _clean_title(generated.get("title_pt"), brands)
            description_es = str(generated.get("description_es") or "").strip()
            description_pt = str(generated.get("description_pt") or "").strip()
            if not description_es or not description_pt:
                raise ValueError("西班牙语和葡萄牙语描述都必须生成")
            invalid_titles = [
                ("西班牙语", title_es), ("葡萄牙语", title_pt),
            ]
            invalid_titles = [
                (language, title)
                for language, title in invalid_titles
                if not 50 <= len(title) <= 60
            ]
            if invalid_titles:
                details = "、".join(f"{language}{len(title)}字符" for language, title in invalid_titles)
                raise ValueError(f"标题必须为 50–60 个字符，当前：{details}")
            return {
                "title_es": title_es,
                "title_pt": title_pt,
                "description_es": description_es[:50000],
                "description_pt": description_pt[:50000],
                "brand_terms": brands,
            }
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            last_error = exc
            feedback = (
                f"\n上次结果未通过校验：{exc}。请重新生成完整 JSON，重点确保两个标题各 50–60 个字符，"
                "并删除所有品牌词；不要截断标题。"
            )
    raise ValueError(str(last_error or "DeepSeek 文案未通过校验"))


__all__ = ["generate_collection_copy"]
