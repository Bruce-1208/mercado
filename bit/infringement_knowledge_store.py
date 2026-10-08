"""Scoped brand-rule metadata, immutable evidence and review history.

All SQL runs through the caller's transaction; importing this module never
connects to the database. Historical rules without a product scope apply across their owning enterprise.
"""
from bit.knowledge_organization import organization as knowledge_organization

import hashlib
import json
from datetime import datetime

from bit.bit_infringement_knowledge import (
    effective_list_type, knowledge_rule_names, normalize_knowledge_record,
    normalize_rule_scope, normalize_rule_values, normalize_valid_until,
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def decode(value, default):
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(value or "null")
        return parsed if isinstance(parsed, type(default)) else default
    except (TypeError, ValueError):
        return default


def ensure_metadata_schema(cursor, ensure_column):
    for name, definition in (
        ("aliases_json", "TEXT NULL"), ("scope_json", "TEXT NULL"),
        ("valid_until", "DATE NULL"), ("rule_version", "BIGINT NOT NULL DEFAULT 1"),
        ("original_source_type", "VARCHAR(16) NULL"),
        ("reviewed_by", "VARCHAR(128) NULL"), ("reviewed_at", "DATETIME NULL"),
    ):
        ensure_column(cursor, "infringement_knowledge", name, definition)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS `infringement_knowledge_evidence` (
            `id` BIGINT NOT NULL AUTO_INCREMENT, `rule_id` BIGINT NOT NULL,
            `source_type` VARCHAR(16) NOT NULL, `source_detail` TEXT NULL,
            `payload_json` LONGTEXT NOT NULL, `fingerprint` CHAR(64) NOT NULL,
            `created_at` DATETIME NOT NULL, PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_knowledge_evidence` (`rule_id`, `fingerprint`),
            KEY `idx_knowledge_evidence_rule` (`rule_id`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS `infringement_knowledge_history` (
            `id` BIGINT NOT NULL AUTO_INCREMENT, `rule_id` BIGINT NOT NULL,
            `rule_version` BIGINT NOT NULL, `action` VARCHAR(32) NOT NULL,
            `actor` VARCHAR(128) NULL, `before_json` LONGTEXT NULL,
            `after_json` LONGTEXT NOT NULL, `created_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`), KEY `idx_knowledge_history_rule` (`rule_id`, `id`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def serialize_metadata(row):
    result = dict(row)
    result["aliases"] = decode(result.pop("aliases_json", None), [])
    raw_scope = result.pop("scope_json", None)
    result["legacy_global"] = not bool(raw_scope)
    # Invalid scope must stay invalid and never become an effective global rule.
    result["scope"] = decode(raw_scope, {}) if raw_scope else normalize_rule_scope(None, allow_legacy=True)
    result["rule_version"] = int(result.get("rule_version") or 1)
    result["original_source_type"] = str(result.get("original_source_type") or result.get("source_type") or "manual")
    result["reviewed_by"] = str(result.get("reviewed_by") or "")
    result["reviewed_at"] = str(result["reviewed_at"]) if result.get("reviewed_at") else None
    result["valid_until"] = str(result["valid_until"]) if result.get("valid_until") else None
    scope = result["scope"]
    if scope.get("mode") == "global":
        result["scope_label"] = "企业内通用规则（历史记录）" if result["legacy_global"] else "企业内通用规则"
    elif scope.get("mode") == "scoped":
        result["scope_label"] = " · ".join(
            f"{label}：{'、'.join(map(str, scope.get(key) or []))}"
            for key, label in (("sites", "站点"), ("categories", "类目"), ("shops", "店铺"))
            if scope.get(key)
        )
    else:
        result["scope_label"] = "范围无效，请复核"
    return result


def prepare_manual_record(record, existing=None):
    normalized = normalize_knowledge_record(record)
    if normalized["list_type"] != "pending" and not normalized["notes"]:
        raise ValueError("风险拦截、人工放行或擦边品牌必须填写可复查的处理依据")
    if existing is None and normalized["list_type"] != "pending" and "scope" not in normalized:
        raise ValueError("新增品牌规则必须明确适用范围；商品放行请使用商品复核")
    if existing and effective_list_type(existing) == "pending" and normalized["list_type"] != "pending" and "scope" not in normalized:
        raise ValueError("复核生效时必须明确适用范围；商品放行请使用商品复核")
    if existing and "rule_version" in normalized:
        if normalized["rule_version"] != int(existing.get("rule_version") or 1):
            raise ValueError("规则已被其他用户更新，请刷新后重新复核")
    return normalized


def append_evidence(cursor, rule_id, payload, now=None):
    """Append once per distinct source payload; never change a rule decision."""
    if not isinstance(payload, dict):
        raise ValueError("证据格式无效")
    source = str(payload.get("source_type") or "manual").strip().lower()
    if source not in ("manual", "analysis", "wipo", "wikidata", "detection"):
        source = "analysis"
    detail = str(payload.get("source_detail") or "")[:16000]
    clean = {key: value for key, value in payload.items()
             if key not in ("reviewed_by", "rule_version", "created_at", "updated_at", "source_type")}
    encoded = encode(clean)
    if len(encoded.encode("utf-8")) > 65536:
        raise ValueError("单条证据内容不能超过 64 KB")
    fingerprint = hashlib.sha256(encode({"source": source, "payload": clean}).encode("utf-8")).hexdigest()
    cursor.execute("""
        INSERT IGNORE INTO `infringement_knowledge_evidence`
            (`rule_id`, `source_type`, `source_detail`, `payload_json`, `fingerprint`, `created_at`)
        VALUES (%s, %s, %s, %s, %s, %s)
    """, (int(rule_id), source, detail, encoded, fingerprint,
            now or datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def record_import_evidence(cursor, records, source, now):
    """Capture feed revisions separately, including evidence for manual rules.

    Deleted records remain tombstones. Batched lookups avoid a new per-brand
    query when synchronizing a large WIPO feed.
    """
    for offset in range(0, len(records), 500):
        batch = records[offset:offset + 500]
        cursor.execute("SELECT `id`, `brand_name`, `deleted_at` FROM `infringement_knowledge` "
                       "WHERE `organization_key` = %s AND `brand_name` IN (" + ",".join(["%s"] * len(batch)) + ") FOR UPDATE",
                       (knowledge_organization(), *[item["brand_name"] for item in batch]))
        stored = {str(item["brand_name"]).casefold(): item for item in (cursor.fetchall() or [])}
        writes = []
        for item in batch:
            row = stored.get(item["brand_name"].casefold())
            if not row or row.get("deleted_at") is not None:
                continue
            payload = {key: value for key, value in item.items()
                       if key not in ("reviewed_by", "rule_version", "created_at", "updated_at")}
            if "wipo_detail" in payload:
                payload["source_detail"] = payload.pop("wipo_detail")
            detail = payload.get("source_detail") or ""
            detail = encode(detail) if isinstance(detail, dict) else str(detail)
            encoded = encode(payload)
            origin = "wikidata" if source == "analysis" and detail.startswith("global_ip:") else source
            fingerprint = hashlib.sha256(encode({"source": origin, "payload": payload}).encode("utf-8")).hexdigest()
            writes.append((int(row["id"]), origin, detail[:16000], encoded, fingerprint, now))
        if writes:
            cursor.executemany("""INSERT IGNORE INTO `infringement_knowledge_evidence`
                (`rule_id`, `source_type`, `source_detail`, `payload_json`, `fingerprint`, `created_at`)
                VALUES (%s, %s, %s, %s, %s, %s)""", writes)


def append_history(cursor, rule_id, before, after, action, actor, now):
    cursor.execute("""
        INSERT INTO `infringement_knowledge_history`
            (`rule_id`, `rule_version`, `action`, `actor`, `before_json`, `after_json`, `created_at`)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (int(rule_id), int(after.get("rule_version") or 1), action,
            str(actor or "")[:128], encode(before) if before else None, encode(after), now))


def save_manual_metadata(cursor, rule_id, normalized, existing, now, action="review"):
    previous = dict(existing or {})
    # Snapshot imported evidence before turning an automatic entry into manual.
    if previous.get("source_detail") or int(previous.get("evidence_count") or 0):
        append_evidence(cursor, rule_id, {
            "source_type": previous.get("original_source_type") or previous.get("source_type") or "manual",
            "source_detail": previous.get("source_detail") or "",
            "evidence_count": int(previous.get("evidence_count") or 0),
            "brand_name": previous.get("brand_name") or normalized["brand_name"],
        }, now)
    aliases = normalized.get("aliases", decode(previous.get("aliases_json"), []))
    scope = normalized.get("scope")
    scope_json = encode(scope) if scope is not None else previous.get("scope_json")
    expiry = normalized.get("valid_until", previous.get("valid_until"))
    actor = normalized.get("reviewed_by") or ""
    reviewed_at = now if normalized["list_type"] != "pending" else None
    origin = previous.get("original_source_type") or previous.get("source_type") or "manual"
    source_detail = previous.get("source_detail") or normalized.get("source_detail") or None
    version = int(previous.get("rule_version") or 0) + 1
    cursor.execute("""
        UPDATE `infringement_knowledge`
        SET `aliases_json` = %s, `scope_json` = %s, `valid_until` = %s,
            `rule_version` = %s, `original_source_type` = %s,
            `reviewed_by` = %s, `reviewed_at` = %s, `source_detail` = %s
        WHERE `id` = %s
    """, (encode(aliases), scope_json, expiry, version, origin, actor, reviewed_at, source_detail, int(rule_id)))
    after = {**previous, **normalized, "id": int(rule_id), "aliases_json": encode(aliases),
             "scope_json": scope_json, "valid_until": expiry, "rule_version": version,
             "original_source_type": origin, "reviewed_by": actor, "reviewed_at": reviewed_at,
             "source_type": "manual", "source_detail": source_detail, "updated_at": now, "deleted_at": None}
    append_history(cursor, rule_id, previous or None, after, action, actor, now)
    if normalized.get("source_detail"):
        append_evidence(cursor, rule_id, {"source_type": "manual",
            "source_detail": normalized["source_detail"], "brand_name": normalized["brand_name"]}, now)
    return version


def get_detail(record_id):
    from bit import bit_mysql as db
    record_id = positive_id(record_id)
    connection = db.pymysql.connect(**db.config)
    try:
        with connection.cursor() as cursor:
            db._ensure_infringement_knowledge_table(cursor)
            cursor.execute("SELECT * FROM `infringement_knowledge` WHERE `organization_key` = %s AND `id` = %s AND `deleted_at` IS NULL", (knowledge_organization(), record_id))
            row = cursor.fetchone()
            if not row:
                raise ValueError("知识库记录不存在")
            cursor.execute("SELECT * FROM `infringement_knowledge_evidence` WHERE `rule_id` = %s ORDER BY `id` DESC LIMIT 200", (record_id,))
            evidence = list(cursor.fetchall() or [])
            for item in evidence:
                item["payload"] = decode(item.pop("payload_json", None), {})
                item["created_at"] = str(item.get("created_at") or "")
            cursor.execute("SELECT * FROM `infringement_knowledge_history` WHERE `rule_id` = %s ORDER BY `id` DESC LIMIT 100", (record_id,))
            history = list(cursor.fetchall() or [])
            for item in history:
                item["before"] = decode(item.pop("before_json", None), {})
                item["after"] = decode(item.pop("after_json", None), {})
                item["created_at"] = str(item.get("created_at") or "")
            record = db._serialize_infringement_knowledge_row(row)
            if not evidence and record.get("source_detail"):
                evidence = [{"id": None, "source_type": record["original_source_type"],
                    "source_detail": record["source_detail"], "payload": {},
                    "created_at": record.get("created_at") or "", "legacy": True}]
        connection.commit()
        return {"record": record, "evidence": evidence, "history": history,
                "evidence_limit": 200, "history_limit": 100}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def positive_id(value):
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("知识库记录编号无效") from exc
    if result < 1:
        raise ValueError("知识库记录编号无效")
    return result


def submit_candidate(record, actor=""):
    """A detector or operator may submit evidence, never a brand-wide release."""
    from bit import bit_mysql as db
    if not isinstance(record, dict):
        raise ValueError("知识库记录格式无效")
    normalized = normalize_knowledge_record({**record, "list_type": "pending"})
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = db.pymysql.connect(**db.config)
    try:
        with connection.cursor() as cursor:
            db._ensure_infringement_knowledge_table(cursor)
            connection.begin()
            cursor.execute("SELECT * FROM `infringement_knowledge` WHERE `organization_key` = %s AND `brand_name` = %s LIMIT 1 FOR UPDATE", (knowledge_organization(), normalized["brand_name"]))
            existing = cursor.fetchone()
            if existing and existing.get("deleted_at") is not None:
                raise ValueError("该品牌已删除，自动线索不会恢复；请人工新增")
            if not existing:
                cursor.execute("""INSERT INTO `infringement_knowledge`
                    (`brand_name`, `list_type`, `notes`, `source_type`, `source_detail`, `created_at`, `updated_at`, `organization_key`)
                    VALUES (%s, 'pending', %s, 'analysis', %s, %s, %s, %s)""",
                    (normalized["brand_name"], normalized["notes"], normalized.get("source_detail") or "", now, now, knowledge_organization()))
                record_id = int(cursor.lastrowid)
                append_history(cursor, record_id, None, {**normalized, "source_type": "analysis",
                    "rule_version": 1}, "candidate", actor, now)
            else:
                record_id = int(existing["id"])
            append_evidence(cursor, record_id, {**record, "list_type": "pending",
                "source_type": "detection", "submitted_by": str(actor or "")[:128]}, now)
        connection.commit()
        return {"id": record_id, "list_type": effective_list_type(existing) if existing else "pending",
                "candidate_status": "pending", "preserved_manual": bool(existing and existing.get("source_type", "manual") == "manual")}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_products(record_id, limit=50, token_ids=None):
    """Reverse actual rule hits, with clearly labelled legacy title candidates."""
    from bit import bit_mysql as db
    record_id = positive_id(record_id)
    try:
        limit = max(1, min(int(limit), 200))
    except (TypeError, ValueError) as exc:
        raise ValueError("关联商品条数无效") from exc
    connection = db.pymysql.connect(**db.config)
    try:
        with connection.cursor() as cursor:
            db._ensure_infringement_knowledge_table(cursor)
            db._ensure_infringement_risk_checks_table(cursor)
            cursor.execute("SELECT * FROM `infringement_knowledge` WHERE `organization_key` = %s AND `id` = %s AND `deleted_at` IS NULL", (knowledge_organization(), record_id))
            rule = cursor.fetchone()
            if not rule:
                raise ValueError("知识库记录不存在")
            safe_ids = "CASE WHEN JSON_VALID(`details_json`) THEN JSON_EXTRACT(`details_json`, '$.matched_rule_ids') ELSE JSON_ARRAY() END"
            exact = f"(JSON_CONTAINS({safe_ids}, %s) OR JSON_CONTAINS({safe_ids}, %s))"
            terms, term_params = [], []
            for name in knowledge_rule_names(rule):
                escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                terms.append("(`title` LIKE %s OR `keywords` LIKE %s)")
                term_params.extend((f"%{escaped}%", f"%{escaped}%"))
            legacy = f"({safe_ids} IS NULL OR JSON_LENGTH({safe_ids}) = 0)"
            conditions = [f"({exact} OR ({legacy} AND ({' OR '.join(terms) or '0=1'})))"]
            params = [encode(record_id), encode(str(record_id)), encode(record_id), encode(str(record_id)), *term_params]
            conditions.append("`token_id` IN (SELECT `id` FROM `mercado_store_tokens` "
                              "WHERE COALESCE(NULLIF(`organization_key`, 'default'), 'wuhan-zeshun') = %s)")
            params.append(knowledge_organization())
            if token_ids is not None:
                accounts = db._normalize_positive_ids(token_ids)
                conditions.append("`token_id` IN (" + ",".join(["%s"] * len(accounts)) + ")" if accounts else "0=1")
                params.extend(accounts)
            cursor.execute(f"SELECT *, CASE WHEN {exact} THEN 'matched_rule' ELSE 'legacy_candidate' END AS `association` "
                           f"FROM `infringement_risk_checks` WHERE {' AND '.join(conditions)} "
                           "ORDER BY `checked_at` DESC, `id` DESC LIMIT %s", (*params, limit + 1))
            rows = [dict(row) for row in (cursor.fetchall() or [])]
            for row in rows:
                row["record_key"] = f"{row.get('source_type')}:{row.get('source_row_id')}"
                row["details"] = decode(row.pop("details_json", None), {})
                for key in ("checked_at", "reviewed_at", "submitted_at"):
                    if row.get(key) is not None:
                        row[key] = str(row[key])
        connection.commit()
        return {"rows": rows[:limit], "has_more": len(rows) > limit, "rule_id": record_id, "limit": limit}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
