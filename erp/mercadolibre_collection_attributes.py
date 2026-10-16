"""Read-only required-attribute checks against a collected item's own category."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from erp.mercadolibre_attribute_rules import (
    ATTRIBUTE_ID_ALIASES, attribute_tag_enabled, is_read_only_attribute,
    resolve_schema_attribute_id, extract_listing_attributes_from_detail,
)
from erp.mercadolibre_collection_quality import finite_number, parsed
from erp.mercadolibre_page_facts import repair_source_facts


def collection_category_id(row):
    source = parsed(row.get("source_json") or row.get("source"))
    # Preserve explicit row values (including invalid overrides), then recover
    # categories saved by older clients in their native snapshots.
    if row.get("category_id"):
        return str(row["category_id"]).strip().upper()
    for snapshot in (source, parsed(row.get("page_snapshot_json") or row.get("page_snapshot")),
                     parsed(row.get("plugin_snapshot_json") or row.get("plugin_snapshot"))):
        for key in ("category_id", "categoryId", "categoryID"):
            value = str(snapshot.get(key) or "").strip().upper()
            if re.fullmatch(r"(?:ML[A-Z]|MCO|CBT)\d+", value):
                return value
    return ""


def attribute_check_unavailable(row, message="类目规则暂时无法读取，请稍后刷新自动重试"):
    return {"id": row.get("id"), "category_id": collection_category_id(row),
            "status": "unavailable", "missing": [], "pending": [], "message": message}


def _present(value):
    if isinstance(value, list):
        return any(_present(item) for item in value)
    if isinstance(value, dict):
        # An attribute's label or ID alone is not evidence of a filled value.
        return any(_present(value.get(key)) for key in
                   ("value_name", "value", "value_id", "value_struct")) or any(
            _present(item.get("name")) or _present(item.get("id")) or _present(item.get("struct"))
            for item in value.get("values") or [] if isinstance(item, dict)
        ) or ("number" in value and _present(value["number"]))
    return value is not None and str(value).strip().lower() not in {
        "", "null", "none", "nan", "-", "—", "-1",
    }


def _attributes(value):
    if isinstance(value, dict):
        return [{"name": name, **item} if isinstance(item, dict)
                else {"name": name, "value": item} for name, item in value.items()]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def check_required_attributes(row, schema):
    """Never infer product facts or call a publication endpoint.

    Conditional requirements cannot be certified by category metadata alone;
    report them separately rather than claiming publication readiness.
    """
    category_id = collection_category_id(row)
    if not re.fullmatch(r"(?:ML[A-Z]|MCO|CBT)\d+", category_id):
        return attribute_check_unavailable(row, "未采集到有效的平台类目，请更新插件后重新采集此商品，再核验必填属性")
    if not isinstance(schema, list) or any(not isinstance(d, dict) or not d.get("id") for d in schema):
        return attribute_check_unavailable(row)
    source = repair_source_facts(parsed(row.get("source_json") or row.get("source")))
    page = parsed(row.get("page_snapshot_json") or row.get("page_snapshot"))
    plugin = parsed(row.get("plugin_snapshot_json") or row.get("plugin_snapshot"))

    schema_ids = {str(definition['id']) for definition in schema}

    def resolved(item):
        key = resolve_schema_attribute_id(item, schema)
        # Local and CBT categories use different IDs for package measurements.
        # Prefer an exact schema match, and never alias ordinary product dimensions.
        if key not in schema_ids and re.fullmatch(r'(SELLER_)?PACKAGE_(LENGTH|WIDTH|HEIGHT|WEIGHT)', key):
            alternate = key.removeprefix('SELLER_') if key.startswith('SELLER_') else 'SELLER_' + key
            if alternate in schema_ids:
                return alternate
        return key

    def filled(attributes):
        return {resolved(item) for item in attributes if _present(item)}

    parent = filled([item for value in (source.get("attributes"), row.get("attributes"),
                                       page.get("specs"), plugin.get("attributes"))
                     for item in _attributes(value)])
    # Older collector snapshots can store identifiers outside the attribute list.
    parent.update(filled([item for snapshot in (source, row, page, plugin)
                          for item in extract_listing_attributes_from_detail(snapshot)
                          if item.get('id') == 'GTIN']))
    for attribute_id, field in (("SELLER_PACKAGE_LENGTH", "package_length_cm"),
                                ("SELLER_PACKAGE_WIDTH", "package_width_cm"),
                                ("SELLER_PACKAGE_HEIGHT", "package_height_cm"),
                                ("SELLER_PACKAGE_WEIGHT", "weight_g")):
        if field == "weight_g" and str(row.get("weight_basis") or "").lower() in {
            "calculated_volumetric", "legacy_unknown", "plugin_volumetric_fallback",
        }:
            continue
        if (finite_number(row.get(field)) or 0) > 0:
            parent.add(resolved({'id': attribute_id}))
    variants = source.get("variations") or []
    scopes = []
    for index, variant in enumerate(variants, 1):
        if not isinstance(variant, dict):
            continue
        attrs = _attributes(variant.get("attributes")) + _attributes(variant.get("attribute_combinations"))
        # A blank explicit SKU value overrides a shared product value.
        overrides = {resolved(item) for item in attrs}
        scopes.append((f"规格 {index}", str(variant.get("seller_sku") or variant.get("seller_custom_field") or variant.get("id") or ""),
                       (parent - overrides) | filled(attrs)))
    scopes = scopes or [("商品", "", parent)]
    condition = str(source.get("condition") or row.get("condition") or "").lower()
    catalog = source.get("catalog_listing") is True
    labels = {value: name for name, value in ATTRIBUTE_ID_ALIASES.items()
              if re.search(r"[\u3400-\u9fff]", name)}
    missing, pending = [], []
    required_ids = set()
    for definition in schema:
        if is_read_only_attribute(definition):
            continue
        key = str(definition["id"])
        tagged = lambda tag: attribute_tag_enabled(definition, tag)
        required = tagged("required") or (tagged("new_required") and condition == "new") or (
            catalog and any(tagged(tag) for tag in ("catalog_required", "catalog_listing_required", "catalog_child_required")))
        conditional = tagged("conditional_required") or (tagged("new_required") and not condition)
        if not required and not conditional:
            continue
        if required:
            required_ids.add(key)
        name = definition.get("name_zh") or labels.get(key) or definition.get("name") or key
        for scope, sku, values in scopes:
            if key in values:
                continue
            entry = {"attribute_id": key, "name": str(name), "scope": scope, "sku": sku}
            if key == "GTIN" and "EMPTY_GTIN_REASON" in values:
                # Whether this exemption applies must still be verified at publication.
                pending.append({**entry, "reason": "已填写无 GTIN 原因，适用性待上架预检确认"})
            elif required:
                missing.append(entry)
            else:
                pending.append({**entry, "reason": "条件必填，待上架预检确认"})
    if source.get("variation_collection_status") in {"incomplete", "needs_recollection"}:
        pending.append({"attribute_id": "", "name": "规格资料", "scope": "商品", "sku": "",
                        "reason": "规格未采全，请重新采集后核验"})
    status = "missing" if missing else "pending" if pending else "complete"
    return {"id": row.get("id"), "category_id": category_id, "status": status,
            "missing": missing, "pending": pending, "required_count": len(required_ids),
            "missing_count": len(missing), "checked_at": datetime.now(timezone.utc).isoformat(),
            "message": {"missing": f"缺少 {len(missing)} 项必填属性", "pending": "条件属性待确认",
                        "complete": "类目必填属性齐全"}[status]}


def check_collection_rows(rows, client_for_category):
    """Deduplicate category reads; the publisher's schema cache has a 24h TTL."""
    from erp.mercadolibre_follow_sell import _category_attribute_schema

    schemas = {}
    results = []
    for row in rows:
        category = collection_category_id(row)
        if not re.fullmatch(r"(?:ML[A-Z]|MCO|CBT)\d+", category):
            results.append(check_required_attributes(row, None))
            continue
        if category not in schemas:
            try:
                schemas[category] = _category_attribute_schema(client_for_category(category), category)
            except Exception:
                schemas[category] = None
        results.append(check_required_attributes(row, schemas[category]))
    return results
