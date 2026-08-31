"""侵权知识库的数据规则。

白名单表示该品牌不侵权，黑名单表示该品牌侵权。数据库和工作台接口共用
这里的校验逻辑，避免不同入口对同一条记录产生不同解释。
"""


LIST_TYPE_WHITELIST = "whitelist"
LIST_TYPE_BLACKLIST = "blacklist"
LIST_TYPES = (LIST_TYPE_WHITELIST, LIST_TYPE_BLACKLIST)
LIST_TYPE_LABELS = {
    LIST_TYPE_WHITELIST: "白名单",
    LIST_TYPE_BLACKLIST: "黑名单",
}


def normalize_list_type(value, allow_empty=False):
    text = str(value or "").strip().lower()
    aliases = {
        "whitelist": LIST_TYPE_WHITELIST,
        "white": LIST_TYPE_WHITELIST,
        "白名单": LIST_TYPE_WHITELIST,
        "blacklist": LIST_TYPE_BLACKLIST,
        "black": LIST_TYPE_BLACKLIST,
        "黑名单": LIST_TYPE_BLACKLIST,
    }
    if not text and allow_empty:
        return ""
    normalized = aliases.get(text)
    if not normalized:
        raise ValueError("名单类型只支持白名单或黑名单")
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

    return {
        "brand_name": brand_name,
        "list_type": normalize_list_type(record.get("list_type")),
        "notes": notes,
    }


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
