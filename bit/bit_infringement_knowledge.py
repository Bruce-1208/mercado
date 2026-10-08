"""侵权知识库的数据规则。

名单表示内部风险处置策略，不构成对品牌是否侵权的法律判断。
WIPO 商标按配置自动纳入风险拦截；其他自动线索需人工复核后生效。
"""


LIST_TYPE_WHITELIST = "whitelist"
LIST_TYPE_BLACKLIST = "blacklist"
LIST_TYPE_PENDING = "pending"
LIST_TYPE_BORDERLINE = "borderline"
LIST_TYPES = (LIST_TYPE_WHITELIST, LIST_TYPE_BLACKLIST, LIST_TYPE_PENDING, LIST_TYPE_BORDERLINE)
LIST_TYPE_LABELS = {
    LIST_TYPE_WHITELIST: "人工放行",
    LIST_TYPE_BLACKLIST: "风险拦截",
    LIST_TYPE_PENDING: "待复核",
    LIST_TYPE_BORDERLINE: "擦边品牌",
}


def normalize_list_type(value, allow_empty=False):
    text = str(value or "").strip().lower()
    aliases = {
        "whitelist": LIST_TYPE_WHITELIST,
        "white": LIST_TYPE_WHITELIST,
        "白名单": LIST_TYPE_WHITELIST,
        "人工放行": LIST_TYPE_WHITELIST,
        "blacklist": LIST_TYPE_BLACKLIST,
        "black": LIST_TYPE_BLACKLIST,
        "黑名单": LIST_TYPE_BLACKLIST,
        "风险拦截": LIST_TYPE_BLACKLIST,
        "borderline": LIST_TYPE_BORDERLINE,
        "擦边品牌": LIST_TYPE_BORDERLINE,
        "pending": LIST_TYPE_PENDING,
        "待复核": LIST_TYPE_PENDING,
    }
    if not text and allow_empty:
        return ""
    normalized = aliases.get(text)
    if not normalized:
        raise ValueError("名单类型只支持待复核、风险拦截、人工放行或擦边品牌")
    return normalized


def normalize_knowledge_record(record):
    if not isinstance(record, dict):
        raise ValueError("知识库记录格式无效")

    brand_name = " ".join(str(record.get("brand_name") or "").split())
    if not brand_name:
        raise ValueError("品牌名称不能为空")
    if len(brand_name) > 255:
        raise ValueError("品牌名称不能超过 255 个字符")

    notes = str(record.get("notes") or "").strip()
    if len(notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符")

    normalized = {
        "brand_name": brand_name,
        "list_type": normalize_list_type(record.get("list_type")),
        "notes": notes,
    }
    # Keep the legacy three-field normalization contract for old callers.
    # Extended fields are explicit on writes and always supplied on reads.
    if "aliases" in record:
        normalized["aliases"] = normalize_rule_values(record["aliases"], "品牌别名")
    if "scope" in record:
        normalized["scope"] = normalize_rule_scope(record["scope"])
    if "valid_until" in record:
        normalized["valid_until"] = normalize_valid_until(record["valid_until"])
    if "reviewed_by" in record:
        reviewer = str(record.get("reviewed_by") or "").strip()
        if len(reviewer) > 128:
            raise ValueError("复核人名称不能超过 128 个字符")
        normalized["reviewed_by"] = reviewer
    if "rule_version" in record:
        try:
            version = int(record["rule_version"])
        except (TypeError, ValueError) as exc:
            raise ValueError("规则版本无效，请刷新后重试") from exc
        if version < 1:
            raise ValueError("规则版本无效，请刷新后重试")
        normalized["rule_version"] = version
    if "source_detail" in record:
        detail = str(record.get("source_detail") or "").strip()
        if len(detail) > 1000:
            raise ValueError("原始来源不能超过 1000 个字符")
        normalized["source_detail"] = detail
    return normalized


def parse_bulk_brand_lines(value, list_type, notes=""):
    """把多行品牌文本转换成去重后的知识库记录。"""
    normalized_type = normalize_list_type(list_type)
    normalized_notes = str(notes or "").strip()
    if len(normalized_notes) > 2000:
        raise ValueError("备注不能超过 2000 个字符")

    records = []
    seen = set()
    for raw_line in str(value or "").splitlines():
        brand_name = " ".join(raw_line.split())
        if not brand_name:
            continue
        record = normalize_knowledge_record(
            {
                "brand_name": brand_name,
                "list_type": normalized_type,
                "notes": normalized_notes,
            }
        )
        key = record["brand_name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
    if not records:
        raise ValueError("请至少输入一个品牌，每行一个")
    if len(records) > 1000:
        raise ValueError("单次最多新增 1000 个品牌")
    return records


def list_type_label(value):
    return LIST_TYPE_LABELS.get(str(value or ""), "")


def effective_list_type(record):
    """Honor WIPO blacklist imports while other automatic evidence awaits review."""
    source_type = str((record or {}).get("source_type") or "manual").strip().lower()
    list_type = str((record or {}).get("list_type") or "").strip().lower()
    if source_type == "wipo" and list_type == LIST_TYPE_BLACKLIST:
        return LIST_TYPE_BLACKLIST
    if source_type != "manual":
        return LIST_TYPE_PENDING
    return list_type


def normalize_rule_values(values, label="适用范围"):
    import re
    if isinstance(values, str):
        values = re.split(r"[,，\n\r]+", values)
    if values is None:
        values = []
    if not isinstance(values, (list, tuple)):
        raise ValueError(f"{label}格式无效")
    result, seen = [], set()
    for value in values:
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(f"{label}格式无效")
        text = " ".join(str(value).split())
        if not text:
            continue
        if len(text) > 255:
            raise ValueError(f"{label}单项不能超过 255 个字符")
        if text.casefold() not in seen:
            result.append(text)
            seen.add(text.casefold())
    if len(result) > 100:
        raise ValueError(f"{label}最多填写 100 项")
    return result


def normalize_rule_scope(scope, allow_legacy=False):
    if scope is None and allow_legacy:
        return {"mode": "global", "sites": [], "categories": [], "shops": []}
    if not isinstance(scope, dict) or scope.get("mode") not in ("global", "scoped"):
        raise ValueError("请选择全局或限定范围的品牌规则")
    result = {"mode": scope["mode"]}
    for key, label in (("sites", "站点"), ("categories", "类目"), ("shops", "店铺")):
        result[key] = normalize_rule_values(scope.get(key), label)
    result["sites"] = [value.upper() for value in result["sites"]]
    if result["mode"] == "scoped" and not any(result[key] for key in ("sites", "categories", "shops")):
        raise ValueError("限定规则至少填写一个站点、类目或店铺；商品放行请使用商品复核")
    if result["mode"] == "global" and any(result[key] for key in ("sites", "categories", "shops")):
        raise ValueError("全局规则不能同时填写限定范围")
    return result


def normalize_valid_until(value):
    from datetime import date
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("有效期必须为 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != text:
        raise ValueError("有效期必须为 YYYY-MM-DD 日期")
    return text


def knowledge_rule_names(record):
    """Canonical brand and aliases, deduplicated for the title matcher."""
    import json
    aliases = (record or {}).get("aliases", (record or {}).get("aliases_json") or [])
    if isinstance(aliases, str):
        try:
            aliases = json.loads(aliases)
        except ValueError:
            aliases = []
    try:
        return normalize_rule_values([str((record or {}).get("brand_name") or ""),
                                      *normalize_rule_values(aliases, "品牌别名")])
    except ValueError:
        return normalize_rule_values([str((record or {}).get("brand_name") or "")])


def knowledge_rule_matches(record, *, site="", category="", shop="", now=None):
    """Fail closed on missing context, expiry, malformed scopes or pending evidence."""
    import json
    from datetime import date, datetime
    if effective_list_type(record) not in (LIST_TYPE_WHITELIST, LIST_TYPE_BLACKLIST, LIST_TYPE_BORDERLINE):
        return False
    raw_scope = record.get("scope", record.get("scope_json"))
    try:
        if isinstance(raw_scope, str):
            raw_scope = json.loads(raw_scope) if raw_scope else None
        scope = normalize_rule_scope(raw_scope, allow_legacy=True)
        expiry = normalize_valid_until(record.get("valid_until"))
        if now is None:
            today = date.today()
        elif isinstance(now, datetime):
            today = now.date()
        elif isinstance(now, date):
            today = now
        else:
            today = date.fromisoformat(str(now)[:10])
        if expiry and date.fromisoformat(expiry) < today:
            return False
    except (TypeError, ValueError):
        return False
    if record.get("deleted_at") is not None:
        return False
    for key, context in (("sites", site), ("categories", category), ("shops", shop)):
        allowed = {str(value).strip().casefold() for value in scope[key]}
        if allowed and str(context or "").strip().casefold() not in allowed:
            return False
    return True
