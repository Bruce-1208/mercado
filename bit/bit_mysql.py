import json
import hashlib
import os
import re
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

import pymysql

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
            for item in _load_configured_shop_sites()
        }
    except Exception as exc:
        print(f"读取启用店铺范围失败，保留原快照：{exc}")
        return rows
    if not active_targets:
        return rows
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
        "一周流量趋势", "提交时间",
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
                elif len(row) > 13:
                    row = row[:13]
                if len(row) < 12:
                    row.extend([""] * (12 - len(row)))
                if len(row) == 12:
                    row.append(submit_time)
                elif len(row) == 13:
                    row[12] = submit_time
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
        系统告警, 更新时间, 一周流量趋势, 提交时间
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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


def _load_configured_shop_sites():
    configured = []
    seen = set()
    for row in list_bit_browser_configs(include_ignored=False):
        name = str(row.get("shop_name") or "").strip()
        if not name:
            continue
        for site in _split_config_sites(row.get("sites")):
            key = (name, site)
            if key in seen:
                continue
            seen.add(key)
            configured.append({"店铺名": name, "站点": site})
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
                for configured_site in _load_configured_shop_sites():
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

            for configured_site in _load_configured_shop_sites():
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


def get_latest_reputation_info():
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_column(cursor, "reputation", "取消率", "VARCHAR(255) NULL")
            _ensure_column(cursor, "reputation", "一周流量趋势", "TEXT NULL")
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
                            "店铺名": shop_name,
                            "站点": site,
                            "状态": task_status.get("状态") or "未知",
                            "状态时间": task_status.get("状态时间") or "",
                        }
                        for (shop_name, site), task_status in task_status_map.items()
                    ],
                    "rows": [],
                }

            # 有完整批次标记时读取该批次；兼容历史数据没有标记的情况时，
            # 按店铺和站点分别取最新记录，让补跑结果与此前成功结果一起展示。
            rows = _active_collection_snapshot_rows(
                _latest_reputation_snapshot_rows(cursor)
            )
            for row in rows:
                for key in ("更新时间", "提交时间"):
                    if row.get(key) is not None:
                        row[key] = str(row[key])
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
                    "店铺名": shop_name,
                    "站点": site,
                    "状态": task_status.get("状态") or "未知",
                    "状态时间": task_status.get("状态时间") or "",
                }
                for (shop_name, site), task_status in task_status_map.items()
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
        row[0] = order_id
        latest[order_id] = row
    return list(latest.values())


def insert_orders(line):
    rows = _deduplicate_order_rows(line)
    if not rows:
        return 0
    connection = pymysql.connect(**config)

    try:
        with connection.cursor() as cursor:
            _ensure_orders_unique_id(cursor)
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

        connection.commit()
        return len(rows)

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


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
    try:
        start_date = (
            datetime.strptime(date_from_text, "%Y-%m-%d")
            if date_from_text
            else None
        )
        end_date = (
            datetime.strptime(date_to_text, "%Y-%m-%d")
            if date_to_text
            else None
        )
    except ValueError as exc:
        raise ValueError("时间范围必须使用 YYYY-MM-DD 格式") from exc
    if start_date and end_date and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    limit = max(1, min(int(limit or 100), 500))
    order_conditions = ["`id` IS NOT NULL", "TRIM(`id`) <> ''"]
    params = []
    if start_date:
        order_conditions.append("`时间` >= %s")
        params.append(start_date.strftime("%Y-%m-%d 00:00:00"))
    if end_date:
        order_conditions.append("`时间` < %s")
        params.append((end_date + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"))
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
    try:
        start_date = (
            datetime.strptime(date_from_text, "%Y-%m-%d")
            if date_from_text
            else None
        )
        end_date = (
            datetime.strptime(date_to_text, "%Y-%m-%d")
            if date_to_text
            else None
        )
    except ValueError as exc:
        raise ValueError("时间范围必须使用 YYYY-MM-DD 格式") from exc
    if start_date and end_date and start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期")
    limit = max(1, min(int(limit or 100), 500))
    order_conditions = ["`id` IS NOT NULL", "TRIM(`id`) <> ''"]
    params = []
    if start_date:
        order_conditions.append("`时间` >= %s")
        params.append(start_date.strftime("%Y-%m-%d 00:00:00"))
    if end_date:
        order_conditions.append("`时间` < %s")
        params.append((end_date + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00"))
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
    """返回每个有效店铺配置站点的最新款项数据，并补充店铺归属人。"""
    owner_filter = str(salesperson or "").strip()
    configs = list_bit_browser_configs(include_ignored=False) or []
    configured_rows = []
    owners = set()
    for config_row in configs:
        shop_name = str(config_row.get("shop_name") or "").strip()
        window_id = str(config_row.get("window_id") or "").strip()
        owner = str(config_row.get("salesperson") or "").strip()
        display_owner = owner or "未分配"
        if not shop_name or (owner_filter and display_owner != owner_filter):
            continue
        owners.add(display_owner)
        sites = _split_config_sites(config_row.get("sites")) or [""]
        for site in sites:
            configured_rows.append(
                {
                    "window_id": window_id,
                    "店铺名": shop_name,
                    "店铺归属人": display_owner,
                    "站点": site,
                    "配置状态": str(config_row.get("status") or "").strip(),
                    "sequence_no": str(config_row.get("sequence_no") or "").strip(),
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
                            submit_time,
                        )
                    )
                    continue

                row = list(record)
                if len(row) >= 15:
                    normalized_list.append(tuple(row[:15] + [submit_time]))
                    continue
                if len(row) >= 13:
                    normalized_list.append(
                        tuple([row[0], "", ""] + row[1:13] + [submit_time])
                    )
                    continue
                if len(row) < 11:
                    row.extend([""] * (11 - len(row)))
                normalized_list.append(
                    tuple([row[0], "", "", "", ""] + row[1:11] + [submit_time])
                )

            if normalized_list:
                cursor.executemany(
                    """
                    INSERT INTO `zying_product` (
                        `产品编号`, `智赢分类编号`, `智赢产品分类`,
                        `分类编号`, `产品分类`, `主图链接`, `标题`, `售价`, `净收益`,
                        `包装毛重`, `包装尺寸`, `审核状态`, `采集页码`,
                        `采集时间`, `页面原始信息`, `提交时间`
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
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
                "SELECT `id` FROM `bit_browser_configs` WHERE `id` = %s",
                (config_id,),
            )
            if not cursor.fetchone():
                raise ValueError("店铺配置不存在")
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
    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_bit_browser_configs_table(cursor)
            where = "" if include_ignored else "WHERE COALESCE(`status`, '') NOT LIKE '%忽略%'"
            cursor.execute(
                f"""
                SELECT `id`, `window_id`, `shop_name`, `status`, `sites`, `sequence_no`,
                       `salesperson`, `email`, `created_at`, `updated_at`
                FROM `bit_browser_configs`
                {where}
                ORDER BY
                    CASE WHEN `sequence_no` REGEXP '^[0-9]+$' THEN CAST(`sequence_no` AS UNSIGNED) ELSE 999999999 END,
                    `id`
                """
            )
            rows = cursor.fetchall()
            return [_serialize_bit_browser_config_row(row) for row in rows]
    finally:
        connection.close()


def get_bit_browser_config(shop_name="", window_id="", include_ignored=True):
    shop_name = _config_text(shop_name)
    window_id = _config_text(window_id)
    if not shop_name and not window_id:
        return None

    clauses = []
    params = []
    if window_id:
        clauses.append("`window_id` = %s")
        params.append(window_id)
    if shop_name:
        clauses.append("`shop_name` = %s")
        params.append(shop_name)
    if not include_ignored:
        clauses.append("COALESCE(`status`, '') NOT LIKE '%忽略%'")

    connection = pymysql.connect(**config)
    try:
        with connection.cursor() as cursor:
            _ensure_bit_browser_configs_table(cursor)
            cursor.execute(
                f"""
                SELECT `id`, `window_id`, `shop_name`, `status`, `sites`, `sequence_no`,
                       `salesperson`, `email`, `created_at`, `updated_at`
                FROM `bit_browser_configs`
                WHERE {' AND '.join(clauses)}
                LIMIT 1
                """,
                tuple(params),
            )
            row = cursor.fetchone()
            return _serialize_bit_browser_config_row(row)
    finally:
        connection.close()


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
        print(f"AI申诉汇总记录写入失败，已回滚: {e}")
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
