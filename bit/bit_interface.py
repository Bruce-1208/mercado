import queue
from collections import deque
import functools
import hashlib
import hmac
import json
import os
import platform
import secrets
import signal
import subprocess
import sys
import threading
import time
import traceback
from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from flask import Flask, Response, request, render_template, jsonify, send_file, session, redirect, url_for, g
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for path in (str(CURRENT_DIR), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def resolve_template_dir():
    bundle_root = Path(getattr(sys, "_MEIPASS", CURRENT_DIR))
    candidates = (
        CURRENT_DIR / "templates",
        CURRENT_DIR / "bit" / "templates",
        bundle_root / "bit" / "templates",
        bundle_root / "templates",
        PROJECT_ROOT / "bit" / "templates",
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return CURRENT_DIR / "templates"

import bit.bit_appeal_ai as bit_appeal_ai
import bit.bit_check_risk as bit_check_risk
import bit.bit_db_api as bit_db_api
import bit.bit_daily_task as bit_daily_task
import bit.bit_infractions_info as bit_infractions_info
import bit.bit_pago_info as bit_pago_info
import bit.bit_print as bit_print
import bit.bit_reputation_info as bit_reputation_info
import bit.bit_update_orders as bit_update_orders
import bit.bit_zying_caiji as bit_zying_caiji
from bit.bit_appeal import *
from bit.bit_collection_control import DEFAULT_COLLECTION_MAX_WORKERS
from bit.bit_config import split_config_sites
from bit.bit_runtime_lock import create_window_lease, get_lock_owner
from bit.bit_mercado_login import (
    MERCADO_LOGIN_JOB_LOCK_KEY,
    is_human_verification_result,
    is_login_blocking_result,
)
from bit.bit_utils import *
from bit.bit_api import *

# 引入数据库入库需要的模块
import logging
from decimal import Decimal
from datetime import datetime, timedelta
# from db_pool import get_db_connection  # 确保你的连接池文件在这个目录下

app = Flask(__name__, template_folder=str(resolve_template_dir()))
app.secret_key = os.environ.get("WORKBENCH_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

PASSWORD_ITERATIONS = 260000

WORKBENCH_PERMISSION_GROUPS = (
    ("appeal", "自动化 AI 申诉", (("appeal.view", "查看"), ("appeal.execute", "执行/终止"))),
    ("tasks", "任务模块", (("tasks.view", "查看"), ("tasks.execute", "启动任务"))),
    ("order_print", "订单打印", (("order_print.view", "查看"), ("order_print.execute", "执行/终止"))),
    ("order_analysis", "订单分析", (("order_analysis.view", "查看"), ("order_analysis.execute", "导入订单"))),
    ("shop_status", "店铺状态", (("shop_status.view", "查看"), ("shop_status.execute", "检测/处理"))),
    ("funds", "资金管理", (("funds.view", "查看"), ("funds.execute", "采集/终止"))),
    ("zying_collection", "智赢产品采集", (("zying_collection.view", "查看"), ("zying_collection.execute", "执行采集"))),
    ("risk_check", "侵权检测", (("risk_check.view", "查看"), ("risk_check.execute", "执行检测"))),
    ("infractions", "侵权数据", (("infractions.view", "查看/导出"), ("infractions.execute", "采集"))),
    ("reputation", "声誉数据", (("reputation.view", "查看/导出"), ("reputation.execute", "采集/更新"))),
    ("ai_appeals", "AI 申诉记录", (("ai_appeals.view", "查看"),)),
    ("access", "人员与权限", (("access.view", "查看"), ("access.manage", "管理账号、角色和店铺配置"))),
)
WORKBENCH_PERMISSION_KEYS = tuple(
    permission_key
    for _, _, permissions in WORKBENCH_PERMISSION_GROUPS
    for permission_key, _ in permissions
)
WORKBENCH_DEFAULT_ROLES = (
    {
        "role_key": "super_admin",
        "role_name": "超级管理员",
        "description": "拥有控制台全部权限",
        "permissions": ("*",),
        "is_system": True,
    },
    {
        "role_key": "operator",
        "role_name": "运营人员",
        "description": "可查看并执行各业务模块，不可管理账号与权限",
        "permissions": tuple(
            key for key in WORKBENCH_PERMISSION_KEYS if not key.startswith("access.")
        ),
        "is_system": True,
    },
    {
        "role_key": "viewer",
        "role_name": "只读人员",
        "description": "只能查看业务数据，不能执行任务或修改配置",
        "permissions": tuple(
            key for key in WORKBENCH_PERMISSION_KEYS if key.endswith(".view")
        ),
        "is_system": True,
    },
)


def _truthy_env(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _resolve_use_db_api():
    mode = os.environ.get("BIT_INTERFACE_DB_MODE", "").strip().lower()
    if mode in ("direct", "local", "server", "mysql"):
        return False
    if mode in ("api", "client", "remote"):
        return True

    use_db_api = os.environ.get("BIT_INTERFACE_USE_DB_API")
    if use_db_api is not None:
        return _truthy_env(use_db_api)

    # 默认平台策略：macOS 作为数据库服务端直连 MySQL；Windows 作为客户端走接口。
    return platform.system() != "Darwin"


USE_DB_API = _resolve_use_db_api()

if USE_DB_API:
    db_list_bit_browser_configs = bit_db_api.list_bit_browser_configs
    db_get_bit_browser_config = bit_db_api.get_bit_browser_config
    db_upsert_bit_browser_configs = bit_db_api.upsert_bit_browser_configs
    db_create_bit_browser_config = bit_db_api.create_bit_browser_config
    db_update_bit_browser_config = bit_db_api.update_bit_browser_config
    db_delete_bit_browser_config = bit_db_api.delete_bit_browser_config
    db_get_latest_infraction_info = bit_db_api.get_latest_infraction_info
    db_get_latest_order_print_records = bit_db_api.get_latest_order_print_records
    db_get_latest_pago_info = bit_db_api.get_latest_pago_info
    db_get_latest_reputation_info = bit_db_api.get_latest_reputation_info
    db_get_ai_appeal_records = bit_db_api.get_ai_appeal_records
    db_list_appeal_phrases = bit_db_api.list_appeal_phrases
    db_get_random_appeal_phrase = bit_db_api.get_random_appeal_phrase
    db_create_appeal_phrase = bit_db_api.create_appeal_phrase
    db_update_appeal_phrase = bit_db_api.update_appeal_phrase
    db_delete_appeal_phrase = bit_db_api.delete_appeal_phrase
    db_get_window_anomalies = bit_db_api.get_window_anomalies
    db_insert_chat_info = bit_db_api.insert_chat_info
    db_insert_appeal_chat_record = bit_db_api.insert_appeal_chat_record
    db_insert_ai_appeal_record = bit_db_api.insert_ai_appeal_record
    db_insert_orders = bit_db_api.insert_orders
    db_get_high_after_sale_alerts = bit_db_api.get_high_after_sale_alerts
    db_get_high_profit_products = bit_db_api.get_high_profit_products
    db_insert_task_record = bit_db_api.insert_task_record
    db_insert_zying_product_info = bit_db_api.insert_zying_product_info
    db_get_existing_zying_product_ids = bit_db_api.get_existing_zying_product_ids
    db_get_zying_risk_candidates = bit_db_api.get_zying_risk_candidates
    db_update_zying_product_risks = bit_db_api.update_zying_product_risks
    db_list_zying_risk_categories = bit_db_api.list_zying_risk_categories
    db_get_zying_risk_results = bit_db_api.get_zying_risk_results
    db_resolve_window_anomaly = bit_db_api.resolve_window_anomaly
    db_upsert_window_anomaly = bit_db_api.upsert_window_anomaly
    db_inset_delay_info = bit_db_api.inset_delay_info
    db_inset_infraction_info = bit_db_api.inset_infraction_info
    db_inset_pago_info = bit_db_api.inset_pago_info
    db_inset_reputation_info = bit_db_api.inset_reputation_info
else:
    import pymysql
    from bit.bit_mysql import config as mysql_config
    from bit.bit_mysql import (
        list_bit_browser_configs,
        get_bit_browser_config,
        upsert_bit_browser_configs,
        create_bit_browser_config,
        update_bit_browser_config,
        delete_bit_browser_config,
        get_latest_infraction_info,
        get_latest_order_print_records,
        get_latest_pago_info,
        get_latest_reputation_info,
        get_ai_appeal_records,
        list_appeal_phrases,
        get_random_appeal_phrase,
        create_appeal_phrase,
        update_appeal_phrase,
        delete_appeal_phrase,
        get_window_anomalies,
        insert_chat_info,
        insert_appeal_chat_record,
        insert_ai_appeal_record,
        insert_orders,
        get_high_after_sale_alerts,
        get_high_profit_products,
        insert_task_record,
        insert_zying_product_info,
        get_existing_zying_product_ids,
        get_zying_risk_candidates,
        update_zying_product_risks,
        list_zying_risk_categories,
        get_zying_risk_results,
        resolve_window_anomaly,
        inset_delay_info,
        inset_infraction_info,
        inset_pago_info,
        inset_reputation_info,
        upsert_window_anomaly,
    )

    db_list_bit_browser_configs = list_bit_browser_configs
    db_get_bit_browser_config = get_bit_browser_config
    db_upsert_bit_browser_configs = upsert_bit_browser_configs
    db_create_bit_browser_config = create_bit_browser_config
    db_update_bit_browser_config = update_bit_browser_config
    db_delete_bit_browser_config = delete_bit_browser_config
    db_get_latest_infraction_info = get_latest_infraction_info
    db_get_latest_order_print_records = get_latest_order_print_records
    db_get_latest_pago_info = get_latest_pago_info
    db_get_latest_reputation_info = get_latest_reputation_info
    db_get_ai_appeal_records = get_ai_appeal_records
    db_list_appeal_phrases = list_appeal_phrases
    db_get_random_appeal_phrase = get_random_appeal_phrase
    db_create_appeal_phrase = create_appeal_phrase
    db_update_appeal_phrase = update_appeal_phrase
    db_delete_appeal_phrase = delete_appeal_phrase
    db_get_window_anomalies = get_window_anomalies
    db_insert_chat_info = insert_chat_info
    db_insert_appeal_chat_record = insert_appeal_chat_record
    db_insert_ai_appeal_record = insert_ai_appeal_record
    db_insert_orders = insert_orders
    db_get_high_after_sale_alerts = get_high_after_sale_alerts
    db_get_high_profit_products = get_high_profit_products
    db_insert_task_record = insert_task_record
    db_insert_zying_product_info = insert_zying_product_info
    db_get_existing_zying_product_ids = get_existing_zying_product_ids
    db_get_zying_risk_candidates = get_zying_risk_candidates
    db_update_zying_product_risks = update_zying_product_risks
    db_list_zying_risk_categories = list_zying_risk_categories
    db_get_zying_risk_results = get_zying_risk_results
    db_resolve_window_anomaly = resolve_window_anomaly
    db_upsert_window_anomaly = upsert_window_anomaly
    db_inset_delay_info = inset_delay_info
    db_inset_infraction_info = inset_infraction_info
    db_inset_pago_info = inset_pago_info
    db_inset_reputation_info = inset_reputation_info


def make_password_hash(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        salt.encode("utf-8"),
        PASSWORD_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_ITERATIONS}${salt}${digest}"


def verify_password(password, password_hash):
    try:
        method, iterations_text, salt, expected = str(password_hash or "").split("$", 3)
        if method != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            str(password).encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations_text),
        ).hex()
        return hmac.compare_digest(digest, expected)
    except Exception:
        return False


def _normalize_workbench_permissions(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = [value]
    if not isinstance(value, (list, tuple, set)):
        return []
    permissions = []
    for item in value:
        permission = str(item or "").strip()
        if permission and permission not in permissions:
            permissions.append(permission)
    return permissions


def workbench_permission_catalog():
    return [
        {
            "module": module,
            "label": label,
            "permissions": [
                {"key": permission_key, "label": permission_label}
                for permission_key, permission_label in permissions
            ],
        }
        for module, label, permissions in WORKBENCH_PERMISSION_GROUPS
    ]


def _validate_workbench_permissions(permissions):
    normalized = _normalize_workbench_permissions(permissions)
    invalid = [
        permission
        for permission in normalized
        if permission not in WORKBENCH_PERMISSION_KEYS
    ]
    if invalid:
        raise ValueError("包含不支持的权限：" + "、".join(invalid))
    for permission in tuple(normalized):
        if permission.endswith(".execute") or permission.endswith(".manage"):
            view_permission = permission.rsplit(".", 1)[0] + ".view"
            if view_permission in WORKBENCH_PERMISSION_KEYS and view_permission not in normalized:
                normalized.append(view_permission)
    return normalized


def _validate_workbench_password(password, required=True):
    password = str(password or "")
    if not password and not required:
        return ""
    if len(password) < 8:
        raise ValueError("密码至少需要 8 位")
    return password


def _validate_workbench_username(username):
    username = str(username or "").strip()
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")
    if not 3 <= len(username) <= 64:
        raise ValueError("账号长度需要为 3–64 位")
    if any(character not in allowed for character in username):
        raise ValueError("账号只支持字母、数字、点、下划线和短横线")
    return username


def ensure_workbench_user_table():
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS `workbench_roles` (
                    `role_key` VARCHAR(64) NOT NULL,
                    `role_name` VARCHAR(64) NOT NULL,
                    `description` VARCHAR(255) NULL,
                    `permissions_json` TEXT NOT NULL,
                    `is_system` TINYINT(1) NOT NULL DEFAULT 0,
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`role_key`),
                    UNIQUE KEY `uniq_workbench_role_name` (`role_name`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for role in WORKBENCH_DEFAULT_ROLES:
                cursor.execute(
                    """
                    INSERT INTO `workbench_roles`
                        (`role_key`, `role_name`, `description`, `permissions_json`,
                         `is_system`, `created_at`, `updated_at`)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        `role_name` = VALUES(`role_name`),
                        `description` = VALUES(`description`),
                        `permissions_json` = VALUES(`permissions_json`),
                        `is_system` = VALUES(`is_system`),
                        `updated_at` = VALUES(`updated_at`)
                    """,
                    (
                        role["role_key"],
                        role["role_name"],
                        role["description"],
                        json.dumps(role["permissions"], ensure_ascii=False),
                        1 if role["is_system"] else 0,
                        now,
                        now,
                    ),
                )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS `workbench_users` (
                    `id` INT NOT NULL AUTO_INCREMENT,
                    `username` VARCHAR(64) NOT NULL,
                    `password_hash` VARCHAR(255) NOT NULL,
                    `display_name` VARCHAR(64) NULL,
                    `email` VARCHAR(128) NULL,
                    `department` VARCHAR(64) NULL,
                    `role_key` VARCHAR(64) NOT NULL DEFAULT 'viewer',
                    `is_active` TINYINT(1) NOT NULL DEFAULT 1,
                    `created_at` DATETIME NOT NULL,
                    `updated_at` DATETIME NOT NULL,
                    PRIMARY KEY (`id`),
                    UNIQUE KEY `uniq_workbench_username` (`username`)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                """
            )
            for column_name, column_definition in (
                ("email", "VARCHAR(128) NULL"),
                ("department", "VARCHAR(64) NULL"),
                ("role_key", "VARCHAR(64) NULL"),
            ):
                cursor.execute(
                    "SHOW COLUMNS FROM `workbench_users` LIKE %s",
                    (column_name,),
                )
                if not cursor.fetchone():
                    cursor.execute(
                        f"ALTER TABLE `workbench_users` ADD COLUMN `{column_name}` "
                        f"{column_definition}"
                    )
            cursor.execute(
                """
                UPDATE `workbench_users`
                SET `role_key` = 'super_admin'
                WHERE `role_key` IS NULL OR `role_key` = ''
                """
            )
            cursor.execute("SELECT COUNT(*) AS total FROM `workbench_users`")
            total = (cursor.fetchone() or {}).get("total") or 0
            if total == 0:
                username = os.environ.get("WORKBENCH_DEFAULT_USER", "admin")
                password = os.environ.get("WORKBENCH_DEFAULT_PASSWORD", "admin123456")
                cursor.execute(
                    """
                    INSERT INTO `workbench_users`
                        (`username`, `password_hash`, `display_name`, `role_key`,
                         `is_active`, `created_at`, `updated_at`)
                    VALUES (%s, %s, %s, 'super_admin', 1, %s, %s)
                    """,
                    (username, make_password_hash(password), "管理员", now, now),
                )
                logging.warning("workbench_users 为空，已创建默认账号 %s", username)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_workbench_user(username="", user_id=None):
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            where_clause = "u.`id` = %s" if user_id is not None else "u.`username` = %s"
            lookup_value = user_id if user_id is not None else username
            cursor.execute(
                f"""
                SELECT u.`id`, u.`username`, u.`password_hash`, u.`display_name`,
                       u.`email`, u.`department`, u.`role_key`, u.`is_active`,
                       r.`role_name`, r.`permissions_json`
                FROM `workbench_users` AS u
                LEFT JOIN `workbench_roles` AS r ON r.`role_key` = u.`role_key`
                WHERE {where_clause}
                LIMIT 1
                """,
                (lookup_value,),
            )
            return cursor.fetchone()
    finally:
        connection.close()


def build_workbench_session_user(user):
    role_key = str(user.get("role_key") or "viewer")
    permissions = (
        ["*"]
        if role_key == "super_admin"
        else _normalize_workbench_permissions(user.get("permissions_json"))
    )
    return {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "email": user.get("email") or "",
        "department": user.get("department") or "",
        "role_key": role_key,
        "role_name": user.get("role_name") or role_key,
        "permissions": permissions,
        "access_version": 1,
    }


def authenticate_workbench_user(username, password):
    if USE_DB_API:
        return bit_db_api.login_workbench_user(username, password)

    user = get_workbench_user(username)
    if not user or not user.get("is_active") or not verify_password(password, user.get("password_hash")):
        return None
    return build_workbench_session_user(user)


def _role_row_to_dict(row):
    row = dict(row or {})
    row["permissions"] = _normalize_workbench_permissions(
        row.pop("permissions_json", [])
    )
    row["is_system"] = bool(row.get("is_system"))
    row["user_count"] = int(row.get("user_count") or 0)
    return row


def list_workbench_roles_local():
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.`role_key`, r.`role_name`, r.`description`,
                       r.`permissions_json`, r.`is_system`, r.`created_at`,
                       r.`updated_at`, COUNT(u.`id`) AS `user_count`
                FROM `workbench_roles` AS r
                LEFT JOIN `workbench_users` AS u ON u.`role_key` = r.`role_key`
                GROUP BY r.`role_key`, r.`role_name`, r.`description`,
                         r.`permissions_json`, r.`is_system`, r.`created_at`,
                         r.`updated_at`
                ORDER BY FIELD(r.`role_key`, 'super_admin', 'operator', 'viewer'),
                         r.`role_name`
                """
            )
            return [_role_row_to_dict(row) for row in (cursor.fetchall() or [])]
    finally:
        connection.close()


def create_workbench_role_local(data):
    role_name = str((data or {}).get("role_name") or "").strip()
    if not role_name or len(role_name) > 64:
        raise ValueError("角色名称不能为空且最多 64 个字符")
    description = str((data or {}).get("description") or "").strip()[:255]
    permissions = _validate_workbench_permissions((data or {}).get("permissions"))
    role_key = "role_" + secrets.token_hex(8)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO `workbench_roles`
                    (`role_key`, `role_name`, `description`, `permissions_json`,
                     `is_system`, `created_at`, `updated_at`)
                VALUES (%s, %s, %s, %s, 0, %s, %s)
                """,
                (
                    role_key,
                    role_name,
                    description,
                    json.dumps(permissions, ensure_ascii=False),
                    now,
                    now,
                ),
            )
        connection.commit()
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("角色名称已存在") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return role_key


def update_workbench_role_local(role_key, data):
    role_key = str(role_key or "").strip()
    role_name = str((data or {}).get("role_name") or "").strip()
    if not role_name or len(role_name) > 64:
        raise ValueError("角色名称不能为空且最多 64 个字符")
    description = str((data or {}).get("description") or "").strip()[:255]
    permissions = _validate_workbench_permissions((data or {}).get("permissions"))
    if role_key == "super_admin":
        permissions = ["*"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT `role_key` FROM `workbench_roles` WHERE `role_key` = %s",
                (role_key,),
            )
            if not cursor.fetchone():
                raise ValueError("角色不存在")
            cursor.execute(
                """
                UPDATE `workbench_roles`
                SET `role_name` = %s, `description` = %s,
                    `permissions_json` = %s, `updated_at` = %s
                WHERE `role_key` = %s
                """,
                (
                    role_name,
                    description,
                    json.dumps(permissions, ensure_ascii=False),
                    now,
                    role_key,
                ),
            )
        connection.commit()
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("角色名称已存在") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def delete_workbench_role_local(role_key):
    role_key = str(role_key or "").strip()
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT r.`is_system`, COUNT(u.`id`) AS `user_count`
                FROM `workbench_roles` AS r
                LEFT JOIN `workbench_users` AS u ON u.`role_key` = r.`role_key`
                WHERE r.`role_key` = %s
                GROUP BY r.`role_key`, r.`is_system`
                """,
                (role_key,),
            )
            role = cursor.fetchone()
            if not role:
                raise ValueError("角色不存在")
            if role.get("is_system"):
                raise ValueError("系统角色不能删除")
            if int(role.get("user_count") or 0) > 0:
                raise ValueError("该角色仍有关联账号，不能删除")
            cursor.execute(
                "DELETE FROM `workbench_roles` WHERE `role_key` = %s",
                (role_key,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_workbench_users_local():
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT u.`id`, u.`username`, u.`display_name`, u.`email`,
                       u.`department`, u.`role_key`, u.`is_active`,
                       u.`created_at`, u.`updated_at`, r.`role_name`
                FROM `workbench_users` AS u
                LEFT JOIN `workbench_roles` AS r ON r.`role_key` = u.`role_key`
                ORDER BY u.`is_active` DESC, u.`id`
                """
            )
            rows = [dict(row) for row in (cursor.fetchall() or [])]
            for row in rows:
                row["is_active"] = bool(row.get("is_active"))
            return rows
    finally:
        connection.close()


def _require_workbench_role(cursor, role_key):
    cursor.execute(
        "SELECT `role_key` FROM `workbench_roles` WHERE `role_key` = %s",
        (role_key,),
    )
    if not cursor.fetchone():
        raise ValueError("所选角色不存在")


def create_workbench_user_local(data):
    data = data or {}
    username = _validate_workbench_username(data.get("username"))
    password = _validate_workbench_password(data.get("password"))
    display_name = str(data.get("display_name") or username).strip()[:64]
    email = str(data.get("email") or "").strip()[:128]
    department = str(data.get("department") or "").strip()[:64]
    role_key = str(data.get("role_key") or "viewer").strip()
    is_active = 1 if data.get("is_active", True) else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            _require_workbench_role(cursor, role_key)
            cursor.execute(
                """
                INSERT INTO `workbench_users`
                    (`username`, `password_hash`, `display_name`, `email`,
                     `department`, `role_key`, `is_active`, `created_at`, `updated_at`)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    username,
                    make_password_hash(password),
                    display_name,
                    email,
                    department,
                    role_key,
                    is_active,
                    now,
                    now,
                ),
            )
            user_id = cursor.lastrowid
        connection.commit()
        return user_id
    except pymysql.err.IntegrityError as exc:
        connection.rollback()
        raise ValueError("账号已存在") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_workbench_user_local(user_id, data):
    data = data or {}
    display_name = str(data.get("display_name") or "").strip()[:64]
    email = str(data.get("email") or "").strip()[:128]
    department = str(data.get("department") or "").strip()[:64]
    role_key = str(data.get("role_key") or "viewer").strip()
    is_active = 1 if data.get("is_active", True) else 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT `id`, `role_key`, `is_active`
                FROM `workbench_users` WHERE `id` = %s
                """,
                (user_id,),
            )
            current = cursor.fetchone()
            if not current:
                raise ValueError("账号不存在")
            _require_workbench_role(cursor, role_key)
            removing_active_admin = (
                current.get("role_key") == "super_admin"
                and bool(current.get("is_active"))
                and (role_key != "super_admin" or not is_active)
            )
            if removing_active_admin:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS total FROM `workbench_users`
                    WHERE `role_key` = 'super_admin' AND `is_active` = 1
                    """
                )
                if int((cursor.fetchone() or {}).get("total") or 0) <= 1:
                    raise ValueError("至少需要保留一个启用状态的超级管理员")
            cursor.execute(
                """
                UPDATE `workbench_users`
                SET `display_name` = %s, `email` = %s, `department` = %s,
                    `role_key` = %s, `is_active` = %s, `updated_at` = %s
                WHERE `id` = %s
                """,
                (
                    display_name,
                    email,
                    department,
                    role_key,
                    is_active,
                    now,
                    user_id,
                ),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reset_workbench_user_password_local(user_id, password):
    password = _validate_workbench_password(password)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    connection = pymysql.connect(**mysql_config)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE `workbench_users`
                SET `password_hash` = %s, `updated_at` = %s
                WHERE `id` = %s
                """,
                (make_password_hash(password), now, user_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("账号不存在")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _workbench_backend(function_name, *args):
    if USE_DB_API:
        return getattr(bit_db_api, function_name)(*args)
    return globals()[f"{function_name}_local"](*args)


def get_current_workbench_user():
    cached = getattr(g, "workbench_user", None)
    if cached is not None:
        return cached
    session_user = session.get("workbench_user")
    if not session_user:
        return None
    # 兼容升级前的会话和测试注入会话；新登录会话会实时读取账号状态和角色权限。
    if session_user.get("access_version") != 1:
        g.workbench_user = dict(session_user)
        return g.workbench_user
    if USE_DB_API:
        user = bit_db_api.get_workbench_session_user(session_user.get("id"))
    else:
        row = get_workbench_user(user_id=session_user.get("id"))
        user = (
            build_workbench_session_user(row)
            if row and row.get("is_active")
            else None
        )
    if not user:
        session.clear()
        return None
    session["workbench_user"] = user
    g.workbench_user = user
    return user


def workbench_user_has_permission(user, permission):
    if not user:
        return False
    if user.get("access_version") != 1:
        return True
    permissions = set(_normalize_workbench_permissions(user.get("permissions")))
    return "*" in permissions or permission in permissions


def login_required(view_func):
    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        if get_current_workbench_user():
            return view_func(*args, **kwargs)
        if request.path.startswith("/api/"):
            return jsonify({"status": "error", "message": "请先登录"}), 401
        return redirect(url_for("login_page", next=request.full_path))

    return wrapper


def internal_api_required(view_func):
    @functools.wraps(view_func)
    def wrapper(*args, **kwargs):
        token = os.environ.get("BIT_DB_API_TOKEN", "")
        request_token = request.headers.get("X-Internal-Token", "")
        if token and hmac.compare_digest(token, request_token):
            return view_func(*args, **kwargs)
        if request.remote_addr in ("127.0.0.1", "::1", "localhost"):
            return view_func(*args, **kwargs)
        return jsonify({"status": "error", "message": "Forbidden"}), 403

    return wrapper


def _required_workbench_permissions(path, method):
    method = str(method or "GET").upper()
    if path.startswith("/api/access/"):
        return ("access.view",) if method == "GET" else ("access.manage",)
    if path.startswith("/api/appeal-phrases"):
        return ("appeal.view",) if method == "GET" else ("appeal.execute",)
    if path.startswith("/api/run_shensu"):
        return ("appeal.execute",)
    if path == "/api/collections/options":
        return (
            "appeal.view",
            "order_print.view",
            "infractions.view",
            "reputation.view",
        )
    if path.startswith("/api/infractions/"):
        return (
            ("infractions.execute",)
            if path == "/api/infractions/collect" and method == "POST"
            else ("infractions.view",)
        )
    if path.startswith("/api/reputation/"):
        return (
            ("reputation.execute",)
            if method == "POST"
            else ("reputation.view",)
        )
    if path.startswith("/api/funds/"):
        return (
            ("funds.execute",)
            if method == "POST"
            else ("funds.view",)
        )
    if path.startswith("/api/order-print/"):
        return (
            ("order_print.execute",)
            if method == "POST"
            else ("order_print.view",)
        )
    if path.startswith("/api/order-analysis/"):
        return (
            ("order_analysis.execute",)
            if method == "POST"
            else ("order_analysis.view",)
        )
    if path.startswith("/api/tasks/daily/"):
        return (
            ("tasks.execute",)
            if path.endswith("/start") and method == "POST"
            else ("tasks.view",)
        )
    if path.startswith("/api/risk-check/"):
        return (
            ("risk_check.execute",)
            if path.endswith("/start") and method == "POST"
            else ("risk_check.view",)
        )
    if path.startswith("/api/zying-collection/"):
        return (
            ("zying_collection.execute",)
            if path.endswith("/start") and method == "POST"
            else ("zying_collection.view",)
        )
    if path == "/api/ai-appeal-records":
        return ("ai_appeals.view",)
    if path.startswith("/api/window-anomalies"):
        return (
            ("shop_status.execute",)
            if method == "POST"
            else ("shop_status.view",)
        )
    return ()


@app.before_request
def enforce_workbench_permissions():
    path = request.path
    if not path.startswith("/api/") or path.startswith("/api/db/"):
        return None
    if path in ("/api/login",):
        return None
    required_permissions = _required_workbench_permissions(path, request.method)
    if not required_permissions:
        return None
    user = get_current_workbench_user()
    if not user:
        return jsonify({"status": "error", "message": "请先登录"}), 401
    if not any(
        workbench_user_has_permission(user, permission)
        for permission in required_permissions
    ):
        return jsonify(
            {
                "status": "error",
                "message": "当前账号没有执行该操作的权限",
                "required_permissions": list(required_permissions),
            }
        ), 403
    return None


def reject_db_api_client_mode():
    if USE_DB_API:
        return jsonify({
            "status": "error",
            "message": "当前 bit_interface 是数据库接口客户端模式；数据库服务器端请设置 BIT_INTERFACE_DB_MODE=direct 后再启动。"
        }), 503
    return None


if USE_DB_API:
    logging.info("bit_interface 使用数据库接口模式：%s", bit_db_api.DB_API_BASE_URL)
else:
    logging.info("bit_interface 使用本地 MySQL 直连模式：%s", mysql_config.get("host"))
    try:
        ensure_workbench_user_table()
    except Exception as e:
        logging.error("初始化工作台登录表失败: %s", e)


# 1. 核心逻辑方法：改造成生成器
_original_stdout = sys.stdout
_original_stderr = sys.stderr
_thread_log_queues = {}
_thread_log_lock = threading.Lock()
_infraction_collect_lock = threading.Lock()
_infraction_collect_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "params": {},
}
_reputation_collect_lock = threading.Lock()
_reputation_collect_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "operation": "",
    "params": {},
}
_risk_check_lock = threading.Lock()
_risk_check_state_lock = threading.RLock()
_risk_check_logs = deque(maxlen=500)
_risk_check_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "params": {},
    "summary": {},
}
_zying_collection_lock = threading.Lock()
_zying_collection_state_lock = threading.RLock()
_zying_collection_logs = deque(maxlen=800)
_zying_collection_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "params": {},
    "summary": {},
}
_fund_collect_lock = threading.Lock()
_fund_collect_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "scope": "",
    "target": "",
    "collected_count": 0,
}
_fund_collect_stop_event = None
_order_print_lock = threading.RLock()
_order_print_stop_event = None
_order_print_logs = deque(maxlen=1000)
_order_print_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "params": {},
    "printed": 0,
    "no_orders": 0,
    "failed": 0,
    "skipped": 0,
    "results": [],
    "site_last_runs": [],
}
_order_analysis_import_lock = threading.Lock()
_daily_task_lock = threading.Lock()
_daily_task_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "params": {},
}
_mercado_login_task_lock = threading.RLock()
_mercado_login_task_process = None
_mercado_login_task_processes = {}
_mercado_login_tasks = {}
MERCADO_LOGIN_TASK_HISTORY_LIMIT = 100
MERCADO_LOGIN_STOP_GRACE_SECONDS = 8
DEFAULT_MERCADO_LOGIN_WORKERS = 3
MAX_MERCADO_LOGIN_WORKERS = 10
_mercado_login_log_path = CURRENT_DIR / "logs" / "bit_mercado_login_console.log"
MERCADO_LOGIN_SINGLE_MANUAL_WAIT_SECONDS = 20 * 60
MERCADO_LOGIN_SELECTED_MANUAL_WAIT_SECONDS = 20 * 60


def _read_recent_mercado_login_logs(max_bytes=256 * 1024, max_lines=800):
    try:
        with _mercado_login_log_path.open("rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            size = log_file.tell()
            log_file.seek(max(0, size - max_bytes), os.SEEK_SET)
            content = log_file.read().decode("utf-8", errors="replace")
        lines = content.splitlines(keepends=True)[-max_lines:]
        for index in range(len(lines) - 1, -1, -1):
            if "===== bit_mercado_login 启动：" in lines[index]:
                return lines[index:]
        return lines
    except OSError:
        return []


_mercado_login_task_logs = deque(
    _read_recent_mercado_login_logs(),
    maxlen=800,
)
_mercado_login_task_state = {
    "running": False,
    "started_at": "",
    "finished_at": "",
    "status": "idle",
    "message": "等待启动",
    "target": "全部未忽略店铺",
    "window_id": "",
    "window_ids": [],
    "pid": None,
    "returncode": None,
    "log_path": str(_mercado_login_log_path),
}

APPEAL_SITES = ("墨西哥", "巴西", "哥伦比亚", "智利", "阿根廷", "乌拉圭")
APPEAL_FORMS = ("延误", "侵权", "取消率", "投诉")
APPEAL_LOOP_COUNTS = (10, 20, 50)
DEFAULT_APPEAL_LOOP_COUNT = 10
PERMANENT_APPEAL_LOOP_COUNT = 0
AI_APPEAL_ROUND_INTERVAL_SECONDS = 60
MANUAL_APPEAL_ROUND_INTERVAL_SECONDS = 600
# 保留旧常量供其他模块调用，默认值等同人工客服的轮次间隔。
APPEAL_ROUND_INTERVAL_SECONDS = MANUAL_APPEAL_ROUND_INTERVAL_SECONDS
APPEAL_STREAM_HEARTBEAT_SECONDS = 15
_appeal_task_lock = threading.Lock()
_appeal_tasks = {}


def normalize_appeal_task_id(task_id):
    task_id = str(task_id or "").strip()[:96]
    if not task_id:
        return ""
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
    return task_id if all(char in allowed for char in task_id) else ""


def register_appeal_task(task_id, metadata=None):
    task_id = normalize_appeal_task_id(task_id)
    if not task_id:
        raise ValueError("任务编号格式无效")
    with _appeal_task_lock:
        if task_id in _appeal_tasks:
            return None
        stop_event = threading.Event()
        _appeal_tasks[task_id] = {
            "stop_event": stop_event,
            "status": "running",
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            **(metadata or {}),
        }
        return stop_event


def request_appeal_task_stop(task_id):
    task_id = normalize_appeal_task_id(task_id)
    with _appeal_task_lock:
        task = _appeal_tasks.get(task_id)
        if task is None:
            return False
        task["status"] = "stopping"
        task["stop_event"].set()
        return True


def finish_appeal_task(task_id):
    task_id = normalize_appeal_task_id(task_id)
    with _appeal_task_lock:
        _appeal_tasks.pop(task_id, None)


class ThreadLogStream:
    def __init__(self, original_stream):
        self.original_stream = original_stream

    def write(self, text):
        if text:
            if isinstance(text, bytes):
                encoding = getattr(self.original_stream, "encoding", None) or "utf-8"
                text = text.decode(encoding, errors="replace")
            with _thread_log_lock:
                output_queue = _thread_log_queues.get(threading.get_ident())
            if output_queue:
                output_queue.put(text)
            else:
                self.original_stream.write(text)

    def flush(self):
        self.original_stream.flush()

    def isatty(self):
        return self.original_stream.isatty()

    @property
    def encoding(self):
        return getattr(self.original_stream, "encoding", "utf-8")

    def __getattr__(self, name):
        return getattr(self.original_stream, name)


sys.stdout = ThreadLogStream(_original_stdout)
sys.stderr = ThreadLogStream(_original_stderr)


def register_thread_log_queue(output_queue):
    with _thread_log_lock:
        _thread_log_queues[threading.get_ident()] = output_queue


def unregister_thread_log_queue():
    with _thread_log_lock:
        _thread_log_queues.pop(threading.get_ident(), None)


def _public_mercado_login_task(task):
    return {
        key: value
        for key, value in dict(task or {}).items()
        if key not in ("scope_keys", "log_chunks", "stop_worker_started")
    }


def _append_mercado_login_task_log(text, task_id=""):
    text = format_log_text(text)
    if not text:
        return
    if not text.endswith("\n"):
        text += "\n"
    with _mercado_login_task_lock:
        _mercado_login_task_logs.append(text)
        task = _mercado_login_tasks.get(str(task_id or ""))
        if task is not None:
            task.setdefault("log_chunks", deque(maxlen=800)).append(text)
        _mercado_login_log_path.parent.mkdir(parents=True, exist_ok=True)
        with _mercado_login_log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(text)


def _terminate_mercado_login_process_tree(process):
    """终止登录控制进程及它创建的并发 worker。"""
    if process is None or process.poll() is not None:
        return

    used_process_group = False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        else:
            process_group_id = os.getpgid(process.pid)
            if process_group_id == process.pid:
                os.killpg(process_group_id, signal.SIGTERM)
                used_process_group = True
            else:
                process.terminate()
    except (OSError, ProcessLookupError):
        return

    deadline = time.monotonic() + MERCADO_LOGIN_STOP_GRACE_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)
    if process.poll() is not None:
        return

    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        elif used_process_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass


def _stop_mercado_login_process_worker(task_id, process):
    _terminate_mercado_login_process_tree(process)
    with _mercado_login_task_lock:
        task = dict(_mercado_login_tasks.get(task_id) or {})
    for window_id in task.get("window_ids", ()):
        try:
            close_result = closeBrowser(window_id)
            if isinstance(close_result, dict) and close_result.get("success") is False:
                _append_mercado_login_task_log(
                    f"{get_now_time()} 停止任务后关闭窗口 {window_id} 失败："
                    f"{close_result.get('msg') or close_result}",
                    task_id=task_id,
                )
        except Exception as exc:
            _append_mercado_login_task_log(
                f"{get_now_time()} 停止任务后关闭窗口 {window_id} 失败：{exc}",
                task_id=task_id,
            )


def _start_mercado_login_stop_worker(task_id, process):
    if process is None:
        return
    with _mercado_login_task_lock:
        task = _mercado_login_tasks.get(task_id)
        if task is None or task.get("stop_worker_started"):
            return
        task["stop_worker_started"] = True
    threading.Thread(
        target=_stop_mercado_login_process_worker,
        args=(task_id, process),
        daemon=True,
    ).start()


def request_mercado_login_task_stop(task_id):
    task_id = normalize_appeal_task_id(task_id)
    with _mercado_login_task_lock:
        task = _mercado_login_tasks.get(task_id)
        if task is None or not task.get("running"):
            return False, _mercado_login_task_snapshot()
        task.update(
            {
                "status": "stopping",
                "message": f"{task.get('target') or '当前'} 登录任务正在停止",
                "stop_requested": True,
            }
        )
        process = _mercado_login_task_processes.get(task_id)
    _append_mercado_login_task_log(
        f"{get_now_time()} 已收到停止请求，正在终止登录任务及子进程",
        task_id=task_id,
    )
    _start_mercado_login_stop_worker(task_id, process)
    return True, _mercado_login_task_snapshot()


def request_mercado_login_window_tasks_stop(window_id):
    """停止指定窗口对应的独立登录任务，不影响其他窗口。"""
    window_id = str(window_id or "").strip()
    if not window_id:
        return []
    with _mercado_login_task_lock:
        task_ids = [
            task_id
            for task_id, task in _mercado_login_tasks.items()
            if task.get("running")
            and window_id in {
                str(item or "").strip()
                for item in task.get("window_ids", ())
            }
            and len(task.get("window_ids", ())) == 1
        ]
    stopped_task_ids = []
    for task_id in task_ids:
        stopped, _ = request_mercado_login_task_stop(task_id)
        if stopped:
            stopped_task_ids.append(task_id)
    return stopped_task_ids


def _mercado_login_task_snapshot():
    with _mercado_login_task_lock:
        completed_task_ids = [
            task_id
            for task_id, task in _mercado_login_tasks.items()
            if not task.get("running")
        ]
        for completed_task_id in completed_task_ids[:-MERCADO_LOGIN_TASK_HISTORY_LIMIT]:
            _mercado_login_tasks.pop(completed_task_id, None)
        active_tasks = [
            task for task in _mercado_login_tasks.values() if task.get("running")
        ]
        latest_task = next(reversed(_mercado_login_tasks.values()), None) if _mercado_login_tasks else None
        current_task = active_tasks[-1] if active_tasks else latest_task
        if active_tasks:
            primary = active_tasks[-1]
            if len(active_tasks) == 1:
                _mercado_login_task_state.update(_public_mercado_login_task(primary))
            else:
                active_window_ids = list(
                    dict.fromkeys(
                        window_id
                        for task in active_tasks
                        for window_id in task.get("window_ids", ())
                    )
                )
                _mercado_login_task_state.update(
                    {
                        "running": True,
                        "started_at": min(
                            str(task.get("started_at") or "") for task in active_tasks
                        ),
                        "finished_at": "",
                        "status": "running",
                        "message": f"{len(active_tasks)} 个登录任务正在异步执行",
                        "target": f"{len(active_tasks)} 个登录任务",
                        "window_id": "",
                        "window_ids": active_window_ids,
                        "pid": None,
                        "returncode": None,
                    }
                )
        elif latest_task:
            _mercado_login_task_state.update(_public_mercado_login_task(latest_task))

        public_tasks = [
            _public_mercado_login_task(task)
            for task in reversed(tuple(_mercado_login_tasks.values()))
        ]
        active_window_ids = list(
            dict.fromkeys(
                window_id
                for task in active_tasks
                for window_id in task.get("window_ids", ())
            )
        )
        all_running = any(task.get("scope") == "all" for task in active_tasks)
        latest_log = (
            "".join(current_task.get("log_chunks") or ())
            if current_task is not None
            else "".join(_mercado_login_task_logs)
        )
        snapshot = {
            **dict(_mercado_login_task_state),
            "log": latest_log,
            "tasks": public_tasks,
            "running_count": len(active_tasks),
            "active_window_ids": active_window_ids,
            "all_running": all_running,
            "current_task_id": (
                str(current_task.get("task_id") or "")
                if current_task is not None and current_task.get("running")
                else ""
            ),
            "current_task_target": (
                str(current_task.get("target") or "")
                if current_task is not None
                else ""
            ),
            "current_task_stopping": bool(
                current_task is not None and current_task.get("stop_requested")
            ),
            "can_stop": bool(current_task is not None and current_task.get("running")),
        }
    process_owner = get_lock_owner(MERCADO_LOGIN_JOB_LOCK_KEY)
    if process_owner:
        local_pids = {
            task.get("pid") for task in active_tasks if task.get("pid") is not None
        }
        owner_is_local_task = process_owner.get("pid") in local_pids
        snapshot["all_running"] = True
        if not owner_is_local_task:
            target = str(
                (process_owner.get("metadata") or {}).get("target")
                or process_owner.get("owner")
                or "现有登录检测任务"
            )
            snapshot.update(
                {
                    "running": True,
                    "status": "running",
                    "message": f"{target} 正在另一个进程中运行",
                    "target": target,
                    "pid": process_owner.get("pid"),
                    "running_count": int(snapshot.get("running_count") or 0) + 1,
                }
            )
    return snapshot


def _mercado_login_task_scope(shop_name="", window_id="", selected_shops=None):
    selected_shops = tuple(selected_shops or ())
    window_ids = list(
        dict.fromkeys(
            str(value or "").strip()
            for value in (
                [shop.get("window_id") for shop in selected_shops]
                if selected_shops
                else [window_id]
            )
            if str(value or "").strip()
        )
    )
    if window_ids:
        return "windows", window_ids, tuple(f"window:{value}" for value in window_ids)
    shop_name = str(shop_name or "").strip()
    if shop_name:
        return "shop", [], (f"shop:{shop_name}",)
    return "all", [], ("all",)


def _build_mercado_login_command(
    shop_name="",
    workers=DEFAULT_MERCADO_LOGIN_WORKERS,
    window_ids=None,
):
    command = [
        sys.executable,
        "-u",
        "-m",
        "bit.bit_mercado_login",
    ]
    shop_name = str(shop_name or "").strip()
    selected_window_ids = tuple(
        dict.fromkeys(
            str(window_id or "").strip()
            for window_id in (window_ids or ())
            if str(window_id or "").strip()
        )
    )
    if shop_name:
        command.extend(
            (
                "--shop",
                shop_name,
                "--auto-login",
                "--keep-browser-open",
                "--manual-login-wait-seconds",
                str(MERCADO_LOGIN_SINGLE_MANUAL_WAIT_SECONDS),
            )
        )
    elif selected_window_ids:
        worker_count = max(
            1,
            min(
                int(workers or DEFAULT_MERCADO_LOGIN_WORKERS),
                MAX_MERCADO_LOGIN_WORKERS,
                len(selected_window_ids),
            ),
        )
        for window_id in selected_window_ids:
            command.extend(("--window-id", window_id))
        command.extend(
            (
                "--workers",
                str(worker_count),
                "--no-email",
                "--keep-browser-open",
                "--manual-login-wait-seconds",
                str(MERCADO_LOGIN_SELECTED_MANUAL_WAIT_SECONDS),
            )
        )
    else:
        command.extend(
            (
                "--all-active-login",
                "--workers",
                str(
                    max(
                        1,
                        min(
                            int(workers or DEFAULT_MERCADO_LOGIN_WORKERS),
                            MAX_MERCADO_LOGIN_WORKERS,
                        ),
                    )
                ),
            )
        )
    command.extend(("--wait-seconds", "60", "--page-load-timeout", "20"))
    return command


def _mercado_login_selected_target(selected_shops):
    shops = tuple(selected_shops or ())
    if not shops:
        return ""
    names = [
        str(shop.get("window_name") or shop.get("shop_name") or "").strip()
        for shop in shops
    ]
    names = [name for name in names if name]
    preview = "、".join(names[:3])
    if len(names) > 3:
        preview += "等"
    return f"所选 {len(shops)} 家待处理人机验证店铺" + (f"（{preview}）" if preview else "")


def run_mercado_login_console_job(
    shop_name="",
    window_id="",
    workers=DEFAULT_MERCADO_LOGIN_WORKERS,
    selected_shops=None,
    task_id="",
):
    """后台运行登录任务，并把子进程及其工作进程输出持久化给控制台。"""
    global _mercado_login_task_process
    selected_shops = tuple(dict(shop) for shop in (selected_shops or ()))
    selected_window_ids = [shop.get("window_id") for shop in selected_shops]
    target = (
        _mercado_login_selected_target(selected_shops)
        or str(shop_name or "").strip()
        or "全部未忽略店铺"
    )
    command = _build_mercado_login_command(
        shop_name=shop_name,
        workers=workers,
        window_ids=selected_window_ids,
    )
    _append_mercado_login_task_log(
        f"{get_now_time()} ===== bit_mercado_login 启动：{target} =====",
        task_id=task_id,
    )
    try:
        child_env = os.environ.copy()
        child_env["PYTHONIOENCODING"] = "utf-8"
        child_env["PYTHONUNBUFFERED"] = "1"
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        )
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        with _mercado_login_task_lock:
            _mercado_login_task_process = process
            _mercado_login_task_processes[task_id] = process
            if task_id in _mercado_login_tasks:
                _mercado_login_tasks[task_id]["pid"] = process.pid
                stop_requested = bool(
                    _mercado_login_tasks[task_id].get("stop_requested")
                )
            else:
                stop_requested = False
        if stop_requested:
            _start_mercado_login_stop_worker(task_id, process)
        if process.stdout is not None:
            for line in process.stdout:
                _append_mercado_login_task_log(
                    f"[{target}] {line}",
                    task_id=task_id,
                )
        returncode = process.wait()
        with _mercado_login_task_lock:
            stop_requested = bool(
                (_mercado_login_tasks.get(task_id) or {}).get("stop_requested")
            )
        succeeded = returncode == 0 and not stop_requested
        resolve_message = ""
        if succeeded and window_id:
            try:
                db_resolve_window_anomaly(window_id)
                resolve_message = "，店铺待登录状态已自动解除"
            except Exception as exc:
                resolve_message = f"，但更新店铺状态失败：{exc}"
        if stop_requested:
            message = f"{target} 登录任务已停止"
        elif succeeded:
            message = f"{target} 登录任务完成{resolve_message}"
        else:
            message = f"{target} 登录任务失败，退出码：{returncode}"
        with _mercado_login_task_lock:
            if task_id in _mercado_login_tasks:
                _mercado_login_tasks[task_id].update({
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": (
                        "stopped"
                        if stop_requested
                        else "success" if succeeded else "error"
                    ),
                    "message": message,
                    "returncode": returncode,
                })
        _append_mercado_login_task_log(
            f"{get_now_time()} {message}",
            task_id=task_id,
        )
    except Exception as exc:
        logging.error("bit_mercado_login console job failed: %s", exc)
        traceback.print_exc()
        with _mercado_login_task_lock:
            stop_requested = bool(
                (_mercado_login_tasks.get(task_id) or {}).get("stop_requested")
            )
            if task_id in _mercado_login_tasks:
                _mercado_login_tasks[task_id].update({
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "stopped" if stop_requested else "error",
                    "message": (
                        f"{target} 登录任务已停止" if stop_requested else str(exc)
                    ),
                    "returncode": None,
                })
        _append_mercado_login_task_log(
            (
                f"{get_now_time()} {target} 登录任务已停止"
                if stop_requested
                else f"{get_now_time()} bit_mercado_login 启动失败：{exc}"
            ),
            task_id=task_id,
        )
    finally:
        with _mercado_login_task_lock:
            _mercado_login_task_processes.pop(task_id, None)
            _mercado_login_task_process = next(
                iter(_mercado_login_task_processes.values()),
                None,
            )


def start_mercado_login_console_job(
    shop_name="",
    window_id="",
    workers=DEFAULT_MERCADO_LOGIN_WORKERS,
    selected_shops=None,
):
    selected_shops = tuple(dict(shop) for shop in (selected_shops or ()))
    workers = max(
        1,
        min(
            int(workers or DEFAULT_MERCADO_LOGIN_WORKERS),
            MAX_MERCADO_LOGIN_WORKERS,
        ),
    )
    scope, selected_window_ids, scope_keys = _mercado_login_task_scope(
        shop_name=shop_name,
        window_id=window_id,
        selected_shops=selected_shops,
    )
    with _mercado_login_task_lock:
        active_tasks = [
            task for task in _mercado_login_tasks.values() if task.get("running")
        ]
        active_all_task = any(task.get("scope") == "all" for task in active_tasks)
        if active_all_task or (scope == "all" and active_tasks):
            snapshot = _mercado_login_task_snapshot()
            snapshot["message"] = "全部店铺自动登录任务与单店任务不能同时运行"
            return False, snapshot
        if get_lock_owner(MERCADO_LOGIN_JOB_LOCK_KEY):
            snapshot = _mercado_login_task_snapshot()
            return False, snapshot
        occupied_scope_keys = {
            key for task in active_tasks for key in task.get("scope_keys", ())
        }
        overlapping_keys = occupied_scope_keys.intersection(scope_keys)
        if overlapping_keys:
            snapshot = _mercado_login_task_snapshot()
            snapshot["message"] = "所选店铺已有自动登录任务正在运行"
            return False, snapshot
        target = (
            _mercado_login_selected_target(selected_shops)
            or str(shop_name or "").strip()
            or "全部未忽略店铺"
        )
        task_id = secrets.token_hex(8)
        task_state = {
            "task_id": task_id,
            "running": True,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "status": "running",
            "message": f"{target} 登录任务已启动",
            "target": target,
            "window_id": str(window_id or "").strip(),
            "window_ids": selected_window_ids,
            "workers": (
                min(workers, len(selected_window_ids))
                if selected_window_ids
                else workers
            ),
            "pid": None,
            "returncode": None,
            "stop_requested": False,
            "stop_worker_started": False,
            "scope": scope,
            "scope_keys": scope_keys,
            "log_chunks": deque(maxlen=800),
        }
        _mercado_login_tasks[task_id] = task_state
        task_thread = threading.Thread(
            target=run_mercado_login_console_job,
            args=(shop_name, window_id, workers, selected_shops, task_id),
            daemon=True,
        )
        try:
            task_thread.start()
        except Exception:
            _mercado_login_tasks.pop(task_id, None)
            raise
        snapshot = _mercado_login_task_snapshot()
        snapshot["started_task_id"] = task_id
        return True, snapshot


def _collection_status_failed(value):
    text = str(value or "").strip()
    return bool(text) and text not in ("成功", "无数据", "未知")


def _failed_collection_shop_options(shop_options, status_rows):
    """按店铺聚合失败站点；任一站点失败时店铺只出现一次。"""

    configured = {
        str(shop.get("shop_name") or "").strip(): shop
        for shop in (shop_options or [])
        if str(shop.get("shop_name") or "").strip()
    }
    failures_by_shop = {}
    for row in status_rows or []:
        shop_name = str(row.get("店铺名") or row.get("shop_name") or "").strip()
        site = str(row.get("站点") or row.get("site") or "").strip()
        status = str(row.get("状态") or row.get("status") or "").strip()
        shop = configured.get(shop_name)
        if (
            shop is None
            or not site
            or site not in (shop.get("sites") or [])
            or not _collection_status_failed(status)
        ):
            continue
        failed_shop = failures_by_shop.setdefault(
            shop_name,
            {
                "shop_name": shop_name,
                "salesperson": str(shop.get("salesperson") or "").strip(),
                "sites": [],
                "failures": [],
            },
        )
        if site not in failed_shop["sites"]:
            failed_shop["sites"].append(site)
        failure = {
            "site": site,
            "status": status,
            "status_time": str(
                row.get("状态时间") or row.get("status_time") or ""
            ),
        }
        if failure not in failed_shop["failures"]:
            failed_shop["failures"].append(failure)

    return [
        failures_by_shop[shop["shop_name"]]
        for shop in (shop_options or [])
        if shop.get("shop_name") in failures_by_shop
    ]


def _collection_config_options(include_failures=False):
    configs = db_list_bit_browser_configs(include_ignored=False) or []
    shops_by_name = {}
    site_order = []
    for config in configs:
        shop_name = str(config.get("shop_name") or "").strip()
        window_id = str(config.get("window_id") or "").strip()
        if not shop_name or not window_id:
            continue
        shop = shops_by_name.setdefault(
            shop_name,
            {
                "shop_name": shop_name,
                "salesperson": str(config.get("salesperson") or "").strip(),
                "sites": [],
            },
        )
        for site in split_config_sites(config.get("sites")):
            if site not in shop["sites"]:
                shop["sites"].append(site)
            if site not in site_order:
                site_order.append(site)
    shop_options = list(shops_by_name.values())
    result = {"shops": shop_options, "sites": site_order}
    if not include_failures:
        return result

    try:
        infraction_data = db_get_latest_infraction_info(30) or {}
        infraction_status_rows = infraction_data.get("summary") or []
    except Exception as exc:
        logging.warning("读取侵权失败店铺失败：%s", exc)
        infraction_status_rows = []

    try:
        reputation_data = db_get_latest_reputation_info() or {}
        reputation_status_rows = reputation_data.get("summary") or []
        if not reputation_status_rows:
            reputation_status_rows = [
                {
                    "店铺名": row.get("店铺名"),
                    "站点": row.get("站点"),
                    "状态": "失败",
                    "状态时间": row.get("更新时间") or row.get("提交时间"),
                }
                for row in (reputation_data.get("rows") or [])
                if "失败" in str(row.get("声誉颜色") or "")
                or str(row.get("系统告警") or "").strip().startswith("失败")
            ]
    except Exception as exc:
        logging.warning("读取声誉失败店铺失败：%s", exc)
        reputation_status_rows = []

    result["failed_shops"] = {
        "infraction": _failed_collection_shop_options(
            shop_options,
            infraction_status_rows,
        ),
        "reputation": _failed_collection_shop_options(
            shop_options,
            reputation_status_rows,
        ),
    }
    return result


def _normalized_collection_list(data, key):
    if key not in data:
        return ()
    raw_values = data.get(key)
    if not isinstance(raw_values, list):
        raise ValueError(f"{key} 必须是数组")
    values = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in raw_values
            if str(value or "").strip()
        )
    )
    if not values:
        label = "店铺" if key == "shops" else "站点"
        raise ValueError(f"请至少选择一个{label}")
    return values


def _parse_collection_request(data):
    data = data if isinstance(data, dict) else {}
    shops = _normalized_collection_list(data, "shops")
    sites = _normalized_collection_list(data, "sites")
    max_workers = _parse_int_param(
        data,
        "max_workers",
        DEFAULT_COLLECTION_MAX_WORKERS,
        min_value=1,
        max_value=10,
    )
    options = _collection_config_options()
    configured = {shop["shop_name"]: shop for shop in options["shops"]}
    unknown_shops = [shop for shop in shops if shop not in configured]
    if unknown_shops:
        raise ValueError("店铺不存在或已被忽略：" + "、".join(unknown_shops))
    unknown_sites = [site for site in sites if site not in options["sites"]]
    if unknown_sites:
        raise ValueError("站点不存在：" + "、".join(unknown_sites))

    target_shops = shops or tuple(configured)
    matching_shop_names = [
        shop_name
        for shop_name in target_shops
        if any(
            not sites or site in sites
            for site in configured[shop_name]["sites"]
        )
    ]
    matching_sites = {
        site
        for shop_name in matching_shop_names
        for site in configured[shop_name]["sites"]
        if not sites or site in sites
    }
    if not matching_sites:
        raise ValueError("所选店铺没有配置所选站点")
    return {
        "selected_shops": shops,
        "selected_sites": sites,
        "max_workers": max_workers,
        "target": (
            f"{len(matching_shop_names)} 家店铺"
        ) + " / " + (
            f"{len(sites)} 个站点" if sites else "全部站点"
        ),
    }


def run_infraction_collect_job(
    selected_shops=None,
    selected_sites=None,
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
):
    try:
        print(
            f"{get_now_time()} 开始执行侵权数据采集："
            f"{len(selected_shops or ()) or '全部'} 家店铺，"
            f"{len(selected_sites or ()) or '全部'} 个站点，并发 {max_workers}<br>"
        )
        target = getattr(bit_infractions_info, "main", None) or bit_infractions_info.get_infractions_info_all
        result = target(
            max_workers=max_workers,
            selected_shops=selected_shops,
            selected_sites=selected_sites,
        )
        failed_count = sum(
            1
            for row in (result or {}).get("results", [])
            if len(row) >= 4 and str(row[3] or "") != "成功"
        )
        with _infraction_collect_lock:
            _infraction_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "message": f"侵权数据采集完成，异常站点 {failed_count} 个",
            })
        print(f"{get_now_time()} 侵权数据采集完成<br>")
    except Exception as e:
        logging.error("Infraction collect failed: %s", e)
        traceback.print_exc()
        with _infraction_collect_lock:
            _infraction_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "error",
                "message": str(e),
            })


def run_reputation_collect_job(
    selected_shops=None,
    selected_sites=None,
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
):
    try:
        with _reputation_collect_lock:
            operation = _reputation_collect_state.get("operation")
        action_label = (
            "所选店铺声誉更新"
            if operation == "selected_update"
            else "声誉数据补跑"
        )
        print(
            f"{get_now_time()} 开始执行{action_label}："
            f"{len(selected_shops or ()) or '全部'} 家店铺，"
            f"{len(selected_sites or ()) or '全部'} 个站点，并发 {max_workers}<br>"
        )
        target = getattr(bit_reputation_info, "main", None) or bit_reputation_info.get_reputation_info_all
        result = target(
            max_workers=max_workers,
            selected_shops=selected_shops,
            selected_sites=selected_sites,
        )
        failed_count = sum(
            1
            for row in (result or {}).get("results", [])
            if len(row) >= 4 and str(row[3] or "") != "成功"
        )
        with _reputation_collect_lock:
            _reputation_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "message": f"{action_label}完成，异常站点 {failed_count} 个",
            })
        print(f"{get_now_time()} {action_label}完成<br>")
    except Exception as e:
        logging.error("Reputation collect failed: %s", e)
        traceback.print_exc()
        with _reputation_collect_lock:
            _reputation_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "error",
                "message": str(e),
            })


def run_fund_collect_job(
    all_shops=True,
    selected_window_ids=None,
    salesperson="",
    max_workers=DEFAULT_COLLECTION_MAX_WORKERS,
    stop_event=None,
):
    global _fund_collect_stop_event
    try:
        rows = bit_pago_info.get_pago_info_all(
            max_workers=max_workers,
            apply_shop_limit=False,
            selected_window_ids=None if all_shops else selected_window_ids,
            salesperson=salesperson,
            stop_event=stop_event,
        )
        collected_count = len(rows or [])
        with _fund_collect_lock:
            stopped = bool(stop_event is not None and stop_event.is_set())
            _fund_collect_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "stopped" if stopped else "success",
                    "message": (
                        f"资金数据采集已终止，已保留 {collected_count} 个店铺站点结果"
                        if stopped
                        else f"资金数据采集完成，共更新 {collected_count} 个店铺站点"
                    ),
                    "collected_count": collected_count,
                }
            )
    except Exception as e:
        logging.error("Fund collect failed: %s", e)
        traceback.print_exc()
        with _fund_collect_lock:
            stopped = bool(stop_event is not None and stop_event.is_set())
            _fund_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "stopped" if stopped else "error",
                "message": "资金数据采集已终止" if stopped else str(e),
            })
    finally:
        with _fund_collect_lock:
            if _fund_collect_stop_event is stop_event:
                _fund_collect_stop_event = None


def _parse_int_param(data, name, default, min_value=0, max_value=None):
    try:
        value = int(data.get(name, default))
    except (TypeError, ValueError):
        value = default
    value = max(min_value, value)
    if max_value is not None:
        value = min(max_value, value)
    return value


def _parse_bool_param(data, name, default=True):
    value = data.get(name, default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "是", "启用")


def build_zying_collection_params(data):
    """校验智赢产品采集页面提交的参数。"""
    data = data if isinstance(data, dict) else {}
    start_page = _parse_int_param(
        data,
        "start_page",
        bit_zying_caiji.DEFAULT_ZYING_START_PAGE,
        min_value=1,
        max_value=10000,
    )
    end_page = _parse_int_param(
        data,
        "end_page",
        max(start_page, bit_zying_caiji.DEFAULT_ZYING_PAGE_COUNT),
        min_value=1,
        max_value=10000,
    )
    if start_page > end_page:
        raise ValueError(f"起始页 {start_page} 不能大于结束页 {end_page}")
    window_id = str(data.get("window_id") or "").strip()[:128]
    return {
        "number": end_page,
        "window_id": window_id or bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID,
        "start_page": start_page,
        "category": str(data.get("category") or "").strip()[:1024] or None,
    }


def _append_zying_collection_log(message):
    text = format_log_text(message).strip()
    if not text:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    with _zying_collection_state_lock:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            _zying_collection_logs.append(f"[{timestamp}] {line}")
            if _zying_collection_state.get("running"):
                _zying_collection_state["message"] = line


class _ZyingCollectionLogSink:
    def __init__(self):
        self.buffer = ""

    def put(self, text):
        self.buffer += format_log_text(text)
        while "\n" in self.buffer:
            line, self.buffer = self.buffer.split("\n", 1)
            _append_zying_collection_log(line)

    def flush(self):
        if self.buffer.strip():
            _append_zying_collection_log(self.buffer)
        self.buffer = ""


def run_zying_collection_job(params, task_lock):
    """在后台执行智赢产品采集，并保留控制台实时日志。"""
    log_sink = _ZyingCollectionLogSink()
    register_thread_log_queue(log_sink)
    try:
        result = bit_zying_caiji.collect_zying_products(
            **params,
            product_writer=db_insert_zying_product_info,
            existing_product_id_reader=db_get_existing_zying_product_ids,
            return_summary=True,
        )
        summary = {
            key: int((result or {}).get(key) or 0)
            for key in (
                "collected_count",
                "inserted_count",
                "skipped_existing_count",
                "duplicate_count",
                "detail_failed_count",
            )
        }
        completion_message = (
            f"智赢产品采集完成：入库 {summary['inserted_count']} 条，"
            f"已有产品跳过 {summary['skipped_existing_count']} 条，"
            f"页面重复跳过 {summary['duplicate_count']} 条"
        )
        _append_zying_collection_log(completion_message)
        with _zying_collection_state_lock:
            _zying_collection_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "success",
                    "message": completion_message,
                    "summary": summary,
                }
            )
    except Exception as exc:
        logging.error("智赢产品采集失败：%s", exc)
        traceback.print_exc()
        _append_zying_collection_log(f"采集失败：{exc}")
        with _zying_collection_state_lock:
            _zying_collection_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": str(exc),
                    "summary": {},
                }
            )
    finally:
        log_sink.flush()
        unregister_thread_log_queue()
        task_lock.release()


def build_risk_check_params(data):
    """校验泽顺控制台提交的侵权检测参数。"""
    data = data if isinstance(data, dict) else {}
    category = str(data.get("category") or "").strip()[:1024]
    model = str(data.get("model") or "").strip()[:128] or None
    return {
        "zying_category": category or None,
        "hours": _parse_int_param(data, "hours", 0, min_value=0, max_value=87600),
        "limit": _parse_int_param(data, "limit", 0, min_value=0, max_value=50000),
        "batch_size": _parse_int_param(
            data,
            "batch_size",
            bit_check_risk.DEFAULT_BATCH_SIZE,
            min_value=1,
            max_value=50,
        ),
        "model": model,
        "retries": _parse_int_param(
            data,
            "ai_retries",
            bit_check_risk.DEFAULT_AI_RETRIES,
            min_value=0,
            max_value=5,
        ),
        "recheck": _parse_bool_param(data, "recheck", False),
        "dry_run": _parse_bool_param(data, "dry_run", False),
    }


def _append_risk_check_log(message):
    text = str(message or "").strip()
    if not text:
        return
    timestamp = datetime.now().strftime("%H:%M:%S")
    with _risk_check_state_lock:
        for line in text.splitlines():
            line = line.strip()
            if line:
                _risk_check_logs.append(f"[{timestamp}] {line}")
                if _risk_check_state.get("running"):
                    _risk_check_state["message"] = line


def run_risk_check_job(params, task_lock):
    """在后台线程执行侵权检测，并更新控制台任务状态。"""
    try:
        summary = bit_check_risk.scan_products(
            **params,
            candidate_reader=db_get_zying_risk_candidates,
            risk_writer=db_update_zying_product_risks,
            log_callback=_append_risk_check_log,
        )
        public_summary = {
            key: int(summary.get(key) or 0)
            for key in ("checked", "risk_0", "risk_1", "risk_2", "updated")
        }
        completion_message = (
            f"检测完成：{public_summary['checked']} 条，"
            f"疑似 {public_summary['risk_1']} 条，"
            f"侵权 {public_summary['risk_2']} 条"
        )
        _append_risk_check_log(completion_message)
        with _risk_check_state_lock:
            _risk_check_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "success",
                    "message": completion_message,
                    "summary": public_summary,
                }
            )
    except Exception as exc:
        logging.error("侵权检测任务失败：%s", exc)
        traceback.print_exc()
        _append_risk_check_log(f"任务失败：{exc}")
        with _risk_check_state_lock:
            _risk_check_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": str(exc),
                    "summary": {},
                }
            )
    finally:
        task_lock.release()


def _risk_result_query_params(args, export=False):
    risk_level = str(args.get("risk_level") or "").strip().lower()
    if risk_level not in {"", "0", "1", "2", "unchecked"}:
        raise ValueError("风险级别只能是 0、1、2 或未检测")
    sort_by = str(args.get("sort_by") or "risk_level").strip()
    if sort_by not in {
        "row_id",
        "product_id",
        "title",
        "zying_category",
        "risk_level",
        "keywords",
        "submitted_at",
    }:
        sort_by = "risk_level"
    sort_dir = "asc" if str(args.get("sort_dir") or "").lower() == "asc" else "desc"
    return {
        "zying_category": str(args.get("category") or "").strip()[:1024] or None,
        "risk_level": risk_level or None,
        "search": str(args.get("search") or "").strip()[:200],
        "sort_by": sort_by,
        "sort_dir": sort_dir,
        "limit": 0 if export else _parse_int_param(args, "limit", 1000, 1, 5000),
    }


def _append_order_print_log(message):
    text = format_log_text(message).rstrip()
    if not text:
        return
    with _order_print_lock:
        _order_print_logs.append(text + "\n")


def _order_print_snapshot():
    with _order_print_lock:
        return {
            **dict(_order_print_state),
            "params": dict(_order_print_state.get("params") or {}),
            "results": [dict(row) for row in (_order_print_state.get("results") or [])],
            "site_last_runs": [
                dict(row) for row in (_order_print_state.get("site_last_runs") or [])
            ],
            "log": "".join(_order_print_logs),
            "can_stop": bool(
                _order_print_state.get("running")
                and _order_print_stop_event is not None
            ),
        }


def _order_print_history_status(outcome):
    text = str(outcome or "").strip()
    if "无待打印订单" in text:
        return "no_orders"
    if text.startswith("成功"):
        return "printed"
    if text.startswith("跳过"):
        return "skipped"
    if text.startswith("失败"):
        return "failed"
    return "unknown"


def _load_order_print_site_last_runs(current_results=None):
    """汇总全部已配置店铺站点及其最近一次订单打印时间。"""

    current_results = [dict(row) for row in (current_results or [])]
    configs_loaded = True
    try:
        configs = db_list_bit_browser_configs(include_ignored=False) or []
    except Exception as exc:
        logging.warning("读取订单打印站点配置失败：%s", exc)
        configs = []
        configs_loaded = False
    try:
        history = db_get_latest_order_print_records() or []
    except Exception as exc:
        logging.warning("读取订单打印历史失败：%s", exc)
        history = []

    latest_by_key = {}
    for record in history:
        shop_name = str(record.get("shop_name") or "").strip()
        site = str(record.get("site") or "").strip()
        if not shop_name or not site:
            continue
        latest_by_key[(shop_name, site)] = {
            "shop_name": shop_name,
            "site": site,
            "status": _order_print_history_status(record.get("outcome")),
            "finished_at": str(record.get("finished_at") or ""),
        }
    for result in current_results:
        shop_name = str(result.get("shop_name") or "").strip()
        site = str(result.get("site") or "").strip()
        if not shop_name or not site:
            continue
        key = (shop_name, site)
        finished_at = str(result.get("finished_at") or "")
        existing = latest_by_key.get(key)
        if existing and str(existing.get("finished_at") or "") > finished_at:
            continue
        latest_by_key[key] = {
            "shop_name": shop_name,
            "site": site,
            "status": str(result.get("status") or "unknown"),
            "finished_at": finished_at,
        }

    rows = []
    seen = set()
    for config in configs:
        shop_name = str(config.get("shop_name") or "").strip()
        if not shop_name:
            continue
        for site in split_config_sites(config.get("sites")):
            key = (shop_name, site)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                latest_by_key.get(
                    key,
                    {
                        "shop_name": shop_name,
                        "site": site,
                        "status": "not_run",
                        "finished_at": "",
                    },
                )
            )
    if not configs_loaded:
        for key, record in latest_by_key.items():
            if key not in seen:
                rows.append(record)
    return rows


def _refresh_order_print_site_last_runs(current_results=None):
    if current_results is None:
        with _order_print_lock:
            current_results = [
                dict(row) for row in (_order_print_state.get("results") or [])
            ]
    rows = _load_order_print_site_last_runs(current_results)
    with _order_print_lock:
        _order_print_state["site_last_runs"] = rows
    return rows


def build_order_print_params(data):
    data = data if isinstance(data, dict) else {}
    raw_targets = data.get("targets")
    selected_targets = []
    if raw_targets is not None:
        if not isinstance(raw_targets, list):
            raise ValueError("targets 必须是数组")
        if not raw_targets:
            raise ValueError("请至少选择一个店铺站点")
        if len(raw_targets) > 1000:
            raise ValueError("单次最多选择 1000 个店铺站点")

        options = _collection_config_options()
        configured_pairs = {
            (shop["shop_name"], site)
            for shop in options["shops"]
            for site in shop["sites"]
        }
        seen_targets = set()
        for raw_target in raw_targets:
            if not isinstance(raw_target, dict):
                raise ValueError("每个店铺站点必须包含 shop_name 和 site")
            shop_name = str(raw_target.get("shop_name") or "").strip()
            site = str(raw_target.get("site") or "").strip()
            if not shop_name or not site:
                raise ValueError("每个店铺站点必须包含 shop_name 和 site")
            pair = (shop_name, site)
            if pair not in configured_pairs:
                raise ValueError(f"店铺站点不存在或已被忽略：{shop_name} / {site}")
            if pair in seen_targets:
                continue
            seen_targets.add(pair)
            selected_targets.append({"shop_name": shop_name, "site": site})

        selected_shops = tuple(
            dict.fromkeys(target["shop_name"] for target in selected_targets)
        )
        selected_sites = tuple(
            dict.fromkeys(target["site"] for target in selected_targets)
        )
        target_label = (
            f"{len(selected_shops)} 家店铺 / "
            f"{len(selected_targets)} 个店铺站点"
        )
    else:
        selection = _parse_collection_request({**data, "max_workers": 1})
        selected_shops = selection["selected_shops"]
        selected_sites = selection["selected_sites"]
        target_label = selection["target"]
    return {
        "mode": "once",
        "max_retries": _parse_int_param(
            data, "max_retries", 3, min_value=1, max_value=3
        ),
        "retry_delay_seconds": _parse_int_param(
            data, "retry_delay_seconds", 300, min_value=0, max_value=600
        ),
        "selected_shops": selected_shops,
        "selected_sites": selected_sites,
        "selected_targets": selected_targets,
        "target": target_label,
    }


def run_order_print_job(params, task_lock, stop_event):
    global _order_print_stop_event
    try:
        with _order_print_lock:
            _order_print_state["message"] = "正在执行订单打印"
        _append_order_print_log(f"{get_now_time()} ===== 订单打印开始 =====")
        summary = bit_print.print_orders_all(
            selected_shops=params["selected_shops"],
            selected_sites=params["selected_sites"],
            selected_targets=params.get("selected_targets"),
            max_retries=params["max_retries"],
            retry_delay_seconds=params["retry_delay_seconds"],
            stop_event=stop_event,
            logger=_append_order_print_log,
        )
        site_last_runs = _load_order_print_site_last_runs(summary.get("results", []))
        with _order_print_lock:
            _order_print_state.update(
                {
                    "printed": summary.get("printed", 0),
                    "no_orders": summary.get("no_orders", 0),
                    "failed": summary.get("failed", 0),
                    "skipped": summary.get("skipped", 0),
                    "results": summary.get("results", []),
                    "site_last_runs": site_last_runs,
                }
            )

        stopped = stop_event.is_set()
        with _order_print_lock:
            _order_print_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "stopped" if stopped else "success",
                    "message": "订单打印已停止" if stopped else "订单打印任务已完成",
                }
            )
    except Exception as exc:
        logging.error("Order print task failed: %s", exc)
        traceback.print_exc()
        _append_order_print_log(f"{get_now_time()} 订单打印异常：{exc}")
        with _order_print_lock:
            _order_print_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": str(exc),
                }
            )
    finally:
        if task_lock is not None:
            task_lock.release()
        with _order_print_lock:
            if _order_print_stop_event is stop_event:
                _order_print_stop_event = None


def _parse_rate_param(data, name="min_rate", default=0):
    value = data.get(name, default)
    text = str(value if value is not None else default).strip().replace("％", "%")
    if not text:
        return float(default)
    is_percent = "%" in text
    number_text = text.replace("%", "").replace("，", ".").replace(",", ".").strip()
    try:
        number = float(number_text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是 0% 到 100% 之间的比率") from exc
    rate = number / 100 if is_percent or number > 1 else number
    if rate < 0 or rate > 1:
        raise ValueError(f"{name} 必须是 0% 到 100% 之间的比率")
    return rate


def build_daily_task_params(data):
    mode = str(data.get("mode", "once")).strip().lower()
    if mode not in ("once", "loop"):
        mode = "once"
    normalized_appeal_type = bit_daily_task.normalize_appeal_type(
        data.get("appeal_type") or bit_daily_task.APPEAL_TYPE_INFRACTION
    )
    appeal_type = (
        "延误率"
        if normalized_appeal_type == bit_daily_task.APPEAL_TYPE_DELAY
        else normalized_appeal_type
    )
    return {
        "mode": mode,
        "appeal_type": appeal_type,
        "top_n": _parse_int_param(data, "top_n", bit_daily_task.DEFAULT_DAILY_TOP_N, 1, 100),
        "max_workers": _parse_int_param(data, "max_workers", bit_daily_task.DEFAULT_DAILY_MAX_WORKERS, 1, 60),
        "recent_days": _parse_int_param(
            data,
            "recent_days",
            bit_daily_task.DEFAULT_DAILY_RECENT_DAYS,
            1,
            365,
        ),
        "round_interval": _parse_int_param(data, "round_interval", 600, 10, 86400),
        "site_pause": _parse_int_param(data, "site_pause", 30, 0, 3600),
        "stop_after_minutes": _parse_int_param(data, "stop_after_minutes", 360, 0, 24 * 60),
        "only_active": _parse_bool_param(data, "only_active", True),
        "min_rate": _parse_rate_param(data),
        "message": str(data.get("message", "") or ""),
    }


def run_daily_task_job(params, task_lock):
    try:
        print(f"{get_now_time()} 开始执行 daily_task：{params}<br>")
        appeal_type = params.get("appeal_type", bit_daily_task.APPEAL_TYPE_INFRACTION)
        min_rate = params.get("min_rate", 0)
        if params["mode"] == "loop":
            stop_at = None
            if params["stop_after_minutes"] > 0:
                stop_at = datetime.now() + timedelta(minutes=params["stop_after_minutes"])
            bit_daily_task.loop_ai_appeal(
                appeal_type,
                top_n=params["top_n"],
                max_workers=params["max_workers"],
                recent_days=params["recent_days"],
                round_interval=params["round_interval"],
                site_pause=params["site_pause"],
                message=params["message"],
                only_active=params["only_active"],
                min_rate=min_rate,
                stop_at=stop_at,
                _task_lock=task_lock,
            )
            result_message = f"daily_task {appeal_type}申诉循环执行完成"
        else:
            bit_daily_task.run_ai_appeal_once(
                appeal_type,
                top_n=params["top_n"],
                max_workers=params["max_workers"],
                recent_days=params["recent_days"],
                site_pause=params["site_pause"],
                message=params["message"],
                only_active=params["only_active"],
                min_rate=min_rate,
                _task_lock=task_lock,
            )
            result_message = f"daily_task {appeal_type}申诉单轮执行完成"

        with _daily_task_lock:
            _daily_task_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "success",
                "message": result_message,
            })
        print(f"{get_now_time()} {result_message}<br>")
    except Exception as e:
        logging.error("daily_task failed: %s", e)
        traceback.print_exc()
        with _daily_task_lock:
            _daily_task_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "error",
                "message": str(e),
            })
    finally:
        if task_lock is not None:
            task_lock.release()


def format_log_text(text):
    return str(text).replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")


def shensu_logic_old(name, site, form, message):
    i = 0
    while i < 10:
        i = i + 1
        try:
            yield f"{get_now_time()}--- 任务启动第{i}次：{name}{site} ---<br>"
            shensu(name, site, form, message)
            # 模拟自动化操作步骤
            yield f"{get_now_time()}✅ {name}{site}申诉执行完毕,！<br>"
        except Exception as e:
            yield f"发生错误: {str(e)}<br>"
        finally:
            yield f"{get_now_time()}{name}{site}关闭浏览器等待十分钟，进行下一次申诉<br>"
            window_id = getWindowidByName(name)
            time.sleep(600)


# 2. 接口路由
def shensu_logic_previous(name, site, form, message):
    for i in range(1, 11):
        output_queue = queue.Queue()

        def run_task():
            writer = StreamWriter(output_queue)
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                try:
                    print(f"{get_now_time()} --- 任务启动第 {i} 次：{name} {site}")
                    shensu(name, site, form, message)
                    print(f"{get_now_time()} {name} {site} 申诉执行完毕")
                except Exception as e:
                    print(f"{get_now_time()} 发生错误: {str(e)}")
                    traceback.print_exc()
                finally:
                    output_queue.put(None)

        task_thread = threading.Thread(target=run_task, daemon=True)
        task_thread.start()

        while True:
            text = output_queue.get()
            if text is None:
                break
            yield format_log_text(text)
            sys.stdout.flush()

        yield f"{get_now_time()} {name} {site} 本轮结束，等待十分钟后进入下一轮\n"
        getWindowidByName(name)
        time.sleep(600)


def resolve_appeal_sites(sites):
    """规范化控制台选中的站点，兼容旧版单个 site 字符串调用。"""
    raw_sites = [sites] if isinstance(sites, str) else list(sites or [])
    selected_sites = []
    invalid_sites = []
    for raw_site in raw_sites:
        site = str(raw_site or "").strip()
        if not site:
            continue
        if site not in APPEAL_SITES:
            invalid_sites.append(site)
            continue
        if site not in selected_sites:
            selected_sites.append(site)
    if invalid_sites:
        raise ValueError(f"不支持的站点：{'、'.join(invalid_sites)}")
    if not selected_sites:
        raise ValueError("请至少选择一个站点")
    return tuple(selected_sites)


def resolve_appeal_forms(forms):
    """规范化申诉类型，并按固定业务顺序执行，兼容旧版单个 form 字符串。"""
    raw_forms = [forms] if isinstance(forms, str) else list(forms or [])
    selected_forms = set()
    invalid_forms = []
    for raw_form in raw_forms:
        appeal_form = str(raw_form or "").strip()
        if not appeal_form:
            continue
        if appeal_form not in APPEAL_FORMS:
            invalid_forms.append(appeal_form)
            continue
        selected_forms.add(appeal_form)
    if invalid_forms:
        raise ValueError(f"不支持的任务类型：{'、'.join(dict.fromkeys(invalid_forms))}")
    if not selected_forms:
        raise ValueError("请至少选择一个任务类型")
    return tuple(
        appeal_form for appeal_form in APPEAL_FORMS if appeal_form in selected_forms
    )


def normalize_appeal_loop_count(value):
    """返回申诉轮数；0 表示永久循环，其他值只允许 10、20、50。"""
    text = str(value if value is not None else DEFAULT_APPEAL_LOOP_COUNT).strip()
    if text.casefold() in ("0", "permanent", "forever", "永久"):
        return PERMANENT_APPEAL_LOOP_COUNT
    try:
        loop_count = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("循环次数只支持 10、20、50 或永久") from exc
    if loop_count not in APPEAL_LOOP_COUNTS:
        raise ValueError("循环次数只支持 10、20、50 或永久")
    return loop_count


def get_appeal_round_interval(mode):
    """AI 客服每轮间隔 1 分钟，人工客服每轮间隔 10 分钟。"""
    if mode == "AI客服":
        return AI_APPEAL_ROUND_INTERVAL_SECONDS
    return MANUAL_APPEAL_ROUND_INTERVAL_SECONDS


def stream_task_output(output_queue, stop_event=None):
    """持续转发任务日志，并在 AI 长时间无输出时发送心跳，避免请求连接超时。"""
    heartbeat_seconds = max(1, APPEAL_STREAM_HEARTBEAT_SECONDS)
    stop_notice_sent = False
    while True:
        if stop_event is not None and stop_event.is_set() and not stop_notice_sent:
            yield (
                f"{get_now_time()} 已收到终结请求，等待当前站点安全结束并释放窗口\n"
            )
            stop_notice_sent = True
        try:
            text = output_queue.get(timeout=heartbeat_seconds)
        except queue.Empty:
            yield f"{get_now_time()} 申诉任务仍在运行（保持连接）\n"
            continue
        if text is None:
            return
        yield format_log_text(text)
        sys.stdout.flush()


def stream_appeal_round_wait(seconds, stop_event=None):
    """分段等待下一轮，并持续向前端发送状态，避免静默导致连接断开。"""
    remaining = max(0, int(seconds))
    heartbeat_seconds = max(1, APPEAL_STREAM_HEARTBEAT_SECONDS)
    while remaining > 0:
        wait_seconds = min(heartbeat_seconds, remaining)
        if stop_event is not None:
            if stop_event.wait(wait_seconds):
                yield f"{get_now_time()} 已终结本次申诉任务，不再进入下一轮\n"
                return False
        else:
            time.sleep(wait_seconds)
        remaining -= wait_seconds
        if remaining > 0:
            yield f"{get_now_time()} 等待下一轮，剩余 {remaining} 秒（保持连接）\n"
    return True


def shensu_logic(
    name,
    sites,
    form,
    message,
    mode,
    loop_count=DEFAULT_APPEAL_LOOP_COUNT,
    stop_event=None,
):
    target_sites = resolve_appeal_sites(sites)
    target_forms = resolve_appeal_forms(form)
    round_limit = normalize_appeal_loop_count(loop_count)
    multiple_tasks_selected = len(target_sites) > 1 or len(target_forms) > 1
    round_interval_seconds = get_appeal_round_interval(mode)
    round_interval_minutes = round_interval_seconds // 60
    round_number = 0
    cancellation_enabled = stop_event is not None
    stop_event = stop_event or threading.Event()

    while round_limit == PERMANENT_APPEAL_LOOP_COUNT or round_number < round_limit:
        if stop_event.is_set():
            yield f"{get_now_time()} 已终结本次申诉任务\n"
            return
        round_number += 1
        if multiple_tasks_selected:
            yield (
                f"{get_now_time()} 第 {round_number} 轮开始，任务类型将按 "
                f"{' → '.join(target_forms)} 的顺序执行；"
                f"选中站点：{'、'.join(target_sites)}\n"
            )

        for current_form in target_forms:
            if len(target_forms) > 1:
                yield (
                    f"{get_now_time()} 第 {round_number} 轮开始执行任务类型："
                    f"{current_form}\n"
                )
            for current_site in target_sites:
                if stop_event.is_set():
                    yield (
                        f"{get_now_time()} 已终结本次申诉任务，"
                        "不再执行后续任务类型和站点\n"
                    )
                    return
                output_queue = queue.Queue()
                task_result = {"value": None}

                def run_task(
                    run_site=current_site,
                    run_form=current_form,
                    run_round=round_number,
                ):
                    register_thread_log_queue(output_queue)
                    window_lease = None
                    try:
                        print(
                            f"{get_now_time()} --- 第 {run_round} 轮任务启动："
                            f"{name} {run_site}，任务类型：{run_form}，客服模式：{mode}"
                        )
                        window_id = getWindowidByName(name)
                        window_lease = create_window_lease(
                            window_id,
                            owner=f"interface_appeal:{name}",
                            shop_name=name,
                            task_type="interface_appeal",
                        )
                        if not window_lease.acquire(timeout=0):
                            task_result["value"] = "窗口正在被其他任务占用"
                            print(
                                f"{get_now_time()} {name} {run_site} {run_form} "
                                f"{task_result['value']}，本轮已跳过"
                            )
                            return
                        if mode == "AI客服":
                            task_result["value"] = bit_appeal_ai.shensu(
                                name, run_site, run_form, message
                            )
                        else:
                            task_result["value"] = shensu(
                                name, run_site, run_form, message, "人工客服"
                            )
                        print(
                            f"{get_now_time()} {name} {run_site} {run_form} "
                            f"申诉执行完毕：{task_result['value']}"
                        )
                    except Exception as e:
                        print(
                            f"{get_now_time()} {name} {run_site} {run_form} "
                            f"发生错误: {str(e)}"
                        )
                        traceback.print_exc()
                    finally:
                        if window_lease is not None:
                            window_lease.release()
                        unregister_thread_log_queue()
                        output_queue.put(None)

                task_thread = threading.Thread(target=run_task, daemon=True)
                task_thread.start()

                yield from stream_task_output(output_queue, stop_event=stop_event)

                if stop_event.is_set():
                    yield (
                        f"{get_now_time()} {name} {current_site} {current_form} "
                        "当前操作已安全结束，本次任务已终结\n"
                    )
                    return

                if is_login_blocking_result(task_result.get("value")):
                    yield (
                        f"{get_now_time()} {name} {current_site} "
                        f"{task_result.get('value')}，已停止该店铺后续站点、"
                        "任务类型和申诉循环\n"
                    )
                    return

        has_next_round = (
            round_limit == PERMANENT_APPEAL_LOOP_COUNT
            or round_number < round_limit
        )
        if multiple_tasks_selected:
            yield (
                f"{get_now_time()} 第 {round_number} 轮任务类型 "
                f"{'、'.join(target_forms)} 和选中站点执行完成，"
                + (
                    f"等待 {round_interval_minutes} 分钟后开始下一轮\n"
                    if has_next_round
                    else "已达到规定循环次数\n"
                )
            )
        else:
            yield (
                f"{get_now_time()} {name} {target_sites[0]} 第 {round_number} 轮结束，"
                + (
                    f"等待 {round_interval_minutes} 分钟后进入下一轮\n"
                    if has_next_round
                    else "已达到规定循环次数\n"
                )
            )
        if (
            round_limit != PERMANENT_APPEAL_LOOP_COUNT
            and round_number >= round_limit
        ):
            yield (
                f"{get_now_time()} 已完成规定的 {round_limit} 轮申诉，任务结束\n"
            )
            return
        wait_completed = yield from stream_appeal_round_wait(
            round_interval_seconds,
            stop_event=stop_event if cancellation_enabled else None,
        )
        if not wait_completed:
            return


@app.route('/api/run_shensu', methods=['GET'])
@login_required
def api_run_shensu():
    # 获取前端传入的参数
    name = request.args.get("name", "")
    try:
        sites = resolve_appeal_sites(request.args.getlist("site"))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    try:
        forms = resolve_appeal_forms(request.args.getlist("form"))
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    try:
        loop_count = normalize_appeal_loop_count(
            request.args.get("loop_count", DEFAULT_APPEAL_LOOP_COUNT)
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    message = request.args.get("message", "")
    mode = request.args.get("mode", "人工客服")
    task_id = normalize_appeal_task_id(request.args.get("task_id", ""))
    if not task_id:
        task_id = secrets.token_hex(16)
    stop_event = register_appeal_task(
        task_id,
        {
            "name": name,
            "sites": list(sites),
            "loop_count": "永久" if loop_count == 0 else loop_count,
            "form": forms[0] if len(forms) == 1 else "、".join(forms),
            "forms": list(forms),
            "mode": mode,
        },
    )
    if stop_event is None:
        return jsonify({"status": "error", "message": "该任务编号正在运行"}), 409

    def generate():
        try:
            yield f"{get_now_time()} 申诉任务编号：{task_id}\n"
            yield from shensu_logic(
                name,
                sites,
                forms,
                message,
                mode,
                loop_count=loop_count,
                stop_event=stop_event,
            )
        finally:
            stop_event.set()
            finish_appeal_task(task_id)

    # 返回流式响应，mimetype 设为 text/html 或 text/event-stream
    response = Response(generate(), mimetype='text/plain; charset=utf-8')
    response.headers["Cache-Control"] = "no-cache"
    response.headers["X-Accel-Buffering"] = "no"
    response.headers["X-Appeal-Task-ID"] = task_id
    return response


@app.route('/api/run_shensu/stop', methods=['POST'])
@login_required
def api_stop_shensu():
    data = request.get_json(silent=True) or {}
    task_id = normalize_appeal_task_id(data.get("task_id"))
    if not task_id:
        return jsonify({"status": "error", "message": "缺少有效任务编号"}), 400
    if not request_appeal_task_stop(task_id):
        return jsonify({"status": "error", "message": "任务已结束或不存在"}), 404
    return jsonify(
        {
            "status": "success",
            "message": "已提交终结请求，当前站点结束后将释放窗口并停止后续任务",
        }
    )


@app.route('/api/appeal-phrases', methods=['GET', 'POST'])
@login_required
def api_appeal_phrases():
    try:
        if request.method == "POST":
            result = db_create_appeal_phrase(request.get_json(silent=True) or {})
        else:
            result = db_list_appeal_phrases()
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/appeal-phrases/<int:phrase_id>', methods=['PUT', 'DELETE'])
@login_required
def api_appeal_phrase_detail(phrase_id):
    try:
        if request.method == "PUT":
            result = db_update_appeal_phrase(
                phrase_id,
                request.get_json(silent=True) or {},
            )
        else:
            result = db_delete_appeal_phrase(phrase_id)
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/infractions/latest', methods=['GET'])
@login_required
def api_latest_infractions():
    try:
        recent_days = request.args.get("days", 30)
        return jsonify({
            "status": "success",
            "data": db_get_latest_infraction_info(recent_days)
        })
    except Exception as e:
        logging.error(f"Latest infraction query failed: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}"
        }), 500


@app.route('/api/infractions/latest/export', methods=['GET'])
@login_required
def api_export_latest_infractions():
    try:
        recent_days = request.args.get("days", 30)
        data = db_get_latest_infraction_info(recent_days)
        rows = data.get("rows") or []
        recent_days = data.get("recent_days") or 30

        wb = Workbook()
        detail_ws = wb.active
        detail_ws.title = "侵权明细"

        header_fill = PatternFill("solid", fgColor="D9EAF7")
        header_font = Font(bold=True, color="1F2937")

        detail_columns = ["店铺名", "站点", "类型", "编号", "标题", "侵权时间", "执行时间", "提交时间"]
        detail_ws.append(detail_columns)
        for row in rows:
            detail_ws.append([row.get(column, "") for column in detail_columns])

        for cell in detail_ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for column_cells in detail_ws.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                value = "" if cell.value is None else str(cell.value)
                max_length = max(max_length, len(value))
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            detail_ws.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 42)
        detail_ws.freeze_panes = "A2"

        output = BytesIO()
        wb.save(output)
        output.seek(0)

        submit_time = str(data.get("latest_submit_time") or datetime.now().strftime("%Y%m%d%H%M%S"))
        safe_time = "".join(ch if ch.isdigit() else "" for ch in submit_time) or datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"最新侵权明细_最近{recent_days}天_{safe_time}.xlsx"
        encoded_filename = quote(filename)
        response = send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
        return response
    except Exception as e:
        logging.error(f"Latest infraction export failed: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Export error: {str(e)}"
        }), 500


@app.route('/api/infractions/collect', methods=['POST'])
@login_required
def api_collect_infractions():
    try:
        params = _parse_collection_request(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.error("读取侵权采集范围失败：%s", exc)
        return jsonify({"status": "error", "message": f"读取采集范围失败：{exc}"}), 500

    with _infraction_collect_lock:
        if _infraction_collect_state.get("running"):
            return jsonify({
                "status": "running",
                "data": dict(_infraction_collect_state),
                "message": "侵权数据采集正在运行中"
            }), 409

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _infraction_collect_state.update({
            "running": True,
            "started_at": started_at,
            "finished_at": "",
            "status": "running",
            "message": f"侵权数据采集已启动：{params['target']}，并发 {params['max_workers']}",
            "params": {
                "shops": list(params["selected_shops"]),
                "sites": list(params["selected_sites"]),
                "max_workers": params["max_workers"],
                "target": params["target"],
            },
        })

    task_thread = threading.Thread(
        target=run_infraction_collect_job,
        args=(
            params["selected_shops"],
            params["selected_sites"],
            params["max_workers"],
        ),
        daemon=True,
    )
    task_thread.start()
    return jsonify({
        "status": "success",
        "data": dict(_infraction_collect_state),
        "message": "侵权数据采集已在后台启动"
    })


@app.route('/api/infractions/collect/status', methods=['GET'])
@login_required
def api_collect_infractions_status():
    with _infraction_collect_lock:
        return jsonify({
            "status": "success",
            "data": dict(_infraction_collect_state),
        })


@app.route('/api/reputation/latest', methods=['GET'])
@login_required
def api_latest_reputation():
    try:
        return jsonify({
            "status": "success",
            "data": db_get_latest_reputation_info()
        })
    except Exception as e:
        logging.error(f"Latest reputation query failed: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}"
        }), 500


@app.route('/api/reputation/latest/export', methods=['GET'])
@login_required
def api_export_latest_reputation():
    try:
        data = db_get_latest_reputation_info()
        rows = data.get("rows") or []
        wb = Workbook()
        ws = wb.active
        ws.title = "最新声誉数据"

        columns = [
            "店铺名", "站点", "声誉颜色", "总单量", "投诉率", "延误率", "取消率",
            "增加或减少", "近七天变化率", "一周流量趋势", "系统告警",
            "更新时间", "提交时间"
        ]
        ws.append(columns)

        header_fill = PatternFill("solid", fgColor="D9EAF7")
        header_font = Font(bold=True, color="1F2937")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")

        for row in rows:
            ws.append([row.get(column, "") for column in columns])

        for column_cells in ws.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                value = "" if cell.value is None else str(cell.value)
                max_length = max(max_length, len(value))
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            ws.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 36)

        ws.freeze_panes = "A2"
        output = BytesIO()
        wb.save(output)
        output.seek(0)

        submit_time = str(data.get("latest_submit_time") or datetime.now().strftime("%Y%m%d%H%M%S"))
        safe_time = "".join(ch if ch.isdigit() else "" for ch in submit_time) or datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"最新声誉数据_{safe_time}.xlsx"
        encoded_filename = quote(filename)
        response = send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["Content-Disposition"] = f"attachment; filename*=UTF-8''{encoded_filename}"
        return response
    except Exception as e:
        logging.error(f"Latest reputation export failed: {str(e)}")
        return jsonify({
            "status": "error",
            "message": f"Export error: {str(e)}"
        }), 500


@app.route('/api/reputation/collect', methods=['POST'])
@login_required
def api_collect_reputation():
    try:
        params = _parse_collection_request(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.error("读取声誉采集范围失败：%s", exc)
        return jsonify({"status": "error", "message": f"读取采集范围失败：{exc}"}), 500

    return _start_reputation_collect_task(params, operation="rerun")


@app.route('/api/reputation/update-selected', methods=['POST'])
@login_required
def api_update_selected_reputation():
    try:
        params = _parse_collection_request(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.error("读取所选店铺声誉更新范围失败：%s", exc)
        return jsonify({"status": "error", "message": f"读取更新范围失败：{exc}"}), 500

    return _start_reputation_collect_task(params, operation="selected_update")


def _start_reputation_collect_task(params, *, operation):
    action_label = (
        "所选店铺声誉更新"
        if operation == "selected_update"
        else "声誉数据补跑"
    )

    with _reputation_collect_lock:
        if _reputation_collect_state.get("running"):
            return jsonify({
                "status": "running",
                "data": dict(_reputation_collect_state),
                "message": "另一个声誉数据任务正在运行中"
            }), 409

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _reputation_collect_state.update({
            "running": True,
            "started_at": started_at,
            "finished_at": "",
            "status": "running",
            "message": f"{action_label}已启动：{params['target']}，并发 {params['max_workers']}",
            "operation": operation,
            "params": {
                "shops": list(params["selected_shops"]),
                "sites": list(params["selected_sites"]),
                "max_workers": params["max_workers"],
                "target": params["target"],
            },
        })

    task_thread = threading.Thread(
        target=run_reputation_collect_job,
        args=(
            params["selected_shops"],
            params["selected_sites"],
            params["max_workers"],
        ),
        daemon=True,
    )
    task_thread.start()
    return jsonify({
        "status": "success",
        "data": dict(_reputation_collect_state),
        "message": f"{action_label}已在后台启动"
    })


@app.route('/api/reputation/collect/status', methods=['GET'])
@login_required
def api_collect_reputation_status():
    with _reputation_collect_lock:
        return jsonify({
            "status": "success",
            "data": dict(_reputation_collect_state),
        })


@app.route('/api/collections/options', methods=['GET'])
@login_required
def api_collection_options():
    try:
        response = jsonify({
            "status": "success",
            "data": _collection_config_options(include_failures=True),
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        logging.error("读取采集店铺和站点失败：%s", exc)
        return jsonify({
            "status": "error",
            "message": f"读取采集店铺和站点失败：{exc}",
        }), 500


@app.route('/api/funds/latest', methods=['GET'])
@login_required
def api_latest_funds():
    try:
        salesperson = str(request.args.get("salesperson") or "").strip()
        response = jsonify({
            "status": "success",
            "data": db_get_latest_pago_info(salesperson),
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as e:
        logging.error("Latest funds query failed: %s", e)
        return jsonify({
            "status": "error",
            "message": f"Database error: {str(e)}",
        }), 500


@app.route('/api/funds/collect', methods=['POST'])
@login_required
def api_collect_funds():
    global _fund_collect_stop_event
    data = request.get_json(silent=True) or {}
    all_shops = _parse_bool_param(data, "all_shops", False)
    salesperson = str(data.get("salesperson") or "").strip()
    max_workers = _parse_int_param(
        data,
        "max_workers",
        DEFAULT_COLLECTION_MAX_WORKERS,
        1,
        10,
    )
    raw_window_ids = data.get("window_ids", [])
    if not isinstance(raw_window_ids, list):
        return jsonify({"status": "error", "message": "window_ids 必须是数组"}), 400
    selected_window_ids = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in raw_window_ids
            if str(value or "").strip()
        )
    )
    legacy_window_id = str(data.get("window_id") or "").strip()
    legacy_shop_name = str(data.get("shop_name") or "").strip()
    if legacy_window_id and legacy_window_id not in selected_window_ids:
        selected_window_ids += (legacy_window_id,)
    if len(selected_window_ids) > 500:
        return jsonify({"status": "error", "message": "单次最多选择 500 家店铺"}), 400

    try:
        configs = db_list_bit_browser_configs(include_ignored=False) or []
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": f"读取店铺配置失败：{str(e)}",
        }), 500
    valid_configs = [
        config
        for config in configs
        if str(config.get("window_id") or "").strip()
        and str(config.get("shop_name") or "").strip()
    ]
    if salesperson:
        valid_configs = [
            config
            for config in valid_configs
            if (str(config.get("salesperson") or "").strip() or "未分配") == salesperson
        ]
    config_by_window = {
        str(config.get("window_id") or "").strip(): config
        for config in valid_configs
    }
    if not selected_window_ids and legacy_shop_name:
        legacy_config = next(
            (
                config
                for config in valid_configs
                if str(config.get("shop_name") or "").strip() == legacy_shop_name
            ),
            None,
        )
        if legacy_config is not None:
            selected_window_ids = (
                str(legacy_config.get("window_id") or "").strip(),
            )

    if all_shops:
        target_configs = valid_configs
        selected_window_ids = ()
    else:
        if not selected_window_ids:
            return jsonify({"status": "error", "message": "请至少勾选一家店铺"}), 400
        unknown_ids = [window_id for window_id in selected_window_ids if window_id not in config_by_window]
        if unknown_ids:
            return jsonify({
                "status": "error",
                "message": "所选店铺不存在、已被忽略或不属于当前归属人",
            }), 404
        target_configs = [config_by_window[window_id] for window_id in selected_window_ids]

    if not target_configs:
        return jsonify({
            "status": "error",
            "message": "当前归属人没有可执行的有效店铺" if salesperson else "没有可执行的有效店铺",
        }), 400

    owner_label = f"归属人 {salesperson}的" if salesperson else ""
    target = (
        f"{owner_label}全部 {len(target_configs)} 家有效店铺"
        if all_shops
        else f"所选 {len(target_configs)} 家店铺"
    )
    with _fund_collect_lock:
        if _fund_collect_state.get("running"):
            return jsonify({
                "status": "running",
                "data": dict(_fund_collect_state),
                "message": "资金数据采集正在运行中",
            }), 409
        stop_event = threading.Event()
        _fund_collect_stop_event = stop_event
        _fund_collect_state.update({
            "running": True,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "status": "running",
            "message": f"{target}资金数据采集已启动",
            "scope": "all" if all_shops else "selected",
            "target": target,
            "salesperson": salesperson,
            "target_count": len(target_configs),
            "window_ids": list(selected_window_ids),
            "collected_count": 0,
        })
        task_state = dict(_fund_collect_state)

    task_thread = threading.Thread(
        target=run_fund_collect_job,
        kwargs={
            "all_shops": all_shops,
            "selected_window_ids": selected_window_ids,
            "salesperson": salesperson,
            "max_workers": max_workers,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    try:
        task_thread.start()
    except Exception:
        with _fund_collect_lock:
            if _fund_collect_stop_event is stop_event:
                _fund_collect_stop_event = None
            _fund_collect_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "error",
                "message": "资金数据采集启动失败",
            })
        raise
    return jsonify({
        "status": "success",
        "data": task_state,
        "message": f"{target}资金数据采集已在后台启动",
    })


@app.route('/api/funds/collect/stop', methods=['POST'])
@login_required
def api_stop_funds_collection():
    with _fund_collect_lock:
        if not _fund_collect_state.get("running") or _fund_collect_stop_event is None:
            return jsonify({
                "status": "error",
                "data": dict(_fund_collect_state),
                "message": "当前没有正在运行的资金采集任务",
            }), 409
        _fund_collect_stop_event.set()
        _fund_collect_state.update({
            "status": "stopping",
            "message": "正在终止资金数据采集，已打开的窗口将在安全边界关闭",
        })
        state = dict(_fund_collect_state)
    return jsonify({
        "status": "success",
        "data": state,
        "message": "已发送终止指令",
    })


@app.route('/api/funds/collect/status', methods=['GET'])
@login_required
def api_collect_funds_status():
    with _fund_collect_lock:
        response = jsonify({
            "status": "success",
            "data": dict(_fund_collect_state),
        })
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/order-print/start', methods=['POST'])
@login_required
def api_start_order_print():
    global _order_print_stop_event
    data = request.get_json(silent=True) or {}
    try:
        params = build_order_print_params(data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.error("读取订单打印范围失败：%s", exc)
        return jsonify({"status": "error", "message": f"读取店铺配置失败：{exc}"}), 500

    with _order_print_lock:
        if _order_print_state.get("running"):
            return jsonify({
                "status": "running",
                "data": _order_print_snapshot(),
                "message": "订单打印任务正在运行",
            }), 409
        task_lock = bit_print.acquire_order_print_lock(
            owner="bit_interface.py",
            mode="once",
        )
        if task_lock is None:
            owner = bit_print.get_order_print_lock_owner()
            return jsonify({
                "status": "running",
                "data": {**_order_print_snapshot(), "lock_owner": owner},
                "message": "订单打印已在其他进程中运行",
            }), 409

        stop_event = threading.Event()
        _order_print_stop_event = stop_event
        _order_print_logs.clear()
        _order_print_state.update({
            "running": True,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": "",
            "status": "running",
            "message": f"{params['target']} 订单打印已启动",
            "params": params,
            "printed": 0,
            "no_orders": 0,
            "failed": 0,
            "skipped": 0,
            "results": [],
        })
        task_state = _order_print_snapshot()

    task_thread = threading.Thread(
        target=run_order_print_job,
        args=(params, task_lock, stop_event),
        daemon=True,
    )
    try:
        task_thread.start()
    except Exception:
        task_lock.release()
        with _order_print_lock:
            if _order_print_stop_event is stop_event:
                _order_print_stop_event = None
            _order_print_state.update({
                "running": False,
                "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "status": "error",
                "message": "订单打印任务启动失败",
            })
        raise
    return jsonify({
        "status": "success",
        "data": task_state,
        "message": "订单打印已在后台启动",
    })


@app.route('/api/order-print/stop', methods=['POST'])
@login_required
def api_stop_order_print():
    with _order_print_lock:
        if not _order_print_state.get("running") or _order_print_stop_event is None:
            return jsonify({
                "status": "error",
                "data": _order_print_snapshot(),
                "message": "当前没有正在运行的订单打印任务",
            }), 409
        _order_print_stop_event.set()
        _order_print_state.update({
            "status": "stopping",
            "message": "正在停止，当前页面操作完成后会关闭窗口",
        })
        state = _order_print_snapshot()
    _append_order_print_log(f"{get_now_time()} 已收到停止订单打印请求")
    return jsonify({
        "status": "success",
        "data": state,
        "message": "已发送停止指令",
    })


@app.route('/api/order-print/status', methods=['GET'])
@login_required
def api_order_print_status():
    _refresh_order_print_site_last_runs()
    state = _order_print_snapshot()
    external_owner = bit_print.get_order_print_lock_owner()
    if external_owner and not state.get("running"):
        state.update({
            "running": True,
            "status": "running",
            "message": "订单打印正在其他进程中运行",
            "started_at": external_owner.get("acquired_at", ""),
            "lock_owner": external_owner,
        })
    response = jsonify({"status": "success", "data": state})
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/order-analysis/import', methods=['POST'])
@login_required
def api_import_order_analysis():
    uploads = []
    skipped_files = []
    for upload in request.files.getlist("files"):
        filename = str(upload.filename or "").strip()
        if bit_update_orders.is_order_excel_file(filename):
            uploads.append((filename, upload.stream))
        elif filename:
            skipped_files.append(filename)
    if not uploads:
        return jsonify({
            "status": "error",
            "message": "所选文件夹中没有可导入的 .xlsx 或 .xlsm 文件",
        }), 400
    if not _order_analysis_import_lock.acquire(blocking=False):
        return jsonify({
            "status": "running",
            "message": "已有订单文件夹正在导入，请等待当前任务完成",
        }), 409
    try:
        result = bit_update_orders.update_order_sources(
            uploads,
            insert_func=db_insert_orders,
        )
        result["skipped_files"] = skipped_files
        result["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return jsonify({
            "status": "success",
            "data": result,
            "message": f"已更新 {result['imported_orders']} 个唯一订单",
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("订单分析文件夹导入失败")
        return jsonify({
            "status": "error",
            "message": f"订单导入失败：{exc}",
        }), 500
    finally:
        _order_analysis_import_lock.release()


def _high_after_sale_query_params(values):
    try:
        limit = int(values.get("limit") or 100)
    except (TypeError, ValueError) as exc:
        raise ValueError("展示数量必须是整数") from exc
    return {
        "sort_by": str(values.get("sort_by") or "after_sale_quantity").strip(),
        "sort_dir": str(values.get("sort_dir") or "desc").strip(),
        "search": str(values.get("search") or "").strip(),
        "date_from": str(values.get("date_from") or "").strip(),
        "date_to": str(values.get("date_to") or "").strip(),
        "limit": max(1, min(limit, 500)),
    }


@app.route('/api/order-analysis/high-after-sales', methods=['GET'])
@login_required
def api_high_after_sale_alerts():
    try:
        data = db_get_high_after_sale_alerts(
            **_high_after_sale_query_params(request.args)
        )
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("读取高售后告警失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


def _high_profit_query_params(values):
    try:
        limit = int(values.get("limit") or 100)
    except (TypeError, ValueError) as exc:
        raise ValueError("展示数量必须是整数") from exc
    return {
        "sort_by": str(values.get("sort_by") or "total_profit").strip(),
        "sort_dir": str(values.get("sort_dir") or "desc").strip(),
        "search": str(values.get("search") or "").strip(),
        "date_from": str(values.get("date_from") or "").strip(),
        "date_to": str(values.get("date_to") or "").strip(),
        "limit": max(1, min(limit, 500)),
    }


@app.route('/api/order-analysis/high-profits', methods=['GET'])
@login_required
def api_high_profit_products():
    try:
        data = db_get_high_profit_products(
            **_high_profit_query_params(request.args)
        )
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("读取高利润产品失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/tasks/daily/start', methods=['POST'])
@login_required
def api_start_daily_task():
    data = request.get_json(silent=True) or {}
    try:
        params = build_daily_task_params(data)
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    with _daily_task_lock:
        if _daily_task_state.get("running"):
            return jsonify({
                "status": "running",
                "data": dict(_daily_task_state),
                "message": "daily_task 正在运行中"
            }), 409

        task_lock = bit_daily_task.acquire_daily_task_lock(
            owner="bit_interface.py",
            mode=params["mode"],
        )
        if task_lock is None:
            owner = bit_daily_task.get_daily_task_lock_owner()
            return jsonify({
                "status": "running",
                "data": {**dict(_daily_task_state), "lock_owner": owner},
                "message": "daily_task 已通过其他进程或启动方式运行"
            }), 409

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _daily_task_state.update({
            "running": True,
            "started_at": started_at,
            "finished_at": "",
            "status": "running",
            "message": "daily_task 已启动",
            "params": params,
        })

    try:
        task_thread = threading.Thread(
            target=run_daily_task_job,
            args=(params, task_lock),
            daemon=True,
        )
        task_thread.start()
    except Exception:
        task_lock.release()
        with _daily_task_lock:
            _daily_task_state.update({"running": False, "status": "error", "message": "daily_task 启动失败"})
        raise
    return jsonify({
        "status": "success",
        "data": dict(_daily_task_state),
        "message": "daily_task 已在后台启动"
    })


@app.route('/api/tasks/daily/status', methods=['GET'])
@login_required
def api_daily_task_status():
    with _daily_task_lock:
        data = dict(_daily_task_state)
        external_owner = bit_daily_task.get_daily_task_lock_owner()
        if external_owner and not data.get("running"):
            data.update({
                "running": True,
                "status": "running",
                "message": "daily_task 正在其他进程中运行",
                "started_at": external_owner.get("acquired_at", ""),
                "lock_owner": external_owner,
            })
        return jsonify({
            "status": "success",
            "data": data,
        })


@app.route('/api/risk-check/categories', methods=['GET'])
@login_required
def api_risk_check_categories():
    try:
        return jsonify({
            "status": "success",
            "data": db_list_zying_risk_categories(),
        })
    except Exception as exc:
        logging.error("读取智赢侵权检测分类失败：%s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/risk-check/results', methods=['GET'])
@login_required
def api_risk_check_results():
    try:
        params = _risk_result_query_params(request.args)
        return jsonify({
            "status": "success",
            "data": db_get_zying_risk_results(**params),
        })
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.error("读取智赢侵权检测结果失败：%s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/risk-check/results/export', methods=['GET'])
@login_required
def api_export_risk_check_results():
    try:
        params = _risk_result_query_params(request.args, export=True)
        data = db_get_zying_risk_results(**params) or {}
        rows = data.get("rows") or []

        wb = Workbook()
        ws = wb.active
        ws.title = "侵权检测结果"
        columns = [
            ("数据行", "row_id"),
            ("产品编号", "product_id"),
            ("标题", "title"),
            ("智赢分类编号", "zying_category_id"),
            ("智赢产品分类", "zying_category"),
            ("产品分类", "product_category"),
            ("风险级别", "risk_level"),
            ("侵权关键词/品牌", "keywords"),
            ("主图链接", "main_image_url"),
            ("采集时间", "collected_at"),
            ("提交时间", "submitted_at"),
        ]
        ws.append([label for label, _ in columns])
        risk_labels = {
            "0": "0 - 无可疑",
            "1": "1 - 疑似/需复核",
            "2": "2 - 侵权",
        }
        for row in rows:
            values = []
            for _, key in columns:
                value = row.get(key, "")
                if key == "risk_level":
                    value = risk_labels.get(str(value or ""), "未检测")
                values.append(value)
            ws.append(values)

        header_fill = PatternFill("solid", fgColor="D9EAF7")
        header_font = Font(bold=True, color="1F2937")
        for cell in ws[1]:
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        for column_cells in ws.columns:
            max_length = 0
            column_letter = column_cells[0].column_letter
            for cell in column_cells:
                value = "" if cell.value is None else str(cell.value)
                max_length = max(max_length, len(value))
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            ws.column_dimensions[column_letter].width = min(max(max_length + 2, 10), 48)
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions

        output = BytesIO()
        wb.save(output)
        output.seek(0)
        filename = f"侵权检测结果_{datetime.now().strftime('%Y%m%d%H%M%S')}.xlsx"
        response = send_file(
            output,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename,
        )
        response.headers["Content-Disposition"] = (
            f"attachment; filename*=UTF-8''{quote(filename)}"
        )
        return response
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.error("导出智赢侵权检测结果失败：%s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/zying-collection/categories', methods=['GET'])
@login_required
def api_zying_collection_categories():
    try:
        return jsonify({
            "status": "success",
            "data": db_list_zying_risk_categories(),
        })
    except Exception as exc:
        logging.error("读取智赢产品采集分类失败：%s", exc)
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/zying-collection/start', methods=['POST'])
@login_required
def api_start_zying_collection():
    try:
        params = build_zying_collection_params(request.get_json(silent=True) or {})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    if not _zying_collection_lock.acquire(blocking=False):
        with _zying_collection_state_lock:
            data = {
                **dict(_zying_collection_state),
                "logs": list(_zying_collection_logs),
            }
        return jsonify({
            "status": "error",
            "message": "智赢产品采集任务正在运行",
            "data": data,
        }), 409

    with _zying_collection_state_lock:
        _zying_collection_logs.clear()
        _zying_collection_state.update(
            {
                "running": True,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "status": "running",
                "message": "正在启动智赢产品采集",
                "params": dict(params),
                "summary": {},
            }
        )
        _append_zying_collection_log(
            f"智赢采集任务已启动：第 {params['start_page']}-{params['number']} 页，"
            f"分类 {params.get('category') or '全部'}；数据库已有产品将直接跳过"
        )
        data = {
            **dict(_zying_collection_state),
            "logs": list(_zying_collection_logs),
        }
    try:
        threading.Thread(
            target=run_zying_collection_job,
            args=(params, _zying_collection_lock),
            daemon=True,
            name="zying-product-collection",
        ).start()
    except Exception:
        _zying_collection_lock.release()
        with _zying_collection_state_lock:
            _zying_collection_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": "智赢产品采集后台线程启动失败",
                }
            )
        raise
    return jsonify({"status": "success", "data": data})


@app.route('/api/zying-collection/status', methods=['GET'])
@login_required
def api_zying_collection_status():
    with _zying_collection_state_lock:
        data = {
            **dict(_zying_collection_state),
            "params": dict(_zying_collection_state.get("params") or {}),
            "summary": dict(_zying_collection_state.get("summary") or {}),
            "logs": list(_zying_collection_logs),
            "defaults": {
                "window_id": bit_zying_caiji.DEFAULT_ZYING_WINDOW_ID,
                "start_page": bit_zying_caiji.DEFAULT_ZYING_START_PAGE,
                "end_page": bit_zying_caiji.DEFAULT_ZYING_PAGE_COUNT,
            },
        }
    return jsonify({"status": "success", "data": data})


@app.route('/api/risk-check/start', methods=['POST'])
@login_required
def api_start_risk_check():
    params = build_risk_check_params(request.get_json(silent=True) or {})
    if not _risk_check_lock.acquire(blocking=False):
        with _risk_check_state_lock:
            data = dict(_risk_check_state)
        return jsonify({
            "status": "error",
            "message": "侵权检测任务正在运行",
            "data": data,
        }), 409

    with _risk_check_state_lock:
        _risk_check_logs.clear()
        _risk_check_state.update(
            {
                "running": True,
                "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "finished_at": "",
                "status": "running",
                "message": "正在读取商品并进行 AI 侵权检测",
                "params": dict(params),
                "summary": {},
            }
        )
        _append_risk_check_log("侵权检测任务已启动：当前仅识别商品标题，主图 Logo 暂不检测")
        data = {**dict(_risk_check_state), "logs": list(_risk_check_logs)}
    try:
        threading.Thread(
            target=run_risk_check_job,
            args=(params, _risk_check_lock),
            daemon=True,
            name="zying-risk-check",
        ).start()
    except Exception:
        _risk_check_lock.release()
        with _risk_check_state_lock:
            _risk_check_state.update(
                {
                    "running": False,
                    "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "status": "error",
                    "message": "侵权检测后台线程启动失败",
                }
            )
        raise
    return jsonify({"status": "success", "data": data})


@app.route('/api/risk-check/status', methods=['GET'])
@login_required
def api_risk_check_status():
    with _risk_check_state_lock:
        data = {
            **dict(_risk_check_state),
            "params": dict(_risk_check_state.get("params") or {}),
            "summary": dict(_risk_check_state.get("summary") or {}),
            "logs": list(_risk_check_logs),
        }
    return jsonify({"status": "success", "data": data})


@app.route('/api/db/task-records', methods=['POST'])
@internal_api_required
def api_db_insert_task_records():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    records = data.get("records") or []
    db_insert_task_record(records)
    return jsonify({"status": "success", "data": {"count": len(records)}})


@app.route('/api/db/task-records/order-print/latest', methods=['GET'])
@internal_api_required
def api_db_latest_order_print_records():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({
        "status": "success",
        "data": db_get_latest_order_print_records(),
    })


@app.route('/api/db/browser-configs', methods=['GET', 'POST'])
@internal_api_required
def api_db_list_browser_configs():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    if request.method == "POST":
        try:
            result = db_create_bit_browser_config(request.get_json(silent=True) or {})
            return jsonify({"status": "success", "data": result})
        except ValueError as exc:
            return jsonify({"status": "error", "message": str(exc)}), 400
    include_ignored = str(request.args.get("include_ignored", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )
    return jsonify(
        {
            "status": "success",
            "data": db_list_bit_browser_configs(include_ignored),
        }
    )


@app.route('/api/db/browser-configs/<int:config_id>', methods=['PUT', 'DELETE'])
@internal_api_required
def api_db_browser_config_detail(config_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "PUT":
            result = db_update_bit_browser_config(
                config_id,
                request.get_json(silent=True) or {},
            )
        else:
            result = db_delete_bit_browser_config(config_id)
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/browser-configs/lookup', methods=['GET'])
@internal_api_required
def api_db_get_browser_config():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    shop_name = request.args.get("shop_name", "")
    window_id = request.args.get("window_id", "")
    if not str(shop_name or "").strip() and not str(window_id or "").strip():
        return jsonify({"status": "error", "message": "Missing shop_name or window_id"}), 422
    include_ignored = str(request.args.get("include_ignored", "1")).strip().lower() not in (
        "0",
        "false",
        "no",
    )
    return jsonify(
        {
            "status": "success",
            "data": db_get_bit_browser_config(shop_name, window_id, include_ignored),
        }
    )


@app.route('/api/db/browser-configs/bulk', methods=['POST'])
@internal_api_required
def api_db_upsert_browser_configs():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    records = data.get("records") or []
    if not isinstance(records, list):
        return jsonify({"status": "error", "message": "records must be an array"}), 422
    replace = bool(data.get("replace", False))
    result = db_upsert_bit_browser_configs(records, replace)
    return jsonify({"status": "success", "data": result})


@app.route('/api/db/reputation/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_reputation():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    replace_targets = data.get("replace_targets") or []
    count = db_inset_reputation_info(
        rows,
        merge_latest=bool(data.get("merge_latest", False)),
        replace_targets=replace_targets,
    )
    return jsonify({"status": "success", "data": {"count": count}})


@app.route('/api/db/infractions/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_infractions():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    replace_targets = data.get("replace_targets") or []
    count = db_inset_infraction_info(
        rows,
        merge_latest=bool(data.get("merge_latest", False)),
        replace_targets=replace_targets,
    )
    return jsonify({"status": "success", "data": {"count": count}})


@app.route('/api/db/delays/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_delays():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    db_inset_delay_info(rows)
    return jsonify({"status": "success", "data": {"count": len(rows)}})


@app.route('/api/db/pago/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_pago():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    db_inset_pago_info(rows)
    return jsonify({"status": "success", "data": {"count": len(rows)}})


@app.route('/api/db/zying-products/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_zying_products():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    count = db_insert_zying_product_info(rows)
    return jsonify({"status": "success", "data": {"count": count}})


@app.route('/api/db/zying-products/existing', methods=['POST'])
@internal_api_required
def api_db_existing_zying_product_ids():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    product_ids = data.get("product_ids") or []
    if not isinstance(product_ids, list):
        return jsonify({
            "status": "error",
            "message": "product_ids 必须是数组",
        }), 400
    existing_ids = sorted(db_get_existing_zying_product_ids(product_ids))
    return jsonify({
        "status": "success",
        "data": {"product_ids": existing_ids},
    })


@app.route('/api/db/zying-risk/candidates', methods=['GET'])
@internal_api_required
def api_db_zying_risk_candidates():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    rows = db_get_zying_risk_candidates(
        hours=_parse_int_param(request.args, "hours", 24, 0, 87600),
        limit=_parse_int_param(request.args, "limit", 0, 0, 50000),
        zying_category=str(request.args.get("category") or "").strip() or None,
        include_checked=_parse_bool_param(request.args, "include_checked", False),
    )
    return jsonify({"status": "success", "data": rows})


@app.route('/api/db/zying-risk/bulk', methods=['POST'])
@internal_api_required
def api_db_update_zying_risks():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    results = data.get("results") or []
    if not isinstance(results, list):
        return jsonify({"status": "error", "message": "results must be an array"}), 422
    count = db_update_zying_product_risks(results)
    return jsonify({"status": "success", "data": {"count": count}})


@app.route('/api/db/zying-risk/categories', methods=['GET'])
@internal_api_required
def api_db_zying_risk_categories():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({"status": "success", "data": db_list_zying_risk_categories()})


@app.route('/api/db/zying-risk/results', methods=['GET'])
@internal_api_required
def api_db_zying_risk_results():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        params = _risk_result_query_params(
            request.args,
            export=str(request.args.get("limit") or "").strip() == "0",
        )
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    return jsonify({
        "status": "success",
        "data": db_get_zying_risk_results(**params),
    })


@app.route('/api/db/orders/bulk', methods=['POST'])
@internal_api_required
def api_db_insert_orders():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    rows = data.get("rows") or []
    db_insert_orders(rows)
    return jsonify({"status": "success", "data": {"count": len(rows)}})


@app.route('/api/db/orders/high-after-sales', methods=['GET'])
@internal_api_required
def api_db_high_after_sale_alerts():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        data = db_get_high_after_sale_alerts(
            **_high_after_sale_query_params(request.args)
        )
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/orders/high-profits', methods=['GET'])
@internal_api_required
def api_db_high_profit_products():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        data = db_get_high_profit_products(
            **_high_profit_query_params(request.args)
        )
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/chat', methods=['POST'])
@internal_api_required
def api_db_insert_chat():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    required_fields = ["name", "site", "message", "chat", "response", "time"]
    missing_fields = [field for field in required_fields if data.get(field) in (None, "")]
    if missing_fields:
        return jsonify({"status": "error", "message": "Missing required fields", "fields": missing_fields}), 422
    chat_id = db_insert_chat_info(
        data["name"],
        data["site"],
        data["message"],
        data["chat"],
        data["response"],
        data["time"],
    )
    return jsonify({"status": "success", "data": {"id": chat_id}})


@app.route('/api/db/appeal-chat-records', methods=['POST'])
@internal_api_required
def api_db_insert_appeal_chat_record():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    record = data.get("record") or {}
    if not isinstance(record, dict):
        return jsonify({"status": "error", "message": "record must be an object"}), 422
    record_id = db_insert_appeal_chat_record(record)
    return jsonify({"status": "success", "data": {"id": record_id}})


@app.route('/api/db/ai-appeal-records', methods=['POST'])
@internal_api_required
def api_db_insert_ai_appeal_record():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    record = data.get("record") or {}
    if not isinstance(record, dict):
        return jsonify({"status": "error", "message": "record must be an object"}), 422
    record_id = db_insert_ai_appeal_record(record)
    return jsonify({"status": "success", "data": {"id": record_id}})


@app.route('/api/db/ai-appeal-records', methods=['GET'])
@internal_api_required
def api_db_get_ai_appeal_records():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    limit = request.args.get("limit", 100)
    return jsonify({"status": "success", "data": db_get_ai_appeal_records(limit)})


@app.route('/api/db/appeal-phrases', methods=['GET', 'POST'])
@internal_api_required
def api_db_appeal_phrases():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "POST":
            result = db_create_appeal_phrase(request.get_json(silent=True) or {})
        else:
            result = db_list_appeal_phrases()
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/appeal-phrases/random', methods=['GET'])
@internal_api_required
def api_db_random_appeal_phrase():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        result = db_get_random_appeal_phrase(request.args.get("appeal_type", ""))
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/appeal-phrases/<int:phrase_id>', methods=['PUT', 'DELETE'])
@internal_api_required
def api_db_appeal_phrase_detail(phrase_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "PUT":
            result = db_update_appeal_phrase(
                phrase_id,
                request.get_json(silent=True) or {},
            )
        else:
            result = db_delete_appeal_phrase(phrase_id)
        return jsonify({"status": "success", "data": result})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400


@app.route('/api/db/infractions/latest', methods=['GET'])
@internal_api_required
def api_db_latest_infractions():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    recent_days = request.args.get("days", 30)
    return jsonify({"status": "success", "data": db_get_latest_infraction_info(recent_days)})


@app.route('/api/db/reputation/latest', methods=['GET'])
@internal_api_required
def api_db_latest_reputation():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    return jsonify({"status": "success", "data": db_get_latest_reputation_info()})


@app.route('/api/db/pago/latest', methods=['GET'])
@internal_api_required
def api_db_latest_pago():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    salesperson = str(request.args.get("salesperson") or "").strip()
    return jsonify({
        "status": "success",
        "data": db_get_latest_pago_info(salesperson),
    })


@app.route('/api/db/window-anomalies', methods=['POST'])
@internal_api_required
def api_db_upsert_window_anomaly():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    if not str(data.get("window_id") or "").strip():
        return jsonify({"status": "error", "message": "Missing window_id"}), 422
    db_upsert_window_anomaly(
        data.get("window_id"),
        data.get("window_name", ""),
        data.get("site", ""),
        data.get("anomaly_type", "需要登录"),
        data.get("reason", ""),
        data.get("source", "bit_daily_task"),
    )
    return jsonify({"status": "success", "data": {"window_id": data.get("window_id")}})


@app.route('/api/db/window-anomalies', methods=['GET'])
@internal_api_required
def api_db_get_window_anomalies():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    active_only = str(request.args.get("active_only", "1")).strip().lower() not in ("0", "false", "no")
    limit = request.args.get("limit", 500)
    return jsonify({"status": "success", "data": db_get_window_anomalies(active_only, limit)})


@app.route('/api/db/window-anomalies/resolve', methods=['POST'])
@internal_api_required
def api_db_resolve_window_anomaly():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    affected = db_resolve_window_anomaly(data.get("window_id"))
    return jsonify({"status": "success", "data": {"affected": affected}})


@app.route('/api/ai-appeal-records', methods=['GET'])
@login_required
def api_ai_appeal_records():
    try:
        limit = request.args.get("limit", 100)
        return jsonify({"status": "success", "data": db_get_ai_appeal_records(limit)})
    except Exception as e:
        logging.error("AI appeal records query failed: %s", e)
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


@app.route('/api/window-anomalies', methods=['GET'])
@login_required
def api_window_anomalies():
    try:
        active_only = str(request.args.get("active_only", "1")).strip().lower() not in ("0", "false", "no")
        limit = request.args.get("limit", 500)
        anomaly_data = filter_human_verification_anomalies(
            db_get_window_anomalies(active_only, limit)
        )
        response = jsonify({
            "status": "success",
            "data": enrich_window_anomaly_salespersons(anomaly_data),
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as e:
        logging.error("Window anomalies query failed: %s", e)
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


def filter_human_verification_anomalies(anomaly_data):
    """店铺状态页只展示明确需要人工处理的人机验证。"""
    data = dict(anomaly_data or {})
    rows = [
        dict(row)
        for row in (data.get("rows") or [])
        if is_human_verification_result(row)
    ]
    data["rows"] = rows
    data["total"] = len(rows)
    return data


def enrich_window_anomaly_salespersons(anomaly_data):
    """按窗口和店铺配置补充归属人与邮箱，不修改历史异常记录。"""
    data = dict(anomaly_data or {})
    rows = [dict(row) for row in (data.get("rows") or [])]
    data["rows"] = rows
    if not rows:
        return data

    try:
        configs = db_list_bit_browser_configs(include_ignored=True) or []
    except Exception as exc:
        logging.warning("读取店铺归属人和邮箱失败：%s", exc)
        return data

    exact_owners = {}
    window_owners = {}
    exact_emails = {}
    window_emails = {}

    for config in configs:
        window_id = str(config.get("window_id") or "").strip()
        shop_name = str(config.get("shop_name") or "").strip()
        salesperson = str(
            config.get("salesperson") or config.get("业务员") or ""
        ).strip()
        email = str(config.get("email") or config.get("邮箱") or "").strip()
        if not window_id:
            continue
        if salesperson:
            exact_owners.setdefault((window_id, shop_name), salesperson)
            window_owners.setdefault(window_id, salesperson)
        if email:
            exact_emails.setdefault((window_id, shop_name), email)
            window_emails.setdefault(window_id, email)

    for row in rows:
        window_id = str(row.get("window_id") or "").strip()
        shop_name = str(row.get("window_name") or "").strip()
        row["salesperson"] = str(
            row.get("salesperson")
            or row.get("业务员")
            or exact_owners.get((window_id, shop_name))
            or window_owners.get(window_id)
            or ""
        ).strip()
        row["email"] = str(
            row.get("email")
            or row.get("邮箱")
            or exact_emails.get((window_id, shop_name))
            or window_emails.get(window_id)
            or ""
        ).strip()
    return data


@app.route('/api/window-anomalies/<window_id>/resolve', methods=['POST'])
@login_required
def api_resolve_window_anomaly(window_id):
    try:
        stopped_task_ids = request_mercado_login_window_tasks_stop(window_id)
        affected = db_resolve_window_anomaly(window_id)
        return jsonify(
            {
                "status": "success",
                "data": {
                    "affected": affected,
                    "stopped_count": len(stopped_task_ids),
                    "stopped_task_ids": stopped_task_ids,
                },
            }
        )
    except Exception as e:
        logging.error("Resolve window anomaly failed: %s", e)
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


@app.route('/api/window-anomalies/mercado-login/status', methods=['GET'])
@login_required
def api_mercado_login_console_status():
    response = jsonify(
        {"status": "success", "data": _mercado_login_task_snapshot()}
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route('/api/window-anomalies/mercado-login/stop', methods=['POST'])
@login_required
def api_stop_mercado_login_console():
    data = request.get_json(silent=True) or {}
    task_id = normalize_appeal_task_id(data.get("task_id"))
    if not task_id:
        return jsonify({"status": "error", "message": "缺少有效任务编号"}), 400
    stopped, task_state = request_mercado_login_task_stop(task_id)
    if not stopped:
        return jsonify(
            {
                "status": "error",
                "data": task_state,
                "message": "登录任务已结束或不存在",
            }
        ), 404
    return jsonify(
        {
            "status": "success",
            "data": task_state,
            "message": "已提交停止请求，正在关闭任务进程和浏览器窗口",
        }
    )


@app.route('/api/window-anomalies/mercado-login/start', methods=['POST'])
@login_required
def api_start_mercado_login_console():
    data = request.get_json(silent=True) or {}
    worker_count = _parse_int_param(
        data,
        "workers",
        DEFAULT_MERCADO_LOGIN_WORKERS,
        1,
        MAX_MERCADO_LOGIN_WORKERS,
    )
    window_id = str(data.get("window_id") or "").strip()
    shop_name = ""
    selected_shops = []
    if "window_ids" in data:
        raw_window_ids = data.get("window_ids")
        if not isinstance(raw_window_ids, list):
            return jsonify(
                {"status": "error", "message": "window_ids 必须是窗口 ID 数组"}
            ), 400
        window_ids = list(
            dict.fromkeys(
                str(item or "").strip()
                for item in raw_window_ids
                if str(item or "").strip()
            )
        )
        if not window_ids:
            return jsonify(
                {"status": "error", "message": "请至少选择一个待处理人机验证店铺"}
            ), 400
        if len(window_ids) > 500:
            return jsonify(
                {"status": "error", "message": "单次最多选择 500 个待处理人机验证店铺"}
            ), 400
        try:
            anomaly_data = filter_human_verification_anomalies(
                db_get_window_anomalies(active_only=True, limit=1000) or {}
            )
        except Exception as exc:
            return jsonify(
                {"status": "error", "message": f"读取店铺状态失败：{exc}"}
            ), 500
        anomaly_by_window_id = {
            str(row.get("window_id") or "").strip(): row
            for row in (anomaly_data.get("rows") or [])
            if str(row.get("window_id") or "").strip()
        }
        missing_window_ids = [
            item for item in window_ids if item not in anomaly_by_window_id
        ]
        if missing_window_ids:
            return jsonify(
                {
                    "status": "error",
                    "message": "部分店铺状态不存在或已恢复，请刷新后重试："
                    + "、".join(missing_window_ids),
                }
            ), 404
        selected_shops = [
            {
                "window_id": item,
                "window_name": str(
                    anomaly_by_window_id[item].get("window_name") or ""
                ).strip(),
            }
            for item in window_ids
        ]
        if any(not shop["window_name"] for shop in selected_shops):
            return jsonify(
                {"status": "error", "message": "部分店铺状态记录缺少店铺名"}
            ), 422
    elif window_id:
        try:
            anomaly_data = filter_human_verification_anomalies(
                db_get_window_anomalies(active_only=True, limit=1000) or {}
            )
            anomaly = next(
                (
                    row
                    for row in (anomaly_data.get("rows") or [])
                    if str(row.get("window_id") or "").strip() == window_id
                ),
                None,
            )
        except Exception as exc:
            return jsonify(
                {"status": "error", "message": f"读取店铺状态失败：{exc}"}
            ), 500
        if anomaly is None:
            return jsonify({"status": "error", "message": "店铺状态不存在或已恢复"}), 404
        shop_name = str(anomaly.get("window_name") or "").strip()
        if not shop_name:
            return jsonify({"status": "error", "message": "店铺状态记录缺少店铺名"}), 422

    if selected_shops:
        started, task_state = start_mercado_login_console_job(
            selected_shops=selected_shops,
            workers=worker_count,
        )
        if not started:
            return jsonify(
                {
                    "status": "running",
                    "data": task_state,
                    "message": task_state.get("message")
                    or "所选店铺已有自动登录任务正在运行",
                }
            ), 409
        started_task_id = str(task_state.get("started_task_id") or "").strip()
        actual_worker_count = min(worker_count, len(selected_shops))
        task_state.update(
            {
                "started_count": len(selected_shops),
                "started_task_ids": [started_task_id] if started_task_id else [],
                "failed_count": 0,
                "failed_shops": [],
                "workers": actual_worker_count,
            }
        )
        return jsonify(
            {
                "status": "success",
                "data": task_state,
                "message": (
                    f"已启动 {len(selected_shops)} 家店铺的重新检测，"
                    f"并发进程数：{actual_worker_count}"
                ),
            }
        )

    job_args = {
        "shop_name": shop_name,
        "window_id": window_id,
        "workers": worker_count,
    }
    started, task_state = start_mercado_login_console_job(**job_args)
    if not started:
        return jsonify(
            {
                "status": "running",
                "data": task_state,
                "message": task_state.get("message")
                or "所选店铺已有自动登录任务正在运行",
            }
        ), 409
    return jsonify(
        {
            "status": "success",
            "data": task_state,
            "message": f"{task_state['target']} 登录任务已启动",
        }
    )


@app.route('/api/db/workbench/session-user', methods=['GET'])
@internal_api_required
def api_db_workbench_session_user():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    user_id = request.args.get("user_id", type=int)
    if not user_id:
        return jsonify({"status": "error", "message": "缺少用户 ID"}), 400
    row = get_workbench_user(user_id=user_id)
    user = (
        build_workbench_session_user(row)
        if row and row.get("is_active")
        else None
    )
    return jsonify({"status": "success", "data": user})


@app.route('/api/db/workbench/roles', methods=['GET', 'POST'])
@internal_api_required
def api_db_workbench_roles():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "GET":
            data = list_workbench_roles_local()
        else:
            data = create_workbench_role_local(request.get_json(silent=True) or {})
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("工作台角色数据库接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/db/workbench/roles/<role_key>', methods=['PUT', 'DELETE'])
@internal_api_required
def api_db_workbench_role_detail(role_key):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "PUT":
            update_workbench_role_local(
                role_key,
                request.get_json(silent=True) or {},
            )
        else:
            delete_workbench_role_local(role_key)
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("工作台角色数据库接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/db/workbench/users', methods=['GET', 'POST'])
@internal_api_required
def api_db_workbench_users():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        if request.method == "GET":
            data = list_workbench_users_local()
        else:
            data = create_workbench_user_local(request.get_json(silent=True) or {})
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("工作台账号数据库接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/db/workbench/users/<int:user_id>', methods=['PUT'])
@internal_api_required
def api_db_workbench_user_detail(user_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        update_workbench_user_local(user_id, request.get_json(silent=True) or {})
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("工作台账号数据库接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/db/workbench/users/<int:user_id>/password', methods=['POST'])
@internal_api_required
def api_db_workbench_user_password(user_id):
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    try:
        data = request.get_json(silent=True) or {}
        reset_workbench_user_password_local(user_id, data.get("password"))
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("工作台密码数据库接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/catalog', methods=['GET'])
@login_required
def api_access_catalog():
    return jsonify(
        {
            "status": "success",
            "data": {"groups": workbench_permission_catalog()},
        }
    )


@app.route('/api/access/roles', methods=['GET', 'POST'])
@login_required
def api_access_roles():
    try:
        if request.method == "GET":
            data = _workbench_backend("list_workbench_roles")
        else:
            data = _workbench_backend(
                "create_workbench_role",
                request.get_json(silent=True) or {},
            )
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限角色接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/roles/<role_key>', methods=['PUT', 'DELETE'])
@login_required
def api_access_role_detail(role_key):
    try:
        if request.method == "PUT":
            _workbench_backend(
                "update_workbench_role",
                role_key,
                request.get_json(silent=True) or {},
            )
        else:
            _workbench_backend("delete_workbench_role", role_key)
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限角色接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/users', methods=['GET', 'POST'])
@login_required
def api_access_users():
    try:
        if request.method == "GET":
            data = _workbench_backend("list_workbench_users")
        else:
            data = _workbench_backend(
                "create_workbench_user",
                request.get_json(silent=True) or {},
            )
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限账号接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/users/<int:user_id>', methods=['PUT'])
@login_required
def api_access_user_detail(user_id):
    data = request.get_json(silent=True) or {}
    current_user = get_current_workbench_user() or {}
    if int(current_user.get("id") or 0) == user_id and not data.get("is_active", True):
        return jsonify({"status": "error", "message": "不能停用当前登录账号"}), 400
    try:
        _workbench_backend("update_workbench_user", user_id, data)
        # 当前账号资料或角色变化后立即刷新本会话。
        if int(current_user.get("id") or 0) == user_id:
            g.pop("workbench_user", None)
            refreshed = get_current_workbench_user()
            if refreshed:
                session["workbench_user"] = refreshed
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限账号接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/users/<int:user_id>/password', methods=['POST'])
@login_required
def api_access_user_password(user_id):
    try:
        data = request.get_json(silent=True) or {}
        _workbench_backend(
            "reset_workbench_user_password",
            user_id,
            data.get("password"),
        )
        return jsonify({"status": "success"})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限密码接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/browser-configs', methods=['GET', 'POST'])
@login_required
def api_access_browser_configs():
    try:
        if request.method == "GET":
            data = db_list_bit_browser_configs(include_ignored=True)
        else:
            data = db_create_bit_browser_config(request.get_json(silent=True) or {})
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限店铺配置接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/access/browser-configs/<int:config_id>', methods=['PUT', 'DELETE'])
@login_required
def api_access_browser_config_detail(config_id):
    try:
        if request.method == "PUT":
            data = db_update_bit_browser_config(
                config_id,
                request.get_json(silent=True) or {},
            )
        else:
            data = db_delete_bit_browser_config(config_id)
        return jsonify({"status": "success", "data": data})
    except ValueError as exc:
        return jsonify({"status": "error", "message": str(exc)}), 400
    except Exception as exc:
        logging.exception("人员与权限店铺配置接口失败")
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route('/api/db/workbench/login', methods=['POST'])
@internal_api_required
def api_db_workbench_login():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked
    data = request.get_json(silent=True) or {}
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        return jsonify({"status": "error", "message": "请输入账号和密码"}), 400

    user = authenticate_workbench_user(username, password)
    if not user:
        return jsonify({"status": "error", "message": "账号或密码错误"}), 401
    return jsonify({"status": "success", "data": user})


@app.route("/")
@login_required
def index():
    return render_template('index.html', current_user=session.get("workbench_user") or {})


@app.route("/login")
def login_page():
    if session.get("workbench_user"):
        return redirect(url_for("index"))
    return render_template("login.html")


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(silent=True) or request.form
    username = str(data.get("username", "")).strip()
    password = str(data.get("password", ""))
    if not username or not password:
        return jsonify({"status": "error", "message": "请输入账号和密码"}), 400

    try:
        user = authenticate_workbench_user(username, password)
    except Exception as e:
        logging.error("工作台登录接口调用失败: %s", e)
        return jsonify({"status": "error", "message": f"登录接口调用失败: {str(e)}"}), 500

    if not user:
        return jsonify({"status": "error", "message": "账号或密码错误"}), 401

    session.clear()
    session["workbench_user"] = user
    return jsonify({"status": "success", "data": session["workbench_user"]})


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    if request.method == "POST" or request.path.startswith("/api/"):
        return jsonify({"status": "success"})
    return redirect(url_for("login_page"))


# 定义路由和返回内容
@app.route("/zs")
@login_required
def hello_whzs():
    return "武汉泽顺"


# --- 新增：1688大模型找货数据插入接口 ---
# @app.route('/api/v1/chat', methods=['POST'])
def api_insert_chat_info():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

    required_fields = ["name", "site", "message", "chat", "response", "time"]
    missing_fields = [field for field in required_fields if data.get(field) in (None, "")]
    if missing_fields:
        return jsonify({
            "status": "error",
            "message": "Missing required fields",
            "fields": missing_fields
        }), 422

    try:
        chat_id = db_insert_chat_info(
            data["name"],
            data["site"],
            data["message"],
            data["chat"],
            data["response"],
            data["time"]
        )
        return jsonify({
            "status": "success",
            "message": "Chat info inserted successfully",
            "id": chat_id
        }), 201
    except Exception as e:
        logging.error(f"Chat info insert failed: {str(e)}")
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500


# @app.route('/api/v1/records', methods=['POST'])
def insert_record():
    blocked = reject_db_api_client_mode()
    if blocked:
        return blocked

    # 获取客户端发送的 JSON 数据
    data = request.get_json()
    if not data:
        return jsonify({"status": "error", "message": "Missing JSON payload"}), 400

    # 安全提取 product_id（必填项项校验）
    product_id = data.get('product_id')
    if not product_id:
        return jsonify({"status": "error", "message": "Field 'product_id' is required"}), 422

    # 提取其余字段，并设置默认值（与你的数据表结构严格对应）
    zhiying_category = data.get('zhiying_category', None)
    original_img_url = data.get('original_img_url', None)
    is_same_style = int(data.get('is_same_style', 0))
    title = data.get('title', None)
    identified_weight = int(data.get('identified_weight', 0))
    pre_modified_weight = int(data.get('pre_modified_weight', 0))
    post_modified_weight = int(data.get('post_modified_weight', 0))

    # 金额与置信度转换为 Decimal 类型，防止精度丢失
    pre_modified_cost_usd = Decimal(str(data.get('pre_modified_cost_usd', '0.0000')))
    post_modified_cost_usd = Decimal(str(data.get('post_modified_cost_usd', '0.0000')))
    max_sku_price_cny = Decimal(str(data.get('max_sku_price_cny', '0.00')))
    model_confidence = Decimal(str(data.get('model_confidence', '0.00')))

    max_sku_spec = data.get('max_sku_spec', None)
    max_sku_id = data.get('max_sku_id', None)
    weight_issue = data.get('weight_issue', None)
    matched_1688_url = data.get('matched_1688_url', None)
    reason = data.get('reason', None)

    sql = """
        INSERT INTO product_mapping_records (
            crawl_time, zhiying_category, original_img_url, is_same_style, 
            product_id, title, identified_weight, pre_modified_weight, 
            post_modified_weight, pre_modified_cost_usd, post_modified_cost_usd, 
            max_sku_price_cny, max_sku_spec, max_sku_id, model_confidence, 
            weight_issue, matched_1688_url, reason
        ) VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
    """

    current_time = datetime.now()
    params = (
        current_time, zhiying_category, original_img_url, is_same_style,
        product_id, title, identified_weight, pre_modified_weight,
        post_modified_weight, pre_modified_cost_usd, post_modified_cost_usd,
        max_sku_price_cny, max_sku_spec, max_sku_id, model_confidence,
        weight_issue, matched_1688_url, reason
    )

    conn = None
    cursor = None
    try:
        # 从连接池中取得一条连接
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        conn.commit()

        logging.info(f"成功录入产品数据，Product ID: {product_id}")
        return jsonify({
            "status": "success",
            "message": "Record inserted successfully",
            "id": cursor.lastrowid
        }), 201

    except Exception as e:
        if conn:
            conn.rollback()
        logging.error(f"数据库写入失败: {str(e)}")
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()  # 将连接放回连接池


if __name__ == '__main__':
    # 保持 5000 端口，多线程模式开启以防流式阻塞
    app.run(host='0.0.0.0', port=5000, threaded=True)
