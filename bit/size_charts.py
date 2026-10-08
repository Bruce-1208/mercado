"""Scoped local template maintenance and verified official size-chart uploads."""

from __future__ import annotations

import hashlib
import math
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from typing import Any

from bit import bit_mysql
from bit.bit_store_link_sync import _client_and_token
from erp.mercadolibre_size_chart_store import SizeChartStore, json_text, normalize_organization
from mercado_api.size_charts import MercadoSizeChartsClient, SizeChartWriteError, bare_domain, extract_fields


def _positive_id(value, label="编号"):
    if isinstance(value, bool) or not (isinstance(value, int) or isinstance(value, str) and re.fullmatch(r"[0-9]+", value)):
        raise ValueError(f"{label}无效")
    try:
        number = int(value)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{label}无效") from exc
    if number <= 0 or isinstance(value, bool):
        raise ValueError(f"{label}无效")
    return number


def _attributes(value) -> list[dict]:
    if not isinstance(value, list):
        raise ValueError("属性必须是数组")
    if len(value) > 250:
        raise ValueError("属性数量过多")
    result, seen = [], set()
    for raw in value:
        if not isinstance(raw, dict):
            raise ValueError("属性格式无效")
        attr_id = str(raw.get("id") or "").strip().upper()
        if not attr_id or attr_id in seen or len(attr_id) > 100:
            raise ValueError("属性编号为空、重复或过长")
        values = raw.get("values")
        if values is None:
            values = [{"id": raw["value_id"]}] if raw.get("value_id") else [{"name": raw["value_name"]}] if raw.get("value_name") else []
        if not isinstance(values, list) or len(values) > 100:
            raise ValueError(f"{attr_id} 的属性值格式无效")
        normalized = []
        for value in values:
            if not isinstance(value, dict):
                raise ValueError(f"{attr_id} 的属性值必须是对象")
            row = {key: value[key] for key in ("id", "name", "struct") if key in value and value[key] is not None}
            if "id" in row:
                row["id"] = str(row["id"]).strip()
            if "name" in row:
                row["name"] = str(row["name"]).strip()
            if len(str(row.get("name") or "")) > 1000:
                raise ValueError(f"{attr_id} 的属性值过长")
            if "struct" in row and (not isinstance(row["struct"], dict) or set(row["struct"]) - {"number", "unit"}):
                raise ValueError(f"{attr_id} 的数值结构无效")
            normalized.append(row)
        result.append({"id": attr_id, "values": normalized})
        seen.add(attr_id)
    return result


def normalize_template(payload: dict) -> dict:
    name = str(payload.get("name") or "").strip()
    if not name or len(name) > 120:
        raise ValueError("请输入 1 到 120 个字符的模板名称")
    domain = bare_domain(payload["domain_id"]) if payload.get("domain_id") else ""
    names = payload.get("names") or {}
    if not isinstance(names, dict):
        raise ValueError("站点名称必须是对象")
    names = {str(site).upper(): str(value).strip() for site, value in names.items()}
    main = payload.get("main_attribute") or []
    if isinstance(main, dict):
        main = main.get("attributes") or []
    if not isinstance(main, list):
        raise ValueError("主尺码属性格式无效")
    main = [{"site_id": str(row.get("site_id") or "").upper(), "id": str(row.get("id") or "").upper()}
            for row in main if isinstance(row, dict)]
    rows = payload.get("rows") or []
    if not isinstance(rows, list) or len(rows) > 500:
        raise ValueError("尺码行格式无效或超过 500 行")
    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("尺码行必须是对象")
        normalized_rows.append({"attributes": _attributes(row.get("attributes") or [])})
    result = {"name": name, "domain_id": domain, "attributes": _attributes(payload.get("attributes") or []),
              "main_attribute": main, "rows": normalized_rows, "names": names}
    measure_type = str(payload.get("measure_type") or "BODY_MEASURE").upper()
    if measure_type not in {"BODY_MEASURE", "CLOTHING_MEASURE", "MIXED_MEASURE"}:
        raise ValueError("量测类型无效")
    result["measure_type"] = measure_type
    if "standard_template" in payload:
        standard = payload["standard_template"]
        if not isinstance(standard, dict) or any(
                not isinstance(standard.get(key), str)
                or not re.fullmatch(r"[a-z0-9_-]{1,80}", standard[key])
                for key in ("preset_id", "size_system")):
            raise ValueError("标准模板来源须包含有效的品类与尺码体系")
        result["standard_template"] = {key: standard[key] for key in ("preset_id", "size_system")}
    # Validate JSON and reject NaN/Infinity in drafts before durable snapshots.
    import json
    if len(json.dumps(result, ensure_ascii=False, allow_nan=False)) > 1_000_000:
        raise ValueError("模板内容过大")
    return result


def _scoped_summaries(organization_key, allowed_token_ids) -> dict[int, dict]:
    allowed = None if allowed_token_ids is None else {_positive_id(value, "店铺编号") for value in allowed_token_ids}
    result = {}
    for row in (bit_mysql.list_mercado_store_tokens() or {}).get("rows") or []:
        token_id = int(row.get("id") or 0)
        if token_id and normalize_organization(row.get("organization_key")) == organization_key and (allowed is None or token_id in allowed):
            result[token_id] = row
    return result


def _adapter(token_id: int, summaries: dict[int, dict], organization_key: str):
    summary = summaries.get(token_id)
    if not summary:
        raise ValueError("店铺不存在或不在当前授权范围")
    if not bool(summary.get("enabled", True)):
        raise ValueError("店铺已关闭")
    secret = dict(bit_mysql.get_mercado_store_token(token_id) or {})
    if normalize_organization(secret.get("organization_key")) != organization_key:
        raise ValueError("店铺所属组织已变更，请刷新后重试")
    client, secret = _client_and_token(secret)
    api = MercadoSizeChartsClient(client)
    identity = api.identity()
    if str(secret.get("meli_user_id") or "") != str(identity["seller_id"]):
        raise ValueError("店铺授权身份与保存的卖家编号不一致，请重新授权")
    return api, identity


def _field_tags(field):
    return {str(value).lower() for value in field.get("tags") or []}


def _canonical_values(values):
    result = []
    for value in values or []:
        if value.get("id"):
            canonical = ("id", str(value["id"]))
        elif value.get("struct"):
            struct = value["struct"]
            number = struct.get("number")
            canonical = ("struct", str(float(number)) if isinstance(number, (float, int)) else str(number), str(struct.get("unit") or ""))
        else:
            canonical = ("name", str(value.get("name") or "").strip().casefold())
        result.append(canonical)
    return sorted(result)


def _matches_attributes(expected, actual):
    actual_by_id = {row.get("id"): row for row in actual or [] if isinstance(row, dict)}
    for row in expected:
        found = actual_by_id.get(row["id"])
        if not found:
            return False
        remaining = list(found.get("values") or [])
        if len(remaining) != len(row.get("values") or []):
            return False
        for wanted in row.get("values") or []:
            def matches(value):
                if wanted.get("id"):
                    return str(value.get("id")) == str(wanted["id"])
                if wanted.get("struct"):
                    actual_struct, expected_struct = value.get("struct") or {}, wanted["struct"]
                    return actual_struct.get("number") == expected_struct.get("number") and actual_struct.get("unit") == expected_struct.get("unit")
                return str(value.get("name") or "").strip().casefold() == str(wanted.get("name") or "").strip().casefold()
            matched = next((value for value in remaining if matches(value)), None)
            if matched is None:
                return False
            remaining.remove(matched)
    return True


def _verify_reconciled_chart(expected: dict, chart: dict, chart_id: str):
    """Only attach an official chart which contains the entire frozen request."""
    try:
        owner_matches = str(chart.get("seller_id")) == str(expected["seller_id"])
        domain_matches = bare_domain(chart.get("domain_id")) == expected["domain_id"]
        site_matches = chart.get("site_id") == expected["site_id"]
        names = chart.get("names") or {}
        names_match = all(names.get(site) == name for site, name in expected["names"].items())
        main = chart.get("main_attribute") or {}
        if isinstance(main, dict):
            main = main.get("attributes") or []
        if not main and chart.get("main_attribute_id"):
            main = [{"site_id": chart.get("site_id"), "id": chart["main_attribute_id"]}]
        actual_main = {row.get("site_id"): row.get("id") for row in main}
        main_matches = all(actual_main.get(row["site_id"]) == row["id"] for row in expected["main_attribute"]["attributes"])
        rows = chart.get("rows") or []
        rows_match = len(rows) == len(expected["rows"]) and all(row.get("id") for row in rows)
        unmatched = list(rows)
        for wanted in expected["rows"]:
            match = next((row for row in unmatched if set(wanted["sites"]) <= set(row.get("sites") or [chart.get("site_id")])
                          and _matches_attributes(wanted["attributes"], row.get("attributes"))), None)
            if match is None:
                rows_match = False
                break
            unmatched.remove(match)
        matches = (str(chart.get("id")) == chart_id and chart.get("type") == "SPECIFIC" and chart.get("chart_status") != "INACTIVE" and owner_matches
                   and domain_matches and site_matches and names_match and main_matches and rows_match
                   and chart.get("measure_type", "BODY_MEASURE") == expected["measure_type"]
                   and _matches_attributes(expected["attributes"], chart.get("attributes")))
    except (ValueError, TypeError, KeyError, AttributeError):
        matches = False
    if not matches:
        raise ValueError("官方尺码表与该上传记录的卖家、站点、名称或尺码数据不一致，无法确认；原记录保持待核对")


def _requirements(api, identity, domain_id, attributes):
    domains = api.active_domains(identity["site_id"])
    result = {"domains": domains, "domain_id": domain_id, "identity": {key: identity[key] for key in ("seller_id", "site_id")},
              "sites": [{"site_id": value} for value in identity["sites"]], "template_fields": [], "row_fields": [],
              "main_attributes": [], "technical_specs": {}, "max_rows": None, "errors": []}
    if not domain_id:
        return result
    domain_id = bare_domain(domain_id)
    if domain_id not in {row["id"] for row in domains}:
        raise ValueError("该店铺当前不支持此商品域的尺码表")
    full = api.technical_specs(identity["site_id"], domain_id)
    metadata = extract_fields(full)
    metadata["template_fields"] = [field for field in metadata["template_fields"]
        if _field_tags(field) & {"grid_filter", "grid_template_required"} or field["id"] in {"GENDER", "BRAND"}]
    result.update(metadata)
    result["technical_specs"] = {"full": full}
    indexed = {row["id"]: row for row in attributes}
    filter_fields = [field for field in metadata["template_fields"] if "grid_template_required" in _field_tags(field)]
    missing = [field for field in filter_fields if not (indexed.get(field["id"], {}).get("values"))]
    if missing:
        result["errors"] = [f"请先选择 {field['name']}，再加载尺码行规范" for field in missing]
        return result
    query_fields = {field["id"] for field in filter_fields}
    query_attributes = [row for row in attributes if row["id"] in query_fields]
    grids = api.technical_specs(identity["site_id"], domain_id, query_attributes, grids=True)
    grid_metadata = extract_fields(grids)
    # Full specs give filter choices; grids give domain/gender-specific rows.
    # Listing filters (e.g. MODEL) are not chart creation requirements.
    combined = {field["id"]: field for field in metadata["template_fields"]
                if field["id"] in query_fields | {"GENDER", "BRAND"}}
    combined.update({field["id"]: field for field in grid_metadata["template_fields"]})
    result.update(grid_metadata)
    result["template_fields"] = list(combined.values())
    result["technical_specs"]["grids"] = grids
    if not result["row_fields"]:
        result["errors"] = ["官方未返回尺码行属性，当前无法上传；请检查商品域及筛选属性"]
    return result


def _validate_attributes(attributes, fields, *, label, measure_type="MIXED_MEASURE"):
    errors, indexed = [], {row["id"]: row for row in attributes}
    field_map = {field["id"]: field for field in fields}
    for attr_id, row in indexed.items():
        field = field_map.get(attr_id)
        if field is None:
            errors.append(f"{label}：{attr_id} 不在官方允许的属性中")
            continue
        values = row["values"]
        tags = _field_tags(field)
        if len(values) > 1 and "multivalued" not in tags:
            errors.append(f"{label}：{field['name']} 仅允许一个值")
        is_list = str(field.get("value_type") or "").lower() in {"list", "enum", "enumeration"} or attr_id == "GENDER" or "fixed" in tags
        choices = {str(value.get("id")) for value in field.get("values") or []
                   if is_list and isinstance(value, dict) and value.get("id") is not None}
        units = {str(value.get("id") or value.get("name")) if isinstance(value, dict) else str(value)
                 for value in field.get("allowed_units") or []}
        numeric = str(field.get("value_type") or "").lower() in {"number", "number_unit", "number_with_unit"}
        for value in values:
            if not (value.get("id") or value.get("name") or value.get("struct")):
                errors.append(f"{label}：{field['name']} 不能为空")
            if choices and str(value.get("id") or "") not in choices:
                errors.append(f"{label}：{field['name']} 必须选择官方属性值编号")
            elif is_list and not value.get("id"):
                errors.append(f"{label}：{field['name']} 必须使用官方属性值编号")
            struct = value.get("struct") or {}
            if numeric and (units or "unit" in str(field.get("value_type"))) and not struct:
                errors.append(f"{label}：{field['name']} 必须填写数值及单位结构")
            if numeric and not struct and not units and "unit" not in str(field.get("value_type")):
                try:
                    number = float(value.get("name", value.get("id", "")))
                    if not math.isfinite(number):
                        raise ValueError
                except (ValueError, TypeError):
                    errors.append(f"{label}：{field['name']} 必须填写有效数字")
                else:
                    for key, compare, description in (("min", lambda n, v: n < v, "小于"), ("max", lambda n, v: n > v, "大于")):
                        boundary = field.get(key, field.get("value_" + key))
                        if isinstance(boundary, (int, float)) and compare(number, boundary):
                            errors.append(f"{label}：{field['name']} 不得{description} {boundary}")
            if struct:
                number = struct.get("number")
                if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number):
                    errors.append(f"{label}：{field['name']} 的数值无效")
                else:
                    if attr_id in {"FOOT_LENGTH", "FOOT_LENGTH_TO"} and not 5 <= number <= 40:
                        errors.append(f"{label}：{field['name']} 必须在 5 到 40 cm 范围内")
                    for key, compare, description in (("min", lambda n, v: n < v, "小于"), ("max", lambda n, v: n > v, "大于")):
                        boundary = field.get(key, field.get("value_" + key))
                        if isinstance(boundary, (int, float)) and compare(number, boundary):
                            errors.append(f"{label}：{field['name']} 不得{description} {boundary}")
                if units and str(struct.get("unit") or "") not in units:
                    errors.append(f"{label}：{field['name']} 使用了官方不支持的单位")
                if attr_id in {"FOOT_LENGTH", "FOOT_LENGTH_TO"} and struct.get("unit") != "cm":
                    errors.append(f"{label}：{field['name']} 必须使用 cm")
                if not value.get("name"):
                    errors.append(f"{label}：{field['name']} 的数值结构还需对应的 name")
    for field in fields:
        tags = _field_tags(field)
        measurement_tags = tags & {"body_measure", "clothing_measure"}
        relevant = not measurement_tags or measure_type == "MIXED_MEASURE" or measure_type.lower() in measurement_tags
        if field.get("required") and relevant and not indexed.get(field["id"], {}).get("values"):
            errors.append(f"{label}：缺少必填属性 {field['name']}")
    return errors


def _prepare_reference_template(template, requirements):
    """Resolve built-in labels against this seller's schema without guessing sizes.

    Keep the saved reference intact. Only known reference columns absent from the
    official grid are omitted from the submitted copy, with visible notices.
    Unknown user-added fields still go through ordinary validation.
    """
    source = template.get("standard_template") or {}
    if not re.fullmatch(r"builtin_v1_(men|women|boys|girls)_(tshirts|shirts|pants|shorts|coats|hoodies|pajamas|dresses|blouses|leggings)", source.get("preset_id", "")):
        return template, [], []
    prepared = deepcopy(template)
    fields = {field["id"]: field for field in requirements["row_fields"]}
    measurement_ids = {f"{base}_{suffix}" for base in (
        "CHEST_CIRCUMFERENCE", "WAIST_CIRCUMFERENCE", "HIP_CIRCUMFERENCE",
        "PERSON_HEIGHT", "NECK_CIRCUMFERENCE", "BUST_CIRCUMFERENCE") for suffix in ("FROM", "TO")}
    filter_ids = {"FILTRABLE_SIZE", "PANTS_FILTRABLE_SIZES"}
    omitted, resolved, errors, renamed = set(), 0, [], set()
    # Live CBT validation applies an adult 30–70 cm neck range to children
    # too. These columns are optional: preserve the reference measurements,
    # omit the pair from the child submission rather than alter their values.
    child_reference = source["preset_id"].startswith(("builtin_v1_boys_", "builtin_v1_girls_"))
    optional_child_neck = child_reference and all(
        not fields.get(f"NECK_CIRCUMFERENCE_{suffix}", {}).get("required") for suffix in ("FROM", "TO"))

    def resolve(attributes, official_fields):
        nonlocal resolved
        for attribute in attributes:
            field = official_fields.get(attribute["id"], {})
            tags = _field_tags(field)
            if not (field.get("value_type") in {"list", "enum", "enumeration"}
                    or "fixed" in tags or attribute["id"] == "GENDER"):
                continue
            choices = field.get("values") or []
            for value in attribute["values"]:
                # Existing explicit IDs are validated, never replaced by a name.
                if value.get("id"):
                    continue
                matches = [choice for choice in choices if isinstance(choice, dict)
                           and str(choice.get("name") or "").strip().casefold()
                           == str(value.get("name") or "").strip().casefold()]
                if len(matches) == 1 and matches[0].get("id") is not None:
                    value["id"] = str(matches[0]["id"])
                    value["name"] = str(matches[0]["name"])
                    resolved += 1

    resolve(prepared["attributes"], {field["id"]: field for field in requirements["template_fields"]})
    for index, row in enumerate(prepared["rows"], 1):
        kept = []
        for attribute in row["attributes"]:
            attr_id = attribute["id"]
            if attr_id in {"CHEST_CIRCUMFERENCE_FROM", "CHEST_CIRCUMFERENCE_TO"} and attr_id not in fields:
                bust_id = attr_id.replace("CHEST_", "BUST_")
                if bust_id in fields:
                    renamed.add(f"{attr_id} → {bust_id}")
                    attribute["id"] = bust_id
                    attr_id = bust_id
            if optional_child_neck and attr_id in {"NECK_CIRCUMFERENCE_FROM", "NECK_CIRCUMFERENCE_TO"}:
                omitted.add(attr_id)
                continue
            if attr_id in filter_ids and attr_id not in fields:
                alternatives = sorted(filter_ids & fields.keys())
                if len(alternatives) == 1 and not any(item["id"] == alternatives[0] for item in row["attributes"]):
                    attribute["id"] = alternatives[0]
                    attr_id = attribute["id"]
            if attr_id in measurement_ids | filter_ids and attr_id not in fields:
                omitted.add(attr_id)
            else:
                kept.append(attribute)
        row["attributes"] = kept
        resolve(kept, fields)
        if not any(item["id"] in measurement_ids and item["values"] for item in kept):
            errors.append(f"第 {index} 行：目标规格没有可接收的内置身体测量字段，请选择相符类目或维护模板")
    notices = []
    if resolved:
        notices.append(f"已按目标店铺官方选项匹配 {resolved} 个尺码或属性值编号，保留原参考数值")
    if omitted:
        notices.append("按目标规格省略以下参考列；原模板仍完整保留：" + "、".join(sorted(omitted)))
    if renamed:
        notices.append("已按该类目官方胸围字段提交，数值及单位保持一致：" + "、".join(sorted(renamed)))
    if optional_child_neck and omitted & {"NECK_CIRCUMFERENCE_FROM", "NECK_CIRCUMFERENCE_TO"}:
        notices.append("平台对颈围限定 30–70 cm；童装参考颈围低于该范围，已省略非必填颈围列，保留真实参考值和其余测量数据")
    return prepared, notices, errors


def _build_payload(template, identity, sites, requirements):
    errors = list(requirements.get("errors") or [])
    all_sites = [identity["site_id"]] + [site for site in sites if site != identity["site_id"]]
    names = {site: str(template["names"].get(site) or template["name"]).strip() for site in all_sites}
    for site, name in names.items():
        if not name or len(name) > 60 or any(not (char.isalnum() or char == " ") for char in name):
            errors.append(f"{site} 尺码表名称只允许字母、数字、空格，且不超过 60 个字符")
    errors += _validate_attributes(template["attributes"], requirements["template_fields"], label="模板属性")
    for required in ("GENDER", "BRAND"):
        if not next((row for row in template["attributes"] if row["id"] == required and row["values"]), None):
            errors.append(f"模板属性：官方自定义尺码表需要 {required}")
    if not template["rows"]:
        errors.append("请至少填写一行真实尺码数据")
    if requirements.get("max_rows") and len(template["rows"]) > requirements["max_rows"]:
        errors.append(f"尺码行超过官方上限 {requirements['max_rows']}")
    candidates = {field["id"] for field in requirements["main_attributes"]}
    selections = {row["site_id"]: row["id"] for row in template["main_attribute"] if row.get("id")}
    main_id = selections.get(identity["site_id"]) or next(iter(selections.values()), "")
    if not main_id or main_id not in candidates:
        errors.append("请选择官方允许的主尺码属性")
    if any(selections.get(site, main_id) != main_id for site in all_sites):
        errors.append("同一尺码表的各站点必须使用相同的主尺码属性")
    unique_values = {}
    for index, row in enumerate(template["rows"], 1):
        errors += _validate_attributes(row["attributes"], requirements["row_fields"], label=f"第 {index} 行", measure_type=template["measure_type"])
        indexed = {attr["id"]: attr["values"] for attr in row["attributes"]}
        if not indexed.get(main_id):
            errors.append(f"第 {index} 行缺少主尺码属性 {main_id}")
        for field in requirements["row_fields"]:
            if field["id"] == main_id or "unique" in _field_tags(field):
                value = indexed.get(field["id"])
                if value:
                    key = (field["id"], json_text(_canonical_values(value)))
                    if key in unique_values:
                        errors.append(f"第 {index} 行与第 {unique_values[key]} 行的 {field['name']} 重复")
                    unique_values[key] = index
        length_values = {attr["id"]: (attr["values"][0].get("struct") or {}).get("number")
                         for attr in row["attributes"] if attr["values"] and attr["id"] in {"FOOT_LENGTH", "FOOT_LENGTH_TO"}}
        minimum, maximum = length_values.get("FOOT_LENGTH"), length_values.get("FOOT_LENGTH_TO")
        if isinstance(minimum, (int, float)) and isinstance(maximum, (int, float)) and maximum < minimum:
            errors.append(f"第 {index} 行的脚长上限不能小于下限")
    payload = {"names": names, "domain_id": bare_domain(template["domain_id"]), "site_id": identity["site_id"],
               "seller_id": identity["seller_id"], "type": "SPECIFIC", "measure_type": template["measure_type"],
               "main_attribute": {"attributes": [{"site_id": site, "id": main_id} for site in all_sites]},
               "attributes": template["attributes"], "rows": [{"sites": all_sites, "attributes": row["attributes"]} for row in template["rows"]]}
    return payload, list(dict.fromkeys(errors))


def dispatch(action: str, payload: dict | None = None, *, organization_key: str,
             allowed_token_ids=None, store: SizeChartStore | None = None) -> dict:
    payload, org = dict(payload or {}), normalize_organization(organization_key)
    storage = store or SizeChartStore()
    action = str(action or "").strip().lower()
    if action == "list":
        return {"templates": storage.list_templates(org)}
    if action == "create":
        return {"template": storage.create_template(org, normalize_template(payload.get("template") or payload))}
    if action in {"update", "copy", "delete"}:
        template_id = _positive_id(payload.get("template_id") or payload.get("id"), "模板编号")
        template = storage.get_template(org, template_id)
        if not template:
            raise ValueError("模板不存在或不在当前组织")
        if action == "copy":
            copied = normalize_template({**template, "name": payload.get("name") or template["name"][:117] + " 副本"})
            return {"template": storage.create_template(org, copied)}
        revision = _positive_id(payload.get("revision"), "模板版本")
        if action == "delete":
            storage.delete_template(org, template_id, revision)
            return {"deleted": True, "template_id": template_id}
        return {"template": storage.update_template(org, template_id, revision, normalize_template(payload.get("template") or payload))}

    summaries = _scoped_summaries(org, allowed_token_ids)
    if action in {"batch-upload", "batch-status"}:
        from bit.size_chart_batch import dispatch as batch_dispatch
        return batch_dispatch(action, payload, org, summaries, storage)
    if action == "reconcile":
        record = storage.get_record(org, str(payload.get("record_id") or ""))
        if not record or record["token_id"] not in summaries:
            raise ValueError("上传记录不存在或不在当前授权范围")
        chart_id = str(payload.get("chart_id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9:_-]{1,120}", chart_id):
            raise ValueError("请输入官方尺码表编号")
        if record["status"] not in {"unknown", "running"}:
            raise ValueError("仅结果未知或中断的上传记录需要核对")
        api, identity = _adapter(record["token_id"], summaries, org)
        if identity["seller_id"] != record["payload"]["seller_id"] or identity["site_id"] != record["payload"]["site_id"]:
            raise ValueError("店铺授权身份已变化，无法核对原上传记录")
        chart = api.get_chart(chart_id)
        _verify_reconciled_chart(record["payload"], chart, chart_id)
        confirmed = storage.finish_upload(org, record["record_id"], {
            "status": "succeeded", "chart_id": chart_id, "rows": chart["rows"], "response": chart,
            "message": "已从官方查询确认尺码表与原上传快照一致", "reconciled": True})
        return {"record": confirmed}
    if action == "records":
        return {"records": storage.list_records(org, list(summaries), payload.get("template_id"))}
    if action == "options":
        def one_option(item):
            token_id, summary = item
            option = {"id": token_id, "name": str(summary.get("display_name") or summary.get("nickname") or token_id),
                      "enabled": bool(summary.get("enabled", True)), "sites": [],
                      "salespersons": sorted({str(row.get("salesperson") or "").strip()
                          for row in summary.get("site_settings") or [] if str(row.get("salesperson") or "").strip()})}
            if option["enabled"]:
                # Opening the selector must not fan out to every remote seller.
                # Preview and upload still verify live identity and site access.
                main_site = str(summary.get("site_id") or "").strip().upper()
                sites = {str(row.get("site_id") or "").strip().upper()
                         for row in summary.get("site_settings") or []}
                if main_site:
                    sites.add(main_site)
                sites = sorted(site for site in sites if re.fullmatch(r"[A-Z]{3}", site))
                option["sites"] = [{"site_id": site} for site in sites]
                option["site_id"] = main_site
                option["message"] = ("站点来自已保存授权，预检时实时核验" if sites
                                     else "暂无已保存的授权站点，请先更新店铺授权")
            return option
        options = [one_option(item) for item in summaries.items()]
        return {"stores": options}
    if action == "requirements":
        api, identity = _adapter(_positive_id(payload.get("token_id"), "店铺编号"), summaries, org)
        domain = bare_domain(payload["domain_id"]) if payload.get("domain_id") else ""
        return _requirements(api, identity, domain, _attributes(payload.get("attributes") or []))
    if action == "preview":
        template_id = _positive_id(payload.get("template_id"), "模板编号")
        template = storage.get_template(org, template_id)
        if not template:
            raise ValueError("模板不存在或不在当前组织")
        revision = _positive_id(payload.get("revision"), "模板版本")
        if revision != template["revision"]:
            raise ValueError("模板版本已变更，请刷新后预检")
        targets = payload.get("targets") or []
        token_ids = payload.get("token_ids")
        if token_ids is not None:
            if not isinstance(token_ids, list):
                raise ValueError("店铺编号须为数组")
            targets = [{"token_id": value} for value in token_ids]
        if not isinstance(targets, list) or not targets or len(targets) > 200:
            raise ValueError("请选择上传店铺（最多 200 项）")
        grouped = {}
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("目标站点格式无效")
            token_id = _positive_id(target.get("token_id"), "店铺编号")
            if token_id not in summaries:
                raise ValueError("目标店铺不在当前授权范围")
            site = str(target.get("site_id") or "").upper().strip()
            grouped.setdefault(token_id, set()).add(site)
        results = []
        for token_id, chosen in grouped.items():
            sites = sorted(chosen)
            result = {"token_id": token_id, "sites": sites, "status": "invalid", "errors": []}
            try:
                api, identity = _adapter(token_id, summaries, org)
                if "" not in chosen and not chosen <= set(identity["sites"]):
                    raise ValueError("目标站点不属于官方返回的店铺销售站点")
                sites = sorted(identity["sites"])
                result["sites"] = sites
                requirements = _requirements(api, identity, template["domain_id"], template["attributes"])
                prepared, notices, preparation_errors = _prepare_reference_template(template, requirements)
                chart_payload, errors = _build_payload(prepared, identity, sites, requirements)
                errors = preparation_errors + errors
                payload_hash = hashlib.sha256(json_text(chart_payload).encode()).hexdigest()
                blocking = storage.blocking_record(org, template_id, revision, token_id, payload_hash)
                result.update({"payload": chart_payload, "payload_hash": payload_hash, "errors": errors, "notices": notices})
                if blocking:
                    result.update({"status": "duplicate", "record": blocking,
                                   "message": "该版本已上传或结果待确认；重复创建已阻止。新增站点请保存新版本后预检"})
                elif errors:
                    result["message"] = f"发现 {len(errors)} 项校验问题"
                else:
                    result.update({"status": "ready", "message": "官方字段与站点校验通过，可提交创建"})
            except ValueError as exc:
                result.update({"message": str(exc), "errors": [str(exc)]})
            except Exception as exc:
                from bit.size_chart_batch import error_message
                detail = error_message(exc)
                result.update({"message": f"官方规范或店铺身份读取失败：{detail}", "errors": [detail]})
            results.append(result)
        expires = (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(microsecond=0).isoformat()
        return storage.save_preview(org, {"template_id": template_id, "revision": revision,
                                         "template_snapshot": template, "results": results}, expires)
    if action == "upload":
        preview = storage.get_preview(org, str(payload.get("preview_id") or ""))
        if not preview:
            raise ValueError("预检不存在或不在当前组织，请重新预检")
        if datetime.fromisoformat(preview["expires_at"]) <= datetime.now(timezone.utc):
            raise ValueError("预检已过期，请重新预检")
        template = storage.get_template(org, preview["template_id"])
        if not template or template["revision"] != preview["revision"]:
            raise ValueError("模板已修改或删除，请重新预检")
        if any(row["token_id"] not in summaries for row in preview["results"]):
            raise ValueError("预检店铺授权范围已变更，请重新预检")
        results = []
        for entry in preview["results"]:
            if entry["status"] != "ready":
                results.append(entry.get("record") or entry)
                continue
            record = {"template_id": preview["template_id"], "revision": preview["revision"],
                      "template_snapshot": preview["template_snapshot"], "token_id": entry["token_id"], "sites": entry["sites"],
                      "payload_hash": entry["payload_hash"], "payload": entry["payload"], "preview_id": preview["preview_id"]}
            # Recheck live authorization BEFORE reserving the irreversible call.
            try:
                api, identity = _adapter(entry["token_id"], summaries, org)
                if identity["seller_id"] != entry["payload"]["seller_id"] or identity["site_id"] != entry["payload"]["site_id"] or not set(entry["sites"]) <= set(identity["sites"]):
                    raise ValueError("店铺身份或销售站点已变化，请重新预检")
            except Exception:
                results.append({**record, "status": "invalid", "message": "店铺授权或站点已变化，请重新预检"})
                continue
            reserved, claimed = storage.claim_upload(org, record)
            if not claimed:
                results.append(reserved)
                continue
            try:
                response = api.create_chart(entry["payload"])
                updates = {"status": "succeeded", "chart_id": str(response["id"]), "rows": response.get("rows") or [],
                           "response": response, "message": "尺码表已创建；发布商品时使用 chart_id 和对应的 row_id"}
            except SizeChartWriteError as exc:
                updates = {"status": "unknown" if exc.unknown else "failed", "message": str(exc), "response": exc.details or {}}
                if isinstance(exc.details, dict):
                    updates["errors"] = [f"{row.get('code', '平台校验')}：{row.get('message', '')}"
                        for row in exc.details.get("errors") or [] if isinstance(row, dict)]
            except Exception:
                updates = {"status": "unknown", "message": "提交过程未获得确定结果；请核对官方尺码表，禁止直接重试"}
            results.append(storage.finish_upload(org, reserved["record_id"], updates))
        return {"results": results, "succeeded": sum(row["status"] == "succeeded" for row in results),
                "failed": sum(row["status"] in {"failed", "invalid"} for row in results),
                "unknown": sum(row["status"] in {"unknown", "running"} for row in results)}
    raise ValueError("不支持的尺码表操作")
