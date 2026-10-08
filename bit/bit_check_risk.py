"""根据工作台中的多种商品来源审核侵权风险。

风险级别会写入“疑似侵权”字段：
0 = 未发现可疑性，1 = 有侵权风险/需人工复核，2 = 有明确侵权特征。
对于 1/2 级结果，同时把品牌或 IP 关键词写入“侵权关键词”字段。

默认只判断标题，可显式选择实际读取图片的多模态检测。侵权知识库中人工确认的风险拦截优先，
人工放行仅约束对应品牌，不代表整个商品安全；自动线索与待复核记录不参与裁决。
"""

import argparse
import base64
import io
import ipaddress
import json
import os
import re
import sys
import socket
from urllib.parse import urlsplit, urljoin
from pathlib import Path
from datetime import datetime, timezone

from bit.infringement_state import canonical_record, content_hash, knowledge_version, normalize_items


if __package__ in (None, ""):
    project_root = str(Path(__file__).resolve().parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

try:
    sys.stdout.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
        line_buffering=True,
    )
    sys.stderr.reconfigure(
        encoding="utf-8",
        errors="backslashreplace",
        line_buffering=True,
    )
except (AttributeError, ValueError):
    pass

from AI_Agent.deepseek import chat_deepseek, QWEN_MODEL
from bit.bit_infringement_knowledge import effective_list_type
from bit.bit_mysql import get_zying_risk_candidates, update_zying_product_risks
from bit.bit_risk import (
    DEFAULT_BATCH_SIZE,
    _extract_json_payload,
    _get_http_session,
    IMAGE_TIMEOUT,
    MAX_IMAGE_BYTES,
)


DEFAULT_HOURS = max(0, int(os.environ.get("BIT_CHECK_RISK_HOURS", "0")))
DEFAULT_AI_RETRIES = max(0, int(os.environ.get("BIT_CHECK_RISK_AI_RETRIES", "1")))
DEFAULT_VISION_MODEL = os.environ.get("BIT_CHECK_RISK_VISION_MODEL") or os.environ.get("BIT_RISK_VISION_MODEL") or "qwen3-vl-plus"


def _read_screening_image(url):
    """Read a bounded public image, validating every redirect before connecting."""
    parsed = urlsplit(url)
    local_asset = re.fullmatch(r"/api/ai-original-products/images/(1688-[A-Za-z0-9_-]+-(?:ai-)?white\.jpg)", parsed.path)
    if local_asset:
        from erp.ai_original_products import IMAGE_DIR, IMAGE_BASE_URL
        configured_host = urlsplit(IMAGE_BASE_URL).netloc
        if not parsed.netloc or parsed.hostname in {"localhost", "127.0.0.1", "::1"} or (configured_host and parsed.netloc == configured_host):
            root = IMAGE_DIR.resolve()
            path = (root / local_asset.group(1)).resolve()
            if path.parent != root or not path.is_file():
                raise ValueError("商品图片资产不存在")
            with path.open("rb") as asset:
                raw = asset.read(MAX_IMAGE_BYTES + 1)
            if not raw or len(raw) > MAX_IMAGE_BYTES:
                raise ValueError("商品图片内容为空或文件过大")
            return raw
    for _ in range(6):
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("商品图片地址无效")
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not addresses or any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise ValueError("商品图片必须来自公开地址")
        response = _get_http_session().get(url, timeout=IMAGE_TIMEOUT, stream=True, allow_redirects=False)
        try:
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("Location")
                if not location:
                    raise ValueError("商品图片重定向地址为空")
                url = urljoin(url, location)
                continue
            response.raise_for_status()
            content = bytearray()
            for block in response.iter_content(64 * 1024):
                content.extend(block)
                if len(content) > MAX_IMAGE_BYTES:
                    raise ValueError("商品图片文件过大")
            if not content:
                raise ValueError("商品图片内容为空")
            return bytes(content)
        finally:
            response.close()
    raise ValueError("商品图片重定向次数过多")

RISK_LABELS = {
    0: "无可疑",
    1: "疑似/需复核",
    2: "明确侵权特征",
}

SOURCE_LABELS = {
    "product_list": "产品列表",
    "collection_list": "采集列表",
    "pulled": "店铺拉取产品",
    "zying": "智赢采集产品",
}

AI_PROVIDER_LABELS = {
    "deepseek": "千问",
    "qianwen": "千问",
    "local": "本地模型",
}


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _emit_log(message, log_callback=None):
    text = str(message or "").strip()
    if not text:
        return
    print(text, flush=True)
    if log_callback is not None:
        log_callback(text)


def _normalize_keywords(value):
    if isinstance(value, str):
        values = re.split(r"[,;，；|\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        values = value
    else:
        values = []

    keywords = []
    seen = set()
    for value in values:
        keyword = re.sub(r"\s+", " ", str(value or "")).strip(" .,:;，；|")
        key = keyword.casefold()
        if not keyword or key in seen:
            continue
        seen.add(key)
        keywords.append(keyword[:100])
        if len(keywords) >= 20:
            break
    return keywords


def _coerce_risk_level(value):
    if isinstance(value, bool):
        return 1 if value else 0
    text = str(value if value is not None else "").strip().lower()
    aliases = {
        "0": 0,
        "none": 0,
        "safe": 0,
        "false": 0,
        "无": 0,
        "无风险": 0,
        "1": 1,
        "risk": 1,
        "suspected": 1,
        "true": 1,
        "疑似": 1,
        "有风险": 1,
        "2": 2,
        "infringement": 2,
        "infringing": 2,
        "侵权": 2,
        "确定侵权": 2,
    }
    if text not in aliases:
        raise ValueError(f"无法识别风险级别 {value!r}")
    return aliases[text]


def _record_key(record):
    return str(record.get("record_key") or record.get("row_id") or "").strip()


def _normalize_ai_results(payload, records):
    if isinstance(payload, dict):
        payload = payload.get("results") or payload.get("data") or []
    if not isinstance(payload, list):
        raise ValueError("千问 返回结果不是数组")

    records_by_key = {_record_key(record): record for record in records}
    wanted_ids = set(records_by_key)
    normalized = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        try:
            row_id = str(item.get("row_id") or "").strip()
        except (TypeError, ValueError, AttributeError):
            continue
        if row_id not in wanted_ids:
            continue
        if row_id in normalized:
            raise ValueError(f"AI 重复返回数据行 {row_id}")

        risk_value = item.get("risk_level", item.get("risk"))
        if risk_value is None and "suspected" in item:
            risk_value = item.get("suspected")
        risk_level = _coerce_risk_level(risk_value)
        keywords = _normalize_keywords(
            item.get("keywords")
            or item.get("brands")
            or item.get("brand")
            or item.get("infringement_keywords")
        )
        if risk_level == 0:
            keywords = []
        elif not keywords:
            raise ValueError(f"数据行 {row_id} 风险级别为 {risk_level}，但 AI 未返回品牌/IP 关键词")

        record = records_by_key[row_id]
        normalized[row_id] = {
            "row_id": record.get("row_id"),
            "source_type": str(record.get("source_type") or "zying"),
            "source_row_id": record.get("source_row_id", record.get("row_id")),
            "record_key": row_id,
            "product_id": str(record.get("product_id") or ""),
            "title": str(record.get("title") or ""),
            "main_image_url": str(record.get("main_image_url") or ""),
            "product_category": str(record.get("product_category") or ""),
            "zying_category_id": str(record.get("zying_category_id") or ""),
            "zying_category": str(record.get("zying_category") or ""),
            "salesperson": str(record.get("salesperson") or ""),
            "group_name": str(record.get("group_name") or ""),
            "token_id": record.get("token_id"),
            "account_name": str(record.get("account_name") or ""),
            "risk_level": risk_level,
            "keywords": keywords,
            "reason": str(item.get("reason") or "").strip()[:500],
        }

    missing_ids = wanted_ids.difference(normalized)
    if missing_ids:
        raise ValueError(
            "AI 未返回以下数据行，本批次不会写库："
            + ", ".join(str(row_id) for row_id in sorted(missing_ids))
        )
    return [normalized[_record_key(record)] for record in records]


def _knowledge_lists(records, product=None):
    from bit import bit_infringement_knowledge as knowledge_rules

    lists = {"blacklist": [], "whitelist": [], "borderline": []}
    for row in records or ():
        if not isinstance(row, dict):
            continue
        list_type = effective_list_type(row)
        brand = " ".join(str(row.get("brand_name") or "").split())
        if product is not None and hasattr(knowledge_rules, "knowledge_rule_matches"):
            if not knowledge_rules.knowledge_rule_matches(
                row, site=str(product.get("site_id") or ""),
                category=str(product.get("category_id") or product.get("product_category") or ""),
                shop=str(product.get("token_id") or product.get("account_name") or ""),
            ):
                continue
        if list_type in lists and brand:
            names = knowledge_rules.knowledge_rule_names(row) if hasattr(knowledge_rules, "knowledge_rule_names") else [brand]
            lists[list_type].extend(names)
    for key in lists:
        lists[key] = list(dict.fromkeys(lists[key]))
    return lists


def _title_matches_brand(title, brand):
    title_text = str(title or "").casefold()
    brand_text = str(brand or "").casefold().strip()
    if not brand_text:
        return False
    if re.search(r"[\u3400-\u9fff]", brand_text):
        return brand_text in title_text
    return re.search(
        rf"(?<![\w]){re.escape(brand_text)}(?![\w])",
        title_text,
        flags=re.IGNORECASE,
    ) is not None


def _matched_knowledge(record, knowledge):
    record = canonical_record(record)
    title = " ".join(str(record.get(field) or "") for field in ("title", "title_es", "title_pt"))
    return {
        key: [brand for brand in knowledge.get(key, ()) if _title_matches_brand(title, brand)]
        for key in ("blacklist", "whitelist", "borderline")
    }


def _apply_borderline_policy(record, result, knowledge_records):
    """Keep advisory evidence separate from actionable risk keywords."""
    result = dict(result)
    evidence = {**record, "title": str(record.get("title") or "") + " " + " ".join(list(result.get("keywords") or []) + list(result.get("borderline_brands") or []))}
    matches = _matched_knowledge(evidence, _knowledge_lists(knowledge_records, record))
    warnings = list(dict.fromkeys(list(result.get("borderline_brands") or []) + matches["borderline"]))
    result["borderline_brands"] = warnings
    if not warnings:
        return result
    if not matches["blacklist"]:
        keywords = list(result.get("keywords") or [])
        remaining = [word for word in keywords if not any(
            str(word).casefold() == brand.casefold() for brand in warnings)]
        if keywords and not remaining:
            result.update(risk_level=0, keywords=[], reason="未发现其他独立风险")
        else:
            result["keywords"] = remaining
    hint = "擦边品牌仅提示，不处理：" + "、".join(warnings)
    if hint not in str(result.get("reason") or ""):
        result["reason"] = hint + "；" + str(result.get("reason") or "")
    return result


def _build_products_payload(records, knowledge=None, knowledge_records=None):
    knowledge = knowledge or {"blacklist": [], "whitelist": [], "borderline": []}
    return [
        {
            "row_id": _record_key(record),
            "product_id": str(record.get("product_id") or ""),
            "title": str(record.get("title") or "")[:1000],
            "title_es": str(canonical_record(record).get("title_es") or "")[:1000],
            "title_pt": str(canonical_record(record).get("title_pt") or "")[:1000],
            "product_category": str(record.get("product_category") or "")[:500],
            "zying_category": str(record.get("zying_category") or "")[:500],
            "knowledge": _matched_knowledge(record, _knowledge_lists(knowledge_records, record) if knowledge_records is not None else knowledge),
        }
        for record in records
    ]


def classify_risk_records(
    records,
    model=None,
    retries=DEFAULT_AI_RETRIES,
    knowledge_records=None,
    ai_provider=None,
    api_key=None,
    base_url=None,
    detection_scope="title",
    vision_model=None,
    image_reader=None,
):
    """让 AI 仅根据商品标题返回 0/1/2 级风险。"""
    knowledge = _knowledge_lists(knowledge_records)
    if detection_scope not in {"title", "title_image"}:
        raise ValueError("检测范围仅支持标题或标题和图片")
    if any(not str(record.get("title") or "").strip() for record in records):
        raise ValueError("商品标题为空，无法完成检测")
    products = _build_products_payload(records, knowledge, knowledge_records)
    system_prompt = """你是跨境电商知识产权风险审核员。请只根据商品标题做筛查。

风险级别定义：
- 0：普通品类词、功能、颜色、尺寸、材质等，未发现品牌、商标或受保护 IP。
- 1：有风险但证据不足，需人工复核。例如兼容性表述、疑似变体词/错拼品牌、不确定是否受保护的角色或图案文字。
- 2：标题明确把知名品牌、商标、影视/动漫/游戏 IP、角色、名人、球队等用作商品本体或装饰，具有明确侵权特征。

规则：
1. “适用于/兼容/for/compatible with”后的品牌通常先判 1，除非标题同时包含仿冒或受保护图案的直接证据。
2. 主图链接、图片内容和 Logo 不在本次检测范围内，禁止根据图片或常识补充判断。
3. risk_level 为 1 或 2 时，keywords 必须列出标题中命中的品牌、人物或 IP；为 0 时 keywords 必须是空数组。
4. knowledge 包含自动导入的 WIPO 黑名单和人工确认的处置规则。knowledge.blacklist 中命中的品牌按风险拦截判 2；knowledge.whitelist 仅表示人工放行该品牌名称，不代表整个商品安全，标题中其他品牌/IP 仍需独立判断。若同一名称同时出现于黑白名单，以黑名单为准。其他自动采集与待复核线索不形成处置规则。
knowledge.borderline 中的品牌只作提示，不因该品牌提高 risk_level；其他品牌/IP 风险仍独立判断，黑名单优先。
5. 每个 row_id 必须返回且只返回一次。只返回 JSON 数组，不要 Markdown 或其他文字。

返回格式：
[{"row_id":123,"risk_level":0,"keywords":[],"reason":"未发现品牌或 IP"}]
"""
    user_prompt = "请逐条审核以下商品：\n" + json.dumps(products, ensure_ascii=False)
    if detection_scope == "title_image":
        system_prompt = system_prompt.replace("请只根据商品标题做筛查。", "请根据商品标题及逐条对应的实际商品图片做筛查，识别文字和图形 Logo、品牌包装、角色、IP 装饰和仿冒特征。")
        system_prompt = system_prompt.replace("2. 主图链接、图片内容和 Logo 不在本次检测范围内，禁止根据图片或常识补充判断。", "2. 判断必须有标题或实际图片证据；图片无法识别不得宣称安全。不要猜测未看清的 Logo。")
        system_prompt = system_prompt.replace("标题中命中的品牌、人物或 IP", "标题或图片中命中的品牌、人物或 IP")
        user_prompt = [{"type": "text", "text": user_prompt}]
        for record in records:
            url = str(record.get("main_image_url") or "").strip()
            if not url:
                raise ValueError(f"商品 {_record_key(record)} 缺少图片，未完成图片检测")
            raw = (image_reader or _read_screening_image)(url)
            # Reject HTML/error pages and unrecognizable bytes before claiming the image was screened.
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as picture:
                picture.verify()
                mime = Image.MIME.get(picture.format, "image/jpeg")
            user_prompt.extend([
                {"type": "text", "text": f"以下图片对应 row_id={_record_key(record)}"},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(raw).decode()}"}},
            ])
        model = vision_model or DEFAULT_VISION_MODEL
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_error = None
    for attempt in range(max(0, int(retries)) + 1):
        request_kwargs = {
            "model": model,
            "temperature": 0,
            "max_tokens": max(1600, len(records) * 140),
        }
        # Keep the legacy environment-based 千问 path working for CLI use,
        # while allowing the workbench to supply one-time credentials for any
        # OpenAI-compatible provider.
        if api_key is not None:
            request_kwargs["api_key"] = api_key
        if base_url:
            request_kwargs["base_url"] = base_url
        response = ""
        try:
            response = chat_deepseek(messages, **request_kwargs)
            results = _normalize_ai_results(_extract_json_payload(response), records)
            return [_apply_borderline_policy(record, result, knowledge_records)
                    for record, result in zip(records, results)]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt >= max(0, int(retries)):
                break
            messages.extend(
                [
                    {"role": "assistant", "content": response},
                    {
                        "role": "user",
                        "content": (
                            f"上次返回无法使用：{exc}。"
                            "请按要求重新返回包含所有 row_id 的完整 JSON 数组。"
                        ),
                    },
                ]
            )
    provider_label = AI_PROVIDER_LABELS.get(str(ai_provider or ""), "AI")
    raise ValueError(f"{provider_label} 结果解析失败：{last_error}")


def scan_products(
    zying_category=None, hours=DEFAULT_HOURS, limit=0,
    batch_size=DEFAULT_BATCH_SIZE, model=None, retries=DEFAULT_AI_RETRIES,
    recheck=False, dry_run=False, candidate_reader=None, risk_writer=None,
    log_callback=None, sources=None, salesperson=None, group_name=None,
    token_ids=None, knowledge_records=None, ai_provider=None, api_key=None,
    base_url=None, items=None, detection_scope="title", rule_version=None,
    vision_model=None, stop_callback=None, item_callback=None, image_reader=None,
):
    """Screen selected current snapshots; never turn a failed request into level zero."""
    if detection_scope not in {"title", "title_image"}:
        raise ValueError("检测范围仅支持标题或标题和图片")
    items = normalize_items(items)
    if items is not None:
        sources = list(dict.fromkeys(item["source_type"] for item in items))
    candidate_reader = candidate_reader or get_zying_risk_candidates
    risk_writer = risk_writer or update_zying_product_risks
    rule_version = rule_version or knowledge_version(knowledge_records)
    candidate_kwargs = {
        "hours": hours, "limit": limit, "zying_category": zying_category,
        "include_checked": recheck,
    }
    if sources is not None:
        candidate_kwargs.update(
            sources=sources, salesperson=salesperson, group_name=group_name,
            token_ids=token_ids, detection_scope=detection_scope, rule_version=rule_version,
        )
    if items is not None:
        candidate_kwargs["items"] = items
    records = [canonical_record(record) for record in candidate_reader(**candidate_kwargs) if record.get("row_id") is not None]
    selected_sources = list(sources or ("zying",))
    scope = "、".join(SOURCE_LABELS.get(value, value) for value in selected_sources)
    _emit_log(f"读取 {scope}候选商品 {len(records)} 条；检测范围：{'标题和图片' if detection_scope == 'title_image' else '标题'}", log_callback)
    checked = updated = cancelled = 0
    all_results = []
    failures = []
    missing_count = 0

    def emit(record, status, **fields):
        event = {
            "source_type": record.get("source_type") or "zying",
            "row_id": record.get("row_id"), "record_key": _record_key(record),
            "status": status, **fields,
        }
        if item_callback:
            item_callback(event)

    def decorate(record, result):
        if result.get("check_status") != "failed":
            result = _apply_borderline_policy(record, result, knowledge_records)
        merged = {**record, **result}
        if result.get("check_status") != "failed":
            merged.pop("error", None)
            merged.pop("check_error", None)
        merged.pop("risk_status", None)
        merged.update(
            source_type=record.get("source_type") or "zying",
            source_row_id=record.get("source_row_id", record.get("row_id")),
            record_key=_record_key(record), content_hash=content_hash(record, detection_scope),
            rule_version=rule_version, detection_scope=detection_scope,
            image_method="vision" if detection_scope == "title_image" else "",
            model_version=(vision_model or DEFAULT_VISION_MODEL) if detection_scope == "title_image" else str(model or QWEN_MODEL),
            image_checked=detection_scope == "title_image" and result.get("check_status") != "failed",
            checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        )
        return merged

    if items is not None and recheck and not limit:
        resolved = {(record.get("source_type"), int(record["row_id"])) for record in records}
        for item in items:
            if (item["source_type"], item["row_id"]) not in resolved:
                missing = decorate({**item, "record_key": f"{item['source_type']}:{item['row_id']}"}, {
                    "risk_level": None, "keywords": [], "check_status": "failed",
                    "error": "商品不存在或已移出当前来源，请刷新列表",
                })
                failures.append(missing)
                missing_count += 1
                emit(missing, "failed", error=missing["error"], risk_level=None)

    batches = list(_chunks(records, 1 if detection_scope == "title_image" else max(1, int(batch_size))))
    for batch_index, batch in enumerate(batches, start=1):
        if stop_callback and stop_callback():
            cancelled = len(records) - checked - (len(failures) - missing_count)
            break
        _emit_log(f"开始{'图文' if detection_scope == 'title_image' else '标题'}审核批次 {batch_index}/{len(batches)}，{len(batch)} 条", log_callback)
        valid = []
        completed = []
        failed_batch = []
        for record in batch:
            emit(record, "processing")
            if not str(record.get("title") or "").strip():
                failed_batch.append(decorate(record, {"risk_level": None, "keywords": [], "check_status": "failed", "error": "商品标题为空，无法完成检测"}))
                continue
            matches = _matched_knowledge(record, _knowledge_lists(knowledge_records, record))
            record["matched_rule_ids"] = [
                row.get("id") for row in knowledge_records or []
                if row.get("id") and any(_matched_knowledge(record, _knowledge_lists([row], record)).values())
            ]
            if matches["blacklist"] and detection_scope == "title":
                completed.append(decorate(record, {"risk_level": 2, "keywords": matches["blacklist"], "reason": "命中侵权知识库黑名单", "check_status": "completed"}))
            else:
                valid.append(record)
        if valid:
            connection = {}
            for name, value in (("ai_provider", ai_provider), ("api_key", api_key), ("base_url", base_url)):
                if value is not None:
                    connection[name] = value
            if knowledge_records:
                connection["knowledge_records"] = knowledge_records
            if detection_scope == "title_image":
                connection.update(detection_scope=detection_scope, vision_model=vision_model, image_reader=image_reader)
            try:
                ai_results = classify_risk_records(valid, model=model, retries=retries, **connection)
                by_key = {str(result.get("record_key") or result.get("row_id")): result for result in ai_results}
                if len(ai_results) != len(valid) or set(by_key) != {_record_key(record) for record in valid}:
                    raise ValueError("AI 结果缺失、重复或编号不对应，本批次未形成风险结论")
                for record in valid:
                    result = dict(by_key[_record_key(record)])
                    # A visual brand may only occur in the returned evidence;
                    # apply scoped confirmed rules to that identified brand too.
                    evidence_record = {**record, "title": str(record.get("title") or "") + " " + " ".join(list(result.get("keywords") or []) + list(result.get("borderline_brands") or []))}
                    matches = _matched_knowledge(evidence_record, _knowledge_lists(knowledge_records, record))
                    record["matched_rule_ids"] = [
                        row.get("id") for row in knowledge_records or [] if row.get("id")
                        and any(_matched_knowledge(evidence_record, _knowledge_lists([row], record)).values())
                    ]
                    if matches["blacklist"]:
                        result.update(risk_level=2, keywords=list(dict.fromkeys(matches["blacklist"] + list(result.get("keywords") or []))), reason="命中侵权知识库黑名单；" + str(result.get("reason") or ""))
                    completed.append(decorate(record, {**result, "check_status": "completed"}))
            except Exception as exc:
                failed_batch.extend(decorate(record, {"risk_level": None, "keywords": [], "check_status": "failed", "error": str(exc)[:1000]}) for record in valid)
                _emit_log(f"批次失败：{exc}", log_callback)
        if not dry_run and completed:
            updated += risk_writer(completed)
        if not dry_run and failed_batch and sources is not None:
            risk_writer(failed_batch)
        checked += len(completed)
        all_results.extend(completed)
        failures.extend(failed_batch)
        for result in completed:
            emit(result, "completed", **{key: result.get(key) for key in ("risk_level", "keywords", "reason", "content_hash", "rule_version", "detection_scope", "checked_at")})
        for result in failed_batch:
            emit(result, "failed", error=result["error"], risk_level=None,
                 **{key: result.get(key) for key in ("content_hash", "rule_version", "detection_scope", "checked_at")})
    summary = {
        "checked": checked, "risk_0": sum(item["risk_level"] == 0 for item in all_results),
        "risk_1": sum(item["risk_level"] == 1 for item in all_results),
        "risk_2": sum(item["risk_level"] == 2 for item in all_results),
        "updated": updated, "results": all_results,
    }
    if sources is not None or stop_callback or item_callback or failures:
        summary.update(total=len(records) + missing_count, failed=len(failures), cancelled=cancelled, failures=failures)
    _emit_log(f"风险审核完成：检查 {checked} 条，失败 {len(failures)} 条，取消 {cancelled} 条，数据库更新 {updated} 条", log_callback)
    return summary


def main():
    parser = argparse.ArgumentParser(
        description="读取 zying_product 标题，用 AI 写入 0/1/2 侵权风险"
    )
    parser.add_argument(
        "--category",
        "--zying-category",
        dest="zying_category",
        default=None,
        help="智赢分类编号、完整路径或末级名称；不填表示全部分类",
    )
    parser.add_argument(
        "--hours",
        type=int,
        default=DEFAULT_HOURS,
        help=f"只审核最近多少小时入库的数据，0 表示不限（默认 {DEFAULT_HOURS}）",
    )
    parser.add_argument("--limit", type=int, default=0, help="最多审核多少条，0 表示不限")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help=f"每次发送给 AI 的商品数（默认 {DEFAULT_BATCH_SIZE}）",
    )
    parser.add_argument("--model", default=None, help="千问 模型，默认读取项目配置")
    parser.add_argument(
        "--ai-retries",
        type=int,
        default=DEFAULT_AI_RETRIES,
        help=f"AI JSON 结果不完整时的重试次数（默认 {DEFAULT_AI_RETRIES}）",
    )
    parser.add_argument(
        "--recheck",
        action="store_true",
        help="重新审核已有 0/1/2 结果的商品",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只输出判断结果，不写入风险级别和关键词",
    )
    args = parser.parse_args()
    try:
        scan_products(
            zying_category=args.zying_category,
            hours=max(0, args.hours),
            limit=max(0, args.limit),
            batch_size=max(1, args.batch_size),
            model=args.model,
            retries=max(0, args.ai_retries),
            recheck=args.recheck,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        parser.exit(status=1, message=f"风险审核失败：{exc}\n")


if __name__ == "__main__":
    main()
