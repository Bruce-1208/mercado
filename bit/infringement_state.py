"""Shared, deterministic validity checks for infringement screening snapshots."""

import hashlib
import json
from datetime import datetime, timezone


DETECTION_SCOPES = {"title", "title_image"}


def _object(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def canonical_record(row, source_type=None):
    """Normalize a live list row and its saved metadata without replacing current content."""
    record = dict(row or {})
    metadata = _object(record.get("infringement_check_json"))
    for name, value in metadata.items():
        if name not in {"title", "main_image_url", "site_id", "category_id", "product_category", "title_es", "title_pt",
                        "source_type", "source_row_id", "row_id", "record_key", "id", "product_id",
                        "image_content_revision", "image_generation_method", "image_fingerprint",
                        "source_snapshot_json", "ai_copy_json", "plugin_snapshot_json", "source_json",
                        "token_id", "account_name"}:
            if record.get(name) is None:
                record[name] = value
    for name, fallback in (("risk_level", "infringement_risk_level"), ("keywords", "infringement_keywords"),
                           ("reason", "infringement_reason"), ("checked_at", "infringement_checked_at")):
        if name not in record:
            record[name] = record.get(fallback)
    if str(record.get("risk_level")) in {"0", "1", "2"}:
        record["risk_level"] = int(record["risk_level"])
    record["product_category"] = str(record.get("category_name") or record.get("product_category") or "")
    snapshot = _object(record.get("source_snapshot_json"))
    plugin = _object(record.get("plugin_snapshot_json")) or _object(snapshot.get("plugin_snapshot"))
    source = _object(record.get("source_json")) or _object(snapshot.get("source"))
    ai = _object(snapshot.get("ai_original"))
    generated_copy = _object(record.get("ai_copy_json")) or _object(snapshot.get("ai_copy"))
    record["image_content_revision"] = str(ai.get("image_content_revision") or record.get("image_content_revision") or "")
    record["image_generation_method"] = str(ai.get("image_generation_method") or record.get("image_generation_method") or "")
    preparation = _object(ai.get("preparation"))
    record["image_fingerprint"] = str(preparation.get("image_fingerprint") or record.get("image_fingerprint") or "")
    for name in ("title_es", "title_pt"):
        record[name] = str(record.get(name) or ai.get(name) or generated_copy.get(name) or "")
    if source_type:
        record["source_type"] = source_type
    row_id = record.get("row_id", record.get("source_row_id", record.get("id")))
    if row_id is not None:
        record["row_id"] = record["source_row_id"] = int(row_id)
        record["record_key"] = f"{record['source_type']}:{row_id}" if record.get("source_type") else str(row_id)
    record["product_id"] = str(record.get("product_id") or record.get("source_item_id") or "")
    item_site = record["product_id"][:3].upper()
    record["site_id"] = str(record.get("site_id") or plugin.get("site_id") or snapshot.get("site_id") or source.get("site_id") or (
        item_site if item_site in {"MLM", "MLB", "MLA", "MLC", "MCO", "MLU", "MPE", "MEC", "CBT"} else ""
    ))
    record["token_id"] = record.get("token_id") or plugin.get("token_id") or snapshot.get("token_id")
    record["account_name"] = str(record.get("account_name") or plugin.get("store_name") or snapshot.get("account_name") or "")
    for field in ("review_site_ids", "review_history"):
        value = record.get(field)
        if isinstance(value, str):
            try:
                record[field] = json.loads(value)
            except ValueError:
                record[field] = []
    if record.get("review_history_json"):
        try:
            record["review_history"] = json.loads(record["review_history_json"])
        except (TypeError, ValueError):
            pass
    return record


def normalize_items(items):
    if items is None:
        return None
    if not isinstance(items, list) or not items:
        raise ValueError("请至少选择一个商品")
    result = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("所选商品格式无效")
        source = str(item.get("source_type") or "").strip().lower()
        try:
            row_id = int(item.get("row_id", item.get("source_row_id")))
        except (TypeError, ValueError):
            raise ValueError("商品编号无效") from None
        if source not in {"collection_list", "product_list", "pulled", "zying"} or row_id <= 0:
            raise ValueError("商品来源或编号无效")
        key = (source, row_id)
        if key not in seen:
            result.append({"source_type": source, "row_id": row_id})
            seen.add(key)
    if len(result) > 50000:
        raise ValueError("单次最多检测 50000 个商品")
    return result


def content_hash(record, detection_scope="title"):
    record = canonical_record(record)
    payload = {"title": str(record.get("title") or "").strip()}
    for field in ("title_es", "title_pt"):
        payload[field] = str(record.get(field) or "").strip()
    if detection_scope == "title_image":
        payload["main_image_url"] = str(record.get("main_image_url") or "").strip()
        for field in ("image_content_revision", "image_generation_method", "image_fingerprint"):
            payload[field] = str(record.get(field) or "")
    # The same title may have different applicability rules by site/category.
    for field in ("site_id", "category_id", "product_category", "management_category_id", "token_id", "account_name"):
        payload[field] = str(record.get(field) or "").strip()
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def knowledge_version(records):
    from bit.bit_infringement_knowledge import effective_list_type

    relevant = []
    for row in records or ():
        if not isinstance(row, dict) or effective_list_type(row) not in {"blacklist", "whitelist", "borderline"}:
            continue
        if row.get("deleted_at") is not None:
            continue
        if row.get("valid_until") and str(row["valid_until"])[:10] < datetime.now(timezone.utc).date().isoformat():
            continue
        relevant.append({key: value for key, value in row.items() if key not in {
            "created_at", "updated_at", "evidence_count", "source_detail", "notes", "list_type_label",
        }})
    serialized = sorted(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) for row in relevant)
    return hashlib.sha256("\n".join(serialized).encode()).hexdigest()


def result_state(record, rule_version=None, detection_scope=None):
    record = canonical_record(record)
    scope = detection_scope or record.get("detection_scope") or "title"
    old_hash = record.get("content_hash")
    if record.get("check_status") == "failed":
        return "failed"
    if record.get("risk_level") is None:
        return "unchecked"
    if not old_hash or old_hash != content_hash(record, scope):
        return "stale"
    if rule_version is not None and record.get("rule_version") != rule_version:
        return "stale"
    if detection_scope and record.get("detection_scope") != detection_scope:
        return "stale"
    return "completed"


def review_is_current(record, now=None, site_id=None):
    record = canonical_record(record)
    if record.get("manual_decision") not in {"allow", "block"}:
        return False
    if record.get("review_content_hash") != content_hash(record, "title_image"):
        return False
    if site_id and record.get("review_site_ids") and str(site_id).upper() not in record["review_site_ids"]:
        return False
    expires = record.get("review_expires_at")
    if expires:
        try:
            expires_at = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except ValueError:
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if expires_at <= now:
            return False
    return True
