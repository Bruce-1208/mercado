"""Describe missing saved collection data without making network requests."""
import json
import math

from erp.mercadolibre_page_facts import repair_source_facts


def parsed(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value if isinstance(value, dict) else {}


def finite_number(value):
    if value is None or not str(value).strip():
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError, OverflowError):
        return None


def collection_quality_issues(row):
    issues = []

    def add(field, message):
        issues.append({"field": field, "message": message})

    weight = finite_number(row.get("weight_g"))
    invalid_basis = str(row.get("weight_basis") or "").lower() in {
        "calculated_volumetric", "legacy_unknown", "plugin_volumetric_fallback",
    }
    if weight is None or weight <= 0 or invalid_basis:
        add("weight", "缺少有效实际重量")
    dimensions = [(key, label) for key, label in (
        ("package_length_cm", "长"), ("package_width_cm", "宽"), ("package_height_cm", "高"),
    ) if (finite_number(row.get(key)) or 0) <= 0]
    if dimensions:
        add("dimensions", "包装尺寸不全：缺少" + "、".join(label for _, label in dimensions))
    price, shipping = finite_number(row.get("sale_price_usd")), finite_number(row.get("shipping_fee_usd"))
    if price is None or price <= 0 or shipping is None or shipping < 0:
        reasons = []
        if price is None or price <= 0:
            reasons.append("美元售价")
        if shipping is None or shipping < 0:
            reasons.append("当前运费")
        add("net", "净收益未计算：待补" + "、".join(reasons))

    source = repair_source_facts(parsed(row.get("source_json") or row.get("source")))
    page = parsed(row.get("page_snapshot_json") or row.get("page_snapshot"))
    plugin = parsed(row.get("plugin_snapshot_json") or row.get("plugin_snapshot"))

    def has_value(value):
        if isinstance(value, dict):
            return any(has_value(value.get(key)) for key in
                       ("value_name", "value", "value_id", "values", "value_struct", "name", "number"))
        if isinstance(value, list):
            return any(has_value(item) for item in value)
        return value is not None and str(value).strip().lower() not in {"", "null", "none", "-", "—"}

    def attributes(items):
        if isinstance(items, dict):
            return [(str(name), has_value(value)) for name, value in items.items()]
        return [(str(item.get("name") or item.get("id") or "未命名属性"),
                 any(has_value(item.get(key)) for key in ("value_name", "value", "value_id", "values", "value_struct")))
                for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    values = {}
    for items in (source.get("attributes"), row.get("attributes"), page.get("specs"), plugin.get("attributes")):
        for name, complete in attributes(items):
            values[name] = values.get(name, False) or complete
    if not values:
        add("attributes", "属性不全：未采集到商品属性")
    else:
        missing = [name for name, complete in values.items() if not complete]
        if missing:
            add("attributes", "属性不全：" + "、".join(missing) + "未填写")
    for index, variant in enumerate(source.get("variations") or [], 1):
        if not isinstance(variant, dict):
            continue
        missing = [name for name, complete in
                   attributes(variant.get("attribute_combinations")) + attributes(variant.get("attributes")) if not complete]
        if missing:
            add("attributes", f"规格 {index} 属性不全：" + "、".join(missing) + "未填写")
    if source.get("variation_collection_status") == "needs_recollection":
        add("variations", "规格待核实：旧采集误将属性表作为变体，请重新采集详情")
    elif source.get("variation_collection_status") == "incomplete":
        add("variations", "规格未采全：页面存在多个选项，但尚未取得完整变体数据")
    return issues
