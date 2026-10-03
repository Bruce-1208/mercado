"""Publication contracts: evidence, diagnostics and immutable draft envelopes.

No network or database writes live here. The publisher checkpoints the envelope
through the existing publication-record store before making a create request.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import time
from contextvars import ContextVar
from decimal import Decimal, InvalidOperation

from erp.mercadolibre_attribute_rules import (
    attribute_tag_enabled, is_read_only_attribute, is_required_attribute,
    semantic_value_key, match_enumerated_value,
)

RULE_VERSION = "2026-10-03.1"
DRAFT_TTL_SECONDS = 900
ACTIVE_REPORT = ContextVar("mercado_publication_report", default=None)
ACTIVE_DRAFT = ContextVar("mercado_publication_draft", default=None)
ACTIVE_SCOPE = ContextVar("mercado_attribute_scope", default="商品")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def new_report():
    return {"status": "ready", "rule_version": RULE_VERSION, "issues": [], "mappings": []}


def issue(code, message, *, attribute_id="", scope=None, severity="error", source=None):
    report = ACTIVE_REPORT.get()
    entry = {"code": code, "message": message, "attribute_id": attribute_id,
             "scope": scope or ACTIVE_SCOPE.get(), "severity": severity}
    if source is not None:
        entry["source"] = copy.deepcopy(source)
    if report is not None and entry not in report["issues"]:
        report["issues"].append(entry)
        refresh_status(report)
    return entry


def refresh_status(report):
    errors = [row for row in report["issues"] if row["severity"] == "error"]
    report["status"] = ("mapping_conflict" if any(row["code"] != "missing_required" for row in errors)
                        else "needs_completion" if errors else "ready")
    return report


def mapped(source, target, reason):
    report = ACTIVE_REPORT.get()
    if report is not None:
        report["mappings"].append({"scope": ACTIVE_SCOPE.get(), "source": copy.deepcopy(source),
                                   "target": copy.deepcopy(target), "rule": reason})


def has_value(attr):
    return any(attr.get(key) not in (None, "", [], {})
               for key in ("value_id", "value_name", "value_struct", "values"))


def normalize_numeric_unit(attribute, definition):
    """Convert only exact, dimension-compatible units; never infer dimensions."""
    if definition.get("value_type") != "number_unit" or attribute.get("values"):
        return
    units = {str(u.get("id")) for u in definition.get("allowed_units") or []}
    struct = attribute.get("value_struct") or {}
    number, unit = struct.get("number"), struct.get("unit")
    if number is None:
        match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(\S+)\s*", str(attribute.get("value_name") or ""))
        if not match:
            return
        number, unit = match.groups()
    if unit in units or not units:
        return
    scales = {"mm": ("length", "0.001"), "cm": ("length", "0.01"), "m": ("length", "1"),
              "mg": ("mass", "0.001"), "g": ("mass", "1"), "kg": ("mass", "1000")}
    if unit not in scales:
        return
    candidates = sorted(u for u in units if u in scales and scales[u][0] == scales[unit][0])
    if not candidates:
        return
    target = definition.get("default_unit")
    if target not in candidates:
        target = candidates[0]
    try:
        converted = Decimal(str(number)) * Decimal(scales[unit][1]) / Decimal(scales[target][1])
        if not converted.is_finite():
            return
    except (InvalidOperation, ValueError):
        return
    original = copy.deepcopy(attribute)
    attribute["value_name"] = f"{format(converted.normalize(), 'f')} {target}"
    attribute["value_struct"] = {"number": float(converted), "unit": target}
    mapped(original, attribute, "exact_unit_conversion")


def validate_attributes(attributes, schema, *, scope="商品", combinations=False):
    definitions = {a["id"]: a for a in schema if a.get("id")}
    seen = {}
    for attr in attributes:
        key = str(attr.get("id") or "")
        definition = definitions.get(key)
        def fail(code, message):
            issue(code, message, attribute_id=key, scope=scope, source=attr)
        if key in seen:
            fail("duplicate_attribute", f"{key} 出现重复或冲突值")
        seen[key] = attr
        if key == "SELLER_SKU":
            continue
        if definition is None:
            fail("unknown_attribute", f"目标类目不支持属性 {key}")
            continue
        if is_read_only_attribute(definition):
            fail("read_only", f"{key} 是只读属性")
        if combinations and definition.get("tags") is not None and not attribute_tag_enabled(definition, "allow_variations"):
            fail("invalid_placement", f"{key} 不能作为目标类目的变体区分属性")
        values = attr.get("values") or [{"id": attr.get("value_id"), "name": attr.get("value_name"),
                                         "struct": attr.get("value_struct")}]
        if len(values) > 1 and not attribute_tag_enabled(definition, "multivalued"):
            fail("multiple_values", f"{key} 不允许同时填写多个值")
        for value in values:
            name = str(value.get("name") or "")
            limit = definition.get("value_max_length")
            if limit and len(name) > int(limit):
                fail("invalid_length", f"{key} 超过最大长度 {limit}")
            kind = definition.get("value_type")
            if kind in {"number", "number_unit"} and has_value(attr):
                struct = value.get("struct") or {}
                number = struct.get("number")
                unit = struct.get("unit")
                if number is None:
                    match = re.fullmatch(r"\s*([+-]?(?:\d+(?:\.\d+)?|\.\d+))\s*(.*?)\s*", name)
                    number, unit = (match.group(1), match.group(2)) if match else (None, None)
                try:
                    valid_number = math.isfinite(float(number))
                except (TypeError, ValueError):
                    valid_number = False
                if not valid_number:
                    fail("invalid_number", f"{key} 必须填写有效数值")
                units = {str(u.get("id")) for u in definition.get("allowed_units") or []}
                if kind == "number_unit" and (not unit or (units and str(unit) not in units)):
                    fail("invalid_unit", f"{key} 的单位不符合目标类目规则")
                if kind == "number" and unit:
                    fail("invalid_unit", f"{key} 应填写不带单位的数值")
            if kind in {"boolean", "list"}:
                choices = definition.get("values") or []
                # Explicit target-schema ID or a supported name is required.
                if choices and not any(
                    (value.get("id") is not None and str(value["id"]) == str(c.get("id")))
                    or (name and semantic_value_key(key, name) == semantic_value_key(key, c.get("name")))
                    for c in choices
                ):
                    fail("unmapped_value", f"{key} 的值无法确定映射到目标类目")
    return seen


def validate_payload(payload, schemas, source):
    """Check the actual payload, including all children of a UP family."""
    payloads = payload if isinstance(payload, list) else [payload]
    for index, item in enumerate(payloads, 1):
        category = item.get("category_id")
        if not category:  # Add-marketplace request: no product attributes sent.
            continue
        schema = schemas[category]
        scope = f"SKU {index}" if isinstance(payload, list) else "商品"
        parent = validate_attributes(item.get("attributes") or [], schema, scope=scope)
        variants = list(item.get("variations") or [])
        for site in item.get("sites_to_sell") or []:
            variants.extend(site.get("variations") or [])
        children = []
        signatures = set()
        for n, variant in enumerate(variants, 1):
            label = f"SKU {variant.get('seller_sku') or n}"
            combo = variant.get("attribute_combinations") or []
            combination_values = validate_attributes(combo, schema, scope=label, combinations=True)
            child = validate_attributes(variant.get("attributes") or [], schema, scope=label)
            for key in set(combination_values) & (set(parent) | set(child)):
                issue("conflicting_placement", f"{key} 同时出现在商品属性或 SKU 属性与变体组合中", attribute_id=key, scope=label)
            child.update(combination_values)
            children.append((label, {**parent, **child}))
            signature = digest(sorted((key, str(a.get("value_id") or semantic_value_key(key, a.get("value_name"))))
                                      for key, a in combination_values.items()))
            if not combo or signature in signatures:
                issue("duplicate_variation", "变体组合缺失或重复，不能合并库存", scope=label)
            signatures.add(signature)
        for label, attrs in children or [(scope, parent)]:
            for definition in schema:
                key = definition.get("id")
                if key == "GTIN" and has_value(attrs.get("EMPTY_GTIN_REASON", {})):
                    continue
                if is_required_attribute(definition) and not is_read_only_attribute(definition) and not has_value(attrs.get(key, {})):
                    issue("missing_required", f"缺少目标类目必填属性 {key}", attribute_id=key, scope=label)
        expected = len(source.get("variations") or [])
        if not isinstance(payload, list) and expected != len(variants):
            issue("variation_count", "映射前后的 SKU 数量不一致")
    if isinstance(payload, list) and len(payload) != len(source.get("variations") or []):
        issue("variation_count", "映射前后的 SKU 数量不一致")


def apply_corrections(payload, schemas, corrections):
    """Apply explicit, evidenced user edits to this draft, never to raw source."""
    if not isinstance(corrections, list) or len(corrections) > 200:
        raise ValueError("属性修正必须是列表，每份草稿最多 200 项")
    # Recompute required-field diagnostics from the corrected payload below.
    report = ACTIVE_REPORT.get()
    if report is not None:
        report["issues"][:] = [row for row in report["issues"] if row["code"] != "missing_required"]
    for correction in corrections:
        key = str(correction.get("attribute_id") or "")
        index = correction.get("sku_index")
        index = int(index) if index is not None else None
        evidence = str(correction.get("evidence") or "").strip()
        if not evidence:
            raise ValueError("请填写属性值的依据，例如包装标签或供应商规格")
        if isinstance(payload, list):
            if index is None or not 0 <= index < len(payload):
                raise ValueError("商品族属性修正必须指定有效 SKU")
            item = payload[index]
            scope = f"SKU {index + 1}"
            attributes = item.setdefault("attributes", [])
        else:
            item = payload
            scope = "商品"
            attributes = item.setdefault("attributes", [])
            if index is not None:
                variants = item.get("variations") or next((site["variations"] for site in item.get("sites_to_sell") or [] if site.get("variations")), [])
                if not 0 <= index < len(variants):
                    raise ValueError("属性修正的 SKU 已变化，请重新预检")
                variant = variants[index]
                scope = f"SKU {variant.get('seller_sku') or index + 1}"
                location = "attribute_combinations" if correction.get("combination") else "attributes"
                attributes = variant.setdefault(location, [])
        if correction.get("category_id") != item.get("category_id"):
            raise ValueError("属性修正的目标类目已变化，请重新预检")
        definition = next((d for d in schemas[item["category_id"]] if d.get("id") == key), None)
        if definition is None or is_read_only_attribute(definition) or key == "SELLER_SKU":
            raise ValueError(f"不能修正目标属性 {key}")
        target = {"id": key}
        if correction.get("value_id"):
            value = next((v for v in definition.get("values") or [] if str(v.get("id")) == str(correction["value_id"])), None)
            if value is None:
                raise ValueError(f"{key} 的选项已变化，请重新预检")
            target.update(value_id=str(value["id"]), value_name=value.get("name"))
        else:
            target["value_name"] = str(correction.get("value_name") or "").strip()
            if definition.get("value_type") in {"boolean", "list"}:
                value = match_enumerated_value(key, None, target["value_name"], definition.get("values") or [])
                if value is not None:
                    target.update(value_id=str(value["id"]), value_name=value.get("name"))
        if not has_value(target):
            raise ValueError(f"{key} 的修正值不能为空")
        original = [copy.deepcopy(a) for a in attributes if a.get("id") == key]
        attributes[:] = [a for a in attributes if a.get("id") != key] + [target]
        report = ACTIVE_REPORT.get()
        if report is not None:
            report["issues"][:] = [row for row in report["issues"] if not (row.get("attribute_id") == key and row.get("scope") == scope)]
        token = ACTIVE_SCOPE.set(scope)
        try:
            mapped({"original": original, "evidence": evidence}, target, "user_confirmed_for_this_product_and_target")
            normalize_numeric_unit(target, definition)
        finally:
            ACTIVE_SCOPE.reset(token)
    refresh_status(ACTIVE_REPORT.get())


def correction_fields(draft):
    """Offer only schema-defined fields; choices come from the checked target."""
    payload = draft["payload"]
    rows = payload if isinstance(payload, list) else [payload]
    fields = []
    for n, item in enumerate(rows):
        category = item.get("category_id")
        if category not in draft["schemas"]:
            continue
        scopes = [(n if isinstance(payload, list) else None, f"SKU {n+1}" if isinstance(payload, list) else "商品")]
        variants = item.get("variations") or next((s["variations"] for s in item.get("sites_to_sell") or [] if s.get("variations")), [])
        scopes.extend((i, f"SKU {v.get('seller_sku') or i+1}") for i, v in enumerate(variants))
        for index, scope in scopes:
            for definition in draft["schemas"][category]:
                key = definition.get("id")
                if key == "SELLER_SKU" or is_read_only_attribute(definition):
                    continue
                if not any(row.get("attribute_id") == key and row.get("scope") == scope for row in draft["report"]["issues"]):
                    continue
                fields.append({"category_id": category, "attribute_id": key, "sku_index": index,
                               "scope": scope, "name": definition.get("name") or key,
                               "combination": bool(variants and index is not None and attribute_tag_enabled(definition, "allow_variations")),
                               "choices": definition.get("values") if definition.get("value_type") in {"boolean", "list"} else []})
    return fields


def make_draft(payload, source, description, context, schemas, report, endpoint, action):
    draft = copy.deepcopy({"payload": payload, "source": source, "description": description,
                           "context": context, "schemas": schemas, "report": report,
                           "endpoint": endpoint, "action": action, "created_at": time.time(),
                           "rule_version": RULE_VERSION})
    draft["revision"] = digest(draft)
    return draft


def verify_draft(draft, source, description, context, schemas):
    body = {k: v for k, v in draft.items() if k not in {"revision", "submission_payload", "submission_hash", "submission_attempts"}}
    if digest(body) != draft.get("revision"):
        raise ValueError("上架草稿内容已改变，请重新预检")
    if draft.get("rule_version") != RULE_VERSION or not 0 <= time.time() - draft["created_at"] <= DRAFT_TTL_SECONDS:
        raise ValueError("上架草稿已过期或映射规则已更新，请重新预检")
    for key, value in {"source": source, "description": description, "context": context, "schemas": schemas}.items():
        if digest(value) != digest(draft.get(key)):
            raise ValueError("商品、账号、发布设置或类目规则已变化，请重新预检")
    if draft["report"]["status"] != "ready":
        raise ValueError("存在未解决的属性问题，不能提交草稿")
    return copy.deepcopy(draft["payload"])


def retry_corrections(draft, source, description, context):
    """Reuse human evidence only for the unchanged product and target.

    A retry always builds and checks a new payload against current schemas;
    expiration of a submission draft does not erase the user's evidence.
    """
    corrections = (draft.get("context") or {}).get("attribute_corrections") or []
    if not corrections:
        return []
    body = {k: v for k, v in draft.items() if k not in {"revision", "submission_payload", "submission_hash", "submission_attempts"}}
    if digest(body) != draft.get("revision"):
        raise ValueError("历史属性修正记录已改变，请重新预检")
    if digest(source) != digest(draft.get("source")) or digest(description) != digest(draft.get("description")):
        raise ValueError("商品资料已变化，不能复用历史属性修正，请重新预检")
    for key in ("user_id", "account_site", "user_products", "destination_site"):
        if context.get(key) != draft["context"].get(key):
            raise ValueError("历史属性修正不属于当前目标账号及站点，请重新预检")
    return copy.deepcopy(corrections)
