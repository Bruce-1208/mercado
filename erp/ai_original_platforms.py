"""Marketplace-specific *draft* policies for AI-original supplier products.

These checks deliberately do not claim category, account or API readiness.
Amazon category constraints must still be resolved from Product Type Definitions.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import re
from typing import Any, Mapping

PLATFORMS = {
    "mercadolibre": {
        "id": "mercadolibre", "label": "美客多", "language": "es+pt_BR",
        "language_label": "西班牙语 / 巴西葡萄牙语", "marketplace": "CBT",
        "title_limit": 60, "description_limit": 50000,
        "notes": ["保留现有美客多类目与发布流程；发布前仍需核对目标站点规则。"],
        "sources": ["https://global-selling.mercadolibre.com/devsite/items-and-searches"],
    },
    "yandex": {
        "id": "yandex", "label": "Yandex Market", "language": "ru_RU",
        "language_label": "俄语", "marketplace": "Yandex Market",
        "title_limit": 256, "description_limit": 6000,
        "notes": ["俄语标题最多 256 字符，建议 60–120；描述最多 6000 字符。",
                  "主图至少 300×300、最多 10 MB，商品主体至少占画面的 2/3。",
                  "上架前提供包装长、宽、高（cm）和包装重量（kg），不得由 AI 猜测。",
                  "不含促销、联系方式、店铺水印；类目、必填属性和销售资料需补齐。"],
        "sources": [
            "https://yandex.ru/dev/market/partner-api/doc/ru/reference/business-offer-mappings/updateOfferMappings",
            "https://www.yandex.com/support/global-marketplace/en/assortment/create/main-fields/title-description",
            "https://www.yandex.com/support/global-marketplace/en/assortment/create/main-fields/images",
            "https://www.yandex.com/support/global-marketplace/en/assortment/create/main-fields/general",
        ],
    },
    "amazon": {
        "id": "amazon", "label": "亚马逊美国站", "language": "en_US",
        "language_label": "英语（美国）", "marketplace": "ATVPDKIKX0DER",
        "title_limit": 75, "item_highlights_limit": 125, "description_limit": 2000,
        "description_limit_scope": "internal_draft_limit",
        "bullet_limit": 5, "bullet_character_limit": 500,
        "bullet_limit_scope": "internal_draft_limit",
        "notes": ["适配 Amazon 美国站；2026-07-27 起非媒体类目标题最多 75 字符，Item Highlights 最多 125 字符；媒体类目需另行核对标题要求。",
                  "标题限制特殊字符和同词重复；描述 2000 字符、最多 5 条各 500 字符卖点是本工具草稿策略。",
                  "上架前按美国站所选类目的最新要求，核对必填资料、品牌、商品编码/豁免与变体。",
                  "主图需真实展示商品、纯白背景，主体约占画面 85%；AI 处理后的图片必须人工核对真实性。"],
        "sources": [
            "https://sellercentral.amazon.com/seller-forums/discussions/t/145b6d0f-999c-4555-896c-c694bda2e470",
            "https://sellercentral.amazon.com/help/hub/reference/external/G200390640?langue=en_US",
            "https://sellercentral.amazon.com/seller-forums/discussions/t/b2b15728-0d43-453e-974f-59eb63f73059/",
            "https://sellercentral.amazon.com/help/hub/reference/external/G1881?ld=a",
            "https://sell.amazon.com/blog/amazon-gtin-guide",
            "https://sellercentral.amazon.com/seller-forums/discussions/t/65f8e647-977d-49ac-9036-2049b96720b2",
            "https://developer-docs.amazon.com/sp-api/lang-en_EN/docs/retrieve-a-product-type-definition",
        ],
    },
}
TEMPLATES = {
    "standard": {"id": "standard", "label": "标准商品", "description": "概述、卖点、规格、包装内容与使用提示，均以货源事实为依据。"},
    "features": {"id": "features", "label": "卖点优先", "description": "先展示有事实支持的卖点和使用场景，再补充规格。"},
    "specs": {"id": "specs", "label": "规格优先", "description": "先展示材质、尺寸和变体等已知规格，再说明用途。"},
}


def listing_profiles_catalog() -> dict[str, Any]:
    return {"platforms": deepcopy(list(PLATFORMS.values())),
            "templates": deepcopy(list(TEMPLATES.values()))}


def normalize_listing_options(target_platform="mercadolibre", template_id="standard") -> tuple[str, str]:
    platform = str(target_platform or "mercadolibre").strip().lower()
    template = str(template_id or "standard").strip().lower()
    if platform not in PLATFORMS:
        raise ValueError("不支持的目标平台，请选择美客多、Yandex 或亚马逊美国站")
    if template not in TEMPLATES:
        raise ValueError("不支持的原创模板，请选择标准商品、卖点优先或规格优先")
    return platform, template


_AMAZON_EXEMPT_WORDS = frozenset("a an the and or but nor for so yet as at by in of on per to up via with from into onto over under between through without within about above below during before after around against upon out off than till until near plus versus".split())
_CONTACT = re.compile(r"https?://|www\.|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:whatsapp|telegram|微信|电话)\s*[:：]", re.I)
_PROMOTION = re.compile(r"\b(?:free shipping|money.back guarantee|100% satisfaction|best seller|limited.time offer|бесплатная доставка|скидка|распродажа|акция)\b", re.I)
_EMOJI = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF]")


def validate_platform_listing(draft: Mapping[str, Any], platform_id="mercadolibre") -> dict[str, list[str]]:
    platform, _ = normalize_listing_options(platform_id)
    profile = PLATFORMS[platform]
    errors, warnings = [], []
    title = str((draft.get("title_es") if platform == "mercadolibre" else draft.get("title")) or "").strip()
    description = str((draft.get("description_es") if platform == "mercadolibre" else draft.get("description")) or "").strip()
    if not title:
        errors.append("商品标题不能为空")
    elif len(title) > profile["title_limit"]:
        errors.append(f"标题不能超过 {profile['title_limit']} 个字符")
    if not description:
        errors.append("商品描述不能为空")
    elif len(description) > profile["description_limit"]:
        suffix = "（工具草稿策略，实际限制以目标类目要求为准）" if platform == "amazon" else ""
        errors.append(f"描述不能超过 {profile['description_limit']} 个字符{suffix}")
    if not str(draft.get("main_image_url") or "").strip():
        errors.append("缺少商品主图")
    bullets = draft.get("bullet_points") or []
    if not isinstance(bullets, list) or any(not isinstance(item, str) for item in bullets):
        errors.append("卖点必须为文本数组")
        bullets = []
    if platform == "mercadolibre":
        from erp.mercadolibre_translation import title_language_issue
        for field, language, label in (("title_es", "es", "西语"), ("title_pt", "pt-BR", "葡语")):
            issue = title_language_issue(draft.get(field), language)
            if issue:
                errors.append(label + issue)
        return {"errors": errors, "warnings": warnings}
    highlights = str(draft.get("item_highlights") or "").strip()
    combined = "\n".join([title, description, highlights, *bullets])
    if _CONTACT.search(combined):
        errors.append("文案不能包含外部链接或联系方式")
    if _PROMOTION.search(combined):
        errors.append("文案不能包含促销、免运费或退款保证等承诺")
    if re.search(r"<\s*/?\s*[a-z][^>]*>", combined, re.I):
        errors.append("当前草稿使用纯文本，请移除 HTML 标签")
    if re.search(r"[\u4e00-\u9fff]", combined):
        errors.append("标题、描述和卖点须翻译为目标平台语言，不能保留中文文案")
    if platform == "yandex":
        if _EMOJI.search(combined):
            errors.append("Yandex 文案不能包含 emoji 或装饰性符号")
        if title and not re.search(r"[А-Яа-яЁё]", title):
            errors.append("Yandex 标题须使用俄语")
        if description and not re.search(r"[А-Яа-яЁё]", description):
            errors.append("Yandex 描述须使用俄语")
        warnings.append("须核实 Yandex 类目、类目必填属性、商家 SKU、价格、库存、履约方式、包装长宽高（cm）与重量（kg）和适用的条码/认证资料。")
    else:
        if len(highlights) > profile["item_highlights_limit"]:
            errors.append("亚马逊 Item Highlights 不能超过 125 个字符")
        forbidden = sorted(set(re.findall(r"[!$?_{}^¬¦]", title)))
        if forbidden:
            errors.append("亚马逊标题含受限字符：" + " ".join(forbidden) + "；真实品牌名例外需人工核验")
        words = Counter(re.findall(r"[a-z0-9]+(?:['’-][a-z0-9]+)*", title.casefold()))
        repeated = [word for word, count in words.items() if count > 2 and word not in _AMAZON_EXEMPT_WORDS]
        if repeated:
            errors.append("亚马逊标题同词不能出现超过 2 次：" + ", ".join(repeated))
        if not bullets:
            errors.append("亚马逊草稿至少填写 1 条商品卖点")
        if len(bullets) > profile["bullet_limit"]:
            errors.append("当前亚马逊草稿模板最多填写 5 条卖点，实际限制以目标类目要求为准")
        if any(len(value) > profile["bullet_character_limit"] for value in bullets):
            errors.append("当前亚马逊草稿每条卖点最多 500 字符，实际限制以目标类目要求为准")
        if any(_EMOJI.search(value) for value in bullets):
            errors.append("亚马逊卖点不能包含 emoji 或装饰性符号")
        if re.search(r"[А-Яа-яЁё]", combined):
            errors.append("亚马逊美国站文案须使用英语")
        warnings.append("仅应用通用草稿规则；须核对美国站具体类目的标题、描述、卖点、必填属性及变体规则。")
        warnings.append("品牌、商品状况、GTIN/UPC/EAN 或豁免资格不得从货源猜测，需卖家核实；还需提供 SKU、价格、库存和履约资料。")
    warnings.append("主图虽经过白底处理，仍需人工核对实物真实性、商品占比、无水印/文字/额外配件及目标类目图片要求。")
    if isinstance(draft.get("attributes"), list) and any(
        isinstance(value, Mapping) and re.search(r"[\u4e00-\u9fff]", str(value.get("name") or "") + str(value.get("value_name") or ""))
        for value in draft["attributes"]
    ):
        warnings.append("部分属性仍含中文，需核实目标语言翻译或真实品牌写法。")
    return {"errors": list(dict.fromkeys(errors)), "warnings": list(dict.fromkeys(warnings))}


def build_listing_export(row: Mapping[str, Any], target_platform="mercadolibre") -> dict[str, Any]:
    platform, _ = normalize_listing_options(target_platform)
    raw = row.get("source_snapshot_json") or {}
    snapshot = dict(raw) if isinstance(raw, Mapping) else json.loads(str(raw))
    listings = snapshot.get("marketplace_listings") or {}
    draft = snapshot.get("ai_original") if platform == "mercadolibre" else listings.get(platform)
    if not isinstance(draft, Mapping) or not draft:
        raise ValueError("该平台尚未生成原创草稿")
    draft = deepcopy(dict(draft))
    draft.setdefault("target_platform", platform)
    draft.setdefault("template_id", "standard")
    if platform == "mercadolibre":
        draft["title"] = draft.get("title_es") or ""
        draft["description"] = draft.get("description_es") or ""
    validation = validate_platform_listing(draft, platform)
    draft["validation"] = validation
    missing = ["目标类目及类目必填属性核验", "商品与主图真实性、图片规范人工审核", "商家 SKU、价格、库存、履约及税务资料"]
    if platform == "amazon":
        missing += ["美国站产品类型、类目必填资料与变体主题核验", "真实品牌、商品状况、GTIN 或已获准的 GTIN 豁免"]
    elif platform == "yandex":
        missing += ["Yandex Market 类目映射及销售资质", "包装长、宽、高（cm）与包装重量（kg）", "适用品类的条码、认证和保修信息"]
    return {"format": "ai-original-listing-draft-v1", "draft_only": True,
            "ready_for_submission": False, "target_platform": platform,
            "platform_label": PLATFORMS[platform]["label"],
            "source_product_id": row.get("id"), "source_url": (snapshot.get("original_1688") or {}).get("source_url") or row.get("source_url"),
            "listing": draft, "validation": validation, "missing_information": missing,
            "notes": "这是供人工审核和补全的商品草稿，不是可直接提交平台 API 的请求。",
            "rule_sources": deepcopy(PLATFORMS[platform]["sources"])}
