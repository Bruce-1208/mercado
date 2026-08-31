"""从侵权记录和有效在售链接中提取品牌/IP，更新侵权知识库。"""

import json
import re
import unicodedata


GENERIC_BRAND_NAMES = {
    "generic",
    "generico",
    "genérico",
    "no brand",
    "unbranded",
    "sin marca",
    "marca generica",
    "marca genérica",
    "other",
    "otros",
    "n/a",
    "na",
    "无品牌",
    "通用",
    "通用品牌",
}


def _chunks(items, size):
    size = max(1, int(size or 100))
    for start in range(0, len(items), size):
        yield items[start : start + size]


def normalize_brand_name(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip(" \t\r\n.,;:|/\\-—_()[]{}\"'")
    if not text or len(text) < 2 or len(text) > 100:
        return ""
    if text.casefold() in GENERIC_BRAND_NAMES or text.isdigit():
        return ""
    return text


def _normalize_extracted_names(payload):
    if isinstance(payload, dict):
        payload = payload.get("brands") or payload.get("results") or payload.get("data") or []
    if not isinstance(payload, list):
        raise ValueError("AI 返回的品牌结果不是数组")
    names = []
    seen = set()
    for item in payload:
        value = item.get("name") if isinstance(item, dict) else item
        name = normalize_brand_name(value)
        key = name.casefold()
        if not name or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def extract_brand_names(
    titles,
    *,
    source_label,
    batch_size=150,
    retries=2,
    chat_client=None,
    log_callback=None,
):
    """使用 DeepSeek 按批提取标题中明确出现的品牌、商标或受保护 IP。"""
    if chat_client is None:
        from AI_Agent.deepseek import chat_deepseek

        chat_client = chat_deepseek
    from bit.bit_risk import _extract_json_payload

    clean_titles = []
    seen_titles = set()
    for value in titles or ():
        title = re.sub(r"\s+", " ", str(value or "")).strip()
        key = title.casefold()
        if title and key not in seen_titles:
            seen_titles.add(key)
            clean_titles.append(title[:600])
    if not clean_titles:
        return []

    system_prompt = """你是跨境电商知识产权数据整理员。请从商品标题中提取明确出现的：
1. 品牌或商标；2. 影视、动漫、游戏等受保护 IP；3. 角色、名人、球队或赛事名称。

不要返回普通品类、材质、颜色、尺寸、功能词，也不要返回 Generic、Genérico、No Brand、Sin Marca 等通用占位品牌。
合并大小写或轻微拼写差异，只返回标题中确有依据的名称。只输出 JSON 数组，不要 Markdown。
格式：[ {"name":"品牌或IP"} ]；没有结果时返回 []。"""
    names = []
    seen_names = set()
    batches = list(_chunks(clean_titles, batch_size))
    for batch_index, batch in enumerate(batches, start=1):
        if log_callback:
            log_callback(
                f"{source_label}品牌提取 {batch_index}/{len(batches)}，"
                f"本批 {len(batch)} 个标题"
            )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": "请提取以下标题：\n" + json.dumps(batch, ensure_ascii=False),
            },
        ]
        last_error = None
        batch_names = []
        for attempt in range(max(0, int(retries)) + 1):
            response = chat_client(
                messages,
                temperature=0,
                max_tokens=max(1000, min(6000, len(batch) * 35)),
            )
            try:
                if not str(response or "").strip():
                    raise ValueError("AI 返回空内容")
                batch_names = _normalize_extracted_names(
                    _extract_json_payload(response)
                )
                last_error = None
                break
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                if log_callback and attempt < max(0, int(retries)):
                    log_callback(
                        f"{source_label}批次 {batch_index} 返回异常，"
                        f"正在重试 {attempt + 1}/{max(0, int(retries))}：{exc}"
                    )
                messages.append(
                    {
                        "role": "user",
                        "content": "上次结果无法解析，请只返回完整 JSON 数组；没有品牌时返回 []。",
                    }
                )
        if last_error is not None:
            raise ValueError(
                f"{source_label}品牌提取批次 {batch_index} 连续失败：{last_error}"
            )
        for name in batch_names:
            key = name.casefold()
            if key not in seen_names:
                seen_names.add(key)
                names.append(name)
    return names


def _normalized_search_text(value):
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def _brand_evidence(name, rows):
    keyword = _normalized_search_text(name)
    matches = []
    for row in rows or ():
        title = str((row or {}).get("title") or "").strip()
        if keyword and keyword in _normalized_search_text(title):
            matches.append(title)
    return max(1, len(matches)), matches[:2]


def analyze_knowledge_sources(
    sources,
    *,
    writer,
    brand_extractor=None,
    batch_size=150,
    log_callback=None,
):
    """黑名单优先；人工记录是否覆盖由数据库写入层决定。"""
    source_data = dict(sources or {})
    infraction_rows = list(source_data.get("infraction_rows") or [])
    active_rows = list(source_data.get("active_rows") or [])
    extractor = brand_extractor or extract_brand_names

    black_names = _normalize_extracted_names(
        extractor(
            [row.get("title") for row in infraction_rows],
            source_label="侵权记录",
            batch_size=batch_size,
            log_callback=log_callback,
        )
    )
    active_names = _normalize_extracted_names(
        extractor(
            [row.get("title") for row in active_rows],
            source_label="活跃成交链接",
            batch_size=batch_size,
            log_callback=log_callback,
        )
    )
    black_keys = {name.casefold() for name in black_names}
    white_names = [name for name in active_names if name.casefold() not in black_keys]

    records = []
    for name in black_names:
        evidence_count, examples = _brand_evidence(name, infraction_rows)
        records.append(
            {
                "brand_name": name,
                "list_type": "blacklist",
                "notes": f"自动分析：在侵权记录中命中 {evidence_count} 条。",
                "evidence_count": evidence_count,
                "source_detail": "；".join(examples)[:1000],
            }
        )
    for name in white_names:
        evidence_count, examples = _brand_evidence(name, active_rows)
        records.append(
            {
                "brand_name": name,
                "list_type": "whitelist",
                "notes": (
                    f"自动分析：在当前 active 且已有销量的链接中命中 "
                    f"{evidence_count} 条，未在侵权记录提取结果中命中。"
                ),
                "evidence_count": evidence_count,
                "source_detail": "；".join(examples)[:1000],
            }
        )

    if log_callback:
        log_callback(
            f"分析得到黑名单 {len(black_names)} 个、白名单 {len(white_names)} 个；"
            "同名冲突已按黑名单优先处理"
        )
    write_result = writer(records)
    return {
        "infraction_titles": len(infraction_rows),
        "active_titles": len(active_rows),
        "blacklist_candidates": len(black_names),
        "whitelist_candidates": len(white_names),
        "write_result": write_result or {},
    }
