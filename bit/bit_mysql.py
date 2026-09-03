import json
import hashlib
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import pymysql

from bit.workbench_runtime import bootstrap_runtime


RUNTIME_SETTINGS = bootstrap_runtime()


def _blocked_client_mysql_connect(*args, **kwargs):
    raise RuntimeError(
        "客户端模式禁止直连 MySQL；请通过 bit.bit_db_api 调用服务端的 /api/db/* 接口"
    )


# 一些旧业务模块仍会导入 bit_mysql。客户端进程允许导入这些模块以保持
# 界面可启动，但任何遗漏的直连路径都会在真正连接前被明确拦截。
if RUNTIME_SETTINGS.is_client and os.environ.get("BIT_DB_DIRECT_DISABLED") == "1":
    pymysql.connect = _blocked_client_mysql_connect

# 1. 配置数据库连接信息
config = {
    'host': os.environ.get('MYSQL_HOST', os.environ.get('DB_HOST', '192.168.1.11')),
    'user': os.environ.get('MYSQL_USER', os.environ.get('DB_USER', 'mercado')),
    'password': os.environ.get('MYSQL_PASSWORD', os.environ.get('DB_PASSWORD', 'mercado')),
    'database': os.environ.get('MYSQL_DATABASE', os.environ.get('DB_NAME', 'mercado')),
    'charset': os.environ.get('MYSQL_CHARSET', 'utf8mb4'),
    'port': int(os.environ.get('MYSQL_PORT', os.environ.get('DB_PORT', '3306'))),
    'cursorclass': pymysql.cursors.DictCursor  # 让查询结果以字典形式返回
}

_MERCADO_PUBLIC_ITEM_BASE_URLS = {
    "MLM": "https://articulo.mercadolibre.com.mx",
    "MLB": "https://produto.mercadolivre.com.br",
    "MLC": "https://articulo.mercadolibre.cl",
    "MCO": "https://articulo.mercadolibre.com.co",
    "MLA": "https://articulo.mercadolibre.com.ar",
    "MLU": "https://articulo.mercadolibre.com.uy",
    "MPE": "https://articulo.mercadolibre.com.pe",
    "MEC": "https://articulo.mercadolibre.com.ec",
}
# config = {
#     'host': 'c766667e.natappfree.cc',
#     'user': 'root',
#     'password': 'zzw@951208',
#     'database': 'mercado',
#     'charset': 'utf8mb4',
#     'port': 39181,
#     'cursorclass': pymysql.cursors.DictCursor  # 让查询结果以字典形式返回
# }
# yuming=c766667e.natappfree.cc:39181


def _parse_filter_datetime(value, label="时间"):
    """解析日期或精确到分钟的日期时间，返回值及其输入精度。"""
    text = str(value or "").strip()
    if not text:
        return None, ""
    normalized = text.replace("T", " ")
    formats = (
        ("%Y-%m-%d", "day"),
        ("%Y-%m-%d %H:%M", "minute"),
        ("%Y-%m-%d %H:%M:%S", "minute"),
    )
    for date_format, precision in formats:
        try:
            return datetime.strptime(normalized, date_format), precision
        except ValueError:
            continue
    raise ValueError(f"{label}必须使用 YYYY-MM-DD HH:MM 格式")


def _filter_datetime_bounds(date_from="", date_to=""):
    start_at, _ = _parse_filter_datetime(date_from, "开始时间")
    end_at, end_precision = _parse_filter_datetime(date_to, "结束时间")
    if start_at and end_at and start_at > end_at:
        raise ValueError("开始日期不能晚于结束日期")
    end_exclusive = None
    if end_at:
        end_exclusive = end_at + (
            timedelta(days=1) if end_precision == "day" else timedelta(minutes=1)
        )
    return start_at, end_exclusive


def _ensure_column(cursor, table_name, column_name, column_definition):
    cursor.execute(f"SHOW COLUMNS FROM `{table_name}` LIKE %s", (column_name,))
    if cursor.fetchone():
        return
    cursor.execute(f"ALTER TABLE `{table_name}` ADD COLUMN `{column_name}` {column_definition}")


def _appeal_phrase_hash(content):
    return hashlib.sha256(str(content or "").encode("utf-8")).hexdigest()


def _normalize_appeal_phrase_record(record, require_active=False):
    from bit.bit_appeal_phrases import normalize_appeal_type

    if not isinstance(record, dict):
        raise ValueError("话术内容格式无效")
    appeal_type = normalize_appeal_type(record.get("appeal_type"))
    content = str(record.get("content") or "").strip()
    if not content:
        raise ValueError("话术内容不能为空")
    if len(content) > 10000:
        raise ValueError("话术内容不能超过 10000 个字符")
    normalized = {
        "appeal_type": appeal_type,
        "content": content,
        "content_hash": _appeal_phrase_hash(content),
    }
    if require_active or "is_active" in record:
        value = record.get("is_active", True)
        if isinstance(value, str):
            value = value.strip().lower() not in ("0", "false", "no", "off", "")
        normalized["is_active"] = 1 if bool(value) else 0
    return normalized


def _ensure_appeal_phrases_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `appeal_phrases` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `source_key` VARCHAR(64) NULL,
            `appeal_type` VARCHAR(32) NOT NULL,
            `content` TEXT NOT NULL,
            `content_hash` CHAR(64) NOT NULL,
            `is_active` TINYINT(1) NOT NULL DEFAULT 1,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            `deleted_at` DATETIME NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_appeal_phrase_source` (`source_key`),
            UNIQUE KEY `uniq_appeal_phrase_content` (`appeal_type`, `content_hash`),
            KEY `idx_appeal_phrase_type_active` (`appeal_type`, `is_active`, `deleted_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )

    from bit.bit_appeal_phrases import default_phrase_rows

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cursor.executemany(
        """
        INSERT IGNORE INTO `appeal_phrases` (
            `source_key`, `appeal_type`, `content`, `content_hash`,
            `is_active`, `created_at`, `updated_at`
        ) VALUES (%s, %s, %s, %s, 1, %s, %s)
        """,
        [
            (
                row["source_key"],
                row["appeal_type"],
                row["content"],
                _appeal_phrase_hash(row["content"]),
                now,
                now,
            )
            for row in default_phrase_rows()
        ],
    )


def _serialize_appeal_phrase_row(row):
    result = dict(row or {})
    result["id"] = int(result.get("id") or 0)
    result["is_active"] = bool(result.get("is_active"))
    for key in ("created_at", "updated_at"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    result.pop("source_key", None)
    result.pop("content_hash", None)
    result.pop("deleted_at", None)
    return result


def list_appeal_phrases():
    from bit.bit_appeal_phrases import APPEAL_TYPES

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_appeal_phrases_table(cursor)
            cursor.execute(
                """
                SELECT `id`, `appeal_type`, `content`, `is_active`,
                       `created_at`, `updated_at`
                FROM `appeal_phrases`
                WHERE `deleted_at` IS NULL
                ORDER BY FIELD(`appeal_type`, '延误', '侵权', '取消率', '投诉'), `id`
                """
            )
            rows = [_serialize_appeal_phrase_row(row) for row in cursor.fetchall()]
        connection.commit()
        summary = []
        for appeal_type in APPEAL_TYPES:
            typed_rows = [row for row in rows if row["appeal_type"] == appeal_type]
            summary.append(
                {
                    "appeal_type": appeal_type,
                    "total": len(typed_rows),
                    "active": sum(1 for row in typed_rows if row["is_active"]),
                }
            )
        return {"summary": summary, "rows": rows, "total": len(rows)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_random_appeal_phrase(appeal_type):
    from bit.bit_appeal_phrases import normalize_appeal_type

    appeal_type = normalize_appeal_type(appeal_type)
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_appeal_phrases_table(cursor)
            cursor.execute(
                """
                SELECT `id`, `appeal_type`, `content`, `is_active`,
                       `created_at`, `updated_at`
                FROM `appeal_phrases`
                WHERE `appeal_type` = %s AND `is_active` = 1 AND `deleted_at` IS NULL
                ORDER BY RAND()
                LIMIT 1
                """,
                (appeal_type,),
            )
            row = cursor.fetchone()
        connection.commit()
        return _serialize_appeal_phrase_row(row) if row else None
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_appeal_phrase(record):
    normalized = _normalize_appeal_phrase_record(record, require_active=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_appeal_phrases_table(cursor)
            cursor.execute(
                """
                SELECT `id`, `deleted_at`
                FROM `appeal_phrases`
                WHERE `appeal_type` = %s AND `content_hash` = %s
                LIMIT 1
                """,
                (normalized["appeal_type"], normalized["content_hash"]),
            )
            existing = cursor.fetchone()
            if existing and existing.get("deleted_at") is None:
                raise ValueError("该申诉类型下已存在相同话术")
            if existing:
                phrase_id = int(existing["id"])
                cursor.execute(
                    """
                    UPDATE `appeal_phrases`
                    SET `content` = %s, `is_active` = %s, `updated_at` = %s,
                        `deleted_at` = NULL
                    WHERE `id` = %s
                    """,
                    (normalized["content"], normalized["is_active"], now, phrase_id),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO `appeal_phrases` (
                        `appeal_type`, `content`, `content_hash`, `is_active`,
                        `created_at`, `updated_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        normalized["appeal_type"],
                        normalized["content"],
                        normalized["content_hash"],
                        normalized["is_active"],
                        now,
                        now,
                    ),
                )
                phrase_id = int(cursor.lastrowid)
        connection.commit()
        return {"id": phrase_id}
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("该申诉类型下已存在相同话术") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_appeal_phrase(phrase_id, record):
    try:
        phrase_id = int(phrase_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("话术编号无效") from exc
    if phrase_id <= 0:
        raise ValueError("话术编号无效")
    normalized = _normalize_appeal_phrase_record(record, require_active=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_appeal_phrases_table(cursor)
            cursor.execute(
                "SELECT `id` FROM `appeal_phrases` WHERE `id` = %s AND `deleted_at` IS NULL",
                (phrase_id,),
            )
            if not cursor.fetchone():
                raise ValueError("话术不存在")
            cursor.execute(
                """
                UPDATE `appeal_phrases`
                SET `appeal_type` = %s, `content` = %s, `content_hash` = %s,
                    `is_active` = %s, `updated_at` = %s
                WHERE `id` = %s
                """,
                (
                    normalized["appeal_type"],
                    normalized["content"],
                    normalized["content_hash"],
                    normalized["is_active"],
                    now,
                    phrase_id,
                ),
            )
        connection.commit()
        return {"id": phrase_id}
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("该申诉类型下已存在相同话术") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_appeal_phrase(phrase_id):
    try:
        phrase_id = int(phrase_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("话术编号无效") from exc
    if phrase_id <= 0:
        raise ValueError("话术编号无效")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_appeal_phrases_table(cursor)
            cursor.execute(
                """
                UPDATE `appeal_phrases`
                SET `deleted_at` = %s, `is_active` = 0, `updated_at` = %s
                WHERE `id` = %s AND `deleted_at` IS NULL
                """,
                (now, now, phrase_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("话术不存在")
        connection.commit()
        return {"id": phrase_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_infringement_knowledge_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `infringement_knowledge` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `brand_name` VARCHAR(255) NOT NULL,
            `list_type` VARCHAR(16) NOT NULL,
            `notes` VARCHAR(2000) NULL,
            `source_type` VARCHAR(16) NOT NULL DEFAULT 'manual',
            `evidence_count` INT NOT NULL DEFAULT 0,
            `source_detail` VARCHAR(1000) NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            `deleted_at` DATETIME NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_infringement_knowledge_brand` (`brand_name`),
            KEY `idx_infringement_knowledge_type` (`list_type`, `deleted_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(
        cursor,
        "infringement_knowledge",
        "source_type",
        "VARCHAR(16) NOT NULL DEFAULT 'manual' AFTER `notes`",
    )
    _ensure_column(
        cursor,
        "infringement_knowledge",
        "evidence_count",
        "INT NOT NULL DEFAULT 0 AFTER `source_type`",
    )
    _ensure_column(
        cursor,
        "infringement_knowledge",
        "source_detail",
        "VARCHAR(1000) NULL AFTER `evidence_count`",
    )


def _serialize_infringement_knowledge_row(row):
    from bit.bit_infringement_knowledge import list_type_label

    result = dict(row or {})
    result["id"] = int(result.get("id") or 0)
    result["list_type_label"] = list_type_label(result.get("list_type"))
    result["notes"] = str(result.get("notes") or "")
    result["source_type"] = str(result.get("source_type") or "manual")
    result["evidence_count"] = int(result.get("evidence_count") or 0)
    result["source_detail"] = str(result.get("source_detail") or "")
    for key in ("created_at", "updated_at"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    result.pop("deleted_at", None)
    return result


def list_infringement_knowledge(list_type="", search="", limit=2000):
    from bit.bit_infringement_knowledge import normalize_list_type

    normalized_type = normalize_list_type(list_type, allow_empty=True)
    search = str(search or "").strip()
    try:
        limit = max(1, min(int(limit or 2000), 5000))
    except (TypeError, ValueError):
        limit = 2000

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_infringement_knowledge_table(cursor)
            cursor.execute(
                """
                SELECT COUNT(*) AS `total`,
                       SUM(CASE WHEN `list_type` = 'whitelist' THEN 1 ELSE 0 END)
                           AS `whitelist`,
                       SUM(CASE WHEN `list_type` = 'blacklist' THEN 1 ELSE 0 END)
                           AS `blacklist`
                FROM `infringement_knowledge`
                WHERE `deleted_at` IS NULL
                """
            )
            summary_row = cursor.fetchone() or {}

            conditions = ["`deleted_at` IS NULL"]
            parameters = []
            if normalized_type:
                conditions.append("`list_type` = %s")
                parameters.append(normalized_type)
            if search:
                conditions.append("(`brand_name` LIKE %s OR `notes` LIKE %s)")
                keyword = f"%{search}%"
                parameters.extend((keyword, keyword))
            cursor.execute(
                f"""
                SELECT `id`, `brand_name`, `list_type`, `notes`, `source_type`,
                       `evidence_count`, `source_detail`,
                       `created_at`, `updated_at`
                FROM `infringement_knowledge`
                WHERE {' AND '.join(conditions)}
                ORDER BY `updated_at` DESC, `id` DESC
                LIMIT %s
                """,
                (*parameters, limit),
            )
            rows = [
                _serialize_infringement_knowledge_row(row)
                for row in (cursor.fetchall() or [])
            ]
        connection.commit()
        return {
            "summary": {
                "total": int(summary_row.get("total") or 0),
                "whitelist": int(summary_row.get("whitelist") or 0),
                "blacklist": int(summary_row.get("blacklist") or 0),
            },
            "rows": rows,
            "filtered_total": len(rows),
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def create_infringement_knowledge(record):
    from bit.bit_infringement_knowledge import normalize_knowledge_record

    normalized = normalize_knowledge_record(record)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_infringement_knowledge_table(cursor)
            cursor.execute(
                "SELECT `id`, `deleted_at` FROM `infringement_knowledge` "
                "WHERE `brand_name` = %s LIMIT 1",
                (normalized["brand_name"],),
            )
            existing = cursor.fetchone()
            if existing and existing.get("deleted_at") is None:
                raise ValueError("该品牌已存在于侵权知识库")
            if existing:
                record_id = int(existing["id"])
                cursor.execute(
                    """
                    UPDATE `infringement_knowledge`
                    SET `list_type` = %s, `notes` = %s, `source_type` = 'manual',
                        `evidence_count` = 0, `source_detail` = NULL, `updated_at` = %s,
                        `deleted_at` = NULL
                    WHERE `id` = %s
                    """,
                    (
                        normalized["list_type"],
                        normalized["notes"],
                        now,
                        record_id,
                    ),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO `infringement_knowledge` (
                        `brand_name`, `list_type`, `notes`, `source_type`,
                        `evidence_count`, `created_at`, `updated_at`
                    ) VALUES (%s, %s, %s, 'manual', 0, %s, %s)
                    """,
                    (
                        normalized["brand_name"],
                        normalized["list_type"],
                        normalized["notes"],
                        now,
                        now,
                    ),
                )
                record_id = int(cursor.lastrowid)
        connection.commit()
        return {"id": record_id}
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("该品牌已存在于侵权知识库") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_infringement_knowledge(record_id, record):
    from bit.bit_infringement_knowledge import normalize_knowledge_record

    try:
        record_id = int(record_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("知识库记录编号无效") from exc
    if record_id <= 0:
        raise ValueError("知识库记录编号无效")
    normalized = normalize_knowledge_record(record)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_infringement_knowledge_table(cursor)
            cursor.execute(
                "SELECT `id` FROM `infringement_knowledge` "
                "WHERE `id` = %s AND `deleted_at` IS NULL",
                (record_id,),
            )
            if not cursor.fetchone():
                raise ValueError("知识库记录不存在")
            cursor.execute(
                """
                UPDATE `infringement_knowledge`
                SET `brand_name` = %s, `list_type` = %s, `notes` = %s,
                    `source_type` = 'manual', `evidence_count` = 0,
                    `source_detail` = NULL, `updated_at` = %s
                WHERE `id` = %s
                """,
                (
                    normalized["brand_name"],
                    normalized["list_type"],
                    normalized["notes"],
                    now,
                    record_id,
                ),
            )
        connection.commit()
        return {"id": record_id}
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("该品牌已存在于侵权知识库") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_infringement_knowledge(record_id):
    try:
        record_id = int(record_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("知识库记录编号无效") from exc
    if record_id <= 0:
        raise ValueError("知识库记录编号无效")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_infringement_knowledge_table(cursor)
            cursor.execute(
                """
                UPDATE `infringement_knowledge`
                SET `deleted_at` = %s, `source_type` = 'manual', `updated_at` = %s
                WHERE `id` = %s AND `deleted_at` IS NULL
                """,
                (now, now, record_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("知识库记录不存在")
        connection.commit()
        return {"id": record_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def bulk_create_infringement_knowledge(records):
    from bit.bit_infringement_knowledge import normalize_knowledge_record

    normalized_records = []
    seen = set()
    for record in records or ():
        normalized = normalize_knowledge_record(record)
        key = normalized["brand_name"].casefold()
        if key not in seen:
            seen.add(key)
            normalized_records.append(normalized)
    if not normalized_records:
        raise ValueError("请至少输入一个品牌，每行一个")
    if len(normalized_records) > 1000:
        raise ValueError("单次最多新增 1000 个品牌")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = restored = skipped = 0
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_infringement_knowledge_table(cursor)
            for normalized in normalized_records:
                cursor.execute(
                    "SELECT `id`, `deleted_at` FROM `infringement_knowledge` "
                    "WHERE `brand_name` = %s LIMIT 1",
                    (normalized["brand_name"],),
                )
                existing = cursor.fetchone()
                if existing and existing.get("deleted_at") is None:
                    skipped += 1
                    continue
                if existing:
                    cursor.execute(
                        """
                        UPDATE `infringement_knowledge`
                        SET `list_type` = %s, `notes` = %s,
                            `source_type` = 'manual', `evidence_count` = 0,
                            `source_detail` = NULL, `updated_at` = %s,
                            `deleted_at` = NULL
                        WHERE `id` = %s
                        """,
                        (
                            normalized["list_type"],
                            normalized["notes"],
                            now,
                            int(existing["id"]),
                        ),
                    )
                    restored += 1
                    continue
                cursor.execute(
                    """
                    INSERT INTO `infringement_knowledge` (
                        `brand_name`, `list_type`, `notes`, `source_type`,
                        `evidence_count`, `created_at`, `updated_at`
                    ) VALUES (%s, %s, %s, 'manual', 0, %s, %s)
                    """,
                    (
                        normalized["brand_name"],
                        normalized["list_type"],
                        normalized["notes"],
                        now,
                        now,
                    ),
                )
                inserted += 1
        connection.commit()
        return {
            "total": len(normalized_records),
            "inserted": inserted,
            "restored": restored,
            "skipped": skipped,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_infringement_knowledge_analysis_sources(
    infraction_limit=10000,
    active_limit=5000,
):
    try:
        infraction_limit = max(1, min(int(infraction_limit or 10000), 20000))
        active_limit = max(1, min(int(active_limit or 5000), 10000))
    except (TypeError, ValueError) as exc:
        raise ValueError("自动分析条数必须是有效整数") from exc

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            from erp.mercadolibre_store_link_store import ensure_store_link_table

            ensure_store_link_table(cursor)
            cursor.execute(
                """
                SELECT CAST(`编号` AS CHAR) AS `item_id`, MAX(`标题`) AS `title`
                FROM `infraction`
                WHERE `编号` IS NOT NULL AND `编号` <> ''
                  AND `标题` IS NOT NULL AND `标题` <> ''
                GROUP BY `编号`
                ORDER BY MAX(`提交时间`) DESC
                LIMIT %s
                """,
                (infraction_limit,),
            )
            infraction_rows = list(cursor.fetchall() or [])
            cursor.execute(
                """
                SELECT `item_id`, `title`, COALESCE(`sold_quantity`, 0) AS `sold_quantity`
                FROM `erp_mercadolibre_store_links`
                WHERE `is_current` = 1 AND `status` = 'active'
                  AND COALESCE(`sold_quantity`, 0) > 0
                  AND `title` IS NOT NULL AND `title` <> ''
                ORDER BY COALESCE(`sold_quantity`, 0) DESC, `id` DESC
                LIMIT %s
                """,
                (active_limit,),
            )
            active_rows = list(cursor.fetchall() or [])
            cursor.execute(
                """
                SELECT COUNT(DISTINCT `编号`) AS `total`
                FROM `infraction`
                WHERE `编号` IS NOT NULL AND `编号` <> ''
                """
            )
            infraction_total = int((cursor.fetchone() or {}).get("total") or 0)
            cursor.execute(
                """
                SELECT COUNT(*) AS `total`
                FROM `erp_mercadolibre_store_links`
                WHERE `is_current` = 1 AND `status` = 'active'
                  AND COALESCE(`sold_quantity`, 0) > 0
                """
            )
            active_total = int((cursor.fetchone() or {}).get("total") or 0)
        connection.commit()
        return {
            "infraction_rows": infraction_rows,
            "active_rows": active_rows,
            "infraction_total": infraction_total,
            "active_total": active_total,
        }
    finally:
        connection.close()


def upsert_analyzed_infringement_knowledge(records):
    from bit.bit_infringement_knowledge import normalize_knowledge_record

    normalized_records = []
    seen = set()
    for record in records or ():
        normalized = normalize_knowledge_record(record)
        key = normalized["brand_name"].casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            evidence_count = max(0, int(record.get("evidence_count") or 0))
        except (TypeError, ValueError):
            evidence_count = 0
        normalized["evidence_count"] = evidence_count
        normalized["source_detail"] = str(record.get("source_detail") or "").strip()[:1000]
        normalized_records.append(normalized)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    inserted = updated = skipped_manual = skipped_blacklist = 0
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_infringement_knowledge_table(cursor)
            for normalized in normalized_records:
                cursor.execute(
                    """
                    SELECT `id`, `list_type`, `source_type`, `deleted_at`
                    FROM `infringement_knowledge`
                    WHERE `brand_name` = %s LIMIT 1
                    """,
                    (normalized["brand_name"],),
                )
                existing = cursor.fetchone()
                if existing:
                    if (
                        str(existing.get("source_type") or "manual") != "analysis"
                        or existing.get("deleted_at") is not None
                    ):
                        skipped_manual += 1
                        continue
                    if (
                        str(existing.get("list_type") or "") == "blacklist"
                        and normalized["list_type"] == "whitelist"
                    ):
                        skipped_blacklist += 1
                        continue
                    cursor.execute(
                        """
                        UPDATE `infringement_knowledge`
                        SET `list_type` = %s, `notes` = %s,
                            `evidence_count` = %s, `source_detail` = %s,
                            `updated_at` = %s, `deleted_at` = NULL
                        WHERE `id` = %s
                        """,
                        (
                            normalized["list_type"],
                            normalized["notes"],
                            normalized["evidence_count"],
                            normalized["source_detail"],
                            now,
                            int(existing["id"]),
                        ),
                    )
                    updated += 1
                    continue
                cursor.execute(
                    """
                    INSERT INTO `infringement_knowledge` (
                        `brand_name`, `list_type`, `notes`, `source_type`,
                        `evidence_count`, `source_detail`, `created_at`, `updated_at`
                    ) VALUES (%s, %s, %s, 'analysis', %s, %s, %s, %s)
                    """,
                    (
                        normalized["brand_name"],
                        normalized["list_type"],
                        normalized["notes"],
                        normalized["evidence_count"],
                        normalized["source_detail"],
                        now,
                        now,
                    ),
                )
                inserted += 1
        connection.commit()
        return {
            "total": len(normalized_records),
            "inserted": inserted,
            "updated": updated,
            "skipped_manual": skipped_manual,
            "skipped_blacklist": skipped_blacklist,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _parse_number(value):
    text = str(value or "").replace(",", "").strip()
    number_text = "".join(ch for ch in text if ch.isdigit() or ch in ".-")
    try:
        return float(number_text) if number_text else 0
    except ValueError:
        return 0


def _parse_traffic_total(value):
    if isinstance(value, (list, tuple)):
        values = value
    else:
        text = str(value or "").strip()
        if not text:
            return 0
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = re.findall(r"-?\d+(?:\.\d+)?", text)
        values = parsed if isinstance(parsed, (list, tuple)) else [parsed]
    return sum(_parse_number(item) for item in values)


def mysql_demo():
    # 建立连接
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = "INSERT INTO `users` (`username`, `email`) VALUES (%s, %s)"
            cursor.execute(sql_insert, ("Gemini", "gemini@example.com"))
            print(f"新增记录ID: {cursor.lastrowid}")

            # --- 查 (Read) ---
            sql_select = "SELECT * FROM `users` WHERE `username` = %s"
            cursor.execute(sql_select, ("Gemini",))
            result = cursor.fetchone()
            print(f"查询结果: {result}")

            # --- 改 (Update) ---
            sql_update = "UPDATE `users` SET `email` = %s WHERE `username` = %s"
            cursor.execute(sql_update, ("new_gemini@example.com", "Gemini"))
            print(f"修改行数: {cursor.rowcount}")

            # --- 删 (Delete) ---
            sql_delete = "DELETE FROM `users` WHERE `username` = %s"
            cursor.execute(sql_delete, ("Gemini",))
            print(f"删除行数: {cursor.rowcount}")

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()
        print("事务已提交")

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def insert_task_record(record_list):
    if not record_list:
        return 0
    # 建立连接
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = "insert into record (type,name,site,isSuccess,datetime) VALUES (%s,%s,%s,%s,%s)"
            cursor.executemany(sql_insert, record_list)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()
        return len(record_list)

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
        raise
    finally:
        # 关闭连接
        connection.close()


def get_latest_order_print_records():
    """返回每个店铺站点最近一次订单打印记录。"""

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT `name`, `site`, `isSuccess`, `datetime`
                FROM `record`
                WHERE `type` = '后台打印订单'
                  AND `name` IS NOT NULL AND `name` <> ''
                  AND `site` IS NOT NULL AND `site` <> ''
                ORDER BY `datetime` DESC, `id` DESC
                """
            )
            latest = []
            seen = set()
            for row in cursor.fetchall():
                shop_name = str(row.get("name") or "").strip()
                site = str(row.get("site") or "").strip()
                key = (shop_name, site)
                if not shop_name or not site or key in seen:
                    continue
                seen.add(key)
                latest.append(
                    {
                        "shop_name": shop_name,
                        "site": site,
                        "outcome": str(row.get("isSuccess") or "").strip(),
                        "finished_at": str(row.get("datetime") or ""),
                    }
                )
            return latest
    finally:
        connection.close()


def _normalize_collection_targets(targets):
    normalized = set()
    for target in targets or ():
        if isinstance(target, dict):
            shop_name = target.get("shop_name") or target.get("店铺名")
            site = target.get("site") or target.get("站点")
        elif isinstance(target, (list, tuple)) and len(target) >= 2:
            shop_name, site = target[:2]
        else:
            continue
        key = (str(shop_name or "").strip(), str(site or "").strip())
        if key[0] and key[1]:
            normalized.add(key)
    return normalized


def _active_collection_snapshot_rows(rows):
    """只保留当前启用店铺站点，避免把已经停用的历史店铺重新带回页面。"""
    rows = list(rows or ())
    try:
        active_targets = {
            (item["店铺名"], item["站点"])
            for item in _load_authorized_shop_sites("visit_stats_enabled")
        }
    except Exception as exc:
        print(f"读取店铺授权访问统计范围失败，按空范围处理：{exc}")
        return []
    if not active_targets:
        return []
    return [
        row
        for row in rows
        if (
            str(row.get("店铺名") or "").strip(),
            str(row.get("站点") or "").strip(),
        ) in active_targets
    ]


def _ensure_collection_snapshots_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `collection_snapshots` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `collection_type` VARCHAR(32) NOT NULL,
            `submit_time` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_collection_snapshot` (`collection_type`, `submit_time`),
            KEY `idx_collection_snapshot_latest` (`collection_type`, `submit_time`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _latest_tracked_collection_snapshot(cursor, collection_type):
    _ensure_collection_snapshots_table(cursor)
    cursor.execute(
        """
        SELECT MAX(`submit_time`) AS `latest_submit_time`
        FROM `collection_snapshots`
        WHERE `collection_type` = %s
        """,
        (str(collection_type or "").strip(),),
    )
    return (cursor.fetchone() or {}).get("latest_submit_time")


def _record_collection_snapshot(cursor, collection_type, submit_time):
    cursor.execute(
        """
        INSERT INTO `collection_snapshots` (`collection_type`, `submit_time`)
        VALUES (%s, %s)
        ON DUPLICATE KEY UPDATE `submit_time` = VALUES(`submit_time`)
        """,
        (str(collection_type or "").strip(), submit_time),
    )


def _next_collection_submit_time(cursor, collection_type, table_name):
    latest_submit_time = _latest_tracked_collection_snapshot(
        cursor,
        collection_type,
    )
    if not latest_submit_time:
        cursor.execute(
            f"SELECT MAX(`提交时间`) AS `latest_submit_time` FROM `{table_name}`"
        )
        latest_submit_time = (cursor.fetchone() or {}).get("latest_submit_time")
    now = datetime.now().replace(microsecond=0)
    if latest_submit_time:
        if isinstance(latest_submit_time, datetime):
            latest_datetime = latest_submit_time.replace(microsecond=0)
        else:
            try:
                latest_datetime = datetime.strptime(
                    str(latest_submit_time),
                    "%Y-%m-%d %H:%M:%S",
                )
            except ValueError:
                latest_datetime = None
        if latest_datetime is not None and now <= latest_datetime:
            now = latest_datetime + timedelta(seconds=1)
    return now.strftime("%Y-%m-%d %H:%M:%S")


def _latest_reputation_snapshot_rows(cursor):
    latest_submit_time = _latest_tracked_collection_snapshot(cursor, "reputation")
    column_names = (
        "店铺名", "站点", "声誉颜色", "总单量", "投诉率", "延误率",
        "取消率", "增加或减少", "近七天变化率", "系统告警", "更新时间",
        "一周流量趋势", "站点状态", "侵权数量", "权利人数量", "提交时间",
    )
    columns = ", ".join(f"`{name}`" for name in column_names)
    if latest_submit_time:
        cursor.execute(
            f"SELECT {columns} FROM `reputation` WHERE `提交时间` = %s",
            (latest_submit_time,),
        )
    else:
        joined_columns = ", ".join(
            f"source_rows.`{name}`" for name in column_names
        )
        cursor.execute(
            f"""
            SELECT {joined_columns}
            FROM `reputation` AS source_rows
            INNER JOIN (
                SELECT `店铺名`, `站点`, MAX(`提交时间`) AS `latest_submit_time`
                FROM `reputation`
                WHERE `提交时间` IS NOT NULL
                GROUP BY `店铺名`, `站点`
            ) AS latest
              ON source_rows.`店铺名` = latest.`店铺名`
             AND source_rows.`站点` = latest.`站点`
             AND source_rows.`提交时间` = latest.`latest_submit_time`
            """
        )
    return cursor.fetchall()


def _latest_infraction_snapshot_rows(cursor):
    latest_submit_time = _latest_tracked_collection_snapshot(cursor, "infraction")
    column_names = (
        "店铺名", "站点", "编号", "标题", "侵权时间",
        "提交时间", "执行时间", "类型",
    )
    columns = ", ".join(f"`{name}`" for name in column_names)
    if latest_submit_time:
        cursor.execute(
            f"SELECT {columns} FROM `infraction` WHERE `提交时间` = %s",
            (latest_submit_time,),
        )
    else:
        joined_columns = ", ".join(
            f"source_rows.`{name}`" for name in column_names
        )
        cursor.execute(
            f"""
            SELECT {joined_columns}
            FROM `infraction` AS source_rows
            INNER JOIN (
                SELECT `店铺名`, `站点`, MAX(`提交时间`) AS `latest_submit_time`
                FROM `infraction`
                WHERE `提交时间` IS NOT NULL AND `提交时间` <> ''
                GROUP BY `店铺名`, `站点`
            ) AS latest
              ON source_rows.`店铺名` = latest.`店铺名`
             AND source_rows.`站点` = latest.`站点`
             AND source_rows.`提交时间` = latest.`latest_submit_time`
            """
        )
    return cursor.fetchall()


def _merge_reputation_snapshot_rows(
    latest_rows,
    collected_rows,
    replace_targets,
    submit_time,
):
    targets = _normalize_collection_targets(replace_targets)
    targets.update(
        (str(row[0] or "").strip(), str(row[1] or "").strip())
        for row in collected_rows
        if str(row[0] or "").strip() and str(row[1] or "").strip()
    )
    merged_rows = {}
    for row in latest_rows or ():
        key = (
            str(row.get("店铺名") or "").strip(),
            str(row.get("站点") or "").strip(),
        )
        if not key[0] or not key[1] or key in targets:
            continue
        merged_rows[key] = [
            row.get("店铺名"),
            row.get("站点"),
            row.get("声誉颜色"),
            row.get("总单量"),
            row.get("投诉率"),
            row.get("延误率"),
            row.get("取消率"),
            row.get("增加或减少"),
            row.get("近七天变化率"),
            row.get("系统告警"),
            row.get("更新时间"),
            row.get("一周流量趋势"),
            row.get("站点状态"),
            row.get("侵权数量"),
            row.get("权利人数量"),
            submit_time,
        ]
    for row in collected_rows:
        merged_rows[(str(row[0]).strip(), str(row[1]).strip())] = row
    return list(merged_rows.values())


def _merge_infraction_snapshot_rows(
    latest_rows,
    collected_rows,
    replace_targets,
    submit_time,
):
    targets = _normalize_collection_targets(replace_targets)
    targets.update(
        (str(row[0] or "").strip(), str(row[1] or "").strip())
        for row in collected_rows
        if str(row[0] or "").strip() and str(row[1] or "").strip()
    )
    merged_rows = []
    for row in latest_rows or ():
        key = (
            str(row.get("店铺名") or "").strip(),
            str(row.get("站点") or "").strip(),
        )
        if not key[0] or not key[1] or key in targets:
            continue
        merged_rows.append(
            [
                row.get("店铺名"),
                row.get("站点"),
                row.get("编号"),
                row.get("标题"),
                row.get("侵权时间"),
                submit_time,
                row.get("执行时间"),
                row.get("类型") or "侵权",
            ]
        )
    return merged_rows + list(collected_rows)


def inset_reputation_info(
    reputation_list,
    merge_latest=False,
    replace_targets=None,
):
    if not reputation_list and not merge_latest:
        return 0
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, "reputation", "取消率", "VARCHAR(255) NULL")
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
            _ensure_column(cursor, "reputation", "站点状态", "VARCHAR(255) NULL")
            _ensure_column(cursor, "reputation", "侵权数量", "INT NULL")
            _ensure_column(cursor, "reputation", "权利人数量", "INT NULL")
            submit_time = _next_collection_submit_time(
                cursor,
                "reputation",
                "reputation",
            )
            normalized_list = []
            for row in reputation_list:
                row = list(row)
                if len(row) == 6:
                    color, orders, complain, delay, name, site = row
                    row = [
                        name,
                        site,
                        color,
                        orders,
                        complain,
                        delay,
                        "",
                        "",
                        "",
                        "",
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "",
                    ]
                elif len(row) == 10:
                    row = row[:6] + [""] + row[6:] + [""]
                elif len(row) == 11:
                    row = row[:6] + [""] + row[6:]
                if len(row) < 12:
                    row.extend([""] * (12 - len(row)))
                extras = row[12:15] if len(row) >= 15 else ["", None, None]
                row = row[:12] + extras + [submit_time]
                normalized_list.append(row)
            if merge_latest:
                normalized_list = _merge_reputation_snapshot_rows(
                    _active_collection_snapshot_rows(
                        _latest_reputation_snapshot_rows(cursor)
                    ),
                    normalized_list,
                    replace_targets,
                    submit_time,
                )
            print(f"准备插入声誉记录 {len(normalized_list)} 条，提交时间 {submit_time}")

            # --- 增 (Create) ---
            sql_insert = """
    INSERT INTO reputation (
         店铺名, 站点, 声誉颜色, 总单量, 
        投诉率, 延误率, 取消率, 增加或减少, 近七天变化率,
        系统告警, 更新时间, 一周流量趋势, 站点状态, 侵权数量,
        权利人数量, 提交时间
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
            if normalized_list:
                cursor.executemany(sql_insert, normalized_list)
            _record_collection_snapshot(cursor, "reputation", submit_time)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()
        return len(normalized_list)

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
        raise
    finally:
        # 关闭连接
        connection.close()


def inset_infraction_info(
    infraction_list,
    merge_latest=False,
    replace_targets=None,
):
    if not infraction_list and not merge_latest:
        return 0
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            normalized_list = []
            submit_time = _next_collection_submit_time(
                cursor,
                "infraction",
                "infraction",
            )
            for row in infraction_list:
                row = list(row)
                if len(row) == 6:
                    row.insert(5, "")
                    row.append("侵权")
                elif len(row) == 7:
                    row.append("侵权")
                if len(row) >= 8:
                    row[5] = submit_time
                normalized_list.append(row)
            if merge_latest:
                normalized_list = _merge_infraction_snapshot_rows(
                    _active_collection_snapshot_rows(
                        _latest_infraction_snapshot_rows(cursor)
                    ),
                    normalized_list,
                    replace_targets,
                    submit_time,
                )
            submit_time_count = sum(1 for row in normalized_list if len(row) > 5 and row[5])
            print(f"准备插入侵权记录 {len(normalized_list)} 条，提交时间 {submit_time}，非空 {submit_time_count} 条")

            # --- 增 (Create) ---
            sql_insert = """
    INSERT INTO infraction (
         店铺名,站点,编号,标题,侵权时间,提交时间,执行时间,类型

    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
    """
            if normalized_list:
                cursor.executemany(sql_insert, normalized_list)
            _record_collection_snapshot(cursor, "infraction", submit_time)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()
        return len(normalized_list)

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
        raise
    finally:
        # 关闭连接
        connection.close()


def _parse_infraction_date(value):
    text = str(value or "").strip()
    if not text:
        return None

    formats = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%m/%d/%y",
        "%m/%d/%Y",
        "%d/%m/%y",
        "%d/%m/%Y",
        "%b %d, %Y",
        "%B %d, %Y",
        "%d %b %Y",
        "%d %B %Y",
    ]
    candidates = []
    for fmt in formats:
        try:
            candidates.append(datetime.strptime(text, fmt))
        except ValueError:
            pass

    if not candidates:
        return None

    now = datetime.now()
    not_future = [item for item in candidates if item <= now]
    return max(not_future or candidates)


def _split_config_sites(value):
    text = str(value or "").strip()
    if not text or text.lower() == "nan":
        return []
    parts = re.split(r"[，,、/;\s]+", text)
    return [part.strip() for part in parts if part and part.strip()]


def _is_ignored_config_value(value):
    return "忽略" in str(value or "").strip()


def _authorization_flag_enabled(value):
    if isinstance(value, str):
        return value.strip().casefold() not in ("", "0", "false", "no", "off")
    return bool(value)


def _load_authorized_shop_sites(setting_field="visit_stats_enabled"):
    """直接从店铺授权读取显式开启任务开关的店铺站点。"""
    if setting_field not in ("appeal_enabled", "visit_stats_enabled"):
        raise ValueError(f"不支持的店铺授权任务开关：{setting_field}")
    configured = []
    seen = set()
    token_data = list_mercado_store_tokens() or {}
    for token in token_data.get("rows") or ():
        if not bool(token.get("enabled", True)):
            continue
        name = str(token.get("display_name") or token.get("nickname") or "").strip()
        if not name:
            continue
        for raw_setting in token.get("site_settings") or ():
            setting = dict(raw_setting or {})
            if not _authorization_flag_enabled(setting.get(setting_field)):
                continue
            site_id = str(setting.get("site_id") or "").strip().upper()
            site = MERCADO_CONFIGURABLE_SITES.get(site_id)
            if not site:
                continue
            key = (name, site)
            if key in seen:
                continue
            seen.add(key)
            configured.append({
                "token_id": int(token.get("id") or 0),
                "店铺名": name,
                "站点": site,
                "site_id": site_id,
                "业务员": str(setting.get("salesperson") or "").strip(),
                "店铺组": str(setting.get("group_name") or "").strip(),
            })
    return configured


def _get_latest_collection_task_status(cursor, record_type):
    cursor.execute(
        """
        SELECT `name`, `site`, `isSuccess`, `datetime`
        FROM record
        WHERE `type` = %s
          AND `name` IS NOT NULL AND `name` <> ''
          AND `site` IS NOT NULL AND `site` <> ''
        ORDER BY `datetime` DESC, `id` DESC
        """,
        (str(record_type or "").strip(),),
    )
    status_map = {}
    for row in cursor.fetchall():
        key = (str(row.get("name") or "").strip(), str(row.get("site") or "").strip())
        if not key[0] or not key[1] or key in status_map:
            continue
        status_map[key] = {
            "状态": str(row.get("isSuccess") or "").strip() or "未知",
            "状态时间": str(row.get("datetime") or ""),
        }
    return status_map


def _get_latest_infraction_task_status(cursor):
    return _get_latest_collection_task_status(cursor, "获取侵权信息")


def get_latest_infraction_info(recent_days=30):
    try:
        recent_days = int(recent_days)
    except (TypeError, ValueError):
        recent_days = 30
    if recent_days not in (7, 30, 100, 365):
        recent_days = 30

    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
            latest_submit_time = _latest_tracked_collection_snapshot(
                cursor,
                "infraction",
            )
            if not latest_submit_time:
                cursor.execute(
                    """
                    SELECT MAX(`提交时间`) AS latest_submit_time
                    FROM infraction
                    WHERE `提交时间` IS NOT NULL AND `提交时间` <> ''
                    """
                )
                latest_submit_time = (cursor.fetchone() or {}).get(
                    "latest_submit_time"
                )
            if not latest_submit_time:
                task_status_map = _get_latest_infraction_task_status(cursor)
                summary = []
                for configured_site in _load_authorized_shop_sites("visit_stats_enabled"):
                    key = (configured_site["店铺名"], configured_site["站点"])
                    task_status = task_status_map.get(key) or {}
                    summary.append({
                        "店铺名": key[0],
                        "站点": key[1],
                        "总数": 0,
                        "侵权": 0,
                        "权利人": 0,
                        "状态": task_status.get("状态") or "无数据",
                        "状态时间": task_status.get("状态时间") or "",
                    })
                return {"latest_submit_time": "", "total": 0, "summary": summary, "rows": []}

            # 有完整批次标记时读取该批次；兼容历史数据没有标记的情况时，
            # 按店铺和站点分别取最新批次，避免一次补跑把其他店铺从页面隐藏。
            rows = _active_collection_snapshot_rows(
                _latest_infraction_snapshot_rows(cursor)
            )
            cutoff = datetime.now() - timedelta(days=recent_days)
            recent_rows = []
            for row in rows:
                infraction_date = _parse_infraction_date(row.get("侵权时间"))
                if infraction_date and infraction_date >= cutoff:
                    row["_侵权时间排序"] = infraction_date
                    recent_rows.append(row)
            recent_rows.sort(
                key=lambda row: row.get("_侵权时间排序") or datetime.min,
                reverse=True,
            )
            for row in recent_rows:
                row.pop("_侵权时间排序", None)

            summary_map = {}
            for row in recent_rows:
                key = (row.get("店铺名") or "", row.get("站点") or "")
                item = summary_map.setdefault(
                    key,
                    {
                        "店铺名": key[0],
                        "站点": key[1],
                        "总数": 0,
                        "侵权": 0,
                        "权利人": 0,
                        "状态": "成功",
                        "状态时间": "",
                    },
                )
                item["总数"] += 1
                infraction_type = row.get("类型") or "侵权"
                if infraction_type == "权利人":
                    item["权利人"] += 1
                else:
                    item["侵权"] += 1

            task_status_map = _get_latest_infraction_task_status(cursor)
            for item in summary_map.values():
                task_status = task_status_map.get((item["店铺名"], item["站点"])) or {}
                item["状态"] = task_status.get("状态") or "成功"
                item["状态时间"] = task_status.get("状态时间") or ""

            for configured_site in _load_authorized_shop_sites("visit_stats_enabled"):
                key = (configured_site["店铺名"], configured_site["站点"])
                if key in summary_map:
                    continue
                task_status = task_status_map.get(key) or {}
                status = task_status.get("状态") or "无数据"
                summary_map[key] = {
                    "店铺名": key[0],
                    "站点": key[1],
                    "总数": 0,
                    "侵权": 0,
                    "权利人": 0,
                    "状态": status,
                    "状态时间": task_status.get("状态时间") or "",
                }

            summary = sorted(
                summary_map.values(),
                key=lambda item: (
                    item["状态"] != "失败",
                    item["总数"],
                    item["侵权"],
                    item["权利人"],
                    item["店铺名"],
                    item["站点"],
                ),
                reverse=True,
            )

            return {
                "latest_submit_time": str(latest_submit_time),
                "latest_total": len(rows),
                "recent_days": recent_days,
                "total": len(recent_rows),
                "summary": summary,
                "rows": recent_rows,
            }
    except Exception as e:
        print(f"查询最新侵权数据失败: {e}")
        raise
    finally:
        connection.close()


def _latest_infraction_counts_by_shop_site(cursor, recent_days=30):
    """按最新侵权快照统计指定周期内每个店铺站点的两类数量。"""
    cutoff = datetime.now() - timedelta(days=max(1, int(recent_days or 30)))
    counts = {}
    rows = _active_collection_snapshot_rows(
        _latest_infraction_snapshot_rows(cursor)
    )
    for row in rows:
        infraction_date = _parse_infraction_date(row.get("侵权时间"))
        if not infraction_date or infraction_date < cutoff:
            continue
        key = (
            str(row.get("店铺名") or "").strip(),
            str(row.get("站点") or "").strip(),
        )
        if not key[0] or not key[1]:
            continue
        item = counts.setdefault(key, {"侵权数量": 0, "权利人数量": 0})
        if str(row.get("类型") or "侵权").strip() == "权利人":
            item["权利人数量"] += 1
        else:
            item["侵权数量"] += 1
    return counts


def get_latest_reputation_info():
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            authorized_sites = _load_authorized_shop_sites("visit_stats_enabled")
            _ensure_column(cursor, "reputation", "取消率", "VARCHAR(255) NULL")
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
            _ensure_column(cursor, "reputation", "站点状态", "VARCHAR(255) NULL")
            _ensure_column(cursor, "reputation", "侵权数量", "INT NULL")
            _ensure_column(cursor, "reputation", "权利人数量", "INT NULL")
            latest_submit_time = _latest_tracked_collection_snapshot(
                cursor,
                "reputation",
            )
            if not latest_submit_time:
                cursor.execute(
                    """
                    SELECT MAX(`提交时间`) AS latest_submit_time
                    FROM reputation
                    WHERE `提交时间` IS NOT NULL
                    """
                )
                latest_submit_time = (cursor.fetchone() or {}).get(
                    "latest_submit_time"
                )
            if not latest_submit_time:
                task_status_map = _get_latest_collection_task_status(
                    cursor,
                    "获取声誉信息",
                )
                return {
                    "latest_submit_time": "",
                    "total": 0,
                    "summary": [
                        {
                            "店铺名": configured_site["店铺名"],
                            "站点": configured_site["站点"],
                            "状态": task_status.get("状态") or "未知",
                            "状态时间": task_status.get("状态时间") or "",
                        }
                        for configured_site in authorized_sites
                        for task_status in [task_status_map.get((
                            configured_site["店铺名"], configured_site["站点"]
                        )) or {}]
                    ],
                    "rows": [],
                }

            # 有完整批次标记时读取该批次；兼容历史数据没有标记的情况时，
            # 按店铺和站点分别取最新记录，让补跑结果与此前成功结果一起展示。
            rows = _active_collection_snapshot_rows(
                _latest_reputation_snapshot_rows(cursor)
            )
            try:
                infraction_counts = _latest_infraction_counts_by_shop_site(
                    cursor,
                    recent_days=100,
                )
            except Exception as exc:
                print(f"读取声誉关联侵权数量失败，将显示为 0: {exc}")
                infraction_counts = {}
            for row in rows:
                for key in ("更新时间", "提交时间"):
                    if row.get(key) is not None:
                        row[key] = str(row[key])
                counts = infraction_counts.get((
                    str(row.get("店铺名") or "").strip(),
                    str(row.get("站点") or "").strip(),
                )) or {}
                if row.get("侵权数量") in (None, ""):
                    row["侵权数量"] = int(counts.get("侵权数量") or 0)
                else:
                    row["侵权数量"] = int(row.get("侵权数量") or 0)
                if row.get("权利人数量") in (None, ""):
                    row["权利人数量"] = int(counts.get("权利人数量") or 0)
                else:
                    row["权利人数量"] = int(row.get("权利人数量") or 0)
                row["侵权统计天数"] = 100
            shop_traffic_totals = {}
            for row in rows:
                shop_name = str(row.get("店铺名") or "")
                shop_traffic_totals[shop_name] = (
                    shop_traffic_totals.get(shop_name, 0)
                    + _parse_traffic_total(row.get("一周流量趋势"))
                )
            rows.sort(
                key=lambda row: (
                    -shop_traffic_totals.get(str(row.get("店铺名") or ""), 0),
                    str(row.get("店铺名") or ""),
                    -_parse_traffic_total(row.get("一周流量趋势")),
                    str(row.get("站点") or ""),
                )
            )
            task_status_map = _get_latest_collection_task_status(
                cursor,
                "获取声誉信息",
            )
            summary = [
                {
                    "店铺名": configured_site["店铺名"],
                    "站点": configured_site["站点"],
                    "状态": task_status.get("状态") or "未知",
                    "状态时间": task_status.get("状态时间") or "",
                }
                for configured_site in authorized_sites
                for task_status in [task_status_map.get((
                    configured_site["店铺名"], configured_site["站点"]
                )) or {}]
            ]
            return {
                "latest_submit_time": str(latest_submit_time),
                "total": len(rows),
                "summary": summary,
                "rows": rows,
            }
    except Exception as e:
        print(f"查询最新声誉数据失败: {e}")
        raise
    finally:
        connection.close()


ORDER_COLUMN_NAMES = (
    "id", "编号", "时间", "业务员", "来源", "状态", "金额", "费用", "退款",
    "人民币收入", "采购成本", "采购单号", "采购追踪", "利润", "产品id",
    "产品分类", "标题", "图片", "数量", "订单运费", "订单备注", "地区", "买家姓名",
)


def _orders_has_unique_id_index(cursor):
    cursor.execute("SHOW INDEX FROM `orders` WHERE `Non_unique` = 0 AND `Column_name` = 'id'")
    return cursor.fetchone() is not None


def _ensure_orders_unique_id(cursor):
    """清理历史重复/空订单，并为订单 id 建立数据库唯一约束。"""

    if _orders_has_unique_id_index(cursor):
        return
    cursor.execute("SELECT GET_LOCK('orders_unique_id_migration', 30) AS `acquired`")
    acquired = int((cursor.fetchone() or {}).get("acquired") or 0)
    if acquired != 1:
        raise RuntimeError("等待订单唯一索引迁移锁超时")
    try:
        if _orders_has_unique_id_index(cursor):
            return
        columns = ", ".join(f"`{name}`" for name in ORDER_COLUMN_NAMES)
        cursor.execute("DROP TEMPORARY TABLE IF EXISTS `_orders_latest_unique`")
        cursor.execute(
            f"""
            CREATE TEMPORARY TABLE `_orders_latest_unique` AS
            SELECT {columns}
            FROM (
                SELECT {columns},
                       ROW_NUMBER() OVER (
                           PARTITION BY `id`
                           ORDER BY (`时间` IS NULL), `时间` DESC, `编号` DESC
                       ) AS `_order_rank`
                FROM `orders`
                WHERE `id` IS NOT NULL AND TRIM(`id`) <> ''
            ) AS `ranked_orders`
            WHERE `_order_rank` = 1
            """
        )
        cursor.execute("DELETE FROM `orders`")
        cursor.execute(
            f"INSERT INTO `orders` ({columns}) SELECT {columns} FROM `_orders_latest_unique`"
        )
        cursor.execute("DROP TEMPORARY TABLE `_orders_latest_unique`")
        cursor.execute("ALTER TABLE `orders` ADD UNIQUE KEY `uniq_orders_id` (`id`)")
    finally:
        cursor.execute("SELECT RELEASE_LOCK('orders_unique_id_migration')")


def _deduplicate_order_rows(rows):
    latest = {}
    for raw_row in rows or ():
        row = list(raw_row or ())
        if len(row) != len(ORDER_COLUMN_NAMES):
            raise ValueError(
                f"订单字段数量应为 {len(ORDER_COLUMN_NAMES)}，实际为 {len(row)}"
            )
        order_id = str(row[0] or "").strip()
        if not order_id:
            continue
        latest[order_id] = row
    return list(latest.values())


def insert_orders(line):
    source_rows = list(line or [])
    rows = _deduplicate_order_rows(source_rows)
    if not rows:
        return 0
    original_count = len(source_rows)

    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_orders_unique_id(cursor)
            # 已存在的订单更新为本次导出值，使导入可以安全地重复执行。
            sql_insert = """
            INSERT INTO orders (
                `id`, `编号`, `时间`, `业务员`, `来源`, `状态`, 
                `金额`, `费用`, `退款`, `人民币收入`, `采购成本`, `采购单号`, 
                `采购追踪`, `利润`, `产品id`, `产品分类`, `标题`, 
                `图片`, `数量`, `订单运费`,`订单备注`, `地区`, `买家姓名`
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                `编号` = VALUES(`编号`),
                `时间` = VALUES(`时间`),
                `业务员` = VALUES(`业务员`),
                `来源` = VALUES(`来源`),
                `状态` = VALUES(`状态`),
                `金额` = VALUES(`金额`),
                `费用` = VALUES(`费用`),
                `退款` = VALUES(`退款`),
                `人民币收入` = VALUES(`人民币收入`),
                `采购成本` = VALUES(`采购成本`),
                `采购单号` = VALUES(`采购单号`),
                `采购追踪` = VALUES(`采购追踪`),
                `利润` = VALUES(`利润`),
                `产品id` = VALUES(`产品id`),
                `产品分类` = VALUES(`产品分类`),
                `标题` = VALUES(`标题`),
                `图片` = VALUES(`图片`),
                `数量` = VALUES(`数量`),
                `订单运费` = VALUES(`订单运费`),
                `订单备注` = VALUES(`订单备注`),
                `地区` = VALUES(`地区`),
                `买家姓名` = VALUES(`买家姓名`)
            """
            cursor.executemany(sql_insert, rows)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()
        duplicate_count = original_count - len(rows)
        print(f"订单入库成功：{len(rows)} 条，合并重复订单 {duplicate_count} 条")
        return len(rows)

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
        raise
    finally:
        # 关闭连接
        connection.close()


def _ensure_mercado_synced_orders_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_synced_orders` (
            `order_id` VARCHAR(64) NOT NULL,
            `token_id` BIGINT NOT NULL,
            `shop_name` VARCHAR(100) NOT NULL,
            `seller_id` VARCHAR(64) NOT NULL,
            `site_id` VARCHAR(32) NULL,
            `country` VARCHAR(32) NULL,
            `status` VARCHAR(64) NULL,
            `status_label` VARCHAR(32) NULL,
            `status_detail` TEXT NULL,
            `date_created` DATETIME NULL,
            `date_closed` DATETIME NULL,
            `last_updated` DATETIME NULL,
            `currency_id` VARCHAR(16) NULL,
            `amount_currency_id` VARCHAR(16) NULL,
            `total_amount` DECIMAL(20, 4) NULL,
            `paid_amount` DECIMAL(20, 4) NULL,
            `shipping_id` VARCHAR(64) NULL,
            `buyer_id` VARCHAR(64) NULL,
            `buyer_name` VARCHAR(255) NULL,
            `product_id` VARCHAR(64) NULL,
            `title` TEXT NULL,
            `image_url` TEXT NULL,
            `image_source` VARCHAR(32) NULL,
            `image_checked_at` DATETIME NULL,
            `image_last_error` TEXT NULL,
            `quantity` INT NULL,
            `sale_fee` DECIMAL(20, 4) NULL,
            `sale_fee_source` VARCHAR(32) NULL,
            `freight` DECIMAL(20, 4) NULL,
            `freight_currency_id` VARCHAR(16) NULL,
            `freight_source` VARCHAR(32) NULL,
            `freight_checked_at` DATETIME NULL,
            `quoted_freight_usd` DECIMAL(20, 4) NULL,
            `quoted_freight_weight_g` DECIMAL(20, 4) NULL,
            `quoted_freight_source` VARCHAR(64) NULL,
            `quoted_freight_checked_at` DATETIME NULL,
            `workflow_status` VARCHAR(32) NULL,
            `purchase_order` VARCHAR(255) NULL,
            `purchase_tracking` VARCHAR(255) NULL,
            `logistics_company` VARCHAR(64) NULL,
            `purchase_cost` DECIMAL(20, 4) NULL,
            `purchase_remark` TEXT NULL,
            `tracking_cache_json` LONGTEXT NULL,
            `tracking_checked_at` DATETIME NULL,
            `manual_updated_at` DATETIME NULL,
            `raw_json` LONGTEXT NOT NULL,
            `first_synced_at` DATETIME NOT NULL,
            `synced_at` DATETIME NOT NULL,
            PRIMARY KEY (`order_id`),
            KEY `idx_mercado_synced_orders_token_updated` (`token_id`, `last_updated`),
            KEY `idx_mercado_synced_orders_created` (`date_created`),
            KEY `idx_mercado_synced_orders_status` (`status_label`),
            KEY `idx_mercado_synced_orders_country` (`country`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(cursor, "mercado_synced_orders", "workflow_status", "VARCHAR(32) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "purchase_order", "VARCHAR(255) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "purchase_tracking", "VARCHAR(255) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "logistics_company", "VARCHAR(64) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "purchase_cost", "DECIMAL(20, 4) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "purchase_remark", "TEXT NULL")
    _ensure_column(cursor, "mercado_synced_orders", "tracking_cache_json", "LONGTEXT NULL")
    _ensure_column(cursor, "mercado_synced_orders", "tracking_checked_at", "DATETIME NULL")
    _ensure_column(cursor, "mercado_synced_orders", "manual_updated_at", "DATETIME NULL")
    _ensure_column(cursor, "mercado_synced_orders", "amount_currency_id", "VARCHAR(16) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "sale_fee_source", "VARCHAR(32) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "freight_currency_id", "VARCHAR(16) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "freight_source", "VARCHAR(32) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "freight_checked_at", "DATETIME NULL")
    _ensure_column(cursor, "mercado_synced_orders", "quoted_freight_usd", "DECIMAL(20, 4) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "quoted_freight_weight_g", "DECIMAL(20, 4) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "quoted_freight_source", "VARCHAR(64) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "quoted_freight_checked_at", "DATETIME NULL")
    _ensure_column(cursor, "mercado_synced_orders", "image_source", "VARCHAR(32) NULL")
    _ensure_column(cursor, "mercado_synced_orders", "image_checked_at", "DATETIME NULL")
    _ensure_column(cursor, "mercado_synced_orders", "image_last_error", "TEXT NULL")
    _ensure_mercado_shipment_costs_table(cursor)
    _ensure_mercado_order_logs_table(cursor)


def _ensure_mercado_shipment_costs_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_shipment_costs` (
            `shipping_id` VARCHAR(64) NOT NULL,
            `token_id` BIGINT NOT NULL,
            `seller_cost` DECIMAL(20, 4) NULL,
            `currency_id` VARCHAR(16) NULL,
            `payload_json` LONGTEXT NULL,
            `last_error` TEXT NULL,
            `checked_at` DATETIME NOT NULL,
            PRIMARY KEY (`shipping_id`),
            KEY `idx_mercado_shipment_costs_token_checked` (`token_id`, `checked_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _ensure_mercado_order_logs_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_order_operation_logs` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `order_id` VARCHAR(64) NOT NULL,
            `action_type` VARCHAR(32) NOT NULL,
            `action_label` VARCHAR(64) NOT NULL,
            `operator_id` BIGINT NULL,
            `operator_name` VARCHAR(100) NULL,
            `changes_json` LONGTEXT NULL,
            `before_json` LONGTEXT NULL,
            `after_json` LONGTEXT NULL,
            `created_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            KEY `idx_mercado_order_logs_order_time` (`order_id`, `created_at`),
            KEY `idx_mercado_order_logs_action` (`action_type`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


_MERCADO_ORDER_STATUS_LABELS = {
    "payment_required": "待付款",
    "payment_in_process": "待付款",
    "partially_paid": "待付款",
    "confirmed": "审核",
    "paid": "找货",
    "ready_to_ship": "待发",
    "shipped": "已发",
    "delivered": "交付",
    "cancelled": "取消",
    "invalid": "问题",
    "partially_refunded": "退货",
    "refunded": "退货",
}
_MERCADO_SITE_COUNTRIES = {
    "MLM": "墨西哥", "MLB": "巴西", "MLC": "智利",
    "MCO": "哥伦比亚", "MLA": "阿根廷", "MLU": "乌拉圭",
}
_MERCADO_SITE_CURRENCIES = {
    "MLM": "MXN", "MLB": "BRL", "MLC": "CLP",
    "MCO": "COP", "MLA": "ARS", "MLU": "UYU",
}


def _mercado_order_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return str(value)[:19]
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def _mercado_https_url(value):
    text = str(value or "").strip()
    if text.startswith("//"):
        return f"https:{text}"
    if text.lower().startswith("http://"):
        return f"https://{text[7:]}"
    if text.lower().startswith("https://"):
        return text
    return ""


def _mercado_public_item_url(item_id, site_id=""):
    item_id = str(item_id or "").strip().upper()
    match = re.fullmatch(r"([A-Z]{3})-?(\d+)", item_id)
    if not match:
        return ""
    item_site_id, digits = match.groups()
    site_id = str(site_id or item_site_id).strip().upper()
    base_url = _MERCADO_PUBLIC_ITEM_BASE_URLS.get(site_id)
    if not base_url:
        return ""
    return f"{base_url}/{site_id}-{digits}-_JM"


def _mercado_order_sku_items(raw_order, fallback_image="", product_assets=None):
    if isinstance(raw_order, str):
        try:
            raw_order = json.loads(raw_order or "{}")
        except (TypeError, ValueError):
            raw_order = {}
    raw_order = raw_order if isinstance(raw_order, dict) else {}
    product_assets = product_assets if isinstance(product_assets, dict) else {}
    result = []
    for order_item in raw_order.get("order_items") or []:
        product = order_item.get("item") if isinstance(order_item.get("item"), dict) else {}
        product_id = str(product.get("id") or "")
        asset = product_assets.get(product_id)
        asset = asset if isinstance(asset, dict) else {}
        attributes = []
        for attribute in product.get("variation_attributes") or []:
            value = str(attribute.get("value_name") or "").strip()
            name = str(attribute.get("name") or attribute.get("id") or "").strip()
            if value:
                attributes.append(f"{name}: {value}" if name else value)
        result.append(
            {
                "product_id": product_id,
                "seller_sku": str(
                    product.get("seller_sku")
                    or product.get("seller_custom_field")
                    or ""
                ),
                "title": str(product.get("title") or ""),
                "variation_id": str(product.get("variation_id") or ""),
                "variation": " · ".join(attributes),
                "quantity": int(order_item.get("quantity") or 0),
                "image_url": _mercado_https_url(
                    product.get("sku_image_url")
                    or product.get("secure_thumbnail")
                    or product.get("thumbnail")
                    or asset.get("thumbnail_url")
                    or fallback_image
                    or ""
                ),
                "product_url": (
                    _mercado_https_url(
                        product.get("permalink") or asset.get("permalink")
                    )
                    or _mercado_public_item_url(
                        product_id,
                        asset.get("site_id") or str(product_id)[:3],
                    )
                ),
            }
        )
    return result


def upsert_mercado_synced_orders(token_record, orders):
    token_record = dict(token_record or {})
    orders = [dict(order or {}) for order in orders or [] if (order or {}).get("id") is not None]
    if not orders:
        return {"total": 0, "inserted": 0, "updated": 0}
    token_id = int(token_record.get("id") or 0)
    shop_name = str(token_record.get("display_name") or token_record.get("nickname") or token_id)
    seller_id = str(token_record.get("meli_user_id") or "")
    default_site_id = str(token_record.get("site_id") or "")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            order_ids = [str(order["id"]) for order in orders]
            placeholders = ",".join(["%s"] * len(order_ids))
            cursor.execute(
                f"SELECT `order_id` FROM `mercado_synced_orders` WHERE `order_id` IN ({placeholders})",
                order_ids,
            )
            existing = {str(row["order_id"]) for row in cursor.fetchall()}
            rows = []
            for order in orders:
                items = list(order.get("order_items") or [])
                first_item = items[0] if items else {}
                product = dict(first_item.get("item") or {})
                titles = [str((item.get("item") or {}).get("title") or "").strip() for item in items]
                titles = [title for title in titles if title]
                title = titles[0] if len(titles) == 1 else (
                    f"{titles[0]} 等 {len(titles)} 件商品" if titles else ""
                )
                quantity = sum(int(item.get("quantity") or 0) for item in items)
                # Mercado Libre returns ``sale_fee`` per unit, not per line.
                sale_fee = sum(
                    Decimal(str(item.get("sale_fee") or 0))
                    * max(0, int(item.get("quantity") or 0))
                    for item in items
                )
                shipping = dict(order.get("shipping") or {})
                buyer = dict(order.get("buyer") or {})
                context = order.get("context") if isinstance(order.get("context"), dict) else {}
                item_site_id = str(product.get("id") or "")[:3]
                site_id = str(
                    order.get("site_id")
                    or context.get("site")
                    or (item_site_id if item_site_id in _MERCADO_SITE_COUNTRIES else "")
                    or default_site_id
                )
                raw_status = str(order.get("status") or "")
                amount_currency_id = _MERCADO_SITE_CURRENCIES.get(
                    site_id,
                    str(order.get("currency_id") or first_item.get("currency_id") or "").upper(),
                )
                raw_freight = (
                    order.get("shipping_cost")
                    if order.get("shipping_cost") is not None
                    else shipping.get("cost") or 0
                )
                try:
                    has_order_freight = Decimal(str(raw_freight or 0)) > 0
                except (InvalidOperation, TypeError, ValueError):
                    raw_freight = 0
                    has_order_freight = False
                order_image_url = str(
                    product.get("sku_image_url")
                    or product.get("secure_thumbnail")
                    or product.get("thumbnail")
                    or ""
                )
                rows.append(
                    (
                        str(order["id"]), token_id, shop_name, seller_id, site_id,
                        _MERCADO_SITE_COUNTRIES.get(site_id, site_id), raw_status,
                        _MERCADO_ORDER_STATUS_LABELS.get(raw_status, raw_status or "未分类"),
                        json.dumps(order.get("status_detail"), ensure_ascii=False),
                        _mercado_order_datetime(order.get("date_created")),
                        _mercado_order_datetime(order.get("date_closed")),
                        _mercado_order_datetime(order.get("last_updated")),
                        str(order.get("currency_id") or first_item.get("currency_id") or ""),
                        order.get("total_amount"), order.get("paid_amount"),
                        str(shipping.get("id") or ""), str(buyer.get("id") or ""),
                        str(buyer.get("nickname") or buyer.get("first_name") or ""),
                        str(product.get("id") or ""), title,
                        order_image_url,
                        quantity, sale_fee, "order_item_quantity",
                        raw_freight, amount_currency_id if has_order_freight else "",
                        "order_shipping_cost" if has_order_freight else "",
                        now if has_order_freight else None,
                        json.dumps(order, ensure_ascii=False, separators=(",", ":")),
                        now, now, amount_currency_id,
                        "marketplace_item" if order_image_url else "",
                        now if order_image_url else None,
                        "",
                    )
                )
            cursor.executemany(
                """
                INSERT INTO `mercado_synced_orders` (
                    `order_id`, `token_id`, `shop_name`, `seller_id`, `site_id`, `country`,
                    `status`, `status_label`, `status_detail`, `date_created`, `date_closed`,
                    `last_updated`, `currency_id`, `total_amount`, `paid_amount`, `shipping_id`,
                    `buyer_id`, `buyer_name`, `product_id`, `title`, `image_url`, `quantity`,
                    `sale_fee`, `sale_fee_source`, `freight`, `freight_currency_id`,
                    `freight_source`, `freight_checked_at`, `raw_json`, `first_synced_at`,
                    `synced_at`, `amount_currency_id`, `image_source`, `image_checked_at`,
                    `image_last_error`
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s
                ) ON DUPLICATE KEY UPDATE
                    `token_id` = VALUES(`token_id`), `shop_name` = VALUES(`shop_name`),
                    `seller_id` = VALUES(`seller_id`), `site_id` = VALUES(`site_id`),
                    `country` = VALUES(`country`), `status` = VALUES(`status`),
                    `status_label` = VALUES(`status_label`), `status_detail` = VALUES(`status_detail`),
                    `date_created` = VALUES(`date_created`), `date_closed` = VALUES(`date_closed`),
                    `last_updated` = VALUES(`last_updated`), `currency_id` = VALUES(`currency_id`),
                    `amount_currency_id` = VALUES(`amount_currency_id`),
                    `total_amount` = VALUES(`total_amount`), `paid_amount` = VALUES(`paid_amount`),
                    `shipping_id` = VALUES(`shipping_id`), `buyer_id` = VALUES(`buyer_id`),
                    `buyer_name` = VALUES(`buyer_name`), `product_id` = VALUES(`product_id`),
                    `title` = VALUES(`title`),
                    `image_url` = COALESCE(NULLIF(VALUES(`image_url`), ''), `image_url`),
                    `image_source` = CASE WHEN COALESCE(VALUES(`image_url`), '') <> ''
                        THEN VALUES(`image_source`) ELSE `image_source` END,
                    `image_checked_at` = CASE WHEN COALESCE(VALUES(`image_url`), '') <> ''
                        THEN VALUES(`image_checked_at`) ELSE `image_checked_at` END,
                    `image_last_error` = CASE WHEN COALESCE(VALUES(`image_url`), '') <> ''
                        THEN '' ELSE `image_last_error` END,
                    `quantity` = VALUES(`quantity`), `sale_fee` = VALUES(`sale_fee`),
                    `sale_fee_source` = VALUES(`sale_fee_source`),
                    `freight` = CASE
                        WHEN `freight_source` = 'shipment_costs' THEN `freight`
                        WHEN COALESCE(VALUES(`freight_source`), '') <> '' THEN VALUES(`freight`)
                        ELSE `freight` END,
                    `freight_currency_id` = CASE
                        WHEN `freight_source` = 'shipment_costs' THEN `freight_currency_id`
                        WHEN COALESCE(VALUES(`freight_source`), '') <> ''
                            THEN VALUES(`freight_currency_id`)
                        ELSE `freight_currency_id` END,
                    `freight_source` = CASE
                        WHEN `freight_source` = 'shipment_costs' THEN `freight_source`
                        WHEN COALESCE(VALUES(`freight_source`), '') <> ''
                            THEN VALUES(`freight_source`)
                        ELSE `freight_source` END,
                    `freight_checked_at` = CASE
                        WHEN `freight_source` = 'shipment_costs' THEN `freight_checked_at`
                        WHEN COALESCE(VALUES(`freight_source`), '') <> ''
                            THEN VALUES(`freight_checked_at`)
                        ELSE `freight_checked_at` END,
                    `raw_json` = VALUES(`raw_json`),
                    `synced_at` = VALUES(`synced_at`)
                """,
                rows,
            )
        connection.commit()
        inserted = sum(1 for order_id in order_ids if order_id not in existing)
        return {"total": len(order_ids), "inserted": inserted, "updated": len(order_ids) - inserted}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_mercado_order_sync_cursor(token_id):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                "SELECT MAX(`last_updated`) AS `last_updated` FROM `mercado_synced_orders` WHERE `token_id` = %s",
                (int(token_id),),
            )
            value = (cursor.fetchone() or {}).get("last_updated")
            if value is None:
                return None
            if isinstance(value, datetime):
                return value.replace(tzinfo=timezone.utc)
            return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    finally:
        connection.close()


def backfill_mercado_order_sale_fees(batch_size=500):
    """Recalculate historical per-unit sale fees with the purchased quantity."""
    batch_size = max(1, min(2000, int(batch_size or 500)))
    connection = pymysql.connect(**config)
    checked = updated = 0
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            while True:
                cursor.execute(
                    """
                    SELECT `order_id`, `raw_json`
                    FROM `mercado_synced_orders`
                    WHERE COALESCE(`sale_fee_source`, '') <> 'order_item_quantity'
                    ORDER BY `order_id`
                    LIMIT %s
                    """,
                    (batch_size,),
                )
                rows = cursor.fetchall() or []
                if not rows:
                    break
                values = []
                for row in rows:
                    raw_order = row.get("raw_json") or {}
                    if isinstance(raw_order, str):
                        try:
                            raw_order = json.loads(raw_order or "{}")
                        except (TypeError, ValueError):
                            raw_order = {}
                    fee = Decimal("0")
                    for item in (
                        raw_order.get("order_items") or []
                        if isinstance(raw_order, dict) else []
                    ):
                        try:
                            fee += Decimal(str((item or {}).get("sale_fee") or 0)) * max(
                                0, int((item or {}).get("quantity") or 0)
                            )
                        except (InvalidOperation, TypeError, ValueError):
                            continue
                    values.append((fee, str(row.get("order_id") or "")))
                cursor.executemany(
                    """
                    UPDATE `mercado_synced_orders`
                    SET `sale_fee` = %s,
                        `sale_fee_source` = 'order_item_quantity'
                    WHERE `order_id` = %s
                    """,
                    values,
                )
                connection.commit()
                checked += len(rows)
                updated += len(values)
        return {"checked": checked, "updated": updated}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_shipment_cost_cache(shipping_ids):
    shipping_ids = list(dict.fromkeys(
        str(value or "").strip() for value in shipping_ids or () if str(value or "").strip()
    ))
    if not shipping_ids:
        return {}
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_shipment_costs_table(cursor)
            placeholders = ",".join(["%s"] * len(shipping_ids))
            cursor.execute(
                f"SELECT * FROM `mercado_shipment_costs` "
                f"WHERE `shipping_id` IN ({placeholders})",
                shipping_ids,
            )
            return {str(row["shipping_id"]): row for row in cursor.fetchall()}
    finally:
        connection.close()


def save_mercado_shipment_costs(token_id, entries):
    """Cache official shipment costs and allocate each shipment across its orders."""
    token_id = int(token_id or 0)
    normalized = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for entry in entries or ():
        shipping_id = str((entry or {}).get("shipping_id") or "").strip()
        if not shipping_id:
            continue
        raw_cost = (entry or {}).get("seller_cost")
        try:
            seller_cost = Decimal(str(raw_cost)) if raw_cost is not None else None
        except (InvalidOperation, TypeError, ValueError):
            seller_cost = None
        if seller_cost is not None and seller_cost < 0:
            seller_cost = Decimal("0")
        payload = (entry or {}).get("payload")
        normalized.append({
            "shipping_id": shipping_id,
            "seller_cost": seller_cost,
            "currency_id": str((entry or {}).get("currency_id") or "").strip().upper(),
            "payload_json": json.dumps(
                payload if isinstance(payload, (dict, list)) else {},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "last_error": str((entry or {}).get("error") or "").strip()[:2000],
            "checked_at": (
                (entry or {}).get("checked_at").strftime("%Y-%m-%d %H:%M:%S")
                if isinstance((entry or {}).get("checked_at"), datetime)
                else str((entry or {}).get("checked_at") or now)[:19]
            ),
        })
    if not normalized:
        return {"shipments": 0, "orders": 0}

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.executemany(
                """
                INSERT INTO `mercado_shipment_costs` (
                    `shipping_id`, `token_id`, `seller_cost`, `currency_id`,
                    `payload_json`, `last_error`, `checked_at`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    `token_id` = VALUES(`token_id`),
                    `seller_cost` = COALESCE(VALUES(`seller_cost`), `seller_cost`),
                    `currency_id` = COALESCE(NULLIF(VALUES(`currency_id`), ''), `currency_id`),
                    `payload_json` = CASE WHEN VALUES(`seller_cost`) IS NOT NULL
                        THEN VALUES(`payload_json`) ELSE `payload_json` END,
                    `last_error` = VALUES(`last_error`),
                    `checked_at` = VALUES(`checked_at`)
                """,
                [
                    (
                        row["shipping_id"], token_id, row["seller_cost"],
                        row["currency_id"], row["payload_json"], row["last_error"],
                        row["checked_at"],
                    )
                    for row in normalized
                ],
            )
            successful = {
                row["shipping_id"]: row
                for row in normalized
                if row["seller_cost"] is not None and row["currency_id"]
            }
            updates = []
            if successful:
                placeholders = ",".join(["%s"] * len(successful))
                cursor.execute(
                    f"SELECT `order_id`, `shipping_id`, COALESCE(`total_amount`, 0) AS `weight` "
                    f"FROM `mercado_synced_orders` WHERE `token_id` = %s "
                    f"AND `shipping_id` IN ({placeholders}) "
                    f"ORDER BY `shipping_id`, `order_id`",
                    [token_id, *successful.keys()],
                )
                grouped = {}
                for row in cursor.fetchall():
                    grouped.setdefault(str(row["shipping_id"]), []).append(row)
                quantum = Decimal("0.0001")
                for shipping_id, orders in grouped.items():
                    cost_row = successful[shipping_id]
                    seller_cost = cost_row["seller_cost"]
                    weights = [max(Decimal("0"), Decimal(str(row.get("weight") or 0))) for row in orders]
                    total_weight = sum(weights, Decimal("0"))
                    allocated = Decimal("0")
                    for index, order in enumerate(orders):
                        if index == len(orders) - 1:
                            share = seller_cost - allocated
                        elif total_weight > 0:
                            share = (seller_cost * weights[index] / total_weight).quantize(quantum)
                        else:
                            share = (seller_cost / len(orders)).quantize(quantum)
                        share = min(
                            max(Decimal("0"), share),
                            max(Decimal("0"), seller_cost - allocated),
                        )
                        allocated += share
                        updates.append((
                            share, cost_row["currency_id"], cost_row["checked_at"],
                            str(order["order_id"]), token_id,
                        ))
                if updates:
                    cursor.executemany(
                        """
                        UPDATE `mercado_synced_orders`
                        SET `freight` = %s, `freight_currency_id` = %s,
                            `freight_source` = 'shipment_costs', `freight_checked_at` = %s
                        WHERE `order_id` = %s AND `token_id` = %s
                        """,
                        updates,
                    )
        connection.commit()
        return {"shipments": len(normalized), "orders": len(updates)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_pending_shipment_costs(token_id, limit=200):
    limit = max(1, min(1000, int(limit or 200)))
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                SELECT synced.`shipping_id`, MAX(synced.`date_created`) AS `latest_order_at`
                FROM `mercado_synced_orders` AS synced
                LEFT JOIN `mercado_shipment_costs` AS costs
                  ON costs.`shipping_id` = synced.`shipping_id`
                WHERE synced.`token_id` = %s
                  AND COALESCE(synced.`shipping_id`, '') <> ''
                  AND (
                      costs.`shipping_id` IS NULL
                      OR (
                          costs.`seller_cost` IS NULL
                          AND costs.`checked_at` < DATE_SUB(NOW(), INTERVAL 6 HOUR)
                      )
                      OR costs.`checked_at` < CURDATE()
                  )
                GROUP BY synced.`shipping_id`
                ORDER BY `latest_order_at` DESC
                LIMIT %s
                """,
                (int(token_id), limit),
            )
            return [str(row["shipping_id"]) for row in cursor.fetchall()]
    finally:
        connection.close()


def list_mercado_pending_shipment_cost_rows(limit=200):
    limit = max(1, min(1000, int(limit or 200)))
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                SELECT synced.`token_id`, synced.`shipping_id`,
                       MAX(synced.`date_created`) AS `latest_order_at`
                FROM `mercado_synced_orders` AS synced
                LEFT JOIN `mercado_shipment_costs` AS costs
                  ON costs.`shipping_id` = synced.`shipping_id`
                WHERE COALESCE(synced.`shipping_id`, '') <> ''
                  AND (
                      costs.`shipping_id` IS NULL
                      OR (
                          costs.`seller_cost` IS NULL
                          AND costs.`checked_at` < DATE_SUB(NOW(), INTERVAL 6 HOUR)
                      )
                      OR costs.`checked_at` < CURDATE()
                  )
                GROUP BY synced.`token_id`, synced.`shipping_id`
                ORDER BY `latest_order_at` DESC
                LIMIT %s
                """,
                (limit,),
            )
            return cursor.fetchall()
    finally:
        connection.close()


def list_mercado_order_ids_before(token_id, cutoff):
    """Return locally stored order IDs created before the UTC cutoff."""

    if isinstance(cutoff, datetime):
        cutoff_value = cutoff
        if cutoff_value.tzinfo is not None:
            cutoff_value = cutoff_value.astimezone(timezone.utc).replace(tzinfo=None)
        cutoff_value = cutoff_value.strftime("%Y-%m-%d %H:%M:%S")
    else:
        cutoff_value = str(cutoff or "").strip()
    if not cutoff_value:
        raise ValueError("老订单刷新缺少截止时间")

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                "SELECT `order_id` FROM `mercado_synced_orders` "
                "WHERE `token_id` = %s AND `date_created` < %s "
                "ORDER BY `date_created` ASC, `order_id` ASC",
                (int(token_id), cutoff_value),
            )
            return [
                str(row.get("order_id") or "")
                for row in (cursor.fetchall() or [])
                if row.get("order_id") not in (None, "")
            ]
    finally:
        connection.close()


def list_mercado_after_sale_order_contexts(token_id, resource_ids):
    """按订单、Pack 或物流 ID 批量补销售后列表所需的本地订单信息。"""
    identifiers = []
    for value in resource_ids or ():
        text = str(value or "").strip()
        if text and text.isdigit() and text not in identifiers:
            identifiers.append(text)
    if not identifiers:
        return []
    if len(identifiers) > 200:
        raise ValueError("单次最多补全 200 条售后订单")

    placeholders = ",".join(["%s"] * len(identifiers))
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                f"""
                SELECT `order_id`, `shop_name`, `site_id`, `country`, `status`,
                       `status_label`, `date_created`, `last_updated`, `shipping_id`,
                       `buyer_name`, `title`, `image_url`, `raw_json`
                FROM `mercado_synced_orders`
                WHERE `token_id` = %s
                  AND (
                      `order_id` IN ({placeholders})
                      OR `shipping_id` IN ({placeholders})
                      OR JSON_UNQUOTE(JSON_EXTRACT(`raw_json`, '$.pack_id'))
                         IN ({placeholders})
                  )
                ORDER BY `last_updated` DESC, `order_id` DESC
                """,
                [int(token_id), *identifiers, *identifiers, *identifiers],
            )
            rows = []
            for row in cursor.fetchall() or ():
                context = dict(row or {})
                raw = {}
                try:
                    raw = json.loads(context.pop("raw_json", "") or "{}")
                except (TypeError, ValueError):
                    pass
                context["pack_id"] = str(raw.get("pack_id") or context.get("order_id") or "")
                for key in ("date_created", "last_updated"):
                    if isinstance(context.get(key), datetime):
                        context[key] = context[key].strftime("%Y-%m-%d %H:%M:%S")
                rows.append(context)
            return rows
    finally:
        connection.close()


def list_orders(
    country="", status="", salesperson="", group_name="", search="", start_date="", end_date="",
    origin="", freight_variance="", page=1, page_size=200, store_ids=None, salespeople=None,
):
    """分页查询当前已授权店铺的 Token 同步订单。"""
    page = max(1, int(page or 1))
    page_size = max(10, min(200, int(page_size or 200)))
    country, status, salesperson, group_name, search = (
        str(value or "").strip()
        for value in (country, status, salesperson, group_name, search)
    )
    start_date, end_date = (str(value or "").strip() for value in (start_date, end_date))
    start_at, end_exclusive = _filter_datetime_bounds(start_date, end_date)
    origin = str(origin or "").strip().lower()
    if origin not in ("", "token", "zying"):
        origin = ""
    freight_variance = str(freight_variance or "").strip().lower()
    if freight_variance not in (
        "", "different", "actual_higher", "actual_lower", "pending_actual", "pending_quote",
    ):
        raise ValueError("运费差异筛选参数无效")

    normalized_store_ids = []
    for value in store_ids or ():
        try:
            store_id = int(value)
        except (TypeError, ValueError):
            continue
        if store_id > 0 and store_id not in normalized_store_ids:
            normalized_store_ids.append(store_id)
    if len(normalized_store_ids) > 200:
        raise ValueError("单次最多筛选 200 个店铺")

    normalized_salespeople = []
    salesperson_values = salespeople if salespeople is not None else ([salesperson] if salesperson else [])
    if isinstance(salesperson_values, str):
        salesperson_values = [salesperson_values]
    for value in salesperson_values or ():
        name = str(value or "").strip()
        if not name or name in normalized_salespeople:
            continue
        if len(name) > 100:
            raise ValueError("业务员名称不能超过 100 个字符")
        normalized_salespeople.append(name)
    if len(normalized_salespeople) > 100:
        raise ValueError("单次最多筛选 100 个业务员")

    def build_where(
        include_country=True, include_status=True, include_origin=True,
        include_salesperson=True, include_group=True, include_store=True,
        table_alias="",
    ):
        clauses, params = [], []
        prefix = f"{table_alias}." if table_alias else ""

        def column(name):
            return f"{prefix}`{name}`"

        if include_country and country:
            clauses.append(f"{column('country')} = %s")
            params.append(country)
        if include_status and status:
            clauses.append(f"{column('status')} = %s")
            params.append(status)
        if include_origin and origin:
            clauses.append(f"{column('data_origin')} = %s")
            params.append(origin)
        if include_store and normalized_store_ids:
            clauses.append(
                f"{column('store_id')} IN ({','.join(['%s'] * len(normalized_store_ids))})"
            )
            params.extend(normalized_store_ids)
        if include_salesperson and normalized_salespeople:
            named_salespeople = [
                value for value in normalized_salespeople if value != "__unassigned__"
            ]
            salesperson_clauses = []
            if "__unassigned__" in normalized_salespeople:
                salesperson_clauses.append(f"COALESCE({column('salesperson')}, '') = ''")
            if named_salespeople:
                salesperson_clauses.append(
                    f"{column('salesperson')} IN "
                    f"({','.join(['%s'] * len(named_salespeople))})"
                )
                params.extend(named_salespeople)
            clauses.append(f"({' OR '.join(salesperson_clauses)})")
        if include_group and group_name:
            if group_name == "__ungrouped__":
                clauses.append(f"COALESCE({column('group_name')}, '') = ''")
            else:
                clauses.append(f"{column('group_name')} = %s")
                params.append(group_name)
        if freight_variance:
            quoted = column("quoted_freight_usd")
            actual = column("actual_freight_usd")
            if freight_variance == "different":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND ABS({actual} - {quoted}) > 0.01"
                )
            elif freight_variance == "actual_higher":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND {actual} - {quoted} > 0.01"
                )
            elif freight_variance == "actual_lower":
                clauses.append(
                    f"{quoted} IS NOT NULL AND {actual} IS NOT NULL "
                    f"AND {quoted} - {actual} > 0.01"
                )
            elif freight_variance == "pending_actual":
                clauses.append(f"{quoted} IS NOT NULL AND {actual} IS NULL")
            elif freight_variance == "pending_quote":
                clauses.append(f"{quoted} IS NULL AND {actual} IS NOT NULL")
        if start_at:
            clauses.append(f"{column('ordered_at')} >= %s")
            params.append(start_at.strftime("%Y-%m-%d %H:%M:%S"))
        if end_exclusive:
            clauses.append(f"{column('ordered_at')} < %s")
            params.append(end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))
        if search:
            pattern = f"%{search}%"
            clauses.append(
                f"(CAST({column('id')} AS CHAR) LIKE %s OR "
                f"{column('order_number')} LIKE %s OR {column('purchase_order')} LIKE %s OR "
                f"{column('purchase_tracking')} LIKE %s OR {column('product_id')} LIKE %s OR "
                f"{column('title')} LIKE %s OR {column('buyer')} LIKE %s OR "
                f"{column('purchase_remark')} LIKE %s OR {column('remark')} LIKE %s OR "
                f"{column('shop_name')} LIKE %s)"
            )
            params.extend([pattern] * 10)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def json_value(value):
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            from erp.mercadolibre_profitability_cache import (
                DAILY_EXCHANGE_RATE_TABLE,
                EXCHANGE_RATE_TABLE,
                ensure_profitability_cache_tables,
            )
            from erp.mercadolibre_store_link_store import (
                STORE_LINK_TABLE,
                ensure_store_link_table,
            )

            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            ensure_profitability_cache_tables(cursor)
            ensure_store_link_table(cursor)
            connection.commit()
            order_date_sql = "DATE(DATE_ADD(synced.`date_created`, INTERVAL 8 HOUR))"
            currency_sql = (
                "UPPER(COALESCE(NULLIF(synced.`amount_currency_id`, ''), "
                "CASE UPPER(COALESCE(synced.`site_id`, '')) "
                "WHEN 'MLM' THEN 'MXN' WHEN 'MLB' THEN 'BRL' WHEN 'MLA' THEN 'ARS' "
                "WHEN 'MLC' THEN 'CLP' WHEN 'MCO' THEN 'COP' WHEN 'MLU' THEN 'UYU' "
                "ELSE NULLIF(synced.`currency_id`, '') END, 'USD'))"
            )
            # Resolve historical rates per distinct currency/day instead of once per
            # order.  A full order set contains tens of thousands of rows but only a
            # small number of currency/day pairs, so this removes most correlated
            # history lookups without changing the nearest-previous-date rule.
            rate_key_table = "`tmp_mercado_order_rate_keys`"
            cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS {rate_key_table}")
            cursor.execute(
                f"""
                CREATE TEMPORARY TABLE {rate_key_table} AS
                SELECT pairs.`currency_id`, pairs.`order_date`,
                       (
                           SELECT historical_rate.`id`
                           FROM `{DAILY_EXCHANGE_RATE_TABLE}` AS historical_rate
                           WHERE historical_rate.`from_currency_id` = pairs.`currency_id`
                             AND historical_rate.`to_currency_id` = 'USD'
                             AND historical_rate.`rate_date` <= pairs.`order_date`
                           ORDER BY historical_rate.`rate_date` DESC
                           LIMIT 1
                       ) AS `daily_rate_id`,
                       (
                           SELECT historical_cny_rate.`id`
                           FROM `{DAILY_EXCHANGE_RATE_TABLE}` AS historical_cny_rate
                           WHERE historical_cny_rate.`from_currency_id` = 'USD'
                             AND historical_cny_rate.`to_currency_id` = 'CNY'
                             AND historical_cny_rate.`rate_date` <= pairs.`order_date`
                           ORDER BY historical_cny_rate.`rate_date` DESC
                           LIMIT 1
                       ) AS `cny_daily_rate_id`
                FROM (
                    SELECT DISTINCT {currency_sql} AS `currency_id`,
                                    {order_date_sql} AS `order_date`
                    FROM `mercado_synced_orders` AS synced
                    INNER JOIN `mercado_store_tokens` AS stores
                      ON stores.`id` = synced.`token_id`
                ) AS pairs
                WHERE pairs.`order_date` IS NOT NULL
                """
            )
            cursor.execute(
                f"ALTER TABLE {rate_key_table} "
                f"ADD PRIMARY KEY (`currency_id`, `order_date`)"
            )
            order_rate_sql = (
                f"CASE WHEN {currency_sql} <> 'USD' "
                "AND UPPER(COALESCE(synced.`currency_id`, '')) = 'USD' "
                "AND COALESCE(synced.`total_amount`, 0) > 0 "
                "AND COALESCE(synced.`paid_amount`, 0) > 0 "
                "THEN synced.`paid_amount` / synced.`total_amount` ELSE NULL END"
            )
            rate_sql = (
                f"CASE WHEN {currency_sql} = 'USD' THEN 1 "
                f"WHEN ({order_rate_sql}) IS NOT NULL THEN ({order_rate_sql}) "
                "WHEN daily_rate.`rate` IS NOT NULL THEN daily_rate.`rate` "
                "ELSE current_rate.`rate` END"
            )
            cny_rate_sql = "COALESCE(cny_daily_rate.`rate`, cny_current_rate.`rate`)"
            usd_amount_sql = f"synced.`total_amount` * ({rate_sql})"
            fee_usd_sql = "COALESCE(synced.`sale_fee`, 0)"
            freight_local_sql = (
                "CASE WHEN COALESCE(synced.`freight_source`, '') <> '' "
                "OR COALESCE(synced.`freight`, 0) > 0 THEN synced.`freight` "
                "WHEN CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(synced.`raw_json`, "
                "'$.shipping_cost')), 'null') AS DECIMAL(20, 4)) > 0 "
                "THEN CAST(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(synced.`raw_json`, "
                "'$.shipping_cost')), 'null') AS DECIMAL(20, 4)) ELSE NULL END"
            )
            freight_currency_sql = (
                f"CASE WHEN COALESCE(synced.`freight_source`, '') <> '' "
                f"OR COALESCE(synced.`freight`, 0) > 0 "
                f"THEN UPPER(COALESCE(NULLIF(synced.`freight_currency_id`, ''), "
                f"{currency_sql})) WHEN ({freight_local_sql}) IS NOT NULL "
                f"THEN {currency_sql} ELSE NULL END"
            )
            freight_usd_sql = (
                f"CASE WHEN ({freight_local_sql}) IS NULL THEN NULL "
                f"WHEN ({freight_currency_sql}) = 'USD' THEN ({freight_local_sql}) "
                f"ELSE ({freight_local_sql}) * ({rate_sql}) END"
            )
            balance_usd_sql = (
                f"({usd_amount_sql}) - ({fee_usd_sql}) - ({freight_usd_sql})"
            )
            cny_income_sql = f"({usd_amount_sql}) * ({cny_rate_sql})"
            source_sql = f"""
                (
                    SELECT synced.`order_id` AS `id`, synced.`order_id` AS `order_number`,
                           synced.`token_id` AS `store_id`,
                           DATE_ADD(synced.`date_created`, INTERVAL 8 HOUR) AS `ordered_at`,
                           site_settings.`salesperson`,
                           site_settings.`discount_rate`, site_settings.`group_name`,
                           synced.`shop_name`, '美客多 Token' AS `source`, 'token' AS `data_origin`,
                           COALESCE(NULLIF(JSON_UNQUOTE(JSON_EXTRACT(synced.`raw_json`, '$.pack_id')), 'null'), '')
                               AS `pack_id`,
                           COALESCE(NULLIF(synced.`workflow_status`, ''), synced.`status_label`) AS `status`,
                           synced.`status_label` AS `platform_status`, synced.`workflow_status`,
                           ROUND({usd_amount_sql}, 2) AS `amount`,
                           synced.`total_amount` AS `amount_local`,
                           ROUND({fee_usd_sql}, 2) AS `fee`, synced.`sale_fee_source`,
                           0 AS `refund`, ROUND({cny_income_sql}, 2) AS `income`,
                           synced.`paid_amount` AS `paid_amount_usd`,
                           COALESCE(synced.`purchase_cost`, 0) AS `cost`, synced.`purchase_order`,
                           synced.`purchase_tracking`, synced.`logistics_company`,
                           synced.`purchase_remark`, synced.`shipping_id` AS `platform_shipping_id`,
                           ROUND({cny_income_sql} - COALESCE(synced.`purchase_cost`, 0), 2)
                               AS `profit`,
                           synced.`product_id`, '' AS `category`, synced.`title`,
                           synced.`image_url`, synced.`quantity`,
                           ROUND({freight_usd_sql}, 2) AS `freight`,
                           ROUND({balance_usd_sql}, 2) AS `balance`,
                           ({freight_local_sql}) AS `freight_local`,
                           ({freight_currency_sql}) AS `freight_currency_id`,
                           synced.`freight_source`, synced.`freight_checked_at`,
                           synced.`quoted_freight_usd`,
                           synced.`quoted_freight_weight_g`,
                           synced.`quoted_freight_source`,
                           synced.`quoted_freight_checked_at`,
                           ROUND(({freight_usd_sql}) - synced.`quoted_freight_usd`, 2)
                               AS `freight_variance_usd`,
                           ROUND({freight_usd_sql}, 2) AS `actual_freight_usd`,
                           CASE WHEN ({freight_local_sql}) IS NULL THEN 1 ELSE 0 END
                               AS `freight_missing`,
                           synced.`status_detail` AS `remark`,
                           synced.`country`,
                           synced.`buyer_name` AS `buyer`, {currency_sql} AS `currency_id`,
                           UPPER(COALESCE(NULLIF(synced.`currency_id`, ''), {currency_sql}))
                               AS `platform_currency_id`,
                           ({rate_sql}) AS `exchange_rate_to_usd`,
                           CASE
                               WHEN {currency_sql} = 'USD' THEN DATE_FORMAT({order_date_sql}, '%%Y-%%m-%%d')
                               WHEN ({order_rate_sql}) IS NOT NULL
                                   THEN DATE_FORMAT({order_date_sql}, '%%Y-%%m-%%d')
                               WHEN daily_rate.`rate_date` IS NOT NULL
                                   THEN DATE_FORMAT(daily_rate.`rate_date`, '%%Y-%%m-%%d')
                               WHEN current_rate.`source_created_at` IS NOT NULL
                                   THEN LEFT(current_rate.`source_created_at`, 10)
                               WHEN current_rate.`refreshed_at` IS NOT NULL
                                   THEN DATE_FORMAT(current_rate.`refreshed_at`, '%%Y-%%m-%%d')
                               ELSE NULL
                           END AS `exchange_rate_date`,
                           CASE
                               WHEN {currency_sql} = 'USD' THEN 'same_currency'
                               WHEN ({order_rate_sql}) IS NOT NULL THEN 'order_implied'
                               WHEN daily_rate.`rate_date` = {order_date_sql} THEN 'order_date'
                               WHEN daily_rate.`rate_date` IS NOT NULL THEN 'nearest_previous'
                               WHEN current_rate.`rate` IS NOT NULL THEN 'current_fallback'
                               ELSE 'missing'
                           END AS `exchange_rate_match`,
                           CASE
                               WHEN {currency_sql} = 'USD' OR ({order_rate_sql}) IS NOT NULL
                                    OR daily_rate.`rate` IS NOT NULL
                                    OR current_rate.`rate` IS NOT NULL THEN 0
                               ELSE 1
                           END AS `exchange_rate_missing`,
                           ({cny_rate_sql}) AS `exchange_rate_usd_to_cny`,
                           CASE
                               WHEN cny_daily_rate.`rate_date` IS NOT NULL
                                   THEN DATE_FORMAT(cny_daily_rate.`rate_date`, '%%Y-%%m-%%d')
                               WHEN cny_current_rate.`source_created_at` IS NOT NULL
                                   THEN LEFT(cny_current_rate.`source_created_at`, 10)
                               WHEN cny_current_rate.`refreshed_at` IS NOT NULL
                                   THEN DATE_FORMAT(cny_current_rate.`refreshed_at`, '%%Y-%%m-%%d')
                               ELSE NULL
                           END AS `cny_exchange_rate_date`,
                           CASE
                               WHEN cny_daily_rate.`rate_date` = {order_date_sql} THEN 'order_date'
                               WHEN cny_daily_rate.`rate_date` IS NOT NULL THEN 'nearest_previous'
                               WHEN cny_current_rate.`rate` IS NOT NULL THEN 'current_fallback'
                               ELSE 'missing'
                           END AS `cny_exchange_rate_match`,
                           CASE
                               WHEN cny_daily_rate.`rate` IS NOT NULL
                                    OR cny_current_rate.`rate` IS NOT NULL THEN 0
                               ELSE 1
                           END AS `cny_exchange_rate_missing`,
                           DATE_ADD(synced.`last_updated`, INTERVAL 8 HOUR) AS `last_updated`
                    FROM `mercado_synced_orders` AS synced
                    INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id`
                    LEFT JOIN `mercado_store_site_settings` AS site_settings
                      ON site_settings.`token_id` = synced.`token_id`
                     AND site_settings.`site_id` = synced.`site_id`
                    LEFT JOIN {rate_key_table} AS rate_keys
                      ON rate_keys.`currency_id` = {currency_sql}
                     AND rate_keys.`order_date` = {order_date_sql}
                    LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS daily_rate
                      ON daily_rate.`id` = rate_keys.`daily_rate_id`
                    LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS current_rate
                      ON current_rate.`from_currency_id` = {currency_sql}
                     AND current_rate.`to_currency_id` = 'USD'
                    LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS cny_daily_rate
                      ON cny_daily_rate.`id` = rate_keys.`cny_daily_rate_id`
                    LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS cny_current_rate
                      ON cny_current_rate.`from_currency_id` = 'USD'
                     AND cny_current_rate.`to_currency_id` = 'CNY'
                ) AS `order_source`
            """
            # Counts and facets only need searchable/filterable fields.  Keep their
            # temporary table deliberately narrow; pricing is calculated once for the
            # summary and only for the 200 rows returned by the current page.
            filter_source_sql = f"""
                (
                    SELECT synced.`order_id` AS `id`, synced.`order_id` AS `order_number`,
                           synced.`token_id` AS `store_id`,
                           DATE_ADD(synced.`date_created`, INTERVAL 8 HOUR) AS `ordered_at`,
                           site_settings.`salesperson`, site_settings.`group_name`,
                           synced.`shop_name`, 'token' AS `data_origin`,
                           COALESCE(NULLIF(synced.`workflow_status`, ''), synced.`status_label`)
                               AS `status`,
                           synced.`purchase_order`, synced.`purchase_tracking`,
                           synced.`product_id`, synced.`title`,
                           synced.`buyer_name` AS `buyer`, synced.`purchase_remark`,
                           synced.`status_detail` AS `remark`, synced.`country`,
                           {currency_sql} AS `currency_id`, {order_date_sql} AS `order_date`,
                           synced.`currency_id` AS `platform_currency_id`,
                           COALESCE(synced.`total_amount`, 0) AS `total_amount`,
                           COALESCE(synced.`paid_amount`, 0) AS `paid_amount`,
                           COALESCE(synced.`sale_fee`, 0) AS `sale_fee`,
                           CASE WHEN COALESCE(synced.`freight_source`, '') <> ''
                                      OR COALESCE(synced.`freight`, 0) > 0
                                THEN synced.`freight` ELSE NULL END AS `freight_local`,
                           UPPER(COALESCE(NULLIF(synced.`freight_currency_id`, ''),
                                          {currency_sql})) AS `freight_currency_id`,
                           CASE WHEN COALESCE(synced.`freight_source`, '') = ''
                                      AND COALESCE(synced.`freight`, 0) = 0
                                THEN 1 ELSE 0 END AS `freight_missing`,
                           synced.`quoted_freight_usd`,
                           CASE
                               WHEN UPPER(COALESCE(synced.`freight_currency_id`, '')) = 'USD'
                                    AND (COALESCE(synced.`freight_source`, '') <> ''
                                         OR COALESCE(synced.`freight`, 0) > 0)
                               THEN synced.`freight` ELSE NULL
                           END AS `actual_freight_usd`,
                           COALESCE(synced.`purchase_cost`, 0) AS `purchase_cost`
                    FROM `mercado_synced_orders` AS synced
                    INNER JOIN `mercado_store_tokens` AS stores
                      ON stores.`id` = synced.`token_id`
                    LEFT JOIN `mercado_store_site_settings` AS site_settings
                      ON site_settings.`token_id` = synced.`token_id`
                     AND site_settings.`site_id` = synced.`site_id`
                ) AS `order_filter_source`
            """
            base_where_sql, base_params = build_where(
                include_country=False,
                include_status=False,
                include_origin=False,
                include_salesperson=False,
                include_group=False,
                include_store=False,
            )
            order_filter_table = "`tmp_mercado_order_filter_source`"
            cursor.execute(f"DROP TEMPORARY TABLE IF EXISTS {order_filter_table}")
            cursor.execute(
                f"CREATE TEMPORARY TABLE {order_filter_table} AS "
                f"SELECT * FROM {filter_source_sql}{base_where_sql}",
                base_params,
            )
            cursor.execute(
                f"ALTER TABLE {order_filter_table} "
                f"ADD PRIMARY KEY (`id`), ADD KEY `idx_ordered_at` (`ordered_at`, `id`)"
            )
            where_sql, params = build_where()
            cursor.execute(
                f"SELECT COUNT(*) AS `total` FROM {order_filter_table}{where_sql}", params
            )
            total = int((cursor.fetchone() or {}).get("total") or 0)
            summary_where_sql, summary_params = build_where(table_alias="scoped")
            summary_order_rate_sql = (
                "CASE WHEN scoped.`currency_id` <> 'USD' "
                "AND UPPER(COALESCE(scoped.`platform_currency_id`, '')) = 'USD' "
                "AND scoped.`total_amount` > 0 AND scoped.`paid_amount` > 0 "
                "THEN scoped.`paid_amount` / scoped.`total_amount` ELSE NULL END"
            )
            summary_rate_sql = (
                "CASE WHEN scoped.`currency_id` = 'USD' THEN 1 "
                f"WHEN ({summary_order_rate_sql}) IS NOT NULL "
                f"THEN ({summary_order_rate_sql}) "
                "WHEN daily_rate.`rate` IS NOT NULL THEN daily_rate.`rate` "
                "ELSE current_rate.`rate` END"
            )
            summary_cny_rate_sql = (
                "COALESCE(cny_daily_rate.`rate`, cny_current_rate.`rate`)"
            )
            summary_amount_sql = (
                f"scoped.`total_amount` * ({summary_rate_sql})"
            )
            summary_freight_rate_sql = (
                "CASE WHEN scoped.`freight_currency_id` = 'USD' THEN 1 "
                f"ELSE ({summary_rate_sql}) END"
            )
            summary_freight_sql = (
                f"scoped.`freight_local` * ({summary_freight_rate_sql})"
            )
            summary_income_sql = (
                f"({summary_amount_sql}) * ({summary_cny_rate_sql})"
            )
            cursor.execute(
                f"SELECT COALESCE(SUM(ROUND({summary_amount_sql}, 2)),0) AS `amount`, "
                f"COALESCE(SUM(ROUND(scoped.`sale_fee`, 2)),0) AS `fee`, "
                f"COALESCE(SUM(ROUND({summary_freight_sql}, 2)),0) AS `freight`, "
                f"COALESCE(SUM(ROUND(({summary_amount_sql}) - scoped.`sale_fee` "
                f"- ({summary_freight_sql}), 2)),0) AS `balance`, "
                f"COALESCE(SUM(ROUND({summary_income_sql}, 2)),0) AS `income`, "
                f"COALESCE(SUM(scoped.`purchase_cost`),0) AS `cost`, "
                f"COALESCE(SUM(ROUND(({summary_income_sql}) - scoped.`purchase_cost`, 2)),0) "
                f"AS `profit`, "
                f"COALESCE(SUM(CASE WHEN scoped.`currency_id` = 'USD' "
                f"OR ({summary_order_rate_sql}) IS NOT NULL OR daily_rate.`rate` IS NOT NULL "
                f"OR current_rate.`rate` IS NOT NULL THEN 0 ELSE 1 END),0) "
                f"AS `exchange_rate_missing_count`, "
                f"COALESCE(SUM(CASE WHEN cny_daily_rate.`rate` IS NOT NULL "
                f"OR cny_current_rate.`rate` IS NOT NULL THEN 0 ELSE 1 END),0) "
                f"AS `cny_exchange_rate_missing_count`, "
                f"COALESCE(SUM(scoped.`freight_missing`),0) AS `freight_missing_count` "
                f"FROM {order_filter_table} AS scoped "
                f"LEFT JOIN {rate_key_table} AS rate_keys "
                f"ON rate_keys.`currency_id` = scoped.`currency_id` "
                f"AND rate_keys.`order_date` = scoped.`order_date` "
                f"LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS daily_rate "
                f"ON daily_rate.`id` = rate_keys.`daily_rate_id` "
                f"LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS current_rate "
                f"ON current_rate.`from_currency_id` = scoped.`currency_id` "
                f"AND current_rate.`to_currency_id` = 'USD' "
                f"LEFT JOIN `{DAILY_EXCHANGE_RATE_TABLE}` AS cny_daily_rate "
                f"ON cny_daily_rate.`id` = rate_keys.`cny_daily_rate_id` "
                f"LEFT JOIN `{EXCHANGE_RATE_TABLE}` AS cny_current_rate "
                f"ON cny_current_rate.`from_currency_id` = 'USD' "
                f"AND cny_current_rate.`to_currency_id` = 'CNY'{summary_where_sql}",
                summary_params,
            )
            summary = {key: json_value(value or 0) for key, value in (cursor.fetchone() or {}).items()}

            status_where, status_params = build_where(include_status=False)
            cursor.execute(
                f"SELECT COALESCE(`status`,'未分类') AS `status`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{status_where} "
                f"GROUP BY COALESCE(`status`,'未分类') ORDER BY `count` DESC",
                status_params,
            )
            status_counts = {str(row["status"]): int(row["count"]) for row in cursor.fetchall()}

            country_where, country_params = build_where(include_country=False, include_status=False)
            cursor.execute(
                f"SELECT COALESCE(`country`,'未分类') AS `country`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{country_where} "
                f"GROUP BY COALESCE(`country`,'未分类') ORDER BY `count` DESC",
                country_params,
            )
            country_counts = {str(row["country"]): int(row["count"]) for row in cursor.fetchall()}

            origin_where, origin_params = build_where(include_origin=False)
            cursor.execute(
                f"SELECT `data_origin`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{origin_where} GROUP BY `data_origin`",
                origin_params,
            )
            origin_counts = {str(row["data_origin"]): int(row["count"]) for row in cursor.fetchall()}

            store_where, store_params = build_where(include_store=False)
            cursor.execute(
                f"SELECT `store_id`, `shop_name`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{store_where} GROUP BY `store_id`, `shop_name` "
                f"ORDER BY `shop_name` ASC, `store_id` ASC",
                store_params,
            )
            store_counts = [
                {
                    "id": int(row["store_id"]),
                    "name": str(row.get("shop_name") or f"店铺 {row['store_id']}"),
                    "count": int(row["count"]),
                }
                for row in cursor.fetchall()
            ]

            salesperson_where, salesperson_params = build_where(include_salesperson=False)
            cursor.execute(
                f"SELECT COALESCE(NULLIF(`salesperson`,''),'未分配') AS `salesperson`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{salesperson_where} "
                f"GROUP BY COALESCE(NULLIF(`salesperson`,''),'未分配') ORDER BY `salesperson` ASC",
                salesperson_params,
            )
            salesperson_counts = {
                str(row["salesperson"]): int(row["count"]) for row in cursor.fetchall()
            }

            group_where, group_params = build_where(include_group=False)
            cursor.execute(
                f"SELECT COALESCE(NULLIF(`group_name`,''),'未分组') AS `group_name`, COUNT(*) AS `count` "
                f"FROM {order_filter_table}{group_where} "
                f"GROUP BY COALESCE(NULLIF(`group_name`,''),'未分组') ORDER BY `group_name` ASC",
                group_params,
            )
            group_counts = {
                str(row["group_name"]): int(row["count"]) for row in cursor.fetchall()
            }

            offset = (page - 1) * page_size
            cursor.execute(
                f"SELECT * FROM {source_sql}{where_sql} "
                f"ORDER BY `ordered_at` DESC, `id` DESC LIMIT %s OFFSET %s",
                [*params, page_size, offset],
            )
            rows = [{key: json_value(value) for key, value in row.items()} for row in cursor.fetchall()]
            order_ids = [str(row.get("id") or "") for row in rows if row.get("id")]
            raw_orders = {}
            if order_ids:
                placeholders = ",".join(["%s"] * len(order_ids))
                cursor.execute(
                    f"SELECT `order_id`, `raw_json` FROM `mercado_synced_orders` "
                    f"WHERE `order_id` IN ({placeholders})",
                    order_ids,
                )
                raw_orders = {
                    str(row.get("order_id") or ""): row.get("raw_json") or "{}"
                    for row in (cursor.fetchall() or [])
                }

            token_ids = set()
            product_ids = set()
            for row in rows:
                order_id = str(row.get("id") or "")
                sku_items = _mercado_order_sku_items(raw_orders.get(order_id, "{}"))
                if row.get("store_id"):
                    token_ids.add(int(row["store_id"]))
                product_ids.update(
                    str(item.get("product_id") or "")
                    for item in sku_items
                    if str(item.get("product_id") or "")
                )
                if row.get("product_id"):
                    product_ids.add(str(row["product_id"]))

            product_assets = {}
            if token_ids and product_ids:
                token_placeholders = ",".join(["%s"] * len(token_ids))
                product_placeholders = ",".join(["%s"] * len(product_ids))
                cursor.execute(
                    f"""
                    SELECT `token_id`, `site_id`, `item_id`, `permalink`, `thumbnail_url`
                    FROM `{STORE_LINK_TABLE}`
                    WHERE `is_current` = 1
                      AND `token_id` IN ({token_placeholders})
                      AND `item_id` IN ({product_placeholders})
                    """,
                    [*sorted(token_ids), *sorted(product_ids)],
                )
                product_assets = {
                    (str(asset.get("token_id") or ""), str(asset.get("item_id") or "")): asset
                    for asset in (cursor.fetchall() or [])
                }

            for row in rows:
                order_id = str(row.get("id") or "")
                store_id = str(row.get("store_id") or "")
                row_assets = {
                    product_id: product_assets[(store_id, product_id)]
                    for product_id in product_ids
                    if (store_id, product_id) in product_assets
                }
                sku_items = _mercado_order_sku_items(
                    raw_orders.get(order_id, "{}"),
                    row.get("image_url") or "",
                    row_assets,
                )
                for item in sku_items:
                    item["order_id"] = order_id
                row["sku_items"] = sku_items
                primary_asset = row_assets.get(str(row.get("product_id") or ""), {})
                row["image_url"] = (
                    _mercado_https_url(row.get("image_url"))
                    or _mercado_https_url(primary_asset.get("thumbnail_url"))
                    or (sku_items[0].get("image_url") if sku_items else "")
                )
                row["product_url"] = (
                    _mercado_https_url(primary_asset.get("permalink"))
                    or (sku_items[0].get("product_url") if sku_items else "")
                    or _mercado_public_item_url(row.get("product_id"), row.get("site_id"))
                )
                row["merged_order_ids"] = [order_id] if order_id else []
            return {
                "rows": rows, "total": total, "page": page, "page_size": page_size,
                "pages": max(1, (total + page_size - 1) // page_size),
                "status_counts": status_counts, "country_counts": country_counts,
                "origin_counts": origin_counts, "store_counts": store_counts,
                "salesperson_counts": salesperson_counts,
                "group_counts": group_counts,
                "summary": summary,
            }
    finally:
        connection.close()


def _positive_order_weight(value):
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number > 0 else None


def _build_mercado_order_weight_quote(order_rows, product_assets, rate_matcher=None):
    """Aggregate listing weights and quote the matching official rate-card row."""

    order_rows = [dict(row or {}) for row in order_rows or ()]
    if not order_rows:
        raise KeyError("没有找到可计算重量的授权店铺订单")
    product_assets = product_assets if isinstance(product_assets, dict) else {}
    order_ids = [str(row.get("order_id") or "") for row in order_rows]
    site_ids = {
        str(row.get("site_id") or "").strip().upper()
        for row in order_rows
        if str(row.get("site_id") or "").strip()
    }
    token_ids = {
        str(row.get("token_id") or "")
        for row in order_rows
        if str(row.get("token_id") or "")
    }
    site_id = next(iter(site_ids)) if len(site_ids) == 1 else ""
    price_local = sum(
        (Decimal(str(row.get("total_amount") or 0)) for row in order_rows),
        Decimal("0"),
    )
    sku_rows = []
    actual_total = Decimal("0")
    volumetric_total = Decimal("0")
    billable_total = Decimal("0")
    total_units = 0
    missing_skus = []

    for order_row in order_rows:
        order_id = str(order_row.get("order_id") or "")
        token_id = str(order_row.get("token_id") or "")
        raw_order = order_row.get("raw_json") or "{}"
        sku_items = _mercado_order_sku_items(raw_order)
        if not sku_items and order_row.get("product_id"):
            sku_items = [{
                "product_id": str(order_row.get("product_id") or ""),
                "seller_sku": "",
                "title": str(order_row.get("title") or ""),
                "quantity": int(order_row.get("quantity") or 1),
            }]
        for item in sku_items:
            product_id = str(item.get("product_id") or "")
            asset = product_assets.get((token_id, product_id), {})
            asset = asset if isinstance(asset, dict) else {}
            quantity = max(1, int(item.get("quantity") or 1))
            actual = _positive_order_weight(asset.get("weight_g"))
            volumetric_kg = _positive_order_weight(asset.get("volumetric_weight_kg"))
            volumetric_g = volumetric_kg * 1000 if volumetric_kg is not None else None
            # Order-management quotes intentionally use the listing's actual
            # weight only.  Volumetric weight remains available for reference,
            # but must not change the weight band selected here.
            billable = actual
            total_units += quantity
            if actual is None:
                missing_label = str(
                    item.get("seller_sku") or product_id or item.get("title") or "未知 SKU"
                )
                if missing_label not in missing_skus:
                    missing_skus.append(missing_label)
            else:
                actual_total += actual * quantity
                volumetric_total += (volumetric_g or Decimal("0")) * quantity
                billable_total += billable * quantity
            sku_rows.append({
                "order_id": order_id,
                "product_id": product_id,
                "seller_sku": str(
                    item.get("seller_sku") or asset.get("seller_sku") or ""
                ),
                "title": str(item.get("title") or asset.get("title") or ""),
                "quantity": quantity,
                "unit_actual_weight_g": float(actual) if actual is not None else None,
                "unit_volumetric_weight_g": (
                    float(volumetric_g) if volumetric_g is not None else None
                ),
                "unit_billable_weight_g": (
                    float(billable) if billable is not None else None
                ),
                "total_actual_weight_g": (
                    float(actual * quantity) if actual is not None else None
                ),
                "total_billable_weight_g": (
                    float(billable * quantity) if billable is not None else None
                ),
                "weight_missing": actual is None,
            })

    weight_complete = bool(sku_rows) and not missing_skus
    matched = None
    if (
        weight_complete
        and site_id
        and len(token_ids) == 1
        and billable_total > 0
        and rate_matcher is not None
    ):
        matched = rate_matcher(
            site_id=site_id,
            price_local=float(price_local),
            billable_weight_g=float(billable_total),
            free_shipping=True,
        )
    matched = dict(matched or {})
    refreshed_at = matched.get("refreshed_at")
    if isinstance(refreshed_at, datetime):
        refreshed_at = refreshed_at.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "order_ids": order_ids,
        "site_id": site_id,
        "price_local": float(price_local),
        "price_currency_id": _MERCADO_SITE_CURRENCIES.get(site_id, ""),
        "total_units": total_units,
        "sku_count": len(sku_rows),
        "actual_weight_g": float(actual_total) if sku_rows else None,
        "volumetric_weight_g": float(volumetric_total) if sku_rows else None,
        "billable_weight_g": float(billable_total) if weight_complete else None,
        "weight_complete": weight_complete,
        "missing_skus": missing_skus,
        "shipping_amount_usd": (
            float(matched["shipping_amount_usd"])
            if matched.get("shipping_amount_usd") is not None
            else None
        ),
        "shipping_currency_id": "USD" if matched else "",
        "rate_kind": str(matched.get("rate_kind") or ""),
        "rate_price_label": str(matched.get("price_label") or ""),
        "rate_weight_label": str(matched.get("weight_label") or ""),
        "rate_source": (
            "official_global_selling_cainiao_rate_card" if matched else ""
        ),
        "rate_source_url": str(matched.get("source_url") or ""),
        "rate_refreshed_at": refreshed_at or "",
        "sku_items": sku_rows,
    }


def _match_mercado_official_rate_rows(
    rate_rows,
    *,
    site_id,
    price_local,
    billable_weight_g,
    free_shipping=True,
):
    """Match a preloaded official rate card without opening one DB connection per order."""

    del free_shipping
    site_id = str(site_id or "").strip().upper()
    site_rows = [
        dict(row or {})
        for row in rate_rows or ()
        if str((row or {}).get("site_id") or "").strip().upper() == site_id
    ]
    if not site_rows:
        return None
    price = Decimal(str(price_local or 0))
    weight = Decimal(str(billable_weight_g or 0))
    above_thresholds = [
        Decimal(str(row["price_min_local"]))
        for row in site_rows
        if row.get("rate_kind") == "above_threshold"
        and row.get("price_min_local") is not None
    ]
    if not above_thresholds:
        return None
    rate_kind = (
        "above_threshold" if price >= min(above_thresholds) else "below_threshold"
    )
    matches = []
    for row in site_rows:
        if str(row.get("rate_kind") or "") != rate_kind:
            continue
        minimum = _positive_order_weight(row.get("weight_min_g"))
        maximum = _positive_order_weight(row.get("weight_max_g"))
        if minimum is not None and weight < minimum:
            continue
        if maximum is not None and weight > maximum:
            continue
        matches.append(row)
    matches.sort(
        key=lambda row: (
            row.get("weight_max_g") is None,
            Decimal(str(row.get("weight_max_g") or "999999999")),
        )
    )
    return matches[0] if matches else None


def get_mercado_order_weight_quote(order_ids):
    """Return total order weight and the official weight-table shipping quote."""

    normalized_ids = []
    for value in order_ids or ():
        order_id = str(value or "").strip()
        if order_id and order_id not in normalized_ids:
            normalized_ids.append(order_id)
    if not normalized_ids:
        raise ValueError("请至少提供一个订单号")
    if len(normalized_ids) > 100:
        raise ValueError("单次最多计算 100 个订单")

    from erp.mercadolibre_store_link_store import (
        STORE_LINK_TABLE,
        ensure_store_link_table,
    )

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            ensure_store_link_table(cursor)
            connection.commit()
            placeholders = ",".join(["%s"] * len(normalized_ids))
            cursor.execute(
                f"""
                SELECT synced.`order_id`, synced.`token_id`, synced.`site_id`,
                       synced.`total_amount`, synced.`product_id`, synced.`title`,
                       synced.`quantity`, synced.`raw_json`
                FROM `mercado_synced_orders` AS synced
                INNER JOIN `mercado_store_tokens` AS stores
                  ON stores.`id` = synced.`token_id`
                WHERE synced.`order_id` IN ({placeholders})
                ORDER BY synced.`date_created` ASC, synced.`order_id` ASC
                """,
                normalized_ids,
            )
            order_rows = [dict(row or {}) for row in (cursor.fetchall() or [])]
            if not order_rows:
                raise KeyError("订单不存在或所属店铺已取消授权")

            product_ids_by_token = {}
            for order_row in order_rows:
                token_id = str(order_row.get("token_id") or "")
                sku_items = _mercado_order_sku_items(order_row.get("raw_json") or "{}")
                product_ids = {
                    str(item.get("product_id") or "")
                    for item in sku_items
                    if str(item.get("product_id") or "")
                }
                if order_row.get("product_id"):
                    product_ids.add(str(order_row["product_id"]))
                product_ids_by_token.setdefault(token_id, set()).update(product_ids)

            asset_clauses = []
            asset_params = []
            for token_id, product_ids in product_ids_by_token.items():
                if not product_ids:
                    continue
                asset_clauses.append(
                    "(`token_id` = %s AND `item_id` IN ("
                    + ",".join(["%s"] * len(product_ids))
                    + "))"
                )
                asset_params.extend([int(token_id), *sorted(product_ids)])
            product_assets = {}
            if asset_clauses:
                cursor.execute(
                    f"""
                    SELECT `token_id`, `item_id`, `site_id`, `title`, `seller_sku`,
                           `weight_g`, `volumetric_weight_kg`
                    FROM `{STORE_LINK_TABLE}`
                    WHERE `is_current` = 1 AND ({' OR '.join(asset_clauses)})
                    """,
                    asset_params,
                )
                product_assets = {
                    (str(row.get("token_id") or ""), str(row.get("item_id") or "")): dict(row)
                    for row in (cursor.fetchall() or [])
                }
    finally:
        connection.close()

    from erp.mercadolibre_shipping_rate_cards import OfficialShippingRateCardStore

    rate_store = OfficialShippingRateCardStore()
    result = _build_mercado_order_weight_quote(
        order_rows,
        product_assets,
        rate_matcher=rate_store.match,
    )
    result["missing_order_ids"] = [
        order_id
        for order_id in normalized_ids
        if order_id not in set(result.get("order_ids") or ())
    ]
    return result


def refresh_mercado_order_quoted_freight(limit=200):
    """Persist weight-table freight for shipments so actual-vs-quoted can be filtered."""

    limit = max(1, min(1000, int(limit or 200)))
    from erp.mercadolibre_store_link_store import (
        STORE_LINK_TABLE,
        ensure_store_link_table,
    )

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            ensure_store_link_table(cursor)
            connection.commit()
            cursor.execute(
                """
                SELECT `token_id`, `shipping_id`, MAX(`date_created`) AS `latest_order_at`
                FROM `mercado_synced_orders`
                WHERE COALESCE(`shipping_id`, '') <> ''
                  AND `freight_source` = 'shipment_costs'
                  AND (
                      `quoted_freight_checked_at` IS NULL
                      OR `quoted_freight_checked_at` < CURDATE()
                  )
                GROUP BY `token_id`, `shipping_id`
                ORDER BY `latest_order_at` DESC
                LIMIT %s
                """,
                (limit,),
            )
            candidates = [dict(row or {}) for row in (cursor.fetchall() or [])]
            if not candidates:
                return {
                    "requested": 0,
                    "quoted_shipments": 0,
                    "missing_shipments": 0,
                    "updated_orders": 0,
                }

            group_clauses = []
            group_params = []
            for candidate in candidates:
                group_clauses.append("(`token_id` = %s AND `shipping_id` = %s)")
                group_params.extend((
                    int(candidate["token_id"]),
                    str(candidate["shipping_id"]),
                ))
            cursor.execute(
                """
                SELECT `order_id`, `token_id`, `shipping_id`, `site_id`,
                       `total_amount`, `product_id`, `title`, `quantity`, `raw_json`
                FROM `mercado_synced_orders`
                WHERE """ + " OR ".join(group_clauses) + " "
                "ORDER BY `token_id`, `shipping_id`, `order_id`",
                group_params,
            )
            order_rows = [dict(row or {}) for row in (cursor.fetchall() or [])]

            product_ids_by_token = {}
            for order_row in order_rows:
                token_id = str(order_row.get("token_id") or "")
                sku_items = _mercado_order_sku_items(order_row.get("raw_json") or "{}")
                product_ids = {
                    str(item.get("product_id") or "")
                    for item in sku_items
                    if str(item.get("product_id") or "")
                }
                if order_row.get("product_id"):
                    product_ids.add(str(order_row["product_id"]))
                product_ids_by_token.setdefault(token_id, set()).update(product_ids)

            asset_clauses = []
            asset_params = []
            for token_id, product_ids in product_ids_by_token.items():
                if not product_ids:
                    continue
                asset_clauses.append(
                    "(`token_id` = %s AND `item_id` IN ("
                    + ",".join(["%s"] * len(product_ids))
                    + "))"
                )
                asset_params.extend([int(token_id), *sorted(product_ids)])
            product_assets = {}
            if asset_clauses:
                cursor.execute(
                    f"""
                    SELECT `token_id`, `item_id`, `site_id`, `title`, `seller_sku`,
                           `weight_g`, `volumetric_weight_kg`
                    FROM `{STORE_LINK_TABLE}`
                    WHERE `is_current` = 1 AND ({' OR '.join(asset_clauses)})
                    """,
                    asset_params,
                )
                product_assets = {
                    (str(row.get("token_id") or ""), str(row.get("item_id") or "")): dict(row)
                    for row in (cursor.fetchall() or [])
                }
    finally:
        connection.close()

    from erp.mercadolibre_shipping_rate_cards import OfficialShippingRateCardStore

    rate_rows = (OfficialShippingRateCardStore().list_rates() or {}).get("rows") or []
    grouped_orders = {}
    for order_row in order_rows:
        key = (
            str(order_row.get("token_id") or ""),
            str(order_row.get("shipping_id") or ""),
        )
        grouped_orders.setdefault(key, []).append(order_row)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updates = []
    quoted_shipments = missing_shipments = 0
    quantum = Decimal("0.0001")
    for group_rows in grouped_orders.values():
        quote = _build_mercado_order_weight_quote(
            group_rows,
            product_assets,
            rate_matcher=lambda **values: _match_mercado_official_rate_rows(
                rate_rows, **values
            ),
        )
        quoted_amount = quote.get("shipping_amount_usd")
        if quoted_amount is None:
            missing_shipments += 1
            source = "weight_missing" if not quote.get("weight_complete") else "rate_missing"
        else:
            quoted_shipments += 1
            source = "official_weight_rate_card"
        weight_by_order = {}
        for item in quote.get("sku_items") or ():
            order_id = str(item.get("order_id") or "")
            raw_weight = item.get("total_actual_weight_g")
            if raw_weight is not None:
                weight_by_order[order_id] = (
                    weight_by_order.get(order_id, Decimal("0"))
                    + Decimal(str(raw_weight))
                )
        allocation_weights = [
            max(Decimal("0"), Decimal(str(row.get("total_amount") or 0)))
            for row in group_rows
        ]
        allocation_total = sum(allocation_weights, Decimal("0"))
        allocated = Decimal("0")
        quoted_decimal = (
            Decimal(str(quoted_amount)) if quoted_amount is not None else None
        )
        for index, order_row in enumerate(group_rows):
            share = None
            if quoted_decimal is not None:
                if index == len(group_rows) - 1:
                    share = quoted_decimal - allocated
                elif allocation_total > 0:
                    share = (
                        quoted_decimal * allocation_weights[index] / allocation_total
                    ).quantize(quantum)
                else:
                    share = (quoted_decimal / len(group_rows)).quantize(quantum)
                allocated += share
            order_id = str(order_row.get("order_id") or "")
            updates.append((
                share,
                weight_by_order.get(order_id),
                source,
                now,
                order_id,
                int(order_row.get("token_id") or 0),
            ))

    save_connection = pymysql.connect(**config)
    try:
        with save_connection.cursor() as cursor:
            if updates:
                cursor.executemany(
                    """
                    UPDATE `mercado_synced_orders`
                    SET `quoted_freight_usd` = %s,
                        `quoted_freight_weight_g` = %s,
                        `quoted_freight_source` = %s,
                        `quoted_freight_checked_at` = %s
                    WHERE `order_id` = %s AND `token_id` = %s
                    """,
                    updates,
                )
        save_connection.commit()
    except Exception:
        save_connection.rollback()
        raise
    finally:
        save_connection.close()
    return {
        "requested": len(candidates),
        "quoted_shipments": quoted_shipments,
        "missing_shipments": missing_shipments,
        "updated_orders": len(updates),
    }


def bulk_update_mercado_orders(
    order_ids,
    workflow_status=None,
    purchase_order=None,
    purchase_tracking=None,
    logistics_company=None,
    purchase_cost=None,
    purchase_remark=None,
    operator_id=None,
    operator_name="",
):
    """批量更新当前授权店铺订单的处理状态和采购信息。"""
    normalized_ids = []
    for value in order_ids or []:
        order_id = str(value or "").strip()
        if order_id and order_id not in normalized_ids:
            normalized_ids.append(order_id)
    if not normalized_ids:
        raise ValueError("请至少选择一个订单")
    if len(normalized_ids) > 500:
        raise ValueError("单次最多操作 500 个订单")

    assignments, values, requested_changes = [], [], {}
    if workflow_status is not None:
        workflow_status = str(workflow_status or "").strip()
        if len(workflow_status) > 32:
            raise ValueError("订单状态不能超过 32 个字符")
        assignments.append("synced.`workflow_status` = %s")
        values.append(workflow_status or None)
        requested_changes["workflow_status"] = workflow_status or None
    if purchase_order is not None:
        purchase_order = str(purchase_order or "").strip()
        if len(purchase_order) > 255:
            raise ValueError("采购单号不能超过 255 个字符")
        assignments.append("synced.`purchase_order` = %s")
        values.append(purchase_order or None)
        requested_changes["purchase_order"] = purchase_order or None
    tracking_changed = False
    if purchase_tracking is not None:
        purchase_tracking = str(purchase_tracking or "").strip()
        if len(purchase_tracking) > 255:
            raise ValueError("物流号不能超过 255 个字符")
        assignments.append("synced.`purchase_tracking` = %s")
        values.append(purchase_tracking or None)
        requested_changes["purchase_tracking"] = purchase_tracking or None
        tracking_changed = True
    if logistics_company is not None:
        logistics_company = str(logistics_company or "").strip().lower()
        if len(logistics_company) > 64:
            raise ValueError("物流公司编码不能超过 64 个字符")
        assignments.append("synced.`logistics_company` = %s")
        values.append(logistics_company or None)
        requested_changes["logistics_company"] = logistics_company or None
        tracking_changed = True
    if purchase_cost is not None:
        cost_text = str(purchase_cost or "").replace(",", "").strip()
        if cost_text:
            try:
                cost_value = Decimal(cost_text)
            except InvalidOperation as exc:
                raise ValueError("采购成本必须是有效数字") from exc
            if cost_value < 0:
                raise ValueError("采购成本不能小于 0")
        else:
            cost_value = None
        assignments.append("synced.`purchase_cost` = %s")
        values.append(cost_value)
        requested_changes["purchase_cost"] = cost_value
    if purchase_remark is not None:
        purchase_remark = str(purchase_remark or "").strip()
        if len(purchase_remark) > 5000:
            raise ValueError("采购备注不能超过 5000 个字符")
        assignments.append("synced.`purchase_remark` = %s")
        values.append(purchase_remark or None)
        requested_changes["purchase_remark"] = purchase_remark or None
    if tracking_changed:
        assignments.extend([
            "synced.`tracking_cache_json` = NULL",
            "synced.`tracking_checked_at` = NULL",
        ])
    if not assignments:
        raise ValueError("没有需要更新的订单内容")
    assignments.append("synced.`manual_updated_at` = %s")
    values.append(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    placeholders = ",".join(["%s"] * len(normalized_ids))
    field_labels = {
        "workflow_status": "订单状态",
        "purchase_order": "采购订单号",
        "purchase_tracking": "物流号",
        "logistics_company": "物流公司",
        "purchase_cost": "采购成本",
        "purchase_remark": "采购备注",
    }

    def audit_value(value):
        if isinstance(value, Decimal):
            return format(value, "f")
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value

    def comparable(value):
        value = audit_value(value)
        return "" if value is None else str(value)

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                f"SELECT synced.`order_id`, synced.`workflow_status`, synced.`purchase_order`, "
                f"synced.`purchase_tracking`, synced.`logistics_company`, synced.`purchase_cost`, "
                f"synced.`purchase_remark` FROM `mercado_synced_orders` AS synced "
                f"INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id` "
                f"WHERE synced.`order_id` IN ({placeholders})",
                normalized_ids,
            )
            before_rows = list(cursor.fetchall() or [])
            matched = len(before_rows)
            cursor.execute(
                f"UPDATE `mercado_synced_orders` AS synced "
                f"INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id` "
                f"SET {', '.join(assignments)} "
                f"WHERE synced.`order_id` IN ({placeholders})",
                [*values, *normalized_ids],
            )
            changed = int(cursor.rowcount)
            log_rows = []
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            procurement_fields = {
                "purchase_order", "purchase_tracking", "logistics_company",
                "purchase_cost", "purchase_remark",
            }
            for before in before_rows:
                changed_fields = {}
                after = {key: audit_value(before.get(key)) for key in field_labels}
                for key, new_value in requested_changes.items():
                    old_value = audit_value(before.get(key))
                    new_value = audit_value(new_value)
                    after[key] = new_value
                    if comparable(old_value) != comparable(new_value):
                        changed_fields[key] = {
                            "label": field_labels[key],
                            "before": old_value,
                            "after": new_value,
                        }
                if not changed_fields:
                    continue
                if procurement_fields.intersection(changed_fields):
                    created = (
                        not comparable(before.get("purchase_order"))
                        and bool(comparable(after.get("purchase_order")))
                    )
                    action_type = "purchase_created" if created else "purchase_updated"
                    action_label = "新增采购单" if created else "修改采购单"
                else:
                    action_type = "status_updated"
                    action_label = "修改订单状态"
                before_payload = {
                    key: audit_value(before.get(key)) for key in changed_fields
                }
                after_payload = {key: after.get(key) for key in changed_fields}
                log_rows.append((
                    str(before.get("order_id") or ""), action_type, action_label,
                    int(operator_id) if str(operator_id or "").isdigit() else None,
                    str(operator_name or "").strip()[:100] or "系统",
                    json.dumps(changed_fields, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(before_payload, ensure_ascii=False, separators=(",", ":")),
                    json.dumps(after_payload, ensure_ascii=False, separators=(",", ":")),
                    now,
                ))
            if log_rows:
                cursor.executemany(
                    """
                    INSERT INTO `mercado_order_operation_logs` (
                        `order_id`, `action_type`, `action_label`, `operator_id`, `operator_name`,
                        `changes_json`, `before_json`, `after_json`, `created_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    log_rows,
                )
        connection.commit()
        return {"matched": matched, "changed": changed}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_mercado_order_label_contexts(order_ids):
    normalized_ids = []
    for value in order_ids or []:
        order_id = str(value or "").strip()
        if order_id and order_id not in normalized_ids:
            normalized_ids.append(order_id)
    if not normalized_ids:
        raise ValueError("请至少选择一个订单")
    if len(normalized_ids) > 100:
        raise ValueError("单次最多打印 100 个订单")
    placeholders = ",".join(["%s"] * len(normalized_ids))
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                f"""
                SELECT synced.`order_id`, synced.`shipping_id`, synced.`token_id`,
                       synced.`shop_name`, synced.`status`, synced.`status_label`,
                       stores.`access_token`, stores.`refresh_token`, stores.`expires_at`
                FROM `mercado_synced_orders` AS synced
                INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id`
                WHERE synced.`order_id` IN ({placeholders})
                ORDER BY FIELD(synced.`order_id`, {placeholders})
                """,
                [*normalized_ids, *normalized_ids],
            )
            return list(cursor.fetchall() or [])
    finally:
        connection.close()


def _ensure_mercado_order_sync_schedule_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_order_sync_schedule` (
            `state_key` VARCHAR(64) NOT NULL,
            `state_value` VARCHAR(255) NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`state_key`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def get_mercado_order_sync_schedule_value(state_key):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_sync_schedule_table(cursor)
            cursor.execute(
                "SELECT `state_value` FROM `mercado_order_sync_schedule` "
                "WHERE `state_key` = %s LIMIT 1",
                (str(state_key or "")[:64],),
            )
            row = cursor.fetchone() or {}
            return str(row.get("state_value") or "")
    finally:
        connection.close()


def get_mercado_order_sync_schedule_state(state_key):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_sync_schedule_table(cursor)
            cursor.execute(
                "SELECT `state_value`, `updated_at` FROM `mercado_order_sync_schedule` "
                "WHERE `state_key` = %s LIMIT 1",
                (str(state_key or "")[:64],),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    finally:
        connection.close()


def set_mercado_order_sync_schedule_value(state_key, state_value):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_sync_schedule_table(cursor)
            cursor.execute(
                """
                INSERT INTO `mercado_order_sync_schedule` (
                    `state_key`, `state_value`, `updated_at`
                ) VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    `state_value` = VALUES(`state_value`),
                    `updated_at` = VALUES(`updated_at`)
                """,
                (str(state_key or "")[:64], str(state_value or "")[:255], now),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_mercado_order_status_cursors_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_order_status_cursors` (
            `token_id` BIGINT NOT NULL,
            `run_date` DATE NULL,
            `completed_for_run` TINYINT(1) NOT NULL DEFAULT 0,
            `completed_through` DATETIME NULL,
            `window_from` DATETIME NULL,
            `window_to` DATETIME NULL,
            `next_offset` INT NOT NULL DEFAULT 0,
            `checked_count` INT NOT NULL DEFAULT 0,
            `updated_count` INT NOT NULL DEFAULT 0,
            `failed_count` INT NOT NULL DEFAULT 0,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`token_id`),
            KEY `idx_order_status_cursors_run` (`run_date`, `completed_for_run`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def begin_mercado_order_status_window(token_id, run_date, default_from, window_to):
    """Start or resume one store's bounded last_updated scan."""
    token_id = int(token_id)
    run_date = str(run_date or "")[:10]
    default_from = default_from or (datetime.utcnow() - timedelta(days=1))
    window_to = window_to or datetime.utcnow()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_status_cursors_table(cursor)
            cursor.execute(
                "SELECT * FROM `mercado_order_status_cursors` "
                "WHERE `token_id` = %s FOR UPDATE",
                (token_id,),
            )
            existing = cursor.fetchone() or {}
            active_window = bool(
                existing
                and not int(existing.get("completed_for_run") or 0)
                and existing.get("window_from")
                and existing.get("window_to")
            )
            completed_today = bool(
                existing
                and str(existing.get("run_date") or "") == run_date
                and int(existing.get("completed_for_run") or 0)
            )
            if active_window or completed_today:
                connection.commit()
                return dict(existing)

            base_from = existing.get("completed_through") or default_from
            if isinstance(base_from, str):
                base_from = datetime.fromisoformat(base_from)
            scan_from = base_from - timedelta(minutes=5)
            cursor.execute(
                """
                INSERT INTO `mercado_order_status_cursors` (
                    `token_id`, `run_date`, `completed_for_run`, `completed_through`,
                    `window_from`, `window_to`, `next_offset`, `checked_count`,
                    `updated_count`, `failed_count`, `updated_at`
                ) VALUES (%s, %s, 0, %s, %s, %s, 0, 0, 0, 0, %s)
                ON DUPLICATE KEY UPDATE
                    `run_date` = VALUES(`run_date`),
                    `completed_for_run` = 0,
                    `window_from` = VALUES(`window_from`),
                    `window_to` = VALUES(`window_to`),
                    `next_offset` = 0,
                    `checked_count` = 0,
                    `updated_count` = 0,
                    `failed_count` = 0,
                    `updated_at` = VALUES(`updated_at`)
                """,
                (
                    token_id, run_date, existing.get("completed_through"),
                    scan_from, window_to, now,
                ),
            )
            cursor.execute(
                "SELECT * FROM `mercado_order_status_cursors` WHERE `token_id` = %s",
                (token_id,),
            )
            result = dict(cursor.fetchone() or {})
        connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def checkpoint_mercado_order_status_window(
    token_id, next_offset, checked_count, updated_count, failed_count,
):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_status_cursors_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_order_status_cursors`
                SET `next_offset` = %s, `checked_count` = %s,
                    `updated_count` = %s, `failed_count` = %s,
                    `updated_at` = %s
                WHERE `token_id` = %s AND `completed_for_run` = 0
                """,
                (
                    max(0, int(next_offset or 0)), max(0, int(checked_count or 0)),
                    max(0, int(updated_count or 0)), max(0, int(failed_count or 0)),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(token_id),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def complete_mercado_order_status_window(token_id):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_status_cursors_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_order_status_cursors`
                SET `completed_for_run` = 1,
                    `completed_through` = `window_to`,
                    `window_from` = NULL, `window_to` = NULL,
                    `next_offset` = 0, `updated_at` = %s
                WHERE `token_id` = %s AND `completed_for_run` = 0
                """,
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(token_id)),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_mercado_order_print_state(token_id):
    """Return API-print tracking state, or ``None`` before the first safe scan."""

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_print_states_table(cursor)
            cursor.execute(
                """
                SELECT `token_id`, `tracking_since`, `last_scan_at`, `created_at`, `updated_at`
                FROM `mercado_order_print_states`
                WHERE `token_id` = %s
                LIMIT 1
                """,
                (int(token_id),),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    finally:
        connection.close()


def save_mercado_order_print_state(token_id, tracking_since, last_scan_at):
    """Mark a store as safely tracked after its Mercado API order scan succeeds."""

    token_id = int(token_id)
    if token_id <= 0:
        raise ValueError("店铺 Token ID 无效")
    tracking_since = _mercado_order_datetime(tracking_since)
    last_scan_at = _mercado_order_datetime(last_scan_at)
    if not tracking_since or not last_scan_at:
        raise ValueError("订单打印追踪时间无效")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_order_print_states_table(cursor)
            cursor.execute(
                "SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s LIMIT 1",
                (token_id,),
            )
            if not cursor.fetchone():
                raise KeyError("店铺授权不存在")
            cursor.execute(
                """
                INSERT INTO `mercado_order_print_states` (
                    `token_id`, `tracking_since`, `last_scan_at`, `created_at`, `updated_at`
                ) VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    `tracking_since` = LEAST(`tracking_since`, VALUES(`tracking_since`)),
                    `last_scan_at` = GREATEST(
                        COALESCE(`last_scan_at`, VALUES(`last_scan_at`)),
                        VALUES(`last_scan_at`)
                    ),
                    `updated_at` = VALUES(`updated_at`)
                """,
                (token_id, tracking_since, last_scan_at, now, now),
            )
        connection.commit()
        return get_mercado_order_print_state(token_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_order_print_candidates(
    token_id,
    *,
    tracking_since,
    end_at=None,
    site_ids=None,
    include_previously_printed=False,
    limit=0,
):
    """List printable API orders while excluding successfully generated labels.

    ``include_previously_printed`` is only used for a store's first run.  At
    that point legacy browser printing cannot be matched to individual order
    IDs, so the caller deliberately falls back to all printable orders inside
    the configured safety window (72 hours by default).
    """

    token_id = int(token_id)
    limit = max(0, min(100000, int(limit or 0)))
    tracking_since = _mercado_order_datetime(tracking_since)
    if not tracking_since:
        raise ValueError("订单打印追踪起始时间无效")
    end_at = _mercado_order_datetime(end_at)
    if end_at and end_at < tracking_since:
        raise ValueError("订单打印结束时间不能早于开始时间")
    normalized_sites = []
    for value in site_ids or ():
        site_id = str(value or "").strip().upper()
        if site_id and site_id not in normalized_sites:
            normalized_sites.append(site_id)
    if len(normalized_sites) > len(MERCADO_CONFIGURABLE_SITES):
        raise ValueError("订单打印站点数量超过支持范围")

    where = [
        "synced.`token_id` = %s",
        "synced.`date_created` >= %s",
        "COALESCE(synced.`shipping_id`, '') <> ''",
        "LOWER(COALESCE(synced.`status`, '')) IN ('paid', 'ready_to_ship')",
    ]
    params = [token_id, tracking_since]
    if end_at:
        where.append("synced.`date_created` <= %s")
        params.append(end_at)
    if normalized_sites:
        where.append(
            f"UPPER(COALESCE(synced.`site_id`, '')) IN "
            f"({','.join(['%s'] * len(normalized_sites))})"
        )
        params.extend(normalized_sites)
    if not include_previously_printed:
        where.append(
            "NOT EXISTS ("
            "SELECT 1 FROM `mercado_order_operation_logs` AS print_logs "
            "WHERE print_logs.`order_id` = synced.`order_id` "
            "AND print_logs.`action_type` IN ('label_printed', 'label_unavailable')"
            ")"
        )

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            limit_sql = " LIMIT %s" if limit else ""
            query_params = list(params)
            if limit:
                query_params.append(limit)
            cursor.execute(
                f"""
                SELECT synced.`order_id`, synced.`shipping_id`, synced.`token_id`,
                       synced.`shop_name`, synced.`site_id`, synced.`country`,
                       synced.`status`, synced.`status_label`, synced.`date_created`,
                       stores.`access_token`, stores.`refresh_token`, stores.`expires_at`
                FROM `mercado_synced_orders` AS synced
                INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id`
                WHERE {' AND '.join(where)}
                ORDER BY synced.`date_created` ASC, synced.`order_id` ASC
                {limit_sql}
                """,
                query_params,
            )
            return list(cursor.fetchall() or [])
    finally:
        connection.close()


def record_mercado_order_print_logs(order_ids, operator_id=None, operator_name=""):
    orders = get_mercado_order_label_contexts(order_ids)
    if not orders:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    actor_id = int(operator_id) if str(operator_id or "").isdigit() else None
    actor_name = str(operator_name or "").strip()[:100] or "系统"
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_order_logs_table(cursor)
            cursor.executemany(
                """
                INSERT INTO `mercado_order_operation_logs` (
                    `order_id`, `action_type`, `action_label`, `operator_id`, `operator_name`,
                    `changes_json`, `before_json`, `after_json`, `created_at`
                ) VALUES (%s, 'label_printed', '打印美客多面单', %s, %s, %s, NULL, NULL, %s)
                """,
                [
                    (
                        str(row.get("order_id") or ""), actor_id, actor_name,
                        json.dumps({"format": "PDF"}, ensure_ascii=False), now,
                    )
                    for row in orders
                ],
            )
        connection.commit()
        return len(orders)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_order_operation_logs(order_id, limit=100):
    limit = max(1, min(200, int(limit or 100)))
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                """
                SELECT logs.`id`, logs.`order_id`, logs.`action_type`, logs.`action_label`,
                       logs.`operator_id`, logs.`operator_name`, logs.`changes_json`,
                       logs.`before_json`, logs.`after_json`, logs.`created_at`
                FROM `mercado_order_operation_logs` AS logs
                INNER JOIN `mercado_synced_orders` AS synced ON synced.`order_id` = logs.`order_id`
                INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id`
                WHERE logs.`order_id` = %s
                ORDER BY logs.`created_at` DESC, logs.`id` DESC
                LIMIT %s
                """,
                (str(order_id), limit),
            )
            rows = list(cursor.fetchall() or [])
            for row in rows:
                for key in ("changes_json", "before_json", "after_json"):
                    raw = row.pop(key, None)
                    target = key.removesuffix("_json")
                    try:
                        row[target] = json.loads(raw) if raw else {}
                    except (TypeError, ValueError):
                        row[target] = {}
                if isinstance(row.get("created_at"), datetime):
                    row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            return rows
    finally:
        connection.close()


def get_mercado_order_procurement(order_id):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                """
                SELECT synced.`order_id`, synced.`token_id`, synced.`shop_name`, synced.`product_id`,
                       synced.`title`, synced.`image_url`, synced.`purchase_order`,
                       synced.`purchase_tracking`, synced.`logistics_company`, synced.`purchase_cost`,
                       synced.`purchase_remark`, synced.`tracking_cache_json`,
                       synced.`tracking_checked_at`
                FROM `mercado_synced_orders` AS synced
                INNER JOIN `mercado_store_tokens` AS stores ON stores.`id` = synced.`token_id`
                WHERE synced.`order_id` = %s LIMIT 1
                """,
                (str(order_id),),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def update_mercado_tracking_cache(order_id, payload):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_synced_orders`
                SET `tracking_cache_json` = %s, `tracking_checked_at` = %s
                WHERE `order_id` = %s
                """,
                (
                    json.dumps(payload or {}, ensure_ascii=False, separators=(",", ":")),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    str(order_id),
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_missing_product_images(token_id, limit=200):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                SELECT `product_id`, MAX(`title`) AS `title`, COUNT(*) AS `order_count`
                FROM `mercado_synced_orders`
                WHERE `token_id` = %s AND COALESCE(`image_url`, '') = ''
                  AND COALESCE(`product_id`, '') <> ''
                GROUP BY `product_id`
                ORDER BY MAX(`date_created`) DESC
                LIMIT %s
                """,
                (int(token_id), max(1, min(1000, int(limit or 200)))),
            )
            return cursor.fetchall() or []
    finally:
        connection.close()


def update_mercado_product_image(token_id, product_id, image_url):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_synced_orders` SET `image_url` = %s
                WHERE `token_id` = %s AND `product_id` = %s
                """,
                (str(image_url or ""), int(token_id), str(product_id)),
            )
            affected = int(cursor.rowcount)
        connection.commit()
        return affected
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_mercado_order_print_states_table(cursor):
    """Persist the point from which per-order print history is trustworthy."""

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_order_print_states` (
            `token_id` BIGINT NOT NULL,
            `tracking_since` DATETIME NOT NULL,
            `last_scan_at` DATETIME NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`token_id`),
            KEY `idx_mercado_order_print_last_scan` (`last_scan_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def get_high_after_sale_alerts(
    sort_by="after_sale_quantity",
    sort_dir="desc",
    search="",
    date_from="",
    date_to="",
    limit=100,
):
    """按产品汇总“取消-发货后”订单数量及其占全部销量的比例。"""

    sort_columns = {
        "after_sale_quantity": "`after_sale_quantity`",
        "after_sale_rate": "`after_sale_rate`",
    }
    sort_column = sort_columns.get(str(sort_by or "").strip())
    if not sort_column:
        raise ValueError("高售后告警仅支持按售后数量或售后比例排序")
    direction = "ASC" if str(sort_dir or "").strip().casefold() == "asc" else "DESC"
    search_text = str(search or "").strip()
    date_from_text = str(date_from or "").strip()
    date_to_text = str(date_to or "").strip()
    start_date, end_exclusive = _filter_datetime_bounds(
        date_from_text,
        date_to_text,
    )
    limit = max(1, min(int(limit or 100), 500))
    order_conditions = ["`id` IS NOT NULL", "TRIM(`id`) <> ''"]
    params = []
    if start_date:
        order_conditions.append("`时间` >= %s")
        params.append(start_date.strftime("%Y-%m-%d %H:%M:%S"))
    if end_exclusive:
        order_conditions.append("`时间` < %s")
        params.append(end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))
    order_where_clause = " AND ".join(order_conditions)
    where_clause = ""
    if search_text:
        keyword = f"%{search_text}%"
        where_clause = "WHERE CAST(`product_id` AS CHAR) LIKE %s OR `title` LIKE %s"
        params.extend((keyword, keyword))
    params.append(limit)

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH `ranked_orders` AS (
                    SELECT `id`, `时间`, `状态`, `产品id`, `产品分类`, `标题`,
                           `图片`, `数量`, `地区`,
                           ROW_NUMBER() OVER (
                               PARTITION BY `id`
                               ORDER BY (`时间` IS NULL), `时间` DESC, `编号` DESC
                           ) AS `_order_rank`
                    FROM `orders`
                    WHERE {order_where_clause}
                ),
                `latest_orders` AS (
                    SELECT *
                    FROM `ranked_orders`
                    WHERE `_order_rank` = 1 AND `产品id` IS NOT NULL
                ),
                `product_summary` AS (
                    SELECT
                        `产品id` AS `product_id`,
                        SUBSTRING_INDEX(
                            GROUP_CONCAT(
                                NULLIF(TRIM(`标题`), '')
                                ORDER BY `时间` DESC SEPARATOR '\n'
                            ),
                            '\n',
                            1
                        ) AS `title`,
                        MAX(`产品分类`) AS `category`,
                        MAX(`图片`) AS `image`,
                        GROUP_CONCAT(DISTINCT `地区` ORDER BY `地区` SEPARATOR '、') AS `sites`,
                        COUNT(*) AS `total_orders`,
                        SUM(GREATEST(COALESCE(`数量`, 1), 1)) AS `total_quantity`,
                        SUM(`状态` = '取消-发货后') AS `after_sale_orders`,
                        SUM(
                            CASE WHEN `状态` = '取消-发货后'
                                 THEN GREATEST(COALESCE(`数量`, 1), 1)
                                 ELSE 0 END
                        ) AS `after_sale_quantity`,
                        MAX(CASE WHEN `状态` = '取消-发货后' THEN `时间` END) AS `latest_after_sale_time`
                    FROM `latest_orders`
                    GROUP BY `产品id`
                    HAVING `after_sale_quantity` > 0
                ),
                `filtered_summary` AS (
                    SELECT *,
                           ROUND(
                               `after_sale_quantity` / NULLIF(`total_quantity`, 0) * 100,
                               2
                           ) AS `after_sale_rate`
                    FROM `product_summary`
                    {where_clause}
                )
                SELECT *,
                       COUNT(*) OVER () AS `_total_alert_products`,
                       SUM(`after_sale_quantity`) OVER () AS `_all_after_sale_quantity`,
                       SUM(`total_quantity`) OVER () AS `_all_total_quantity`
                FROM `filtered_summary`
                ORDER BY {sort_column} {direction},
                         `after_sale_quantity` DESC,
                         `product_id` ASC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = [dict(row) for row in (cursor.fetchall() or [])]
    finally:
        connection.close()

    first = rows[0] if rows else {}
    total_alert_products = int(first.get("_total_alert_products") or 0)
    total_quantity = int(first.get("_all_total_quantity") or 0)
    after_sale_quantity = int(first.get("_all_after_sale_quantity") or 0)
    for row in rows:
        for key in (
            "total_orders",
            "total_quantity",
            "after_sale_orders",
            "after_sale_quantity",
        ):
            row[key] = int(row.get(key) or 0)
        row["product_id"] = str(row.get("product_id") or "")
        row["after_sale_rate"] = float(row.get("after_sale_rate") or 0)
        row["latest_after_sale_time"] = str(row.get("latest_after_sale_time") or "")
        row.pop("_total_alert_products", None)
        row.pop("_all_after_sale_quantity", None)
        row.pop("_all_total_quantity", None)
    return {
        "summary": {
            "alert_products": total_alert_products,
            "after_sale_quantity": after_sale_quantity,
            "total_quantity": total_quantity,
            "after_sale_rate": round(
                after_sale_quantity / total_quantity * 100,
                2,
            ) if total_quantity else 0,
        },
        "rows": rows,
        "sort_by": sort_by,
        "sort_dir": direction.casefold(),
        "search": search_text,
        "date_from": date_from_text,
        "date_to": date_to_text,
    }


def record_mercado_order_label_unavailable(
    order_ids,
    *,
    shipment_status="",
    reason="",
    operator_name="订单打印/API",
):
    """Remember terminal shipment states so they are not retried every round."""

    normalized = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in order_ids or ()
            if str(value or "").strip()
        )
    )
    if not normalized:
        return 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    changes = json.dumps(
        {
            "shipment_status": str(shipment_status or "").strip().lower(),
            "reason": str(reason or "").strip()[:1000],
        },
        ensure_ascii=False,
    )
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            placeholders = ",".join(["%s"] * len(normalized))
            cursor.execute(
                f"SELECT `order_id` FROM `mercado_synced_orders` "
                f"WHERE `order_id` IN ({placeholders})",
                normalized,
            )
            existing = {str(row.get("order_id") or "") for row in cursor.fetchall() or []}
            rows = [
                (
                    order_id,
                    str(operator_name or "系统").strip()[:100] or "系统",
                    changes,
                    now,
                )
                for order_id in normalized
                if order_id in existing
            ]
            if rows:
                cursor.executemany(
                    """
                    INSERT INTO `mercado_order_operation_logs` (
                        `order_id`, `action_type`, `action_label`, `operator_id`,
                        `operator_name`, `changes_json`, `before_json`, `after_json`,
                        `created_at`
                    ) VALUES (%s, 'label_unavailable', '跳过不可打印面单', NULL,
                              %s, %s, NULL, NULL, %s)
                    """,
                    rows,
                )
        connection.commit()
        return len(rows)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_missing_order_images(token_id, limit=200):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                SELECT `order_id`, `product_id`, `raw_json`
                FROM `mercado_synced_orders`
                WHERE `token_id` = %s AND COALESCE(`image_url`, '') = ''
                  AND COALESCE(`product_id`, '') <> ''
                ORDER BY `date_created` DESC, `order_id` DESC
                LIMIT %s
                """,
                (int(token_id), max(1, min(int(limit or 200), 5000))),
            )
            return cursor.fetchall() or []
    finally:
        connection.close()


def update_mercado_order_image(token_id, order_id, image_url):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_synced_orders` SET `image_url` = %s
                WHERE `token_id` = %s AND `order_id` = %s
                """,
                (str(image_url or ""), int(token_id), str(order_id or "")),
            )
            affected = int(cursor.rowcount)
        connection.commit()
        return affected
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_pending_order_image_rows(limit=100):
    """Return orders whose variation image has not been read from Marketplace Items."""
    limit = max(1, min(500, int(limit or 100)))
    pending_sql = """
        COALESCE(`product_id`, '') <> ''
        AND COALESCE(`image_source`, '') <> 'marketplace_item'
        AND (
            `image_checked_at` IS NULL
            OR `image_checked_at` < DATE_SUB(NOW(), INTERVAL 24 HOUR)
        )
    """
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.execute(
                f"""
                SELECT `token_id`, `product_id`, MAX(`date_created`) AS `latest_order_at`
                FROM `mercado_synced_orders`
                WHERE {pending_sql}
                GROUP BY `token_id`, `product_id`
                ORDER BY `latest_order_at` DESC
                LIMIT %s
                """,
                (limit,),
            )
            products = cursor.fetchall() or []
            if not products:
                return []
            product_where = " OR ".join(
                ["(`token_id` = %s AND `product_id` = %s)"] * len(products)
            )
            params = []
            for row in products:
                params.extend((int(row["token_id"]), str(row["product_id"])))
            cursor.execute(
                f"""
                SELECT `order_id`, `token_id`, `product_id`, `raw_json`,
                       `image_checked_at`, `image_last_error`
                FROM `mercado_synced_orders`
                WHERE {pending_sql} AND ({product_where})
                ORDER BY `date_created` DESC, `order_id` DESC
                """,
                params,
            )
            return cursor.fetchall() or []
    finally:
        connection.close()


def save_mercado_order_image_results(entries):
    """Persist original variation images inside both the order row and raw payload."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rows = []
    for entry in entries or ():
        order_id = str((entry or {}).get("order_id") or "").strip()
        if not order_id:
            continue
        image_url = _mercado_https_url((entry or {}).get("image_url"))
        raw_order = (entry or {}).get("raw_order")
        raw_json = (
            json.dumps(raw_order, ensure_ascii=False, separators=(",", ":"))
            if image_url and isinstance(raw_order, dict)
            else None
        )
        rows.append((
            image_url,
            raw_json,
            "marketplace_item" if image_url else "",
            str((entry or {}).get("error") or "").strip()[:2000],
            now,
            order_id,
            int((entry or {}).get("token_id") or 0),
        ))
    if not rows:
        return {"checked": 0, "updated": 0, "failed": 0}
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_synced_orders_table(cursor)
            cursor.executemany(
                """
                UPDATE `mercado_synced_orders`
                SET `image_url` = COALESCE(NULLIF(%s, ''), `image_url`),
                    `raw_json` = COALESCE(%s, `raw_json`),
                    `image_source` = CASE WHEN %s <> ''
                        THEN 'marketplace_item' ELSE `image_source` END,
                    `image_last_error` = %s,
                    `image_checked_at` = %s
                WHERE `order_id` = %s AND `token_id` = %s
                """,
                rows,
            )
        connection.commit()
        updated = sum(1 for row in rows if row[0])
        return {"checked": len(rows), "updated": updated, "failed": len(rows) - updated}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_high_profit_products(
    sort_by="total_profit",
    sort_dir="desc",
    search="",
    date_from="",
    date_to="",
    limit=100,
):
    """按产品汇总利润，并按总利润或利润率排序。"""

    sort_columns = {
        "total_profit": "`total_profit`",
        "profit_rate": "`profit_rate`",
    }
    sort_column = sort_columns.get(str(sort_by or "").strip())
    if not sort_column:
        raise ValueError("高利润产品仅支持按利润或利润率排序")
    direction = "ASC" if str(sort_dir or "").strip().casefold() == "asc" else "DESC"
    search_text = str(search or "").strip()
    date_from_text = str(date_from or "").strip()
    date_to_text = str(date_to or "").strip()
    start_date, end_exclusive = _filter_datetime_bounds(
        date_from_text,
        date_to_text,
    )
    limit = max(1, min(int(limit or 100), 500))
    order_conditions = ["`id` IS NOT NULL", "TRIM(`id`) <> ''"]
    params = []
    if start_date:
        order_conditions.append("`时间` >= %s")
        params.append(start_date.strftime("%Y-%m-%d %H:%M:%S"))
    if end_exclusive:
        order_conditions.append("`时间` < %s")
        params.append(end_exclusive.strftime("%Y-%m-%d %H:%M:%S"))
    order_where_clause = " AND ".join(order_conditions)
    where_clause = ""
    if search_text:
        keyword = f"%{search_text}%"
        where_clause = "WHERE CAST(`product_id` AS CHAR) LIKE %s OR `title` LIKE %s"
        params.extend((keyword, keyword))
    params.append(limit)

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                WITH `ranked_orders` AS (
                    SELECT `id`, `时间`, `产品id`, `产品分类`, `标题`, `图片`,
                           `数量`, `地区`, `人民币收入`, `利润`,
                           ROW_NUMBER() OVER (
                               PARTITION BY `id`
                               ORDER BY (`时间` IS NULL), `时间` DESC, `编号` DESC
                           ) AS `_order_rank`
                    FROM `orders`
                    WHERE {order_where_clause}
                ),
                `latest_orders` AS (
                    SELECT *
                    FROM `ranked_orders`
                    WHERE `_order_rank` = 1 AND `产品id` IS NOT NULL
                ),
                `product_summary` AS (
                    SELECT
                        `产品id` AS `product_id`,
                        SUBSTRING_INDEX(
                            GROUP_CONCAT(
                                NULLIF(TRIM(`标题`), '')
                                ORDER BY `时间` DESC SEPARATOR '\n'
                            ),
                            '\n',
                            1
                        ) AS `title`,
                        MAX(`产品分类`) AS `category`,
                        MAX(`图片`) AS `image`,
                        GROUP_CONCAT(DISTINCT `地区` ORDER BY `地区` SEPARATOR '、') AS `sites`,
                        COUNT(*) AS `order_count`,
                        SUM(GREATEST(COALESCE(`数量`, 1), 1)) AS `total_quantity`,
                        ROUND(SUM(COALESCE(`人民币收入`, 0)), 2) AS `total_income`,
                        ROUND(SUM(COALESCE(`利润`, 0)), 2) AS `total_profit`,
                        MAX(`时间`) AS `latest_order_time`
                    FROM `latest_orders`
                    GROUP BY `产品id`
                    HAVING `total_profit` > 0
                ),
                `filtered_summary` AS (
                    SELECT *,
                           ROUND(
                               `total_profit` / NULLIF(`total_income`, 0) * 100,
                               2
                           ) AS `profit_rate`
                    FROM `product_summary`
                    {where_clause}
                )
                SELECT *,
                       COUNT(*) OVER () AS `_total_products`,
                       SUM(`total_income`) OVER () AS `_all_total_income`,
                       SUM(`total_profit`) OVER () AS `_all_total_profit`
                FROM `filtered_summary`
                ORDER BY {sort_column} {direction},
                         `total_profit` DESC,
                         `product_id` ASC
                LIMIT %s
                """,
                tuple(params),
            )
            rows = [dict(row) for row in (cursor.fetchall() or [])]
    finally:
        connection.close()

    first = rows[0] if rows else {}
    total_products = int(first.get("_total_products") or 0)
    total_income = float(first.get("_all_total_income") or 0)
    total_profit = float(first.get("_all_total_profit") or 0)
    for row in rows:
        row["product_id"] = str(row.get("product_id") or "")
        row["order_count"] = int(row.get("order_count") or 0)
        row["total_quantity"] = int(row.get("total_quantity") or 0)
        row["total_income"] = round(float(row.get("total_income") or 0), 2)
        row["total_profit"] = round(float(row.get("total_profit") or 0), 2)
        row["profit_rate"] = round(float(row.get("profit_rate") or 0), 2)
        row["latest_order_time"] = str(row.get("latest_order_time") or "")
        row.pop("_total_products", None)
        row.pop("_all_total_income", None)
        row.pop("_all_total_profit", None)
    return {
        "summary": {
            "profitable_products": total_products,
            "total_income": round(total_income, 2),
            "total_profit": round(total_profit, 2),
            "profit_rate": round(
                total_profit / total_income * 100,
                2,
            ) if total_income else 0,
        },
        "rows": rows,
        "sort_by": sort_by,
        "sort_dir": direction.casefold(),
        "search": search_text,
        "date_from": date_from_text,
        "date_to": date_to_text,
    }


def inset_delay_info(delay_list):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = """
            INSERT INTO `delay` (
    `店铺`, 
    `站点`, 
    `延误率`, 
    `下单时间`, 
    `销售单号`, 
    `订单标题`, 
    `截止延误时间`, 
    `实际揽收时间`, 
    `更新时间`, 
    `文件路径`
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
);    
    """
            cursor.executemany(sql_insert, delay_list)
            print("执行sql成功", sql_insert)

        # 核心：涉及写操作（增删改）必须提交事务
        connection.commit()

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def _ensure_pago_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `pago` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `店铺名` VARCHAR(128) NULL,
            `站点` VARCHAR(64) NULL,
            `已释放美元` VARCHAR(64) NULL,
            `未释放美元` VARCHAR(64) NULL,
            `状态` VARCHAR(64) NULL,
            `更新时间` DATETIME NULL,
            `页面原始信息` LONGTEXT NULL,
            `提交时间` DATETIME NULL,
            PRIMARY KEY (`id`),
            KEY `idx_pago_submit_time` (`提交时间`),
            KEY `idx_pago_shop_site` (`店铺名`, `站点`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def inset_pago_info(pago_list):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_pago_table(cursor)
            submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            normalized_list = []
            for row in pago_list:
                row = list(row)
                if len(row) < 7:
                    row.extend([""] * (7 - len(row)))
                elif len(row) > 7:
                    row = row[:7]
                row.append(submit_time)
                normalized_list.append(row)

            sql_insert = """
                INSERT INTO `pago` (
                    `店铺名`,
                    `站点`,
                    `已释放美元`,
                    `未释放美元`,
                    `状态`,
                    `更新时间`,
                    `页面原始信息`,
                    `提交时间`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.executemany(sql_insert, normalized_list)
            print(f"执行sql成功 {sql_insert}，准备插入款项记录 {len(normalized_list)} 条，提交时间 {submit_time}")

        connection.commit()

    except Exception as e:
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
        raise
    finally:
        connection.close()


def _parse_currency_decimal(value):
    """把 Pago 页面金额转换为 Decimal，兼容逗号或点作为小数分隔符。"""
    text = str(value or "").strip()
    if not text:
        return Decimal("0")
    negative = text.startswith("-") or ("(" in text and ")" in text)
    number = re.sub(r"[^0-9,.-]", "", text).replace("-", "")
    if not number:
        return Decimal("0")

    if "," in number and "." in number:
        decimal_separator = "," if number.rfind(",") > number.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        number = number.replace(thousands_separator, "")
        if decimal_separator == ",":
            number = number.replace(",", ".")
    elif "," in number:
        tail = number.rsplit(",", 1)[-1]
        number = number.replace(",", ".") if len(tail) in (1, 2) else number.replace(",", "")
    elif number.count(".") > 1:
        parts = number.split(".")
        number = "".join(parts[:-1]) + "." + parts[-1]

    try:
        amount = Decimal(number)
    except InvalidOperation:
        return Decimal("0")
    return -amount if negative else amount


def _format_currency_decimal(value):
    return f"{Decimal(value or 0):,.2f}"


def get_latest_pago_info(salesperson=""):
    """返回店铺授权中已配置站点的最新款项数据，并补充店铺归属人。"""
    owner_filter = str(salesperson or "").strip()
    configured_rows = []
    owners = set()
    token_data = list_mercado_store_tokens() or {}
    for token in token_data.get("rows") or ():
        shop_name = str(token.get("display_name") or token.get("nickname") or "").strip()
        if not shop_name:
            continue
        settings = []
        for raw_setting in token.get("site_settings") or ():
            setting = dict(raw_setting or {})
            if any((
                str(setting.get("salesperson") or "").strip(),
                str(setting.get("group_name") or "").strip(),
                setting.get("discount_rate") not in (None, ""),
                _authorization_flag_enabled(setting.get("appeal_enabled")),
                _authorization_flag_enabled(setting.get("visit_stats_enabled")),
            )):
                settings.append(setting)
        if not settings:
            token_site = str(token.get("site_id") or "").strip().upper()
            if token_site in MERCADO_CONFIGURABLE_SITES:
                settings = [{"site_id": token_site}]
        for setting in settings:
            owner = str(setting.get("salesperson") or "").strip()
            display_owner = owner or "未分配"
            if owner_filter and display_owner != owner_filter:
                continue
            owners.add(display_owner)
            site_id = str(setting.get("site_id") or "").strip().upper()
            site = MERCADO_CONFIGURABLE_SITES.get(site_id, site_id)
            configured_rows.append(
                {
                    "window_id": "",
                    "店铺名": shop_name,
                    "店铺归属人": display_owner,
                    "站点": site,
                    "配置状态": "",
                    "sequence_no": "",
                }
            )

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_pago_table(cursor)
            cursor.execute(
                """
                SELECT
                    p.`店铺名`, p.`站点`, p.`已释放美元`, p.`未释放美元`,
                    p.`状态`, p.`更新时间`, p.`提交时间`
                FROM `pago` p
                INNER JOIN (
                    SELECT `店铺名`, `站点`, MAX(`id`) AS latest_id
                    FROM `pago`
                    GROUP BY `店铺名`, `站点`
                ) latest ON latest.latest_id = p.`id`
                """
            )
            latest_rows = cursor.fetchall() or []
    finally:
        connection.close()

    latest_by_shop_site = {
        (
            str(row.get("店铺名") or "").strip(),
            str(row.get("站点") or "").strip(),
        ): row
        for row in latest_rows
    }
    rows = []
    released_total = Decimal("0")
    pending_total = Decimal("0")
    latest_submit_time = ""
    for configured in configured_rows:
        key = (configured["店铺名"], configured["站点"])
        latest = latest_by_shop_site.get(key) or {}
        released = str(latest.get("已释放美元") or "").strip()
        pending = str(latest.get("未释放美元") or "").strip()
        submit_time = str(latest.get("提交时间") or "")
        update_time = str(latest.get("更新时间") or "")
        released_total += _parse_currency_decimal(released)
        pending_total += _parse_currency_decimal(pending)
        if submit_time > latest_submit_time:
            latest_submit_time = submit_time
        rows.append(
            {
                **configured,
                "已释放美元": released,
                "待释放美元": pending,
                "未释放美元": pending,
                "状态": str(latest.get("状态") or "无数据"),
                "更新时间": update_time,
                "提交时间": submit_time,
            }
        )

    def sequence_sort_value(row):
        value = str(row.get("sequence_no") or "")
        return (int(value) if value.isdigit() else 999999999, row["店铺名"], row["站点"])

    rows.sort(key=sequence_sort_value)
    return {
        "latest_submit_time": latest_submit_time,
        "total": len(rows),
        "shop_total": len({row["店铺名"] for row in rows}),
        "released_total": _format_currency_decimal(released_total),
        "pending_total": _format_currency_decimal(pending_total),
        "owners": sorted(owners),
        "rows": rows,
    }


def _ensure_zying_product_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `zying_product` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `产品编号` VARCHAR(128) NULL,
            `智赢分类编号` VARCHAR(64) NULL,
            `智赢产品分类` VARCHAR(1024) NULL,
            `分类编号` VARCHAR(64) NULL,
            `产品分类` VARCHAR(2048) NULL,
            `主图链接` TEXT NULL,
            `标题` VARCHAR(1024) NULL,
            `售价` VARCHAR(128) NULL,
            `净收益` VARCHAR(128) NULL,
            `包装毛重` VARCHAR(128) NULL,
            `包装尺寸` VARCHAR(255) NULL,
            `审核状态` VARCHAR(128) NULL,
            `疑似侵权` VARCHAR(8) NULL,
            `侵权关键词` VARCHAR(1024) NULL,
            `采集页码` INT NULL,
            `采集时间` DATETIME NOT NULL,
            `页面原始信息` LONGTEXT NULL,
            `提交时间` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            KEY `idx_zying_product_collect_time` (`采集时间`),
            KEY `idx_zying_product_id_time` (`产品编号`, `采集时间`),
            KEY `idx_zying_product_review_status` (`审核状态`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(cursor, "zying_product", "智赢分类编号", "VARCHAR(64) NULL")
    _ensure_column(cursor, "zying_product", "智赢产品分类", "VARCHAR(1024) NULL")
    _ensure_column(cursor, "zying_product", "分类编号", "VARCHAR(64) NULL")
    _ensure_column(cursor, "zying_product", "产品分类", "VARCHAR(2048) NULL")
    _ensure_column(cursor, "zying_product", "上架快照", "LONGTEXT NULL")
    _ensure_column(cursor, "zying_product", "疑似侵权", "VARCHAR(8) NULL")
    _ensure_column(cursor, "zying_product", "侵权关键词", "VARCHAR(1024) NULL")


def insert_zying_product_info(product_list):
    """创建智赢产品表，并将本次采集结果作为快照批量写入。"""
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            submit_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            normalized_list = []
            for record in product_list or []:
                if isinstance(record, dict):
                    normalized_list.append(
                        (
                            record.get("product_id", record.get("产品编号", "")),
                            record.get(
                                "zying_category_id",
                                record.get("智赢分类编号", ""),
                            ),
                            record.get(
                                "zying_category",
                                record.get("智赢产品分类", ""),
                            ),
                            record.get(
                                "product_category_id",
                                record.get("分类编号", ""),
                            ),
                            record.get(
                                "product_category",
                                record.get("产品分类", ""),
                            ),
                            record.get("main_image_url", record.get("主图链接", "")),
                            record.get("title", record.get("标题", "")),
                            record.get("sale_price", record.get("售价", "")),
                            record.get("net_income", record.get("净收益", "")),
                            record.get("package_gross_weight", record.get("包装毛重", "")),
                            record.get("package_dimensions", record.get("包装尺寸", "")),
                            record.get("review_status", record.get("审核状态", "")),
                            record.get("page_number", record.get("采集页码")),
                            record.get("collected_at", record.get("采集时间")) or submit_time,
                            record.get("raw_text", record.get("页面原始信息", "")),
                            json.dumps(
                                record.get("listing_snapshot", record.get("上架快照", {})) or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                                default=str,
                            ),
                            submit_time,
                        )
                    )
                    continue

                row = list(record)
                if len(row) >= 16:
                    normalized_list.append(tuple(row[:16] + [submit_time]))
                    continue
                if len(row) >= 15:
                    normalized_list.append(tuple(row[:15] + [""] + [submit_time]))
                    continue
                if len(row) >= 13:
                    normalized_list.append(
                        tuple([row[0], "", ""] + row[1:13] + [""] + [submit_time])
                    )
                    continue
                if len(row) < 11:
                    row.extend([""] * (11 - len(row)))
                normalized_list.append(
                    tuple([row[0], "", "", "", ""] + row[1:11] + [""] + [submit_time])
                )

            if normalized_list:
                cursor.executemany(
                    """
                    INSERT INTO `zying_product` (
                        `产品编号`, `智赢分类编号`, `智赢产品分类`,
                        `分类编号`, `产品分类`, `主图链接`, `标题`, `售价`, `净收益`,
                        `包装毛重`, `包装尺寸`, `审核状态`, `采集页码`,
                        `采集时间`, `页面原始信息`, `上架快照`, `提交时间`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    normalized_list,
                )
        connection.commit()
        return len(normalized_list)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_existing_zying_product_ids(product_ids):
    """返回已存在于智赢产品表的产品编号。"""
    normalized_ids = []
    seen_ids = set()
    for value in product_ids or ():
        product_id = str(value or "").strip()
        if product_id and product_id not in seen_ids:
            seen_ids.add(product_id)
            normalized_ids.append(product_id)
    if not normalized_ids:
        return set()

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            existing_ids = set()
            for offset in range(0, len(normalized_ids), 1000):
                batch = normalized_ids[offset:offset + 1000]
                placeholders = ", ".join(["%s"] * len(batch))
                cursor.execute(
                    f"""
                    SELECT DISTINCT `产品编号`
                    FROM `zying_product`
                    WHERE `产品编号` IN ({placeholders})
                      AND `上架快照` IS NOT NULL
                      AND `上架快照` NOT IN ('', '{{}}')
                    """,
                    tuple(batch),
                )
                existing_ids.update(
                    str(row.get("产品编号") or "").strip()
                    for row in (cursor.fetchall() or ())
                    if str(row.get("产品编号") or "").strip()
                )
            return existing_ids
    finally:
        connection.close()


def get_zying_risk_candidates(
    hours=24,
    limit=0,
    zying_category=None,
    include_checked=False,
):
    """读取智赢商品，供标题和主图侵权风险检查。

    ``zying_category`` 可以是智赢分类编号、完整分类路径或末级分类名。
    ``hours`` 为 0 时不限制入库时间。默认跳过已有 0/1/2 审核结果的数据。
    """
    hours = max(0, int(hours or 0))
    limit = max(0, int(limit or 0))
    category = str(zying_category or "").strip()
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            sql = """
                SELECT
                    `id` AS `row_id`,
                    `产品编号` AS `product_id`,
                    `主图链接` AS `main_image_url`,
                    `标题` AS `title`,
                    `产品分类` AS `product_category`,
                    `智赢分类编号` AS `zying_category_id`,
                    `智赢产品分类` AS `zying_category`,
                    `提交时间` AS `submitted_at`,
                    `疑似侵权` AS `suspected_infringement`,
                    `侵权关键词` AS `infringement_keywords`
                FROM `zying_product`
                WHERE 1 = 1
            """
            params = []
            if hours:
                since = datetime.now() - timedelta(hours=hours)
                sql += " AND `提交时间` >= %s"
                params.append(since.strftime("%Y-%m-%d %H:%M:%S"))
            if not include_checked:
                sql += " AND COALESCE(`疑似侵权`, '') NOT IN ('0', '1', '2')"
            if category:
                sql += """
                    AND (
                        `智赢分类编号` = %s
                        OR `智赢产品分类` = %s
                        OR `智赢产品分类` LIKE %s
                    )
                """
                params.extend((category, category, f"%/{category}"))
            sql += " ORDER BY `id` ASC"
            if limit:
                sql += " LIMIT %s"
                params.append(limit)
            cursor.execute(sql, tuple(params))
            return cursor.fetchall()
    finally:
        connection.close()


def mark_zying_products_suspected(row_ids):
    """兼容旧调用：把指定数据行的风险级别标记为 1。"""
    normalized_ids = sorted(
        {
            int(row_id)
            for row_id in (row_ids or [])
            if str(row_id or "").strip().isdigit()
        }
    )
    if not normalized_ids:
        return 0

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            cursor.executemany(
                "UPDATE `zying_product` SET `疑似侵权` = '1' WHERE `id` = %s",
                [(row_id,) for row_id in normalized_ids],
            )
            updated_count = cursor.rowcount
        connection.commit()
        return updated_count
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_zying_product_risks(results):
    """批量写入智赢商品的 0/1/2 侵权风险和命中关键词。

    ``results`` 中每项需要包含 ``row_id`` 和 ``risk_level``，
    ``keywords`` 可以是字符串或字符串数组。0 级会清空旧的关键词。
    """
    normalized = {}
    for result in results or []:
        if not isinstance(result, dict):
            continue
        try:
            row_id = int(result.get("row_id"))
            risk_level = int(result.get("risk_level"))
        except (TypeError, ValueError):
            continue
        if row_id <= 0 or risk_level not in {0, 1, 2}:
            continue

        raw_keywords = result.get("keywords") or []
        if isinstance(raw_keywords, str):
            keywords = raw_keywords.strip()
        else:
            keywords = ", ".join(
                str(item).strip()
                for item in raw_keywords
                if str(item or "").strip()
            )
        normalized[row_id] = (
            str(risk_level),
            keywords[:1024] if risk_level else None,
            row_id,
        )

    if not normalized:
        return 0

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            cursor.executemany(
                """
                UPDATE `zying_product`
                SET `疑似侵权` = %s, `侵权关键词` = %s
                WHERE `id` = %s
                """,
                list(normalized.values()),
            )
            updated_count = cursor.rowcount
        connection.commit()
        return updated_count
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_zying_risk_categories():
    """返回 zying_product 中可用的智赢分类及各风险级别数量。"""
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            cursor.execute(
                """
                SELECT
                    COALESCE(`智赢分类编号`, '') AS `category_id`,
                    COALESCE(`智赢产品分类`, '') AS `category_name`,
                    COUNT(*) AS `total`,
                    SUM(CASE WHEN `疑似侵权` = '0' THEN 1 ELSE 0 END) AS `risk_0`,
                    SUM(CASE WHEN `疑似侵权` = '1' THEN 1 ELSE 0 END) AS `risk_1`,
                    SUM(CASE WHEN `疑似侵权` = '2' THEN 1 ELSE 0 END) AS `risk_2`,
                    SUM(
                        CASE
                            WHEN COALESCE(`疑似侵权`, '') NOT IN ('0', '1', '2')
                            THEN 1 ELSE 0
                        END
                    ) AS `unchecked`
                FROM `zying_product`
                WHERE COALESCE(`智赢分类编号`, '') <> ''
                   OR COALESCE(`智赢产品分类`, '') <> ''
                GROUP BY `智赢分类编号`, `智赢产品分类`
                ORDER BY `智赢产品分类` ASC, `智赢分类编号` ASC
                """
            )
            return cursor.fetchall()
    finally:
        connection.close()


def get_zying_risk_results(
    zying_category=None,
    risk_level=None,
    search="",
    sort_by="risk_level",
    sort_dir="desc",
    limit=1000,
):
    """按控制台筛选与排序条件返回智赢侵权检测结果。"""
    category = str(zying_category or "").strip()
    risk = str(risk_level if risk_level is not None else "").strip().lower()
    keyword = str(search or "").strip()[:200]
    limit = max(0, int(limit or 0))
    sort_columns = {
        "row_id": "`id`",
        "product_id": "`产品编号`",
        "title": "`标题`",
        "zying_category": "`智赢产品分类`",
        "risk_level": "CAST(COALESCE(NULLIF(`疑似侵权`, ''), '-1') AS SIGNED)",
        "keywords": "`侵权关键词`",
        "submitted_at": "`提交时间`",
    }
    order_column = sort_columns.get(str(sort_by or "").strip(), sort_columns["risk_level"])
    order_direction = "ASC" if str(sort_dir or "").strip().lower() == "asc" else "DESC"

    where = ["1 = 1"]
    params = []
    if category:
        where.append(
            "(`智赢分类编号` = %s OR `智赢产品分类` = %s OR `智赢产品分类` LIKE %s)"
        )
        params.extend((category, category, f"%/{category}"))
    if risk in {"0", "1", "2"}:
        where.append("`疑似侵权` = %s")
        params.append(risk)
    elif risk in {"unchecked", "pending", "未检测"}:
        where.append("COALESCE(`疑似侵权`, '') NOT IN ('0', '1', '2')")
    if keyword:
        like_value = f"%{keyword}%"
        where.append(
            "(`产品编号` LIKE %s OR `标题` LIKE %s OR `侵权关键词` LIKE %s)"
        )
        params.extend((like_value, like_value, like_value))

    where_sql = " AND ".join(where)
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_zying_product_table(cursor)
            cursor.execute(
                f"""
                SELECT
                    COUNT(*) AS `total`,
                    SUM(CASE WHEN `疑似侵权` = '0' THEN 1 ELSE 0 END) AS `risk_0`,
                    SUM(CASE WHEN `疑似侵权` = '1' THEN 1 ELSE 0 END) AS `risk_1`,
                    SUM(CASE WHEN `疑似侵权` = '2' THEN 1 ELSE 0 END) AS `risk_2`,
                    SUM(
                        CASE
                            WHEN COALESCE(`疑似侵权`, '') NOT IN ('0', '1', '2')
                            THEN 1 ELSE 0
                        END
                    ) AS `unchecked`
                FROM `zying_product`
                WHERE {where_sql}
                """,
                tuple(params),
            )
            counts = cursor.fetchone() or {}
            sql = f"""
                SELECT
                    `id` AS `row_id`,
                    `产品编号` AS `product_id`,
                    `主图链接` AS `main_image_url`,
                    `标题` AS `title`,
                    `产品分类` AS `product_category`,
                    `智赢分类编号` AS `zying_category_id`,
                    `智赢产品分类` AS `zying_category`,
                    `疑似侵权` AS `risk_level`,
                    `侵权关键词` AS `keywords`,
                    `采集时间` AS `collected_at`,
                    `提交时间` AS `submitted_at`
                FROM `zying_product`
                WHERE {where_sql}
                ORDER BY {order_column} {order_direction}, `id` DESC
            """
            row_params = list(params)
            if limit:
                sql += " LIMIT %s"
                row_params.append(limit)
            cursor.execute(sql, tuple(row_params))
            rows = cursor.fetchall()
            return {
                "total": int(counts.get("total") or 0),
                "risk_0": int(counts.get("risk_0") or 0),
                "risk_1": int(counts.get("risk_1") or 0),
                "risk_2": int(counts.get("risk_2") or 0),
                "unchecked": int(counts.get("unchecked") or 0),
                "rows": rows,
                "sort_by": str(sort_by or "risk_level"),
                "sort_dir": order_direction.lower(),
            }
    finally:
        connection.close()


def _ensure_bit_browser_configs_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `bit_browser_configs` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `window_id` VARCHAR(64) NOT NULL,
            `shop_name` VARCHAR(255) NOT NULL,
            `status` VARCHAR(255) NULL,
            `sites` TEXT NULL,
            `sequence_no` VARCHAR(64) NULL,
            `salesperson` VARCHAR(255) NULL,
            `email` VARCHAR(320) NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_bit_browser_shop_name` (`shop_name`),
            KEY `idx_bit_browser_window_id` (`window_id`),
            KEY `idx_bit_browser_status` (`status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def _config_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _normalize_bit_browser_config(record):
    if isinstance(record, dict):
        values = {
            "window_id": record.get("window_id", record.get("窗口ID")),
            "shop_name": record.get("shop_name", record.get("账号名", record.get("店铺名"))),
            "status": record.get("status", record.get("状态", record.get("状态（若为忽略则跳过）"))),
            "sites": record.get("sites", record.get("站点")),
            "sequence_no": record.get("sequence_no", record.get("序号")),
            "salesperson": record.get("salesperson", record.get("业务员")),
            "email": record.get("email", record.get("邮箱")),
        }
    else:
        row = list(record or [])
        row.extend([None] * (7 - len(row)))
        values = dict(
            zip(
                ("window_id", "shop_name", "status", "sites", "sequence_no", "salesperson", "email"),
                row[:7],
            )
        )
    normalized = {key: _config_text(value) for key, value in values.items()}
    if not normalized["window_id"] or not normalized["shop_name"]:
        raise ValueError("比特浏览器配置缺少窗口ID或账号名")
    return normalized


def upsert_bit_browser_configs(records, replace=False):
    normalized_records = [_normalize_bit_browser_config(record) for record in records or []]
    if replace and not normalized_records:
        raise ValueError("替换比特浏览器配置时不允许提交空数据")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_bit_browser_configs_table(cursor)
            if replace:
                cursor.execute("DELETE FROM `bit_browser_configs`")
            if normalized_records:
                cursor.executemany(
                    """
                    INSERT INTO `bit_browser_configs` (
                        `window_id`, `shop_name`, `status`, `sites`, `sequence_no`,
                        `salesperson`, `email`, `created_at`, `updated_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `window_id` = VALUES(`window_id`),
                        `shop_name` = VALUES(`shop_name`),
                        `status` = VALUES(`status`),
                        `sites` = VALUES(`sites`),
                        `sequence_no` = VALUES(`sequence_no`),
                        `salesperson` = VALUES(`salesperson`),
                        `email` = VALUES(`email`),
                        `updated_at` = VALUES(`updated_at`)
                    """,
                    [
                        (
                            record["window_id"],
                            record["shop_name"],
                            record["status"],
                            record["sites"],
                            record["sequence_no"],
                            record["salesperson"],
                            record["email"],
                            now,
                            now,
                        )
                        for record in normalized_records
                    ],
                )
        connection.commit()
        return {"count": len(normalized_records), "replaced": bool(replace)}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _serialize_bit_browser_config_row(row):
    if not row:
        return row
    result = dict(row)
    for key in ("created_at", "updated_at"):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return result


def create_bit_browser_config(record):
    normalized = _normalize_bit_browser_config(record)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_bit_browser_configs_table(cursor)
            cursor.execute(
                """
                INSERT INTO `bit_browser_configs` (
                    `window_id`, `shop_name`, `status`, `sites`, `sequence_no`,
                    `salesperson`, `email`, `created_at`, `updated_at`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    normalized["window_id"],
                    normalized["shop_name"],
                    normalized["status"],
                    normalized["sites"],
                    normalized["sequence_no"],
                    normalized["salesperson"],
                    normalized["email"],
                    now,
                    now,
                ),
            )
            config_id = int(cursor.lastrowid)
        connection.commit()
        return {"id": config_id}
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("店铺名称已存在") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_bit_browser_config(config_id, record):
    try:
        config_id = int(config_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("店铺配置编号无效") from exc
    if config_id <= 0:
        raise ValueError("店铺配置编号无效")

    normalized = _normalize_bit_browser_config(record)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_bit_browser_configs_table(cursor)
            cursor.execute(
                """
                UPDATE `bit_browser_configs`
                SET `window_id` = %s, `shop_name` = %s, `status` = %s,
                    `sites` = %s, `sequence_no` = %s, `salesperson` = %s,
                    `email` = %s, `updated_at` = %s
                WHERE `id` = %s
                """,
                (
                    normalized["window_id"],
                    normalized["shop_name"],
                    normalized["status"],
                    normalized["sites"],
                    normalized["sequence_no"],
                    normalized["salesperson"],
                    normalized["email"],
                    now,
                    config_id,
                ),
            )
        connection.commit()
        return {"id": config_id}
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("店铺名称已存在") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_bit_browser_config(config_id):
    try:
        config_id = int(config_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("店铺配置编号无效") from exc
    if config_id <= 0:
        raise ValueError("店铺配置编号无效")

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_bit_browser_configs_table(cursor)
            cursor.execute(
                "DELETE FROM `bit_browser_configs` WHERE `id` = %s",
                (config_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError("店铺配置不存在")
        connection.commit()
        return {"id": config_id}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_bit_browser_configs(include_ignored=True):
    del include_ignored
    raise RuntimeError(
        "bit_browser_configs 已停用；请读取店铺授权及站点任务开关"
    )


def get_bit_browser_config(shop_name="", window_id="", include_ignored=True):
    del shop_name, window_id, include_ignored
    raise RuntimeError(
        "bit_browser_configs 已停用；请读取店铺授权及站点任务开关"
    )


def insert_chat_info(name, site, message, chat, response, time):
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            # --- 增 (Create) ---
            sql_insert = """
                INSERT INTO `record_chat` (
        `店铺`, 
        `站点`, 
        `话术`, 
        `客服消息`, 
        `回复`, 
        `时间`
    ) VALUES (
        %s, %s, %s, %s, %s, %s
    );    
        """
            cursor.execute(sql_insert, (name, site, message, chat, response, time))
            chat_id = cursor.lastrowid
            print("执行sql成功", sql_insert)
        connection.commit()
        return chat_id

    except Exception as e:
        # 发生错误则回滚
        connection.rollback()
        print(f"操作失败，已回滚: {e}")
    finally:
        # 关闭连接
        connection.close()


def _ensure_appeal_chat_records_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `appeal_chat_records` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `record_time` DATETIME NULL,
            `window_name` VARCHAR(128) NULL,
            `site` VARCHAR(64) NULL,
            `event` VARCHAR(128) NULL,
            `message` LONGTEXT NULL,
            `response` LONGTEXT NULL,
            `chat_json` LONGTEXT NULL,
            `extra_json` LONGTEXT NULL,
            `raw_json` LONGTEXT NULL,
            `created_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            KEY `idx_appeal_chat_time` (`record_time`),
            KEY `idx_appeal_chat_shop_site` (`window_name`, `site`),
            KEY `idx_appeal_chat_event` (`event`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def insert_appeal_chat_record(record):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_appeal_chat_records_table(cursor)
            record = dict(record or {})
            record_time = record.get("time") or None
            sql_insert = """
                INSERT INTO `appeal_chat_records` (
                    `record_time`,
                    `window_name`,
                    `site`,
                    `event`,
                    `message`,
                    `response`,
                    `chat_json`,
                    `extra_json`,
                    `raw_json`,
                    `created_at`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(
                sql_insert,
                (
                    record_time,
                    record.get("window", ""),
                    record.get("site", ""),
                    record.get("event", ""),
                    record.get("message", ""),
                    record.get("response", ""),
                    json.dumps(record.get("chat", []), ensure_ascii=False),
                    json.dumps(record.get("extra", {}), ensure_ascii=False),
                    json.dumps(record, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            record_id = cursor.lastrowid
        connection.commit()
        return record_id
    except Exception as e:
        connection.rollback()
        print(f"AI申诉聊天记录写入失败，已回滚: {e}")
        raise
    finally:
        connection.close()


def _ensure_ai_appeal_records_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `ai_appeal_records` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `appeal_time` DATETIME NULL,
            `appeal_type` VARCHAR(64) NULL,
            `shop_name` VARCHAR(128) NULL,
            `site` VARCHAR(64) NULL,
            `status` VARCHAR(64) NULL,
            `appeal_content` LONGTEXT NULL,
            `identifiers_json` LONGTEXT NULL,
            `success_ids_json` LONGTEXT NULL,
            `failed_ids_json` LONGTEXT NULL,
            `ai_replies_json` LONGTEXT NULL,
            `ai_summary` LONGTEXT NULL,
            `error` LONGTEXT NULL,
            `raw_json` LONGTEXT NULL,
            `created_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            KEY `idx_ai_appeal_time` (`appeal_time`),
            KEY `idx_ai_appeal_shop_site` (`shop_name`, `site`),
            KEY `idx_ai_appeal_type_status` (`appeal_type`, `status`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def insert_ai_appeal_record(record):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_ai_appeal_records_table(cursor)
            record = dict(record or {})
            sql_insert = """
                INSERT INTO `ai_appeal_records` (
                    `appeal_time`,
                    `appeal_type`,
                    `shop_name`,
                    `site`,
                    `status`,
                    `appeal_content`,
                    `identifiers_json`,
                    `success_ids_json`,
                    `failed_ids_json`,
                    `ai_replies_json`,
                    `ai_summary`,
                    `error`,
                    `raw_json`,
                    `created_at`
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(
                sql_insert,
                (
                    record.get("appeal_time") or None,
                    record.get("appeal_type", ""),
                    record.get("shop_name", ""),
                    record.get("site", ""),
                    record.get("status", ""),
                    record.get("appeal_content", ""),
                    json.dumps(record.get("identifiers", []), ensure_ascii=False),
                    json.dumps(record.get("success_ids", []), ensure_ascii=False),
                    json.dumps(record.get("failed_ids", []), ensure_ascii=False),
                    json.dumps(record.get("ai_replies", []), ensure_ascii=False),
                    record.get("ai_summary", ""),
                    record.get("error", ""),
                    json.dumps(record, ensure_ascii=False),
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                ),
            )
            record_id = cursor.lastrowid
        connection.commit()
        return record_id
    except Exception as e:
        connection.rollback()
        print(f"AI申诉记录写入失败，已回滚: {e}")
        raise
    finally:
        connection.close()


def get_ai_appeal_records(limit=100):
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_ai_appeal_records_table(cursor)
            cursor.execute(
                """
                SELECT
                    `id`,
                    `appeal_time`,
                    `appeal_type`,
                    `shop_name`,
                    `site`,
                    `status`,
                    `appeal_content`,
                    `identifiers_json`,
                    `success_ids_json`,
                    `failed_ids_json`,
                    `ai_replies_json`,
                    `ai_summary`,
                    `error`,
                    `created_at`
                FROM `ai_appeal_records`
                ORDER BY `appeal_time` DESC, `id` DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            for row in rows:
                for time_key in ("appeal_time", "created_at"):
                    if row.get(time_key) is not None:
                        row[time_key] = str(row[time_key])
                for json_key in ("identifiers_json", "success_ids_json", "failed_ids_json", "ai_replies_json"):
                    value = row.get(json_key)
                    try:
                        row[json_key] = json.loads(value) if value else []
                    except Exception:
                        row[json_key] = []
            return {"total": len(rows), "rows": rows}
    finally:
        connection.close()


def _ensure_mercado_store_tokens_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_store_tokens` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `display_name` VARCHAR(100) NOT NULL,
            `enabled` TINYINT(1) NOT NULL DEFAULT 1,
            `meli_user_id` VARCHAR(64) NULL,
            `nickname` VARCHAR(255) NULL,
            `site_id` VARCHAR(32) NULL,
            `email` VARCHAR(255) NULL,
            `client_id` VARCHAR(64) NULL,
            `access_token` LONGTEXT NOT NULL,
            `refresh_token` LONGTEXT NULL,
            `token_type` VARCHAR(32) NOT NULL DEFAULT 'Bearer',
            `scope` TEXT NULL,
            `expires_at` DATETIME NULL,
            `last_verified_at` DATETIME NULL,
            `last_refreshed_at` DATETIME NULL,
            `last_error` TEXT NULL,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_mercado_store_display_name` (`display_name`),
            UNIQUE KEY `uniq_mercado_store_user_id` (`meli_user_id`),
            KEY `idx_mercado_store_expires_at` (`expires_at`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(
        cursor,
        "mercado_store_tokens",
        "enabled",
        "TINYINT(1) NOT NULL DEFAULT 1 AFTER `display_name`",
    )
    _ensure_column(
        cursor,
        "mercado_store_tokens",
        "email",
        "VARCHAR(255) NULL AFTER `site_id`",
    )


MERCADO_CONFIGURABLE_SITES = {
    "MLM": "墨西哥",
    "MLB": "巴西",
    "MLC": "智利",
    "MCO": "哥伦比亚",
    "MLA": "阿根廷",
    "MLU": "乌拉圭",
}


def _ensure_mercado_store_site_settings_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `mercado_store_site_settings` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `token_id` BIGINT NOT NULL,
            `site_id` VARCHAR(32) NOT NULL,
            `salesperson` VARCHAR(100) NULL,
            `discount_rate` DECIMAL(7,4) NULL,
            `group_name` VARCHAR(100) NULL,
            `appeal_enabled` TINYINT(1) NOT NULL DEFAULT 0,
            `visit_stats_enabled` TINYINT(1) NOT NULL DEFAULT 0,
            `created_at` DATETIME NOT NULL,
            `updated_at` DATETIME NOT NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_mercado_store_site_setting` (`token_id`, `site_id`),
            KEY `idx_mercado_site_salesperson` (`salesperson`),
            KEY `idx_mercado_site_group` (`group_name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    _ensure_column(
        cursor,
        "mercado_store_site_settings",
        "appeal_enabled",
        "TINYINT(1) NOT NULL DEFAULT 0",
    )
    _ensure_column(
        cursor,
        "mercado_store_site_settings",
        "visit_stats_enabled",
        "TINYINT(1) NOT NULL DEFAULT 0",
    )


def _mercado_store_site_setting_rows(cursor, token_id):
    cursor.execute(
        """
        SELECT `token_id`, `site_id`, `salesperson`, `discount_rate`, `group_name`,
               `appeal_enabled`, `visit_stats_enabled`,
               `created_at`, `updated_at`
        FROM `mercado_store_site_settings`
        WHERE `token_id` = %s
        ORDER BY `site_id` ASC
        """,
        (int(token_id),),
    )
    configured = {str(row["site_id"]): dict(row) for row in (cursor.fetchall() or [])}
    result = []
    for site_id, site_name in MERCADO_CONFIGURABLE_SITES.items():
        row = configured.get(site_id, {})
        discount_rate = row.get("discount_rate")
        result.append(
            {
                "token_id": int(token_id),
                "site_id": site_id,
                "site_name": site_name,
                "salesperson": str(row.get("salesperson") or ""),
                "discount_rate": (
                    float(discount_rate) if discount_rate is not None else None
                ),
                "group_name": str(row.get("group_name") or ""),
                "appeal_enabled": bool(row.get("appeal_enabled")),
                "visit_stats_enabled": bool(row.get("visit_stats_enabled")),
                "created_at": str(row["created_at"]) if row.get("created_at") else None,
                "updated_at": str(row["updated_at"]) if row.get("updated_at") else None,
            }
        )
    return result


def list_mercado_store_site_settings(token_id):
    token_id = int(token_id)
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            cursor.execute(
                "SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s LIMIT 1",
                (token_id,),
            )
            if not cursor.fetchone():
                raise KeyError("店铺授权不存在")
            rows = _mercado_store_site_setting_rows(cursor, token_id)
            return {"token_id": token_id, "rows": rows}
    finally:
        connection.close()


def upsert_mercado_store_site_settings(token_id, settings):
    token_id = int(token_id)
    if not isinstance(settings, list):
        raise ValueError("站点配置必须是数组")
    if len(settings) > len(MERCADO_CONFIGURABLE_SITES):
        raise ValueError("站点配置数量超过支持范围")

    normalized = []
    seen_sites = set()
    for raw in settings:
        if not isinstance(raw, dict):
            raise ValueError("站点配置格式不正确")
        site_id = str(raw.get("site_id") or "").strip().upper()
        if site_id not in MERCADO_CONFIGURABLE_SITES:
            raise ValueError(f"不支持的美客多站点：{site_id or '空'}")
        if site_id in seen_sites:
            raise ValueError(f"站点 {site_id} 配置重复")
        seen_sites.add(site_id)

        salesperson = str(raw.get("salesperson") or "").strip()
        group_name = str(raw.get("group_name") or "").strip()
        if len(salesperson) > 100:
            raise ValueError(f"{site_id} 的业务员不能超过 100 个字符")
        if len(group_name) > 100:
            raise ValueError(f"{site_id} 的组别不能超过 100 个字符")

        raw_discount = raw.get("discount_rate")
        if raw_discount in (None, ""):
            discount_rate = None
        else:
            try:
                discount_rate = Decimal(str(raw_discount))
            except Exception as exc:
                raise ValueError(f"{site_id} 的折扣比例不是有效数字") from exc
            if discount_rate < 0 or discount_rate > 100:
                raise ValueError(f"{site_id} 的折扣比例必须在 0 到 100 之间")
            discount_rate = discount_rate.quantize(Decimal("0.0001"))
        appeal_enabled = raw.get("appeal_enabled", False)
        visit_stats_enabled = raw.get("visit_stats_enabled", False)
        if isinstance(appeal_enabled, str):
            appeal_enabled = appeal_enabled.strip().lower() not in (
                "", "0", "false", "no", "off",
            )
        if isinstance(visit_stats_enabled, str):
            visit_stats_enabled = visit_stats_enabled.strip().lower() not in (
                "", "0", "false", "no", "off",
            )
        normalized.append(
            (
                site_id,
                salesperson,
                discount_rate,
                group_name,
                1 if bool(appeal_enabled) else 0,
                1 if bool(visit_stats_enabled) else 0,
            )
        )

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            cursor.execute(
                "SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s LIMIT 1",
                (token_id,),
            )
            if not cursor.fetchone():
                raise KeyError("店铺授权不存在")
            for (
                site_id,
                salesperson,
                discount_rate,
                group_name,
                appeal_enabled,
                visit_stats_enabled,
            ) in normalized:
                if (
                    not salesperson
                    and discount_rate is None
                    and not group_name
                    and not appeal_enabled
                    and not visit_stats_enabled
                ):
                    cursor.execute(
                        "DELETE FROM `mercado_store_site_settings` WHERE `token_id` = %s AND `site_id` = %s",
                        (token_id, site_id),
                    )
                    continue
                cursor.execute(
                    """
                    INSERT INTO `mercado_store_site_settings` (
                        `token_id`, `site_id`, `salesperson`, `discount_rate`, `group_name`,
                        `appeal_enabled`, `visit_stats_enabled`, `created_at`, `updated_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `salesperson` = VALUES(`salesperson`),
                        `discount_rate` = VALUES(`discount_rate`),
                        `group_name` = VALUES(`group_name`),
                        `appeal_enabled` = VALUES(`appeal_enabled`),
                        `visit_stats_enabled` = VALUES(`visit_stats_enabled`),
                        `updated_at` = VALUES(`updated_at`)
                    """,
                    (
                        token_id, site_id, salesperson or None, discount_rate,
                        group_name or None, appeal_enabled, visit_stats_enabled, now, now,
                    ),
                )
            rows = _mercado_store_site_setting_rows(cursor, token_id)
        connection.commit()
        return {"token_id": token_id, "rows": rows}
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _mercado_token_datetime(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _mercado_token_record(record):
    normalized = {
        "display_name": str(record.get("display_name") or "").strip(),
        "meli_user_id": str(record.get("meli_user_id") or "").strip() or None,
        "nickname": str(record.get("nickname") or "").strip(),
        "site_id": str(record.get("site_id") or "").strip(),
        "email": str(record.get("email") or "").strip(),
        "client_id": str(record.get("client_id") or "").strip(),
        "access_token": str(record.get("access_token") or "").strip(),
        "refresh_token": str(record.get("refresh_token") or "").strip(),
        "token_type": str(record.get("token_type") or "Bearer").strip() or "Bearer",
        "scope": str(record.get("scope") or "").strip(),
        "expires_at": _mercado_token_datetime(record.get("expires_at")),
        "last_verified_at": _mercado_token_datetime(record.get("last_verified_at")),
        "last_refreshed_at": _mercado_token_datetime(record.get("last_refreshed_at")),
        "last_error": str(record.get("last_error") or "").strip(),
    }
    if not normalized["display_name"]:
        raise ValueError("店铺授权缺少自定义名称")
    if len(normalized["display_name"]) > 100:
        raise ValueError("自定义店铺名称不能超过 100 个字符")
    if len(normalized["email"]) > 255:
        raise ValueError("店铺邮箱不能超过 255 个字符")
    if not normalized["access_token"]:
        raise ValueError("店铺授权缺少 Access Token")
    return normalized


def _mercado_token_summary(row, now=None):
    result = dict(row or {})
    result.pop("access_token", None)
    result.pop("refresh_token", None)
    now = now or datetime.now()
    expires_at = result.get("expires_at")
    if isinstance(expires_at, str):
        try:
            expires_at = datetime.fromisoformat(expires_at)
        except ValueError:
            expires_at = None
    has_refresh_token = bool(result.pop("has_refresh_token", False))
    last_error = str(result.get("last_error") or "")
    if expires_at is None:
        status = "unknown"
        status_text = "有效期未知"
    elif expires_at <= now:
        status = "expired"
        status_text = "已过期，可刷新" if has_refresh_token else "已过期，需重新授权"
    elif expires_at <= now + timedelta(minutes=30):
        status = "expiring"
        status_text = "即将过期"
    else:
        status = "active"
        status_text = "有效"
    if last_error and status not in ("expired",):
        status = "warning"
        status_text = "需检查"
    result["has_refresh_token"] = has_refresh_token
    result["enabled"] = bool(result.get("enabled", True))
    result["status"] = status
    result["status_text"] = status_text
    for key in (
        "expires_at",
        "last_verified_at",
        "last_refreshed_at",
        "created_at",
        "updated_at",
    ):
        if result.get(key) is not None:
            result[key] = str(result[key])
    return result


def upsert_mercado_store_token(record):
    token = _mercado_token_record(record)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                "SELECT `id`, `display_name`, `meli_user_id` "
                "FROM `mercado_store_tokens` "
                "WHERE `display_name` = %s OR (`meli_user_id` IS NOT NULL AND `meli_user_id` = %s)",
                (token["display_name"], token["meli_user_id"]),
            )
            matches = cursor.fetchall() or []
            matched_ids = {int(row["id"]) for row in matches}
            if len(matched_ids) > 1:
                raise ValueError("自定义名称已被另一个授权店铺使用，请更换名称")

            values = (
                token["display_name"],
                token["meli_user_id"],
                token["nickname"],
                token["site_id"],
                token["email"],
                token["client_id"],
                token["access_token"],
                token["refresh_token"],
                token["token_type"],
                token["scope"],
                token["expires_at"],
                token["last_verified_at"],
                token["last_refreshed_at"],
                token["last_error"],
                now,
            )
            if matched_ids:
                token_id = matched_ids.pop()
                cursor.execute(
                    """
                    UPDATE `mercado_store_tokens`
                    SET `display_name` = %s, `meli_user_id` = %s, `nickname` = %s,
                        `site_id` = %s, `email` = %s, `client_id` = %s, `access_token` = %s,
                        `refresh_token` = %s, `token_type` = %s, `scope` = %s,
                        `expires_at` = %s, `last_verified_at` = %s,
                        `last_refreshed_at` = %s, `last_error` = %s, `updated_at` = %s
                    WHERE `id` = %s
                    """,
                    values + (token_id,),
                )
            else:
                cursor.execute(
                    """
                    INSERT INTO `mercado_store_tokens` (
                        `display_name`, `meli_user_id`, `nickname`, `site_id`, `email`, `client_id`,
                        `access_token`, `refresh_token`, `token_type`, `scope`, `expires_at`,
                        `last_verified_at`, `last_refreshed_at`, `last_error`, `created_at`,
                        `updated_at`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values + (now,),
                )
                token_id = cursor.lastrowid
        connection.commit()
        return get_mercado_store_token_summary(token_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_mercado_store_tokens():
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            cursor.execute(
                """
                SELECT `id`, `display_name`, `enabled`, `meli_user_id`, `nickname`, `site_id`, `email`,
                       `client_id`, `token_type`, `scope`, `expires_at`,
                       `last_verified_at`, `last_refreshed_at`, `last_error`,
                       `created_at`, `updated_at`,
                       (`refresh_token` IS NOT NULL AND `refresh_token` <> '') AS `has_refresh_token`
                FROM `mercado_store_tokens`
                ORDER BY `display_name` ASC, `id` ASC
                """
            )
            rows = [_mercado_token_summary(row) for row in (cursor.fetchall() or [])]
            for row in rows:
                row["site_settings"] = _mercado_store_site_setting_rows(cursor, row["id"])
            return {"total": len(rows), "rows": rows}
    finally:
        connection.close()


def get_mercado_store_token_summary(token_id):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            cursor.execute(
                """
                SELECT `id`, `display_name`, `enabled`, `meli_user_id`, `nickname`, `site_id`, `email`,
                       `client_id`, `token_type`, `scope`, `expires_at`,
                       `last_verified_at`, `last_refreshed_at`, `last_error`,
                       `created_at`, `updated_at`,
                       (`refresh_token` IS NOT NULL AND `refresh_token` <> '') AS `has_refresh_token`
                FROM `mercado_store_tokens` WHERE `id` = %s LIMIT 1
                """,
                (int(token_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            result = _mercado_token_summary(row)
            result["site_settings"] = _mercado_store_site_setting_rows(cursor, token_id)
            return result
    finally:
        connection.close()


def get_mercado_store_token(token_id, include_disabled=False):
    """Return secrets for server-side refresh/API use; never expose via UI routes."""
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            cursor.execute(
                "SELECT * FROM `mercado_store_tokens` WHERE `id` = %s LIMIT 1",
                (int(token_id),),
            )
            row = cursor.fetchone()
            if row and not include_disabled and not bool(row.get("enabled", 1)):
                raise ValueError("该店铺已关闭，任何业务操作均不会执行")
            return row
    finally:
        connection.close()


def update_mercado_store_token(token_id, record):
    token = _mercado_token_record(record)
    token_id = int(token_id)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_store_tokens`
                SET `meli_user_id` = %s, `nickname` = %s, `site_id` = %s, `email` = %s,
                    `client_id` = %s, `access_token` = %s, `refresh_token` = %s,
                    `token_type` = %s, `scope` = %s, `expires_at` = %s,
                    `last_verified_at` = %s, `last_refreshed_at` = %s,
                    `last_error` = %s, `updated_at` = %s
                WHERE `id` = %s
                """,
                (
                    token["meli_user_id"], token["nickname"], token["site_id"], token["email"],
                    token["client_id"], token["access_token"], token["refresh_token"],
                    token["token_type"], token["scope"], token["expires_at"],
                    token["last_verified_at"], token["last_refreshed_at"],
                    token["last_error"], now, token_id,
                ),
            )
            if cursor.rowcount == 0:
                cursor.execute("SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s", (token_id,))
                if not cursor.fetchone():
                    raise KeyError("店铺授权不存在")
        connection.commit()
        return get_mercado_store_token_summary(token_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_mercado_store_token_email(token_id, email):
    """Persist email read from /users/me without touching rotating token secrets."""
    token_id = int(token_id)
    email = str(email or "").strip()
    if not email:
        raise ValueError("店铺身份接口未返回邮箱")
    if len(email) > 255:
        raise ValueError("店铺邮箱不能超过 255 个字符")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_store_tokens`
                SET `email` = %s, `last_verified_at` = %s, `last_error` = '',
                    `updated_at` = %s
                WHERE `id` = %s
                """,
                (email, now, now, token_id),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s",
                    (token_id,),
                )
                if not cursor.fetchone():
                    raise KeyError("店铺授权不存在")
        connection.commit()
        return get_mercado_store_token_summary(token_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def set_mercado_store_token_enabled(token_id, enabled):
    """Enable or disable every business operation for one authorized store."""
    token_id = int(token_id)
    enabled = bool(enabled)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                """
                UPDATE `mercado_store_tokens`
                SET `enabled` = %s, `updated_at` = %s
                WHERE `id` = %s
                """,
                (1 if enabled else 0, now, token_id),
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    "SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s",
                    (token_id,),
                )
                if not cursor.fetchone():
                    raise KeyError("店铺授权不存在")
        connection.commit()
        return get_mercado_store_token_summary(token_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def record_mercado_store_token_error(token_id, message):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                "UPDATE `mercado_store_tokens` SET `last_error` = %s, `updated_at` = %s WHERE `id` = %s",
                (str(message or "")[:2000], datetime.now().strftime("%Y-%m-%d %H:%M:%S"), int(token_id)),
            )
            affected = cursor.rowcount
        connection.commit()
        return affected
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def rename_mercado_store_token(token_id, display_name):
    token_id = int(token_id)
    display_name = str(display_name or "").strip()
    if not display_name:
        raise ValueError("请输入自定义店铺名称")
    if len(display_name) > 100:
        raise ValueError("自定义店铺名称不能超过 100 个字符")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            cursor.execute(
                "SELECT `id` FROM `mercado_store_tokens` WHERE `display_name` = %s AND `id` <> %s LIMIT 1",
                (display_name, token_id),
            )
            if cursor.fetchone():
                raise ValueError("自定义店铺名称已存在")
            cursor.execute(
                "UPDATE `mercado_store_tokens` SET `display_name` = %s, `updated_at` = %s WHERE `id` = %s",
                (display_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), token_id),
            )
            if cursor.rowcount == 0:
                cursor.execute("SELECT 1 FROM `mercado_store_tokens` WHERE `id` = %s", (token_id,))
                if not cursor.fetchone():
                    raise KeyError("店铺授权不存在")
        connection.commit()
        return get_mercado_store_token_summary(token_id)
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_mercado_store_token(token_id):
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_mercado_store_tokens_table(cursor)
            _ensure_mercado_store_site_settings_table(cursor)
            cursor.execute("SHOW TABLES LIKE 'mercado_synced_orders'")
            if cursor.fetchone():
                cursor.execute(
                    "DELETE FROM `mercado_synced_orders` WHERE `token_id` = %s",
                    (int(token_id),),
                )
            cursor.execute(
                "DELETE FROM `mercado_store_site_settings` WHERE `token_id` = %s",
                (int(token_id),),
            )
            cursor.execute("DELETE FROM `mercado_store_tokens` WHERE `id` = %s", (int(token_id),))
            affected = cursor.rowcount
        connection.commit()
        return affected
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _ensure_window_anomalies_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS `window_anomalies` (
            `id` BIGINT NOT NULL AUTO_INCREMENT,
            `window_id` VARCHAR(64) NOT NULL,
            `window_name` VARCHAR(128) NULL,
            `site` VARCHAR(64) NULL,
            `anomaly_type` VARCHAR(64) NOT NULL DEFAULT '需要登录',
            `reason` LONGTEXT NULL,
            `source` VARCHAR(64) NULL,
            `active` TINYINT(1) NOT NULL DEFAULT 1,
            `occurrence_count` INT NOT NULL DEFAULT 1,
            `first_detected_at` DATETIME NOT NULL,
            `last_detected_at` DATETIME NOT NULL,
            `resolved_at` DATETIME NULL,
            PRIMARY KEY (`id`),
            UNIQUE KEY `uniq_window_anomaly_window` (`window_id`),
            KEY `idx_window_anomaly_active_time` (`active`, `last_detected_at`),
            KEY `idx_window_anomaly_name` (`window_name`)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )


def upsert_window_anomaly(
    window_id,
    window_name,
    site="",
    anomaly_type="需要登录",
    reason="",
    source="bit_daily_task",
):
    window_id = str(window_id or "").strip()
    if not window_id:
        raise ValueError("窗口异常缺少 window_id")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_window_anomalies_table(cursor)
            cursor.execute(
                """
                INSERT INTO `window_anomalies` (
                    `window_id`, `window_name`, `site`, `anomaly_type`, `reason`,
                    `source`, `active`, `occurrence_count`, `first_detected_at`,
                    `last_detected_at`, `resolved_at`
                ) VALUES (%s, %s, %s, %s, %s, %s, 1, 1, %s, %s, NULL)
                ON DUPLICATE KEY UPDATE
                    `window_name` = VALUES(`window_name`),
                    `site` = VALUES(`site`),
                    `anomaly_type` = VALUES(`anomaly_type`),
                    `reason` = VALUES(`reason`),
                    `source` = VALUES(`source`),
                    `active` = 1,
                    `occurrence_count` = `occurrence_count` + 1,
                    `last_detected_at` = VALUES(`last_detected_at`),
                    `resolved_at` = NULL
                """,
                (
                    window_id,
                    str(window_name or ""),
                    str(site or ""),
                    str(anomaly_type or "需要登录"),
                    str(reason or ""),
                    str(source or "bit_daily_task"),
                    now,
                    now,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def resolve_window_anomaly(window_id):
    window_id = str(window_id or "").strip()
    if not window_id:
        return 0
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_window_anomalies_table(cursor)
            cursor.execute(
                """
                UPDATE `window_anomalies`
                SET `active` = 0, `resolved_at` = %s
                WHERE `window_id` = %s AND `active` = 1
                """,
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), window_id),
            )
            affected = cursor.rowcount
        connection.commit()
        return affected
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_window_anomalies(active_only=True, limit=500):
    try:
        limit = max(1, min(int(limit), 1000))
    except (TypeError, ValueError):
        limit = 500
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_window_anomalies_table(cursor)
            where = "WHERE `active` = 1" if active_only else ""
            cursor.execute(
                f"""
                SELECT `id`, `window_id`, `window_name`, `site`, `anomaly_type`,
                       `reason`, `source`, `active`, `occurrence_count`,
                       `first_detected_at`, `last_detected_at`, `resolved_at`
                FROM `window_anomalies`
                {where}
                ORDER BY `active` DESC, `last_detected_at` DESC, `id` DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cursor.fetchall()
            for row in rows:
                for key in ("first_detected_at", "last_detected_at", "resolved_at"):
                    if row.get(key) is not None:
                        row[key] = str(row[key])
                row["active"] = bool(row.get("active"))
            return {"total": len(rows), "rows": rows}
    finally:
        connection.close()


if __name__ == "__main__":
    mysql_demo()
